# Pipeline Observability API

API (FastAPI) que lê os artefatos do **dbt** (`run_results.json` e `sources.json`) e responde a pergunta do plantão: *"os dados de hoje estão confiáveis?"* — um semáforo (`healthy` / `degraded` / `failing`) que **explica o motivo**, histórico por modelo, frescor das fontes e trilha de auditoria. Pensada para sustentação de dados, com **LGPD por desenho**.

## O que ela faz
- `POST /v1/ingest/run-results` e `/v1/ingest/sources` — ingestão **idempotente e atômica** (reenviar o mesmo artefato não duplica).
- `GET /v1/overview` — semáforo + motivos (modelos com erro/pulados, testes falhando, fontes vencidas, última execução velha).
- `GET /v1/runs`, `/v1/runs/latest`, `/v1/runs/{id}`, `/v1/models/{nome}/history`, `/v1/freshness`.
- `GET /v1/audit` (admin) e `POST /v1/retention/purge` (admin).
- `GET /health` (liveness) e `/ready` (readiness: o banco responde?) — públicas e fora da auditoria.

## Decisões de segurança e LGPD
| Decisão | Por quê |
|---|---|
| Parser com **lista de campos permitidos**; nunca lê `compiled_code`, `args`, `adapter_response` | O `run_results.json` real traz SQL compilado e caminhos da máquina. O que não é lido não vaza. |
| Mensagens passam por **redação de PII** (CNPJ, CPF, e-mail, telefone, caminhos) **antes** de truncar | Truncar antes poderia cortar um CPF ao meio e escapar da regex. |
| Chaves de API guardadas só como **SHA-256**, comparação com `hmac.compare_digest` | Vazamento da configuração não revela chave utilizável. |
| Papéis `viewer` / `admin`, **rate limit** por chave (janela deslizante) | Menor privilégio e proteção contra abuso. |
| Auditoria registra quem/o quê/quando, **sem corpo e sem IP** | Minimização: auditar sem criar novo dado pessoal. |
| Retenção configurável (auditoria 90 d, histórico 180 d) + CronJob | Armazenamento limitado a uma finalidade. |

## Rodando
```bash
pip install -r requirements.txt
python -m obs_api new-key --name grafana --role viewer      # mostra a chave UMA vez + o hash
export API_KEYS='{"grafana": {"sha256": "<hash>", "role": "viewer"}}'
uvicorn --factory obs_api.main:create_app --port 8000
python -m obs_api ingest --dir target/                      # ingere direto de um diretório do dbt
```
Variáveis: `API_KEYS`, `DB_PATH`, `AUDIT_RETENTION_DAYS`, `HISTORY_RETENTION_DAYS`, `MAX_MESSAGE_CHARS`, `RATE_LIMIT_PER_MINUTE`, `STALE_AFTER_HOURS`, `ENABLE_DOCS`.

## Kubernetes (`deploy/k8s`)
`kubectl apply -k deploy/k8s` + criar o Secret `obs-api-keys` fora do Git (ver `secret.example.yaml`).
Deployment não-root, FS somente leitura, `drop ALL`, seccomp, probes, NetworkPolicy (entrada só de namespaces com `access=obs-api`, saída só DNS) e CronJob de retenção.

**Limitação assumida:** SQLite em volume `ReadWriteOnce` ⇒ **1 réplica, estratégia `Recreate`, sem HPA**. Escalar horizontalmente exige trocar o `store.py` por Postgres.

## Testes
```bash
pip install -r requirements-dev.txt
pytest -q          # 153 testes
ruff check . && ruff format --check .
kubernetes-validate -k 1.34 --strict deploy/k8s/*.yaml
```
As fixtures são **artefatos reais do dbt** (dbt-duckdb) gerados por `tools/demo_dbt_project` — um mini-projeto com um caso de cada desfecho (modelo ok, com erro, pulado; teste passando, falhando e em `warn`; fonte em dia, em alerta e vencida): `PASS=3 WARN=1 ERROR=3 SKIP=3`. Regenerar: `python tools/make_fixtures.py` (ver o docstring).

### Validado
- 153 testes (unidade, API, LGPD — incluindo prova de que `compiled_code` e PII crua não saem pela API —, contrato manifesto × código).
- Servidor real com `uvicorn --factory`: `/health` e `/ready` 200, rota protegida sem chave 401.
- Manifestos validados contra os schemas do Kubernetes 1.30 e 1.34 (`--strict`), com controle negativo (valor inválido é rejeitado).
- `ruff` limpo.

### Não validado (falta ambiente)
- `docker build` e a imagem rodando (o job `image` do CI faz isso, mas não foi executado).
- `kubectl apply` em cluster real, NetworkPolicy efetiva (depende do CNI) e o CronJob de purge compartilhando o PVC.
- Integração com um Grafana/Slack reais.
- O workflow do GitHub Actions só teve a sintaxe YAML conferida.
