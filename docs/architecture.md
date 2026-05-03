# Architecture - Talent / Soldier Selection Engine

## Purpose

Spire Talent Engine is an operational FastAPI service for mission roster
recommendations. Given a mission, roles, and a candidate pool, it returns a
primary roster, second-choice roster, model disagreement, confidence, risk
factors, fairness audit, career forecast, and trace metadata.

The service is advisory. It does not publish orders, slate personnel, or replace
commander and career-manager judgment.

Cross-application data sharing is governed by
`docs/shared-data-contract.md`. Agent outputs must cite the Postgres rows,
pgvector chunks, and FalkorDB facts they used so another app can validate or
replay the decision from canonical IDs.

## Runtime Flow

```text
API client
    |
    | POST /v1/score
    v
FastAPI request validation
    |
    v
candidate pool + role requirements
    |
    +--> TabPFN-compatible deterministic adapter
    |
    +--> hierarchical-pooling deterministic adapter
    |
    v
disagreement-aware blend
    |
    v
Hungarian assignment solver
    |
    +--> primary roster
    +--> second-choice roster
    |
    v
fairness audit + narrative + trace metadata
    |
    v
RosterRecommendation
```

There is no chat interface and no open-ended agent loop. The request and
response are typed Pydantic contracts.

## Package Layout

The active package is `src/system2/`.

| Module | Role |
|---|---|
| `api.py` | FastAPI routes, versioned scoring endpoint, health checks, kill switch endpoints |
| `service.py` | Orchestrates scoring, assignment, fairness, career forecast, trace metadata, and audit logging |
| `models.py` | Pydantic request/response contracts and enums |
| `data.py` | Default role requirements and deterministic synthetic candidate generation |
| `scoring.py` | Role-fit model, TabPFN-compatible estimate, Bayes-compatible estimate, blend, assignment solver |
| `fairness.py` | Counterfactual, proxy-feature, demographic-parity, and equalized-odds proxy audits |
| `narrative.py` | Deterministic explanation and risk-factor generation from fixed scores |
| `career.py` | Five-year forecast for the top selected candidate |
| `calibration.py` | Calibration bins and disagreement histogram trace summaries |
| `audit.py` | Redacted append-only JSONL audit log with hash-chain validation |
| `registry.py` | Model version strings, prompt hash, and DoD AI Ethics mapping |

## API Surface

Canonical endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/healthz` | Liveness and kill-switch state |
| `POST` | `/v1/score` | Mission roster scoring |
| `POST` | `/v1/agent-runs` | Agentic recommendation workflow |
| `GET` | `/v1/agent-runs/{run_id}` | Agent run lookup |
| `POST` | `/v1/agent-runs/{run_id}/approval` | Human approval or rejection |
| `POST` | `/v1/context/chunks` | Retrieval context ingestion |
| `POST` | `/v1/graph/facts` | Graph fact ingestion |
| `POST` | `/admin/disable` | Disable scoring |
| `POST` | `/admin/enable` | Re-enable scoring after an authorized operational action |

Compatibility aliases:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Legacy health alias |
| `POST` | `/score` | Legacy scoring alias |

Admin routes are not self-authenticating in this package; an operational
deployment must protect them at the gateway, service mesh, or ingress layer.

## Contracts

Inbound request contracts reject unknown fields with `extra="forbid"`.

Core contracts:

- `ScoreRequest`
- `Soldier`
- `RoleRequirement`
- `RosterRecommendation`
- `CandidateAssessment`
- `RiskFactor`
- `FairnessAudit`
- `CareerForecast`
- `TraceMetadata`

Every candidate assessment includes:

- blended `fit_score`
- `p_success_tabpfn`
- `p_success_bayes_mean`
- Bayesian credible interval
- `model_disagreement`
- `confidence`
- risk factors
- narrative
- second-choice ID for primary roster entries

## Scoring

`scoring.py` implements local deterministic model adapters that preserve the
operational contract without requiring GPU weights, MCMC sampling, or external
LLM calls at runtime.

The role-fit model excludes protected attributes. It uses readiness, fitness,
mission experience, simulation score, milestones, and role-specific
competencies. Hard constraints such as required MOS and minimum ACFT are
enforced in the assignment cost matrix rather than narrative code.

The blend treats disagreement as an uncertainty signal:

| Disagreement | Confidence | Behavior |
|---|---|---|
| `< 0.10` | high | Average TabPFN-compatible and Bayes-compatible estimates |
| `0.10-0.25` | medium | Weighted blend with disagreement risk surfaced |
| `> 0.25` | low | Demote blended score and add model-disagreement risk |

## Agent Workflow

The agent workflow is orchestration, not model training. It records typed steps
for request context, pgvector retrieval readiness, FalkorDB graph readiness, the
deterministic roster recommendation, and human approval. The roster tool remains
the source of recommendation data; the agent only sequences tools and preserves
an inspectable run record.

Approval is explicit. Runs that require human approval stop at
`awaiting_approval`; `POST /v1/agent-runs/{run_id}/approval` moves them to
`completed` or `rejected` and records approver ID, rationale, decision, and a
timestamp in the run payload.

Runtime backend selection is environment-driven:

| Backend | Env | Operational adapter | Local fallback |
|---|---|---|---|
| Audit log | `AUDIT_BACKEND=postgres` | Postgres hash-chain audit table | JSONL file audit |
| Agent runs | `AGENT_REPOSITORY_BACKEND=postgres` | Postgres JSONB repository | In-memory repository |
| Agent state | `AGENT_STATE_BACKEND=redis` | Redis status and locks | In-memory state |
| Retrieval | `RETRIEVAL_BACKEND=pgvector` | Postgres/pgvector context chunks | Packaged local context |
| Graph | `GRAPH_BACKEND=falkordb` | FalkorDB graph queries | Request-local graph facts |

Postgres remains canonical for durable agent runs and audit records. Redis is
only ephemeral coordination state. FalkorDB graph facts should be rebuildable
from canonical records.

Context ingestion accepts text chunks and optional precomputed embedding
vectors. This service stores and retrieves those chunks; it does not generate
embeddings. Graph ingestion accepts derived `(subject, predicate, object)` facts
for relationship lookup. Both ingestion routes must be protected by the same
external auth boundary as admin routes.

## Assignment

Assignments use `scipy.optimize.linear_sum_assignment`.

The cost matrix is:

```text
C[i, j] = -log(p_blended(soldier_i, role_j))
```

Disqualified `(soldier, role)` pairs receive a large penalty. The second-choice
roster is solved by blocking the primary chosen pairs and solving again.

## Fairness

Protected attributes are excluded from scoring, assignment, and feature hashes.
The fairness module may read them only for measurement.

Returned fairness outputs:

- counterfactual protected-attribute flip violation rate
- proxy-feature audit
- demographic-parity delta
- equalized-odds proxy delta
- pass/halt status
- operator notes

The current counterfactual audit asserts invariance because the scoring path
does not read protected attributes. Proxy features are surfaced for review and
reweighting before live policy use.

## Traceability

Every response includes:

- model version registry
- protected-attribute-excluding feature hash
- prompt hash
- seed
- generated timestamp
- DoD AI Ethics mapping
- calibration bins
- disagreement histogram

Audit logging is hash-chained. The file backend writes redacted JSONL records.
The Postgres backend writes the same canonical record into `system2_audit_log`.
Both remove protected attributes and hash clear unit/MOS values before
persistence.

## Cross-System Boundaries

System 1 can supply longitudinal training features through a read model.
System 3 can receive finalized assignments and deployment outcomes. This service
should still score a request when those systems are unavailable, as long as the
request contains the candidate and role data needed for scoring.
