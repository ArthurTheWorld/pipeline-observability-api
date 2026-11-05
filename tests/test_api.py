import copy
from datetime import timedelta

import pytest
from conftest import ADMIN, KEYS, VIEWER
from helpers import healthy_run, healthy_sources, load, now_after, set_status

VIEWER_ROUTES = ["/v1/overview", "/v1/runs", "/v1/runs/latest", "/v1/freshness", "/v1/models/stg_orders/history"]
ADMIN_GET = ["/v1/audit"]


# ------------------------------------------------------------- autenticação
def test_health_and_ready_are_public(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").status_code == 200


@pytest.mark.parametrize("route", VIEWER_ROUTES + ADMIN_GET)
def test_every_other_route_requires_a_key(client, route):
    r = client.get(route)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "ApiKey"
    assert client.get(route, headers={"X-API-Key": "chave-errada"}).status_code == 401


@pytest.mark.parametrize("route", ADMIN_GET)
def test_a_viewer_key_cannot_use_admin_routes(client, route):
    assert client.get(route, headers=VIEWER).status_code == 403


@pytest.mark.parametrize(
    "method, route", [("post", "/v1/ingest/run-results"), ("post", "/v1/ingest/sources"), ("post", "/v1/retention/purge")]
)
def test_a_viewer_key_cannot_write(client, method, route):
    assert getattr(client, method)(route, json={}, headers=VIEWER).status_code == 403
    assert getattr(client, method)(route, json={}).status_code == 401


@pytest.mark.parametrize("route", VIEWER_ROUTES)
def test_admin_can_also_read(loaded, route):
    assert loaded.get(route, headers=ADMIN).status_code == 200


def test_the_key_in_the_query_string_is_not_accepted(client):
    assert client.get(f"/v1/overview?api_key={KEYS['viewer']}").status_code == 401  # nunca na URL: iria para logs de proxy


def test_docs_are_off_by_default_and_can_be_enabled(client, make_client):
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404
    assert make_client(enable_docs=True).get("/openapi.json").status_code == 200


def test_security_headers_and_request_id(client):
    r = client.get("/health")
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    assert len(r.headers["x-request-id"]) == 36 and r.headers["x-request-id"] != client.get("/health").headers["x-request-id"]


# ----------------------------------------------------------------- semáforo
def test_overview_with_real_artifacts_is_failing_and_explains_why(loaded):
    body = loaded.get("/v1/overview", headers=VIEWER).json()
    assert body["status"] == "failing"
    # o dbt reporta SKIP=3, mas um deles é teste; o painel conta só MODELOS pulados (downstream_of_broken, fct_orders)
    assert (body["failing_models"], body["skipped_models"], body["failing_tests"], body["warning_tests"]) == (1, 2, 2, 1)
    assert (body["stale_sources"], body["warning_sources"]) == (1, 1)
    text = " | ".join(body["reasons"])
    assert "modelo(s) com erro" in text and "teste(s) falhando" in text and "fonte(s) vencida(s)" in text
    assert body["last_run"]["command"] == "build" and body["last_run_age_hours"] == 1.0


def test_overview_unknown_when_nothing_was_ingested(client):
    assert client.get("/v1/overview", headers=VIEWER).json() == {
        "status": "unknown",
        "reasons": ["nenhum artefato ingerido ainda"],
        "last_run": None,
        "last_run_age_hours": None,
        "failing_models": 0,
        "skipped_models": 0,
        "failing_tests": 0,
        "warning_tests": 0,
        "stale_sources": 0,
        "warning_sources": 0,
    }


def test_overview_healthy(client):
    client.post("/v1/ingest/run-results", json=healthy_run(), headers=ADMIN)
    client.post("/v1/ingest/sources", json=healthy_sources(), headers=ADMIN)
    body = client.get("/v1/overview", headers=VIEWER).json()
    assert body["status"] == "healthy" and body["reasons"] == []


def test_overview_degraded_on_a_test_warning_only(client):
    client.post("/v1/ingest/run-results", json=set_status(healthy_run(), "assert_no_big_orders", "warn"), headers=ADMIN)
    client.post("/v1/ingest/sources", json=healthy_sources(), headers=ADMIN)
    body = client.get("/v1/overview", headers=VIEWER).json()
    assert body["status"] == "degraded" and body["warning_tests"] == 1 and body["failing_tests"] == 0


def test_overview_degraded_when_a_source_is_close_to_expiring(client):
    client.post("/v1/ingest/run-results", json=healthy_run(), headers=ADMIN)
    client.post("/v1/ingest/sources", json=set_status(healthy_sources(), "warning_orders", "warn"), headers=ADMIN)
    assert client.get("/v1/overview", headers=VIEWER).json()["status"] == "degraded"


def test_overview_failing_on_a_single_stale_source(client):
    client.post("/v1/ingest/run-results", json=healthy_run(), headers=ADMIN)
    client.post("/v1/ingest/sources", json=set_status(healthy_sources(), "stale_orders", "error"), headers=ADMIN)
    assert client.get("/v1/overview", headers=VIEWER).json()["status"] == "failing"


def test_overview_degraded_when_the_last_run_is_too_old(make_client):
    late = now_after(load("run_results.json"), hours=48)
    client = make_client(now=late)
    client.post("/v1/ingest/run-results", json=healthy_run(), headers=ADMIN)
    client.post("/v1/ingest/sources", json=healthy_sources(), headers=ADMIN)
    body = client.get("/v1/overview", headers=VIEWER).json()
    assert body["status"] == "degraded" and "limite 26h" in " ".join(body["reasons"])


def test_overview_with_only_a_run_and_no_freshness_data(client):
    client.post("/v1/ingest/run-results", json=healthy_run(), headers=ADMIN)
    assert client.get("/v1/overview", headers=VIEWER).json()["status"] == "healthy"


# ----------------------------------------------------------------- consultas
def test_latest_run_lists_errors_first_with_redacted_messages(loaded):
    body = loaded.get("/v1/runs/latest", headers=VIEWER).json()
    assert body["total_results"] == 10 and body["counts"] == {"ok": 3, "warn": 1, "error": 3, "skipped": 3}
    assert [r["status"] for r in body["results"][:3]] == ["error"] * 3
    assert {"unique_id", "resource_type", "name", "status", "raw_status", "execution_seconds", "failures", "message"} == set(
        body["results"][0]
    )


def test_filters_and_pagination(loaded):
    errors = loaded.get("/v1/runs/latest?status=error&resource_type=test", headers=VIEWER).json()
    assert errors["total_results"] == 2 and all(r["resource_type"] == "test" for r in errors["results"])
    page1 = loaded.get("/v1/runs/latest?limit=4", headers=VIEWER).json()["results"]
    page2 = loaded.get("/v1/runs/latest?limit=4&offset=4", headers=VIEWER).json()["results"]
    assert len({r["unique_id"] for r in page1 + page2}) == 8


@pytest.mark.parametrize("query", ["limit=0", "limit=501", "limit=abc", "offset=-1", "status=nope", "resource_type=" + "x" * 31])
def test_invalid_query_parameters_are_rejected(loaded, query):
    assert loaded.get(f"/v1/runs/latest?{query}", headers=VIEWER).status_code == 422


def test_get_run_by_id_and_404(loaded):
    run_id = load("run_results.json")["metadata"]["invocation_id"]
    assert loaded.get(f"/v1/runs/{run_id}", headers=VIEWER).json()["invocation_id"] == run_id
    assert loaded.get("/v1/runs/nao-existe", headers=VIEWER).status_code == 404
    assert loaded.get("/v1/runs/" + "x" * 500, headers=VIEWER).status_code == 404


def test_run_history_and_404_without_data(client, loaded):
    assert len(loaded.get("/v1/runs", headers=VIEWER).json()) == 1
    assert loaded.get("/v1/models/broken_model/history", headers=VIEWER).json()[0]["status"] == "error"
    assert loaded.get("/v1/models/nope/history", headers=VIEWER).status_code == 404


def test_latest_run_404_when_empty(client):
    assert client.get("/v1/runs/latest", headers=VIEWER).status_code == 404
    assert client.get("/v1/freshness", headers=VIEWER).json() == {"invocation": None, "items": []}


def test_freshness_report_and_filter(loaded):
    body = loaded.get("/v1/freshness", headers=VIEWER).json()
    assert [i["status"] for i in body["items"]] == ["error", "warn", "ok"]
    assert body["invocation"]["counts"] == {"ok": 1, "warn": 1, "error": 1}
    stale = loaded.get("/v1/freshness?status=error", headers=VIEWER).json()["items"]
    assert [i["name"] for i in stale] == ["raw.stale_orders"] and stale[0]["error_after_seconds"] == 72 * 3600


# ----------------------------------------------------------------- ingestão
def test_ingest_is_idempotent_over_http(client):
    doc = load("run_results.json")
    first = client.post("/v1/ingest/run-results", json=doc, headers=ADMIN).json()
    again = client.post("/v1/ingest/run-results", json=doc, headers=ADMIN).json()
    assert first["created"] is True and again["created"] is False and first["items"] == again["items"] == 10
    assert len(client.get("/v1/runs", headers=VIEWER).json()) == 1


def test_invalid_bodies_are_rejected_without_echoing_content(client):
    r = client.post("/v1/ingest/run-results", content=b"{nao e json", headers={**ADMIN, "Content-Type": "application/json"})
    assert r.status_code == 400 and "nao e json" not in r.text
    r = client.post("/v1/ingest/run-results", json={"segredo": "valor-secreto-123"}, headers=ADMIN)
    assert r.status_code == 422 and "valor-secreto-123" not in r.text
    assert client.post("/v1/ingest/run-results", json=load("sources.json"), headers=ADMIN).status_code == 422


def test_oversized_bodies_are_refused(make_client):
    client = make_client(max_body_bytes=2048)
    huge = copy.deepcopy(load("run_results.json"))
    assert client.post("/v1/ingest/run-results", json=huge, headers=ADMIN).status_code == 413


def test_the_endpoint_takes_no_file_path_so_path_traversal_is_impossible(client):
    r = client.post("/v1/ingest/run-results?path=/etc/passwd", json=load("run_results.json"), headers=ADMIN)
    assert r.status_code == 200 and "root:" not in r.text


# ----------------------------------------------------------- auditoria (LGPD)
def test_every_access_is_audited_including_failures_and_never_stores_the_key(loaded):
    loaded.get("/v1/overview", headers=VIEWER)
    loaded.get("/v1/overview")  # sem chave
    loaded.get("/v1/audit", headers=VIEWER)  # papel insuficiente
    entries = loaded.get("/v1/audit?limit=50", headers=ADMIN).json()
    seen = [(e["key_name"], e["path"], e["status_code"]) for e in entries]
    assert ("grafana", "/v1/overview", 200) in seen
    assert ("anonymous", "/v1/overview", 401) in seen
    assert ("grafana", "/v1/audit", 403) in seen
    assert ("airflow", "/v1/ingest/run-results", 200) in seen
    assert all(e["request_id"] and e["ts"] and e["duration_ms"] is not None for e in entries)
    dump = str(entries)
    assert not any(secret in dump for secret in KEYS.values())


def test_health_probes_do_not_flood_the_audit_log(client):
    for _ in range(5):
        client.get("/health"), client.get("/ready")
    # a própria requisição de auditoria só é registrada DEPOIS de responder: a 1ª consulta vem vazia,
    # o que prova que 10 sondas não deixaram rastro
    assert client.get("/v1/audit", headers=ADMIN).json() == []
    entries = client.get("/v1/audit", headers=ADMIN).json()
    assert [e["path"] for e in entries] == ["/v1/audit"]


def test_audit_filter_by_key(loaded):
    loaded.get("/v1/overview", headers=VIEWER)
    only = loaded.get("/v1/audit?key_name=grafana", headers=ADMIN).json()
    assert only and {e["key_name"] for e in only} == {"grafana"}


def test_query_strings_are_audited_but_capped(loaded):
    loaded.get("/v1/runs/latest?limit=5&status=error", headers=VIEWER)
    entry = next(e for e in loaded.get("/v1/audit", headers=ADMIN).json() if e["path"] == "/v1/runs/latest")
    assert entry["query"] == "limit=5&status=error"


def test_retention_purge_via_api_removes_old_entries(make_client, tmp_path):
    import sqlite3

    client = make_client()
    client.get("/v1/overview", headers=VIEWER)
    with sqlite3.connect(str(tmp_path / "obs.db")) as db:
        db.execute("UPDATE audit_log SET ts = '2020-01-01T00:00:00+00:00'")
    body = client.post("/v1/retention/purge", headers=ADMIN).json()
    assert body["deleted_audit"] == 1
    assert all(e["ts"] > "2025" for e in client.get("/v1/audit", headers=ADMIN).json())


def test_old_audit_rows_are_purged_on_startup(tmp_path, fixed_now):
    import sqlite3

    from conftest import make_settings

    from obs_api.main import create_app
    from obs_api.store import Store

    settings = make_settings(tmp_path)
    Store(settings.db_path).log_audit(
        request_id="r",
        key_name="a",
        role=None,
        method="GET",
        path="/x",
        query=None,
        status_code=200,
        duration_ms=1,
        now=fixed_now - timedelta(days=400),
    )
    create_app(settings, clock=lambda: fixed_now)
    with sqlite3.connect(settings.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


# --------------------------------------------------- limite e erros internos
def test_rate_limit_returns_429_with_retry_after(make_client):
    client = make_client(rate_limit_per_minute=3)
    codes = [client.get("/v1/overview", headers=VIEWER).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]
    blocked = client.get("/v1/overview", headers=VIEWER)
    assert int(blocked.headers["retry-after"]) >= 1
    assert client.get("/v1/overview", headers=ADMIN).status_code == 200  # outra chave, outro balde
    assert client.get("/health").status_code == 200  # as sondas nunca são limitadas


def test_brute_force_with_wrong_keys_is_rate_limited_too(make_client):
    client = make_client(rate_limit_per_minute=3)
    codes = [client.get("/v1/overview", headers={"X-API-Key": f"palpite-{i}"}).status_code for i in range(6)]
    assert codes == [401, 401, 401, 429, 429, 429]


def test_internal_errors_never_leak_details(loaded, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("SEGREDO-INTERNO /home/usuario/senha.txt")

    monkeypatch.setattr(loaded.app.state.store, "latest_invocation", boom)
    r = loaded.get("/v1/overview", headers=VIEWER)
    assert r.status_code == 500 and "SEGREDO" not in r.text and "senha" not in r.text
    body = r.json()
    assert body["detail"] == "erro interno" and body["request_id"] == r.headers["x-request-id"]
    monkeypatch.undo()
    assert any(
        e["status_code"] == 500 and e["request_id"] == body["request_id"] for e in loaded.get("/v1/audit", headers=ADMIN).json()
    )


def test_not_ready_when_the_database_is_down(client, monkeypatch):
    monkeypatch.setattr(client.app.state.store, "ping", lambda: False)
    assert client.get("/ready").status_code == 503 and client.get("/health").status_code == 200
