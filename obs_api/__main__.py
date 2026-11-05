"""CLI: gerar chave, ingerir artefatos de um diretório, aplicar retenção.

python -m obs_api new-key --name grafana --role viewer
python -m obs_api ingest --dir target/
python -m obs_api purge
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import ROLES, Settings
from .ingest import ArtifactError, parse_run_results, parse_sources, read_json_file
from .security import generate_key, hash_key
from .store import Store


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="obs_api", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    key = sub.add_parser("new-key", help="gera uma chave de API (mostrada UMA vez) e o trecho de configuração com o hash")
    key.add_argument("--name", required=True)
    key.add_argument("--role", required=True, choices=ROLES)
    ingest = sub.add_parser("ingest", help="ingere run_results.json e sources.json de um diretório")
    ingest.add_argument("--dir", required=True)
    sub.add_parser("purge", help="aplica a retenção da auditoria e do histórico")
    args = parser.parse_args(argv)

    if args.command == "new-key":
        plain = generate_key()
        snippet = {args.name: {"sha256": hash_key(plain), "role": args.role}}
        print(f"CHAVE (guarde agora, não será mostrada de novo):\n  {plain}\n")
        print("Trecho para API_KEYS (só o hash vai para a configuração):")
        print(json.dumps(snippet))
        return 0

    settings = Settings.from_env(require_keys=False)
    store = Store(settings.db_path)

    if args.command == "purge":
        print(
            f"auditoria apagada: {store.purge_audit(settings.audit_retention_days)} | "
            f"histórico apagado: {store.purge_history(settings.history_retention_days)}"
        )
        return 0

    directory, found = Path(args.dir), 0
    try:
        for filename, parse, ingest_fn in (
            ("run_results.json", lambda d: parse_run_results(d, settings.max_message_chars), store.ingest_run),
            ("sources.json", parse_sources, store.ingest_sources),
        ):
            path = directory / filename
            if path.exists():
                found += 1
                parsed = parse(read_json_file(path, settings.max_body_bytes))
                print(f"{filename}: {'ingerido' if ingest_fn(parsed) else 'já existia (ignorado)'}")
    except ArtifactError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1
    if not found:
        print(f"nenhum run_results.json nem sources.json em {directory}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
