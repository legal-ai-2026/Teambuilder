# System 2 - Talent / Soldier Selection Engine

FastAPI service for ranking soldiers into mission rosters with model disagreement, fairness checks, second choices, career forecasts, trace metadata, and an operator kill switch surfaced in the API.

The service is operationally shaped and dependency-light. The scoring layer uses deterministic TabPFN-compatible and hierarchical-Bayes-compatible adapters behind stable contracts, so heavier model implementations can replace them without changing API behavior.

## Supporting Infrastructure

The main application does not need to run in Kubernetes. Kubernetes can host the
supporting stateful infrastructure that the application connects to.

Before running the application in an operational environment, deploy:

- Postgres with the `pgvector` extension enabled. This is the durable source of
  truth for missions, candidates, agent runs, recommendations, approvals,
  audit metadata, and retrieval embeddings.
- PgBouncer in front of Postgres. This should be the application-facing
  Postgres endpoint.
- Redis for short-lived agent state, cache, locks, rate limits, and transient
  queue state.
- FalkorDB for graph relationships such as soldier-skill, mission-role,
  unit-history, qualification-policy, and prior-assignment links.
- Backup storage and scheduled Postgres backups.
- Monitoring for Postgres, Redis, and FalkorDB.
- Secret management for database, cache, graph, and agent credentials.
- Private networking, VPN, or private load balancers. Do not publicly expose
  Postgres, Redis, or FalkorDB.

The application should receive these connections through environment variables:

```env
DATABASE_URL=postgresql://app_user:password@pgbouncer.internal:6432/system2
REDIS_URL=redis://:password@redis.internal:6379/0
FALKORDB_URL=redis://:password@falkordb.internal:6379
PGVECTOR_ENABLED=true
SYSTEM2_AUDIT_LOG=/var/log/system2/audit.jsonl
```

Postgres should remain the canonical store. Redis should be treated as
disposable cache/coordination state. FalkorDB should hold derived graph state
that can be rebuilt from canonical Postgres records.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn system2.api:app --reload
```

Open `http://127.0.0.1:8000/docs`.

## Endpoints

- `POST /v1/score` - ranks candidates into a 14-slot direct-action roster by default.
- `GET /v1/healthz` - service health and kill-switch state.
- `POST /admin/disable` - disables scoring.
- `POST /admin/enable` - re-enables scoring after an authorized operational action.

`/score` and `/health` remain as compatibility aliases.

## Example

```bash
curl -X POST http://127.0.0.1:8000/v1/score \
  -H 'content-type: application/json' \
  -d '{"mission_id":"raid-tonight","candidate_count":80}'
```

The response includes assigned slots, second choices, TabPFN/Bayes probabilities, confidence, risk factors, fairness audit metrics, trace metadata, and one five-year career forecast.
