"""API de observabilidade de pipelines (FastAPI).

Princípios:
  * Toda rota (menos /health e /ready) exige chave de API; papéis `viewer` (lê) e `admin` (ingere,
    audita e aplica retenção).
  * Toda requisição autenticada, e toda tentativa FALHA, gera uma linha em audit_log: quem
    (nome da chave, nunca a chave), o quê e quando. Sem corpo, sem IP.
  * Nenhum dado pessoal é gravado: o parser só lê campos permitidos e redige o texto livre.
  * Erros internos nunca devolvem detalhes ao cliente, só um request_id para correlacionar com o log.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import __version__, schemas
from .config import Settings
from .ingest import ArtifactError, parse_run_results, parse_sources
from .security import Principal, RateLimiter, authenticate
from .store import Store

log = logging.getLogger("obs_api")
PUBLIC_PATHS = {"/health", "/ready"}
QUIET_PATHS = PUBLIC_PATHS  # sondas do Kubernetes não entram na auditoria (seria só ruído)
StatusFilter = Literal["ok", "warn", "error", "skipped", "unknown"]


def create_app(
    settings: Settings | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.db_path)
    limiter = RateLimiter(settings.rate_limit_per_minute, monotonic)

    app = FastAPI(
        title="Pipeline Observability API",
        version=__version__,
        description="Saúde dos pipelines de dados a partir dos artefatos do dbt, com autenticação, auditoria e LGPD.",
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
    )
    app.state.settings, app.state.store = settings, store

    # retenção aplicada na subida: dados pessoais de auditoria não ficam para sempre
    store.purge_audit(settings.audit_retention_days, clock())

    # ------------------------------------------------------------- middleware
    @app.middleware("http")
    async def security_and_audit(request: Request, call_next):
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        principal = authenticate(settings, request.headers.get("x-api-key"))
        request.state.principal, request.state.request_id = principal, request_id
        identity = principal.name if principal else "anon:" + (request.client.host if request.client else "?")

        retry_after = None if request.url.path in PUBLIC_PATHS else limiter.check(identity)
        if retry_after is not None:
            response = JSONResponse(
                {"detail": "limite de requisições excedido", "request_id": request_id},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        else:
            try:
                response = await call_next(request)
            except Exception:  # noqa: BLE001 - nunca vaza detalhes: loga aqui, devolve só o request_id
                log.exception("erro interno request_id=%s", request_id)
                response = JSONResponse({"detail": "erro interno", "request_id": request_id}, status_code=500)

        response.headers["X-Request-ID"] = request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"

        if request.url.path not in QUIET_PATHS:
            try:
                store.log_audit(
                    request_id=request_id,
                    key_name=principal.name if principal else "anonymous",
                    role=principal.role if principal else None,
                    method=request.method,
                    path=request.url.path,
                    query=request.url.query,
                    status_code=response.status_code,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    now=clock(),
                )
            except Exception:  # noqa: BLE001 - falha de auditoria não derruba a resposta, mas é registrada
                log.exception("falha ao gravar auditoria request_id=%s", request_id)
        return response

    # ------------------------------------------------------------ autorização
    def require(role: str):
        def dependency(request: Request) -> Principal:
            principal: Principal | None = request.state.principal
            if principal is None:
                raise HTTPException(401, "chave de API ausente ou inválida", headers={"WWW-Authenticate": "ApiKey"})
            if not principal.allows(role):
                raise HTTPException(403, f"esta rota exige o papel '{role}'")
            return principal

        return dependency

    viewer, admin = Depends(require("viewer")), Depends(require("admin"))

    # ----------------------------------------------------------------- sondas
    @app.get("/health", response_model=schemas.Health, tags=["sondas"], summary="Liveness (público)")
    def health():
        return schemas.Health()

    @app.get("/ready", response_model=schemas.Health, tags=["sondas"], summary="Readiness: o banco responde? (público)")
    def ready():
        if not store.ping():
            raise HTTPException(503, "banco indisponível")
        return schemas.Health()

    # ------------------------------------------------------------ helpers
    def invocation_out(row: dict) -> schemas.InvocationOut:
        counts = store.status_counts(row["invocation_id"]) if row["kind"] == "run" else {}
        if row["kind"] == "freshness":
            for item in store.freshness(row["invocation_id"]):
                counts[item["status"]] = counts.get(item["status"], 0) + 1
        return schemas.InvocationOut(
            counts=counts,
            **{k: row[k] for k in ("invocation_id", "kind", "command", "dbt_version", "generated_at", "elapsed_seconds")},
        )

    # -------------------------------------------------------------- consulta
    @app.get("/v1/overview", response_model=schemas.Overview, tags=["consulta"], summary="Semáforo: a plataforma está saudável?")
    def overview(_: Principal = viewer):
        run, fresh = store.latest_invocation("run"), store.latest_invocation("freshness")
        if run is None and fresh is None:
            return schemas.Overview(status="unknown", reasons=["nenhum artefato ingerido ainda"])

        # Tudo é calculado em variáveis locais e o modelo é construído UMA vez no fim: o Pydantic copia
        # listas ao validar, então acrescentar a `reasons` depois de criar o modelo não chegaria à resposta.
        reasons: list[str] = []
        fields = {}
        if run:
            fields["last_run"] = invocation_out(run)
            age = (clock() - datetime.fromisoformat(run["generated_at"])).total_seconds() / 3600
            fields["last_run_age_hours"] = round(age, 2)
            models, tests = store.status_counts(run["invocation_id"], "model"), store.status_counts(run["invocation_id"], "test")
            failing_models, skipped = models.get("error", 0), models.get("skipped", 0)
            failing_tests, warning_tests = tests.get("error", 0), tests.get("warn", 0)
            fields.update(
                failing_models=failing_models, skipped_models=skipped, failing_tests=failing_tests, warning_tests=warning_tests
            )
            if failing_models:
                reasons.append(f"{failing_models} modelo(s) com erro e {skipped} pulado(s)")
            if failing_tests:
                reasons.append(f"{failing_tests} teste(s) falhando")
            if warning_tests:
                reasons.append(f"{warning_tests} teste(s) em alerta")
            if age > settings.stale_after_hours:
                reasons.append(f"a última execução tem {age:.0f}h (limite {settings.stale_after_hours}h)")
        else:
            reasons.append("nenhuma execução do dbt ingerida")
        if fresh:
            items = store.freshness(fresh["invocation_id"])
            stale = sum(1 for i in items if i["status"] == "error")
            close = sum(1 for i in items if i["status"] == "warn")
            fields.update(stale_sources=stale, warning_sources=close)
            if stale:
                reasons.append(f"{stale} fonte(s) vencida(s)")
            if close:
                reasons.append(f"{close} fonte(s) perto de vencer")

        failing = fields.get("failing_models", 0) or fields.get("failing_tests", 0) or fields.get("stale_sources", 0)
        status = "failing" if failing else ("degraded" if reasons else "healthy")
        return schemas.Overview(status=status, reasons=reasons, **fields)

    @app.get("/v1/runs", response_model=list[schemas.InvocationOut], tags=["consulta"], summary="Histórico de execuções")
    def list_runs(limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0, le=100_000), _: Principal = viewer):
        return [invocation_out(row) for row in store.list_invocations("run", limit, offset)]

    @app.get(
        "/v1/runs/latest", response_model=schemas.InvocationDetail, tags=["consulta"], summary="Última execução, com resultados"
    )
    def latest_run(
        status: StatusFilter | None = None,
        resource_type: str | None = Query(None, max_length=30),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0, le=100_000),
        _: Principal = viewer,
    ):
        row = store.latest_invocation("run")
        if row is None:
            raise HTTPException(404, "nenhuma execução ingerida")
        return run_detail(row, status, resource_type, limit, offset)

    @app.get(
        "/v1/runs/{invocation_id}",
        response_model=schemas.InvocationDetail,
        tags=["consulta"],
        summary="Uma execução, com resultados",
    )
    def get_run(
        invocation_id: str,
        status: StatusFilter | None = None,
        resource_type: str | None = Query(None, max_length=30),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0, le=100_000),
        _: Principal = viewer,
    ):
        row = store.get_invocation(invocation_id[:64], "run")
        if row is None:
            raise HTTPException(404, "execução não encontrada")
        return run_detail(row, status, resource_type, limit, offset)

    def run_detail(row: dict, status, resource_type, limit, offset) -> schemas.InvocationDetail:
        page = store.results(row["invocation_id"], status, resource_type, limit, offset)
        base = invocation_out(row)
        return schemas.InvocationDetail(
            **base.model_dump(),
            total_results=page["total"],
            results=[schemas.ResultOut(**{k: r[k] for k in schemas.ResultOut.model_fields}) for r in page["items"]],
        )

    @app.get(
        "/v1/models/{name}/history",
        response_model=list[schemas.ModelHistoryItem],
        tags=["consulta"],
        summary="Histórico de um modelo ou teste",
    )
    def model_history(name: str, limit: int = Query(30, ge=1, le=200), _: Principal = viewer):
        rows = store.model_history(name[:200], limit)
        if not rows:
            raise HTTPException(404, "modelo ou teste sem histórico")
        return [schemas.ModelHistoryItem(**{k: r[k] for k in schemas.ModelHistoryItem.model_fields}) for r in rows]

    @app.get(
        "/v1/freshness", response_model=schemas.FreshnessReport, tags=["consulta"], summary="Frescor das fontes (última medição)"
    )
    def freshness(status: StatusFilter | None = None, _: Principal = viewer):
        row = store.latest_invocation("freshness")
        if row is None:
            return schemas.FreshnessReport(invocation=None, items=[])
        items = [
            schemas.FreshnessOut(**{k: i[k] for k in schemas.FreshnessOut.model_fields})
            for i in store.freshness(row["invocation_id"], status)
        ]
        return schemas.FreshnessReport(invocation=invocation_out(row), items=items)

    # --------------------------------------------------------------- ingestão
    async def read_json_body(request: Request):
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_body_bytes:
            raise HTTPException(413, "corpo grande demais")
        body = await request.body()
        if len(body) > settings.max_body_bytes:
            raise HTTPException(413, "corpo grande demais")
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(400, "o corpo não é um JSON válido") from None

    @app.post(
        "/v1/ingest/run-results",
        response_model=schemas.IngestOut,
        tags=["ingestão (admin)"],
        summary="Envia o run_results.json de uma execução do dbt",
    )
    async def ingest_run_results(request: Request, _: Principal = admin):
        document = await read_json_body(request)
        try:
            run = await run_in_threadpool(parse_run_results, document, settings.max_message_chars)
        except ArtifactError as exc:
            raise HTTPException(422, str(exc)) from None
        created = await run_in_threadpool(store.ingest_run, run, clock())
        return schemas.IngestOut(invocation_id=run.invocation_id, kind="run", created=created, items=len(run.results))

    @app.post(
        "/v1/ingest/sources",
        response_model=schemas.IngestOut,
        tags=["ingestão (admin)"],
        summary="Envia o sources.json (dbt source freshness)",
    )
    async def ingest_sources(request: Request, _: Principal = admin):
        document = await read_json_body(request)
        try:
            src = await run_in_threadpool(parse_sources, document)
        except ArtifactError as exc:
            raise HTTPException(422, str(exc)) from None
        created = await run_in_threadpool(store.ingest_sources, src, clock())
        return schemas.IngestOut(invocation_id=src.invocation_id, kind="freshness", created=created, items=len(src.items))

    # ------------------------------------------------- auditoria e retenção (LGPD)
    @app.get(
        "/v1/audit",
        response_model=list[schemas.AuditEntry],
        tags=["auditoria (admin)"],
        summary="Quem consultou o quê (mais recentes primeiro)",
    )
    def audit(
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0, le=1_000_000),
        key_name: str | None = Query(None, max_length=40),
        _: Principal = admin,
    ):
        return [schemas.AuditEntry(**row) for row in store.list_audit(limit, offset, key_name)]

    @app.post(
        "/v1/retention/purge",
        response_model=schemas.PurgeOut,
        tags=["auditoria (admin)"],
        summary="Aplica a política de retenção (auditoria e histórico)",
    )
    def purge(_: Principal = admin):
        return schemas.PurgeOut(
            deleted_audit=store.purge_audit(settings.audit_retention_days, clock()),
            deleted_history=store.purge_history(settings.history_retention_days, clock()),
        )

    return app
