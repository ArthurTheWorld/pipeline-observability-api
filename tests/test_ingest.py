import copy
import json

import pytest
from helpers import load

from obs_api.ingest import ArtifactError, canonical_status, display_name, parse_run_results, parse_sources

RAW_TO_CANONICAL = {
    "success": "ok",
    "pass": "ok",
    "warn": "warn",
    "error": "error",
    "fail": "error",
    "runtime error": "error",
    "skipped": "skipped",
}


def test_parses_every_result_of_the_real_artifact():
    doc = load("run_results.json")
    run = parse_run_results(doc)
    assert len(run.results) == len(doc["results"]) == 10
    assert run.command == "build" and run.dbt_version and run.invocation_id == doc["metadata"]["invocation_id"]


def test_status_mapping_matches_an_independent_count():
    doc = load("run_results.json")
    expected = {}
    for r in doc["results"]:
        key = RAW_TO_CANONICAL[r["status"]]
        expected[key] = expected.get(key, 0) + 1
    got = {}
    for r in parse_run_results(doc).results:
        got[r.status] = got.get(r.status, 0) + 1
    assert got == expected == {"ok": 3, "warn": 1, "error": 3, "skipped": 3}


def test_each_real_outcome_is_recognised():
    by_name = {r.name: r for r in parse_run_results(load("run_results.json")).results}
    assert by_name["broken_model"].status == "error" and by_name["broken_model"].resource_type == "model"
    assert by_name["downstream_of_broken"].status == "skipped"
    assert by_name["stg_orders"].status == "ok"
    big = by_name["assert_no_big_orders"]
    assert (big.status, big.raw_status, big.failures, big.resource_type) == ("warn", "warn", 1, "test")
    bad = by_name["assert_customer_has_valid_cpf"]
    assert (bad.status, bad.failures) == ("error", 1)


def test_test_names_lose_the_hash_suffix():
    names = {r.name for r in parse_run_results(load("run_results.json")).results}
    assert "unique_stg_orders_order_id" in names
    assert not any(n.split(".")[-1].isalnum() and len(n.split(".")[-1]) == 10 and "." in n for n in names)
    assert display_name("test.demo.not_null_x.0123456789") == "not_null_x"
    assert display_name("model.demo.stg_orders") == "stg_orders"
    assert display_name("source.demo.raw.orders") == "raw.orders"


def test_allow_list_nothing_dangerous_is_carried():
    """O artefato real traz SQL compilado, args com caminhos e adapter_response; nada disso é lido."""
    doc = load("run_results.json")
    assert any(
        "compiled_code" in r and r["compiled_code"] for r in doc["results"]
    )  # o fixture TEM o SQL: o teste é significativo
    run = parse_run_results(doc)
    fields = set(run.results[0].__dataclass_fields__)
    assert fields == {"unique_id", "resource_type", "name", "status", "raw_status", "execution_seconds", "failures", "message"}
    blob = json.dumps([r.__dict__ for r in run.results] + [run.command, run.dbt_version])
    for marker in (
        "compiled_code",
        "from raw.fresh_orders",
        "customer_email, cpf",
        "adapter_response",
        "project_dir",
        "/REDACTED",
    ):
        assert marker not in blob


def test_messages_are_redacted_and_truncated():
    doc = copy.deepcopy(load("run_results.json"))
    doc["results"][0]["message"] = "duplicate 123.456.789-09 e ana@x.com " + "z" * 1000
    msg = parse_run_results(doc, max_message_chars=80).results[0].message
    assert len(msg) <= 80 and "123.456" not in msg and "ana@x" not in msg


def test_non_string_message_and_unusable_rows_do_not_break_ingestion():
    doc = copy.deepcopy(load("run_results.json"))
    doc["results"][0]["message"] = {"nested": "x"}
    doc["results"].append({"status": "success"})  # sem unique_id
    doc["results"].append("lixo")
    run = parse_run_results(doc)
    assert run.results[0].message is None and len(run.results) == 10


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.pop("metadata"), "metadata"),
        (lambda d: d["metadata"].update(dbt_schema_version="https://x/other/v1.json"), "run-results"),
        (lambda d: d["metadata"].update(invocation_id="../../etc"), "invocation_id"),
        (lambda d: d["metadata"].pop("invocation_id"), "invocation_id"),
        (lambda d: d["metadata"].update(generated_at="ontem"), "ISO"),
        (lambda d: d.update(results="x"), "lista"),
    ],
)
def test_invalid_artifacts_are_rejected_with_a_safe_message(mutate, message):
    doc = copy.deepcopy(load("run_results.json"))
    mutate(doc)
    with pytest.raises(ArtifactError, match=message):
        parse_run_results(doc)


@pytest.mark.parametrize("bad", [None, [], "texto", 42])
def test_non_object_documents_are_rejected(bad):
    with pytest.raises(ArtifactError):
        parse_run_results(bad)
    with pytest.raises(ArtifactError):
        parse_sources(bad)


def test_sources_are_parsed_with_thresholds_in_seconds():
    items = {i.name: i for i in parse_sources(load("sources.json")).items}
    assert set(items) == {"raw.fresh_orders", "raw.warning_orders", "raw.stale_orders"}
    assert items["raw.fresh_orders"].status == "ok"
    assert items["raw.warning_orders"].status == "warn"
    assert items["raw.stale_orders"].status == "error"
    assert (
        items["raw.fresh_orders"].warn_after_seconds == 24 * 3600 and items["raw.fresh_orders"].error_after_seconds == 72 * 3600
    )
    assert items["raw.stale_orders"].age_seconds > 72 * 3600 and items["raw.stale_orders"].max_loaded_at


def test_a_run_results_file_is_not_accepted_as_sources_and_vice_versa():
    with pytest.raises(ArtifactError, match="sources"):
        parse_sources(load("run_results.json"))
    with pytest.raises(ArtifactError, match="run-results"):
        parse_run_results(load("sources.json"))


def test_canonical_status_defaults_to_unknown():
    assert canonical_status("something-new") == "unknown" and canonical_status("PASS") == "ok"
