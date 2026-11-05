"""Modelos de resposta. Só o que está aqui sai pela API: nada de SQL compilado, caminhos ou dados."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Status = Literal["ok", "warn", "error", "skipped", "unknown"]


class Health(BaseModel):
    status: Literal["ok"] = "ok"


class ResultOut(BaseModel):
    unique_id: str
    resource_type: str
    name: str
    status: Status
    raw_status: str = Field(description="Status original do dbt (success, pass, fail, runtime error...)")
    execution_seconds: float | None = None
    failures: int | None = Field(None, description="Linhas que violaram o teste (só testes)")
    message: str | None = Field(None, description="Mensagem do dbt, com dados pessoais REDIGIDOS e truncada")


class InvocationOut(BaseModel):
    invocation_id: str
    kind: Literal["run", "freshness"]
    command: str | None = None
    dbt_version: str | None = None
    generated_at: str
    elapsed_seconds: float | None = None
    counts: dict[str, int] = Field(default_factory=dict, description="Resultados por status")


class InvocationDetail(InvocationOut):
    total_results: int
    results: list[ResultOut]


class ModelHistoryItem(BaseModel):
    invocation_id: str
    generated_at: str
    status: Status
    execution_seconds: float | None = None
    failures: int | None = None


class FreshnessOut(BaseModel):
    unique_id: str
    name: str
    status: Status
    max_loaded_at: str | None = None
    age_seconds: float | None = None
    warn_after_seconds: float | None = None
    error_after_seconds: float | None = None


class FreshnessReport(BaseModel):
    invocation: InvocationOut | None = None
    items: list[FreshnessOut]


class Overview(BaseModel):
    status: Literal["healthy", "degraded", "failing", "unknown"]
    reasons: list[str]
    last_run: InvocationOut | None = None
    last_run_age_hours: float | None = None
    failing_models: int = 0
    skipped_models: int = 0
    failing_tests: int = 0
    warning_tests: int = 0
    stale_sources: int = 0
    warning_sources: int = 0


class IngestOut(BaseModel):
    invocation_id: str
    kind: Literal["run", "freshness"]
    created: bool = Field(description="False se essa execução já tinha sido ingerida (idempotente)")
    items: int


class AuditEntry(BaseModel):
    id: int
    ts: str
    request_id: str
    key_name: str
    role: str | None = None
    method: str
    path: str
    query: str | None = None
    status_code: int
    duration_ms: float | None = None


class PurgeOut(BaseModel):
    deleted_audit: int
    deleted_history: int
