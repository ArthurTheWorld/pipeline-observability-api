"""LGPD de ponta a ponta: dados pessoais vindos nos artefatos do dbt não podem ser gravados nem devolvidos."""

import copy
from pathlib import Path

import pytest
from conftest import ADMIN, KEYS, VIEWER
from helpers import fresh_id, load

PII = {
    "cpf": "123.456.789-09",
    "cpf_cru": "98765432100",
    "cnpj": "11.222.333/0001-81",
    "email": "maria.silva@empresa.com.br",
    "telefone": "(61) 99876-5432",
    "caminho": "/home/maria/projetos/dbt/models/clientes.sql",
}
ALL_ROUTES = [
    "/v1/overview",
    "/v1/runs",
    "/v1/runs/latest?limit=500",
    "/v1/freshness",
    "/v1/models/stg_orders/history",
    "/v1/models/broken_model/history",
]


@pytest.fixture
def poisoned(client):
    """run_results.json real, mas com dados pessoais plantados nas mensagens de erro (como um erro de
    banco que ecoa o valor que violou a restrição)."""
    doc = fresh_id(copy.deepcopy(load("run_results.json")))
    messages = [
        f"Duplicate key value violates unique constraint: ({PII['cpf']})",
        f"Conversion failed for '{PII['email']}' in {PII['caminho']}",
        f"cliente {PII['cnpj']} / {PII['cpf_cru']} tel {PII['telefone']}",
    ]
    for result, message in zip(doc["results"], messages, strict=False):
        result["message"] = message
    assert client.post("/v1/ingest/run-results", json=doc, headers=ADMIN).status_code == 200
    client.post("/v1/ingest/sources", json=load("sources.json"), headers=ADMIN)
    return client


def database_bytes(client) -> bytes:
    """Main + WAL: o dado pode estar no arquivo de log do SQLite e não no principal."""
    base = Path(client.app.state.settings.db_path)
    return b"".join(
        p.read_bytes() for p in (base, base.with_name(base.name + "-wal"), base.with_name(base.name + "-shm")) if p.exists()
    )


def test_no_personal_data_in_any_response(poisoned):
    for route in ALL_ROUTES:
        body = poisoned.get(route, headers=VIEWER).text
        for kind, value in PII.items():
            assert value not in body, f"{kind} vazou em {route}"
    # e a redação deixou marcas auditáveis em vez de apagar a mensagem
    messages = " ".join(r["message"] or "" for r in poisoned.get("/v1/runs/latest", headers=VIEWER).json()["results"])
    for token in ("[CPF]", "[EMAIL]", "[CAMINHO]", "[CNPJ]", "[TELEFONE]"):
        assert token in messages


def test_no_personal_data_in_the_database_file_itself(poisoned):
    poisoned.get("/v1/overview", headers=VIEWER)
    raw = database_bytes(poisoned)
    assert len(raw) > 1000
    for kind, value in PII.items():
        assert value.encode() not in raw, f"{kind} foi gravado no banco"


def test_the_compiled_sql_never_reaches_the_database_or_the_responses(loaded):
    """O run_results real carrega o SQL compilado de cada modelo. Ele não pode ser lido, só ignorado."""
    doc = load("run_results.json")
    marker = next(r["compiled_code"] for r in doc["results"] if r.get("compiled_code") and "fresh_orders" in r["compiled_code"])
    assert "customer_email, cpf" in marker  # o fixture realmente carrega isso
    raw = database_bytes(loaded)
    assert b"customer_email, cpf" not in raw and b"from raw.fresh_orders" not in raw
    for route in ALL_ROUTES:
        assert "customer_email, cpf" not in loaded.get(route, headers=VIEWER).text


def test_machine_paths_and_args_are_not_stored(loaded):
    raw = database_bytes(loaded)
    for needle in (b"/REDACTED", b"project_dir", b"profiles_dir", b"/home/", b"adapter_response"):
        assert needle not in raw


def test_api_keys_are_never_written_anywhere(loaded):
    loaded.get("/v1/overview", headers=VIEWER)
    loaded.get("/v1/audit", headers=ADMIN)
    loaded.get("/v1/overview", headers={"X-API-Key": "tentativa-de-chave-errada-xyz"})
    raw = database_bytes(loaded)
    for secret in list(KEYS.values()) + ["tentativa-de-chave-errada-xyz"]:
        assert secret.encode() not in raw


def test_the_audit_log_has_no_ip_and_no_request_body(loaded):
    entry = loaded.get("/v1/audit", headers=ADMIN).json()[0]
    assert set(entry) == {"id", "ts", "request_id", "key_name", "role", "method", "path", "query", "status_code", "duration_ms"}
    raw = database_bytes(loaded)
    assert b"testclient" not in raw and b"127.0.0.1" not in raw


def test_oversized_messages_are_truncated(client):
    doc = fresh_id(copy.deepcopy(load("run_results.json")))
    doc["results"][0]["message"] = "x" * 100_000
    client.post("/v1/ingest/run-results", json=doc, headers=ADMIN)
    longest = max(len(r["message"] or "") for r in client.get("/v1/runs/latest", headers=VIEWER).json()["results"])
    assert longest <= 300
