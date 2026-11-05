import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from helpers import fresh_id, load

from obs_api.ingest import parse_run_results, parse_sources
from obs_api.store import Store

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "s.db"))


def test_ingest_is_idempotent_per_invocation(store):
    run = parse_run_results(load("run_results.json"))
    assert store.ingest_run(run) is True
    assert store.ingest_run(run) is False
    assert len(store.list_invocations("run", 10)) == 1
    assert store.results(run.invocation_id)["total"] == 10


def test_a_run_and_a_freshness_check_can_share_an_invocation_id(store):
    run, src = load("run_results.json"), load("sources.json")
    src["metadata"]["invocation_id"] = run["metadata"]["invocation_id"]
    assert store.ingest_run(parse_run_results(run)) and store.ingest_sources(parse_sources(src))


def test_results_are_sorted_errors_first_and_filterable(store):
    run = parse_run_results(load("run_results.json"))
    store.ingest_run(run)
    page = store.results(run.invocation_id)
    statuses = [i["status"] for i in page["items"]]
    assert statuses == sorted(statuses, key=["error", "warn", "skipped", "ok"].index)
    assert store.results(run.invocation_id, status="error")["total"] == 3
    assert store.results(run.invocation_id, resource_type="model")["total"] == 4
    assert store.results(run.invocation_id, status="error", resource_type="test")["total"] == 2


def test_pagination_is_consistent(store):
    run = parse_run_results(load("run_results.json"))
    store.ingest_run(run)
    first, second = store.results(run.invocation_id, limit=6), store.results(run.invocation_id, limit=6, offset=6)
    ids = [i["unique_id"] for i in first["items"] + second["items"]]
    assert len(ids) == 10 and len(set(ids)) == 10 and first["total"] == 10


def test_latest_follows_generated_at_not_ingestion_order(store):
    old, new = fresh_id(load("run_results.json")), fresh_id(load("run_results.json"))
    old["metadata"]["generated_at"], new["metadata"]["generated_at"] = "2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z"
    store.ingest_run(parse_run_results(new))
    store.ingest_run(parse_run_results(old))  # chega DEPOIS, mas é mais antigo
    assert store.latest_invocation("run")["invocation_id"] == new["metadata"]["invocation_id"]


def test_model_history_spans_executions(store):
    for day in (1, 2, 3):
        doc = fresh_id(load("run_results.json"))
        doc["metadata"]["generated_at"] = f"2026-10-0{day}T00:00:00Z"
        store.ingest_run(parse_run_results(doc))
    history = store.model_history("broken_model", 10)
    assert len(history) == 3 and [h["generated_at"][:10] for h in history] == ["2026-10-03", "2026-10-02", "2026-10-01"]
    assert store.model_history("nao_existe", 10) == []


def test_audit_log_roundtrip_and_filter(store):
    for who in ("a", "b", "a"):
        store.log_audit(
            request_id="r",
            key_name=who,
            role="viewer",
            method="GET",
            path="/v1/x",
            query=None,
            status_code=200,
            duration_ms=1.234,
            now=NOW,
        )
    assert len(store.list_audit(10)) == 3
    assert [e["key_name"] for e in store.list_audit(10, key_name="b")] == ["b"]
    assert store.list_audit(1)[0]["id"] > store.list_audit(10)[-1]["id"]  # mais recentes primeiro


def test_audit_retention_deletes_only_old_rows(store):
    for days_ago in (200, 100, 10, 0):
        store.log_audit(
            request_id="r",
            key_name="a",
            role=None,
            method="GET",
            path="/p",
            query=None,
            status_code=200,
            duration_ms=1,
            now=NOW - timedelta(days=days_ago),
        )
    assert store.purge_audit(90, NOW) == 2
    assert len(store.list_audit(10)) == 2


def test_history_retention_removes_the_whole_invocation(store):
    old = fresh_id(load("run_results.json"))
    old["metadata"]["generated_at"] = "2025-01-01T00:00:00Z"
    store.ingest_run(parse_run_results(old))
    store.ingest_run(parse_run_results(load("run_results.json")))
    assert store.purge_history(180, NOW) == 1
    assert store.results(old["metadata"]["invocation_id"])["total"] == 0
    assert len(store.list_invocations("run", 10)) == 1


def test_a_failed_ingest_is_atomic_nothing_is_left_behind(store):
    """A execução e seus resultados entram na MESMA transação: se um resultado não puder ser gravado,
    a linha da execução também não fica (senão a ingestão seria 'idempotente' sobre um dado pela metade)."""
    from dataclasses import replace

    run = parse_run_results(load("run_results.json"))
    broken = replace(run, results=run.results[:3] + [replace(run.results[3], unique_id=object())])  # não cabe no SQLite
    with pytest.raises(sqlite3.Error):
        store.ingest_run(broken)
    assert store.get_invocation(run.invocation_id) is None and store.results(run.invocation_id)["total"] == 0
    assert store.ingest_run(run) is True  # e uma nova tentativa com o dado correto funciona


def test_ping(store):
    assert store.ping() is True
