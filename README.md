# System 2 - Cognitive Mission Adaptation Engine

FastAPI service for turning live training evidence into cognitive state
estimates and instructor-approved scenario adaptations. The roster and career
scoring path still exists, but it is now the downstream talent lane; the primary
loop is observe, estimate, recommend, approve, inject, and learn.

The service is operationally shaped and dependency-light. The adaptation layer
uses deterministic evidence fusion, cognitive-state estimation, scenario
direction, and safety/doctrine auditing behind stable contracts. The scoring
layer uses deterministic TabPFN-compatible and hierarchical-Bayes-compatible
adapters, so heavier models can replace either path without changing API
behavior.

For integration with the other Spire applications, share
`docs/shared-data-contract.md`. It defines canonical IDs, shared Postgres,
pgvector, Redis, and FalkorDB usage, source references for agent outputs, and
append-only update storage for drift detection.

Frontend integration details live in `docs/frontend-integration.md`, including
request/response examples, UI mapping, approval flows, and known backend gaps.

## Supporting Infrastructure

The main application does not need to run in Kubernetes. Kubernetes can host the
supporting stateful infrastructure that the application connects to.

Before running the application in an operational environment, deploy:

- Postgres with the `pgvector` extension enabled. This is the durable source of
  truth for missions, candidates, training evidence, state snapshots, scenario
  recommendations, agent runs, approvals, audit metadata, and retrieval
  embeddings.
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
PGVECTOR_CONNECTION_STRING=postgresql+psycopg://app_user:password@pgbouncer.internal:6432/system2
REDIS_URL=redis://:password@redis.internal:6379/0
FALKORDB_URL=redis://:password@falkordb.internal:6379
ADAPTATION_REPOSITORY_BACKEND=postgres
AUDIT_BACKEND=postgres
AGENT_REPOSITORY_BACKEND=postgres
AGENT_STATE_BACKEND=redis
CANDIDATE_POOL_BACKEND=postgres
RETRIEVAL_BACKEND=pgvector
GRAPH_BACKEND=falkordb
SHARED_DATA_BACKEND=postgres
SYSTEM2_AUDIT_LOG=/var/log/system2/audit.jsonl
```

Postgres should remain the canonical store. Redis should be treated as
disposable cache/coordination state. FalkorDB should hold derived graph state
that can be rebuilt from canonical Postgres records.

Real connection files should stay out of git. Put the generated infra env file
in an ignored path such as `.env.infra` and point the app at it:

```bash
SYSTEM2_ENV_FILE=.env.infra uvicorn system2.api:app --reload
```

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn system2.api:app --reload
```

Open `http://127.0.0.1:8000/docs`.

When running against deployed infrastructure, install the optional infra
dependencies:

```bash
pip install -e ".[dev,infra]"
```

With the Postgres-backed operational adapters enabled, the app connects to
`DATABASE_URL` through PgBouncer and initializes the adaptation, agent-run,
audit, candidate-pool projection, shared update ledger, decision snapshot, and
pgvector context tables at startup when those backends are enabled. Keep
`ADAPTATION_REPOSITORY_BACKEND=memory`, `AGENT_REPOSITORY_BACKEND=memory`,
`AUDIT_BACKEND=file`, `CANDIDATE_POOL_BACKEND=local`,
`RETRIEVAL_BACKEND=local`, and `SHARED_DATA_BACKEND=memory` for local runs
without Postgres.

Before starting against the infrastructure, smoke-test the generated env file:

```bash
python scripts/smoke_infra.py --env-file .env.infra --migrate
```

## Endpoints

- `POST /v1/score` - ranks candidates into a 14-slot direct-action roster by default.
- `POST /v1/adaptations` - estimates cognitive/team state from field evidence and recommends scenario injects.
- `GET /v1/adaptations/{adaptation_id}` - fetches a stored adaptation.
- `GET /v1/missions/{mission_id}/adaptations` - lists stored adaptations for a mission.
- `POST /v1/adaptations/{adaptation_id}/approval` - records instructor approval or rejection for a proposed inject.
- `POST /v1/agent-runs` - runs the agentic recommendation workflow and returns an approval-ready run.
- `GET /v1/agent-runs/{run_id}` - fetches an agent run by ID.
- `POST /v1/agent-runs/{run_id}/approval` - records an authorized approve/reject decision.
- `POST /v1/context/chunks` - ingests externally embedded context chunks for pgvector retrieval.
- `POST /v1/graph/facts` - ingests derived relationship facts for FalkorDB graph context.
- `GET /v1/healthz` - service health and kill-switch state.
- `POST /admin/disable` - disables scoring.
- `POST /admin/enable` - re-enables scoring after an authorized operational action.

`/score` and `/health` remain as compatibility aliases.

## Adaptation Example

```bash
curl -X POST http://127.0.0.1:8000/v1/adaptations \
  -H 'content-type: application/json' \
  -d '{
    "mission_id": "raid-tonight",
    "instructor_id": "instructor-1",
    "team_id": "alpha",
    "target_soldier_ids": ["RGR-0001"],
    "evidence": [
      {
        "evidence_id": "obs-001",
        "source_type": "voice_note",
        "text": "Soldier handled direct contact, but missed the second-order relationship between terrain, timing, support, civilian movement, and a delayed comms relay under moderate fatigue.",
        "soldier_ids": ["RGR-0001"],
        "tags": ["systems_thinking", "fatigue"],
        "metrics": {"sleep_hours": 4.5}
      }
    ]
  }'
```

The response includes a cognitive state snapshot, the primary developmental
dimension, three scenario recommendations when safety permits, blocked options,
source references, confidence, risk, and doctrine rationale. Recommendations
remain advisory until an instructor approves or rejects one.

```bash
curl -X POST http://127.0.0.1:8000/v1/adaptations/ADAPTATION_ID/approval \
  -H 'content-type: application/json' \
  -d '{
    "recommendation_id": "RECOMMENDATION_ID",
    "decision": "approved",
    "approver_id": "instructor-1",
    "rationale": "Targets systems thinking without increasing general difficulty."
  }'
```

## Roster Example

```bash
curl -X POST http://127.0.0.1:8000/v1/score \
  -H 'content-type: application/json' \
  -d '{"mission_id":"raid-tonight","candidate_count":80}'
```

The response includes assigned slots, second choices, TabPFN/Bayes probabilities, confidence, risk factors, fairness audit metrics, trace metadata, and one five-year career forecast.

Operational callers can send IDs instead of full candidate payloads once
`candidate_pools_current`, `soldiers_current`, and `role_slots_current` are
populated:

```bash
curl -X POST http://127.0.0.1:8000/v1/score \
  -H 'content-type: application/json' \
  -d '{"mission_id":"raid-tonight","candidate_pool_id":"pool-2026-05-02-a"}'
```

With `CANDIDATE_POOL_BACKEND=postgres`, a missing pool is a request error. With
`CANDIDATE_POOL_BACKEND=local`, the service can still fall back to deterministic
synthetic candidates and marks that fallback in `trace.source_refs`.

## Agentic Workflow

```bash
curl -X POST http://127.0.0.1:8000/v1/agent-runs \
  -H 'content-type: application/json' \
  -d '{
    "score_request": {
      "mission_id": "raid-tonight",
      "candidate_count": 80,
      "seed": 42
    },
    "require_human_approval": true
  }'
```

The agent workflow records request context, retrieval evidence, graph evidence,
the deterministic roster recommendation, and the human-approval gate. It writes
a `decision_snapshots` row for drift comparison when Postgres shared-data
persistence is enabled. It does not train a model and does not make the final
personnel decision.

Record the human decision after review:

```bash
curl -X POST http://127.0.0.1:8000/v1/agent-runs/RUN_ID/approval \
  -H 'content-type: application/json' \
  -d '{
    "decision": "approved",
    "approver_id": "commander-1",
    "rationale": "Reviewed recommendation, fairness audit, and second choices."
  }'
```

Approval and rejection decisions append an `entity_update_events` row. Context
chunk ingestion and graph fact ingestion also append update events so Systems 1
and 3 can track exactly what System 2 changed.

Load retrieval context with embeddings generated outside this service:

```bash
curl -X POST http://127.0.0.1:8000/v1/context/chunks \
  -H 'content-type: application/json' \
  -d '{
    "chunks": [{
      "chunk_id": "sop-001",
      "source": "unit-sop",
      "title": "Roster approval",
      "content": "Mission roster recommendations require authorized review.",
      "metadata": {"kind": "sop"},
      "embedding": null
    }]
  }'
```

Load derived graph facts:

```bash
curl -X POST http://127.0.0.1:8000/v1/graph/facts \
  -H 'content-type: application/json' \
  -d '{
    "facts": [{
      "subject": "raid-tonight",
      "predicate": "requires_role",
      "object": "medic",
      "metadata": {"slot_id": "MED-1"}
    }]
  }'
```

The agent stack uses these adapters:

- `PostgresAgentRunRepository` for durable agent runs.
- `PostgresAuditLog` for durable hash-chained audit records.
- `PostgresCandidatePoolResolver` for ID-only candidate and role-slot
  resolution from shared projections.
- `RedisAgentStateStore` for ephemeral run status and distributed locks.
- `PgVectorContextRetriever` for retrieval over externally embedded context
  chunks.
- `FalkorDBGraphContextProvider` for mission, role, skill, unit, policy, and
  assignment graph facts.
- `PostgresSharedDataSink` for shared `entity_update_events` and
  `decision_snapshots` records.

All infra adapters are selected by env. Local fallbacks remain available for
development and tests.
