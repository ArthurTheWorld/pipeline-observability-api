"""Gera tests/fixtures/ a partir dos artefatos REAIS do dbt do mini-projeto de demonstração.

    cd tools/demo_dbt_project && DBT_PROFILES_DIR=. python seed_demo.py
    DBT_PROFILES_DIR=. dbt build; DBT_PROFILES_DIR=. dbt source freshness   # (ambos terminam com erro: é proposital)
    cd ../.. && python tools/make_fixtures.py

O mini-projeto produz todos os desfechos: modelo OK, modelo com erro, modelo pulado, teste que passa,
teste que falha, teste em `warn`, e fontes em dia, em alerta e vencida.

Sanitização: o `run_results.json` real traz caminhos da máquina (args.project_dir, profiles_dir) e
variáveis de ambiente. Isso não vai para o repositório. O `compiled_code` (o SQL de cada modelo)
PERMANECE de propósito: os testes provam que a API nunca o lê nem o devolve.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "tools" / "demo_dbt_project" / "target"
OUT = ROOT / "tests" / "fixtures"


def sanitize(doc: dict) -> dict:
    for key in list(doc.get("args", {})):
        value = doc["args"][key]
        if isinstance(value, str) and re.search(r"(/[\w.\-]+){2,}", value):
            doc["args"][key] = "/REDACTED/" + key
    doc.get("metadata", {}).pop("env", None)
    doc.get("metadata", {}).setdefault("env", {})
    return doc


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name in ("run_results.json", "sources.json"):
        source = TARGET / name
        if not source.exists():
            sys.exit(f"{source} não existe: rode o dbt no mini-projeto primeiro (ver o docstring)")
        doc = sanitize(json.loads(source.read_text(encoding="utf-8")))
        (OUT / name).write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"{name}: {len(doc['results'])} resultados -> tests/fixtures/{name}")
    leaked = [
        p for p in OUT.glob("*.json") if "/home/" in p.read_text() or "/Users/" in p.read_text() or "/tmp/" in p.read_text()
    ]
    if leaked:
        sys.exit(f"caminhos locais ainda presentes em {[p.name for p in leaked]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
