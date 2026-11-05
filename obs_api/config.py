"""Configuração por variáveis de ambiente. Falha RÁPIDO e com mensagem clara: uma API de
segurança que sobe com a configuração errada é pior do que uma que não sobe."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

ROLES = ("viewer", "admin")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ApiKey:
    name: str
    sha256: str
    role: str


@dataclass(frozen=True)
class Settings:
    db_path: str = "data/observability.db"
    api_keys: dict[str, ApiKey] = field(default_factory=dict)
    audit_retention_days: int = 90
    history_retention_days: int = 180
    max_message_chars: int = 300
    max_body_bytes: int = 20 * 1024 * 1024
    rate_limit_per_minute: int = 120
    stale_after_hours: int = 26
    enable_docs: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ, require_keys: bool = True) -> Settings:
        keys = _parse_keys(env.get("API_KEYS", ""))
        if require_keys and not keys:
            raise ConfigError(
                "API_KEYS vazio: sem chaves nenhum endpoint protegido seria acessível. "
                "Gere uma com `python -m obs_api new-key --name <nome> --role viewer|admin`."
            )
        return cls(
            db_path=env.get("DB_PATH", cls.db_path),
            api_keys=keys,
            audit_retention_days=_int(env, "AUDIT_RETENTION_DAYS", 90, 1, 3650),
            history_retention_days=_int(env, "HISTORY_RETENTION_DAYS", 180, 1, 3650),
            max_message_chars=_int(env, "MAX_MESSAGE_CHARS", 300, 20, 2000),
            max_body_bytes=_int(env, "MAX_BODY_BYTES", 20 * 1024 * 1024, 1024, 200 * 1024 * 1024),
            rate_limit_per_minute=_int(env, "RATE_LIMIT_PER_MINUTE", 120, 1, 100_000),
            stale_after_hours=_int(env, "STALE_AFTER_HOURS", 26, 1, 24 * 30),
            enable_docs=env.get("ENABLE_DOCS", "").lower() in ("1", "true", "yes"),
        )


def _int(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} deve ser um inteiro, recebi {raw!r}") from None
    if not low <= value <= high:
        raise ConfigError(f"{name} deve estar entre {low} e {high}, recebi {value}")
    return value


def _parse_keys(raw: str) -> dict[str, ApiKey]:
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"API_KEYS não é um JSON válido: {exc.msg}") from None
    if not isinstance(data, dict):
        raise ConfigError('API_KEYS deve ser um objeto: {"nome": {"sha256": "...", "role": "viewer"}}')
    keys: dict[str, ApiKey] = {}
    for name, entry in data.items():
        if not _NAME.match(str(name)):
            raise ConfigError(f"nome de chave inválido: {name!r} (use a-z, 0-9, _ ou -, até 40 caracteres)")
        if not isinstance(entry, dict) or entry.get("role") not in ROLES:
            raise ConfigError(f"chave {name!r}: 'role' deve ser um de {ROLES}")
        digest = str(entry.get("sha256", ""))
        if not _SHA256.match(digest):
            raise ConfigError(
                f"chave {name!r}: 'sha256' deve ter 64 caracteres hexadecimais minúsculos (guarde o HASH, nunca a chave)"
            )
        keys[name] = ApiKey(name=name, sha256=digest, role=entry["role"])
    return keys
