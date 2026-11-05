import json

import pytest

from obs_api.config import ConfigError, Settings
from obs_api.security import hash_key

GOOD = json.dumps({"grafana": {"sha256": hash_key("k"), "role": "viewer"}})


def env(**kw):
    return {"API_KEYS": GOOD, **kw}


def test_defaults_and_parsing():
    s = Settings.from_env(env())
    assert s.api_keys["grafana"].role == "viewer" and s.audit_retention_days == 90 and s.enable_docs is False


def test_refuses_to_start_without_keys():
    with pytest.raises(ConfigError, match="API_KEYS vazio"):
        Settings.from_env({})
    assert Settings.from_env({}, require_keys=False).api_keys == {}  # CLI de ingestão/retenção não precisa de chaves


@pytest.mark.parametrize(
    "raw, message",
    [
        ("{not json", "JSON válido"),
        ("[]", "objeto"),
        (json.dumps({"UPPER": {"sha256": hash_key("k"), "role": "viewer"}}), "nome de chave inválido"),
        (json.dumps({"x": {"sha256": hash_key("k"), "role": "root"}}), "role"),
        (json.dumps({"x": {"sha256": "abc", "role": "viewer"}}), "sha256"),
        (json.dumps({"x": {"sha256": hash_key("k").upper(), "role": "viewer"}}), "minúsculos"),
        (json.dumps({"x": {"role": "viewer"}}), "sha256"),
    ],
)
def test_invalid_keys_fail_fast_with_a_clear_message(raw, message):
    with pytest.raises(ConfigError, match=message):
        Settings.from_env({"API_KEYS": raw})


def test_a_plain_text_key_is_rejected_as_a_hash():
    with pytest.raises(ConfigError, match="HASH"):
        Settings.from_env({"API_KEYS": json.dumps({"x": {"sha256": "obs_minha_chave_em_texto", "role": "admin"}})})


@pytest.mark.parametrize(
    "name, value",
    [
        ("AUDIT_RETENTION_DAYS", "0"),
        ("AUDIT_RETENTION_DAYS", "abc"),
        ("RATE_LIMIT_PER_MINUTE", "-5"),
        ("MAX_MESSAGE_CHARS", "5000"),
        ("MAX_BODY_BYTES", "10"),
    ],
)
def test_numeric_settings_are_validated(name, value):
    with pytest.raises(ConfigError, match=name):
        Settings.from_env(env(**{name: value}))


def test_docs_flag_and_overrides():
    s = Settings.from_env(env(ENABLE_DOCS="true", AUDIT_RETENTION_DAYS="30", DB_PATH="/data/x.db"))
    assert s.enable_docs and s.audit_retention_days == 30 and s.db_path == "/data/x.db"
