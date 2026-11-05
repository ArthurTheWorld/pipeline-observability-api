"""Construtores de cenários a partir dos artefatos REAIS do dbt (tests/fixtures)."""

import copy
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fresh_id(doc):
    doc = copy.deepcopy(doc)
    doc["metadata"]["invocation_id"] = str(uuid.uuid4())
    return doc


def healthy_run(doc=None):
    doc = fresh_id(doc or load("run_results.json"))
    for r in doc["results"]:
        r["status"] = "pass" if r["unique_id"].startswith("test.") else "success"
        r["failures"] = 0 if r["unique_id"].startswith("test.") else None
        r["message"] = "OK" if r["status"] == "success" else None
    return doc


def healthy_sources(doc=None):
    doc = fresh_id(doc or load("sources.json"))
    for r in doc["results"]:
        r["status"] = "pass"
    return doc


def set_status(doc, name_part, status, message=None):
    doc = fresh_id(doc)
    for r in doc["results"]:
        if name_part in r["unique_id"]:
            r["status"] = status
            if message is not None:
                r["message"] = message
    return doc


def generated_at(doc):
    return datetime.fromisoformat(doc["metadata"]["generated_at"].replace("Z", "+00:00"))


def now_after(doc, hours=1):
    return generated_at(doc) + timedelta(hours=hours)
