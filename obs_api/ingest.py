"""Leitura dos artefatos do dbt (run_results.json e sources.json).

LGPD / minimização: o parser usa uma LISTA DE CAMPOS PERMITIDOS. O `run_results.json` real também
traz o SQL compilado de cada modelo (`compiled_code`), caminhos da máquina (`args`) e a resposta do
banco (`adapter_response`): nada disso é lido, armazenado ou devolvido. Do que é lido, só o texto
livre (`message`) passa por redação de dados pessoais.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .redact import redact

MAX_RESULTS = 100_000
_UUID = re.compile(r"^[A-Za-z0-9\-]{8,64}$")
_COMMAND = re.compile(r"^[a-z][a-z\-_ ]{0,29}$")
_TEST_HASH = re.compile(r"\.[0-9a-f]{10}$")
_PERIOD_SECONDS = {"minute": 60, "hour": 3600, "day": 86400}

# status do dbt -> status canônico da API
_STATUS = {
    "success": "ok",
    "pass": "ok",
    "warn": "warn",
    "partial success": "warn",
    "error": "error",
    "fail": "error",
    "runtime error": "error",
    "skipped": "skipped",
    "no-op": "skipped",
}
RESOURCE_TYPES = {"model", "test", "seed", "snapshot", "operation", "analysis", "unit_test"}


class ArtifactError(ValueError):
    """Artefato inválido. A mensagem é segura para devolver ao cliente (não ecoa conteúdo do arquivo)."""


@dataclass(frozen=True)
class ParsedResult:
    unique_id: str
    resource_type: str
    name: str
    status: str
    raw_status: str
    execution_seconds: float | None
    failures: int | None
    message: str | None


@dataclass(frozen=True)
class ParsedRun:
    invocation_id: str
    dbt_version: str | None
    generated_at: str
    elapsed_seconds: float | None
    command: str | None
    results: list[ParsedResult] = field(default_factory=list)


@dataclass(frozen=True)
class ParsedFreshness:
    unique_id: str
    name: str
    status: str
    raw_status: str
    max_loaded_at: str | None
    age_seconds: float | None
    warn_after_seconds: float | None
    error_after_seconds: float | None


@dataclass(frozen=True)
class ParsedSources:
    invocation_id: str
    dbt_version: str | None
    generated_at: str
    elapsed_seconds: float | None
    items: list[ParsedFreshness] = field(default_factory=list)


# ------------------------------------------------------------------ helpers
def canonical_status(raw: Any) -> str:
    return _STATUS.get(str(raw).lower(), "unknown")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise ArtifactError("metadata.generated_at ausente ou inválido")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ArtifactError("metadata.generated_at não é uma data ISO válida") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _header(doc: Any, schema_kind: str) -> dict[str, Any]:
    if not isinstance(doc, dict):
        raise ArtifactError("o artefato deve ser um objeto JSON")
    metadata = doc.get("metadata")
    if not isinstance(metadata, dict):
        raise ArtifactError("campo 'metadata' ausente")
    schema = str(metadata.get("dbt_schema_version", ""))
    if f"/{schema_kind}/v" not in schema:
        raise ArtifactError(f"não parece um {schema_kind}.json do dbt")
    invocation_id = metadata.get("invocation_id")
    if not isinstance(invocation_id, str) or not _UUID.match(invocation_id):
        raise ArtifactError("metadata.invocation_id ausente ou inválido")
    version = metadata.get("dbt_version")
    return {
        "invocation_id": invocation_id,
        "dbt_version": version[:30] if isinstance(version, str) else None,
        "generated_at": _timestamp(metadata.get("generated_at")),
        "elapsed_seconds": _number(doc.get("elapsed_time")),
    }


def _items(doc: dict[str, Any]) -> list[dict[str, Any]]:
    results = doc.get("results")
    if not isinstance(results, list):
        raise ArtifactError("campo 'results' deve ser uma lista")
    if len(results) > MAX_RESULTS:
        raise ArtifactError(f"resultados demais (máximo {MAX_RESULTS})")
    return [item for item in results if isinstance(item, dict)]


def display_name(unique_id: str) -> str:
    """model.demo.stg_orders -> stg_orders;  test.demo.unique_x_id.e3b841c71a -> unique_x_id;
    source.demo.raw.orders -> raw.orders."""
    parts = unique_id.split(".")
    kind = parts[0]
    if kind == "source" and len(parts) >= 4:
        return ".".join(parts[2:4])
    body = ".".join(parts[2:]) if len(parts) > 2 else unique_id
    return _TEST_HASH.sub("", body) if kind in ("test", "unit_test") else body


# ----------------------------------------------------------------- parsers
def parse_run_results(doc: Any, max_message_chars: int = 300) -> ParsedRun:
    header = _header(doc, "run-results")
    which = (doc.get("args") or {}).get("which") if isinstance(doc.get("args"), dict) else None
    command = which if isinstance(which, str) and _COMMAND.match(which) else None

    results: list[ParsedResult] = []
    for item in _items(doc):
        unique_id = item.get("unique_id")
        if not isinstance(unique_id, str) or not 3 <= len(unique_id) <= 300:
            continue  # linha sem identificador utilizável: ignora em vez de derrubar a ingestão inteira
        resource_type = unique_id.split(".", 1)[0]
        raw_status = str(item.get("status", "unknown"))[:30]
        results.append(
            ParsedResult(
                unique_id=unique_id,
                resource_type=resource_type if resource_type in RESOURCE_TYPES else "other",
                name=display_name(unique_id)[:200],
                status=canonical_status(raw_status),
                raw_status=raw_status,
                execution_seconds=_number(item.get("execution_time")),
                failures=_count(item.get("failures")),
                message=redact(item.get("message") if isinstance(item.get("message"), str) else None, max_message_chars),
            )
        )
    return ParsedRun(command=command, results=results, **header)


def _seconds(criteria: Any, key: str) -> float | None:
    rule = criteria.get(key) if isinstance(criteria, dict) else None
    if not isinstance(rule, dict):
        return None
    count, period = _number(rule.get("count")), _PERIOD_SECONDS.get(str(rule.get("period")))
    return count * period if count is not None and period else None


def parse_sources(doc: Any) -> ParsedSources:
    header = _header(doc, "sources")
    items: list[ParsedFreshness] = []
    for item in _items(doc):
        unique_id = item.get("unique_id")
        if not isinstance(unique_id, str) or not unique_id.startswith("source."):
            continue
        raw_status = str(item.get("status", "unknown"))[:30]
        loaded = item.get("max_loaded_at")
        items.append(
            ParsedFreshness(
                unique_id=unique_id[:300],
                name=display_name(unique_id)[:200],
                status=canonical_status(raw_status),
                raw_status=raw_status,
                max_loaded_at=_timestamp(loaded) if isinstance(loaded, str) else None,
                age_seconds=_number(item.get("max_loaded_at_time_ago_in_s")),
                warn_after_seconds=_seconds(item.get("criteria"), "warn_after"),
                error_after_seconds=_seconds(item.get("criteria"), "error_after"),
            )
        )
    return ParsedSources(items=items, **header)


# ------------------------------------------------------ ingestão de diretório
def read_json_file(path: Path, max_bytes: int) -> Any:
    if path.stat().st_size > max_bytes:
        raise ArtifactError(f"{path.name} excede {max_bytes} bytes")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise ArtifactError(f"{path.name} não é um JSON válido") from None
