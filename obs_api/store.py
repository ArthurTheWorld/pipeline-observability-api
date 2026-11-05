"""Persistência em SQLite (stdlib, sem ORM): uma conexão por operação, thread-safe.

Uma execução e seus resultados são gravados na MESMA transação (ou entram inteiros, ou não entram), e a
retenção apaga os dois juntos.

SQLite basta para o volume (uma linha por modelo/teste por execução) e mantém o serviço sem
dependências externas. Em produção com mais de uma réplica, troque por Postgres: a camada abaixo é
o único lugar que conhece SQL.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from .ingest import ParsedRun, ParsedSources

SCHEMA = """
CREATE TABLE IF NOT EXISTS invocations (
    invocation_id TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('run', 'freshness')),
    command       TEXT,
    dbt_version   TEXT,
    generated_at  TEXT NOT NULL,
    elapsed_seconds REAL,
    ingested_at   TEXT NOT NULL,
    PRIMARY KEY (invocation_id, kind)
);
CREATE TABLE IF NOT EXISTS results (
    invocation_id TEXT NOT NULL,
    unique_id     TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    name          TEXT NOT NULL,
    status        TEXT NOT NULL,
    raw_status    TEXT NOT NULL,
    execution_seconds REAL,
    failures      INTEGER,
    message       TEXT,
    PRIMARY KEY (invocation_id, unique_id)
);
CREATE INDEX IF NOT EXISTS idx_results_name ON results(name);
CREATE TABLE IF NOT EXISTS source_freshness (
    invocation_id TEXT NOT NULL,
    unique_id     TEXT NOT NULL,
    name          TEXT NOT NULL,
    status        TEXT NOT NULL,
    raw_status    TEXT NOT NULL,
    max_loaded_at TEXT,
    age_seconds   REAL,
    warn_after_seconds  REAL,
    error_after_seconds REAL,
    PRIMARY KEY (invocation_id, unique_id)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    request_id  TEXT NOT NULL,
    key_name    TEXT NOT NULL,
    role        TEXT,
    method      TEXT NOT NULL,
    path        TEXT NOT NULL,
    query       TEXT,
    status_code INTEGER NOT NULL,
    duration_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
"""


def utcnow_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat()


class Store:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ping(self) -> bool:
        try:
            with self.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ----------------------------------------------------------- ingestão
    def ingest_run(self, run: ParsedRun, now: datetime | None = None) -> bool:
        """True se gravou; False se essa execução já existia (a ingestão é idempotente)."""
        with self.connect() as conn:
            created = (
                conn.execute(
                    "INSERT OR IGNORE INTO invocations VALUES (?, 'run', ?, ?, ?, ?, ?)",
                    (run.invocation_id, run.command, run.dbt_version, run.generated_at, run.elapsed_seconds, utcnow_iso(now)),
                ).rowcount
                == 1
            )
            if created:
                conn.executemany(
                    "INSERT OR IGNORE INTO results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            run.invocation_id,
                            r.unique_id,
                            r.resource_type,
                            r.name,
                            r.status,
                            r.raw_status,
                            r.execution_seconds,
                            r.failures,
                            r.message,
                        )
                        for r in run.results
                    ],
                )
            return created

    def ingest_sources(self, src: ParsedSources, now: datetime | None = None) -> bool:
        with self.connect() as conn:
            created = (
                conn.execute(
                    "INSERT OR IGNORE INTO invocations VALUES (?, 'freshness', ?, ?, ?, ?, ?)",
                    (
                        src.invocation_id,
                        "source freshness",
                        src.dbt_version,
                        src.generated_at,
                        src.elapsed_seconds,
                        utcnow_iso(now),
                    ),
                ).rowcount
                == 1
            )
            if created:
                conn.executemany(
                    "INSERT OR IGNORE INTO source_freshness VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            src.invocation_id,
                            i.unique_id,
                            i.name,
                            i.status,
                            i.raw_status,
                            i.max_loaded_at,
                            i.age_seconds,
                            i.warn_after_seconds,
                            i.error_after_seconds,
                        )
                        for i in src.items
                    ],
                )
            return created

    # ------------------------------------------------------------- leitura
    def latest_invocation(self, kind: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM invocations WHERE kind = ? ORDER BY generated_at DESC, ingested_at DESC LIMIT 1", (kind,)
            ).fetchone()
        return dict(row) if row else None

    def get_invocation(self, invocation_id: str, kind: str | None = None) -> dict[str, Any] | None:
        sql, args = "SELECT * FROM invocations WHERE invocation_id = ?", [invocation_id]
        if kind:
            sql, args = sql + " AND kind = ?", args + [kind]
        with self.connect() as conn:
            row = conn.execute(sql + " ORDER BY ingested_at DESC LIMIT 1", args).fetchone()
        return dict(row) if row else None

    def list_invocations(self, kind: str, limit: int, offset: int = 0) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM invocations WHERE kind = ? ORDER BY generated_at DESC, ingested_at DESC LIMIT ? OFFSET ?",
                (kind, limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]

    def status_counts(self, invocation_id: str, resource_type: str | None = None) -> dict[str, int]:
        sql, args = "SELECT status, COUNT(*) n FROM results WHERE invocation_id = ?", [invocation_id]
        if resource_type:
            sql, args = sql + " AND resource_type = ?", args + [resource_type]
        with self.connect() as conn:
            return {r["status"]: r["n"] for r in conn.execute(sql + " GROUP BY status", args)}

    def results(
        self, invocation_id: str, status: str | None = None, resource_type: str | None = None, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        where, args = "invocation_id = ?", [invocation_id]
        for column, value in (("status", status), ("resource_type", resource_type)):
            if value:
                where, args = where + f" AND {column} = ?", args + [value]
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM results WHERE {where}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM results WHERE {where} ORDER BY CASE status WHEN 'error' THEN 0 WHEN 'warn' THEN 1 "
                f"WHEN 'skipped' THEN 2 ELSE 3 END, name LIMIT ? OFFSET ?",
                args + [limit, offset],
            ).fetchall()
        return {"total": total, "items": [dict(r) for r in rows]}

    def model_history(self, name: str, limit: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT r.*, i.generated_at FROM results r JOIN invocations i "
                "ON i.invocation_id = r.invocation_id AND i.kind = 'run' "
                "WHERE r.name = ? ORDER BY i.generated_at DESC LIMIT ?",
                (name, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def freshness(self, invocation_id: str, status: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM source_freshness WHERE invocation_id = ?", [invocation_id]
        if status:
            sql, args = sql + " AND status = ?", args + [status]
        with self.connect() as conn:
            rows = conn.execute(
                sql + " ORDER BY CASE status WHEN 'error' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, name", args
            ).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- auditoria
    def log_audit(
        self,
        *,
        request_id: str,
        key_name: str,
        role: str | None,
        method: str,
        path: str,
        query: str | None,
        status_code: int,
        duration_ms: float,
        now: datetime | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, request_id, key_name, role, method, path, query, status_code, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    utcnow_iso(now),
                    request_id,
                    key_name,
                    role,
                    method,
                    path[:300],
                    (query or "")[:300] or None,
                    status_code,
                    round(duration_ms, 2),
                ),
            )

    def list_audit(self, limit: int, offset: int = 0, key_name: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM audit_log", []
        if key_name:
            sql, args = sql + " WHERE key_name = ?", [key_name]
        with self.connect() as conn:
            rows = conn.execute(sql + " ORDER BY id DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
        return [dict(r) for r in rows]

    # --------------------------------------------------------------- retenção
    def purge_audit(self, older_than_days: int, now: datetime | None = None) -> int:
        cutoff = utcnow_iso((now or datetime.now(UTC)) - timedelta(days=older_than_days))
        with self.connect() as conn:
            return conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,)).rowcount

    def purge_history(self, older_than_days: int, now: datetime | None = None) -> int:
        cutoff = utcnow_iso((now or datetime.now(UTC)) - timedelta(days=older_than_days))
        with self.connect() as conn:
            ids = [
                (r["invocation_id"], r["kind"])
                for r in conn.execute("SELECT invocation_id, kind FROM invocations WHERE generated_at < ?", (cutoff,))
            ]
            for invocation_id, kind in ids:
                table = "results" if kind == "run" else "source_freshness"
                conn.execute(f"DELETE FROM {table} WHERE invocation_id = ?", (invocation_id,))
                conn.execute("DELETE FROM invocations WHERE invocation_id = ? AND kind = ?", (invocation_id, kind))
            return len(ids)
