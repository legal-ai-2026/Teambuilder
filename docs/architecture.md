# Architecture - Cognitive Mission Adaptation Engine

## Purpose

System 2 is an operational FastAPI service for developmental training
adaptation. Given live field evidence, it estimates cognitive and team state,
recommends instructor-approved scenario injects, and records traceable
provenance for later AAR and drift review.

The existing roster scorer remains available as a downstream talent lane. Given
a mission, roles, and a candidate pool, it returns a primary roster,
second-choice roster, model disagreement, confidence, risk factors, fairness
audit, career forecast, and trace metadata.

The service is advisory. It does not publish orders, slate personnel, or replace
commander and career-manager judgment.

Cross-application data sharing is governed by
`docs/shared-data-contract.md`. Agent outputs must cite the Postgres rows,
pgvector chunks, and FalkorDB facts they used so another app can validate or
replay the decision from canonical IDs.

## Adaptation Runtime Flow

```text
Frontend JSON / field evidence
    |
    | POST /v1/adaptations
    v
Evidence normalization
    |
    v
Cognitive state estimator
    |
    v
Scenario director
    |
    v
Safety/doctrine auditor
    |
    +--> blocked recommendations
    +--> approval-ready recommendations
    |
    v
Decision-quality, utility, and reliance assessment
    |
    v
Instructor approve / reject
    |
    v
entity_update_events + AAR/lessons lane
```

The live adaptation loop is advisory. It does not autonomously push new
scenario injects.

## Operational Twin Agent Loop

```text
Field artifacts and observations
    |
    | POST /v1/operational-twin/runs
    v
Perception normalizer
    |
    v
Artifact + Observation records
    |
    v
Latent state estimator
    |
    v
Evidence bundle with model, policy, and hash-chain metadata
    |
    v
Scenario director drafts exactly three options
    |
    v
Critic marks pass / modify / escalate / reject
    |
    v
Decision-quality, utility, and reliance assessment
    |
    v
Human approve / reject / escalate decision
    |
    v
Decision + lesson learned + outcome capture + entity_update_events
```

This loop is the Foundry-style operational twin slice. Raw evidence, inferred
state, draft options, and human decisions are separate objects. The service
does not auto-approve scenario options or mission COAs. Outcomes are stored for
evaluation and calibration readiness, not automatic model learning.

The loop can run as a complete agentic system. With
`SYSTEM2_AGENTIC_PROVIDER=auto` or `openai` and `OPENAI_API_KEY` configured,
the backend invokes four JSON-producing agents:

| Agent stage | Responsibility | Hard boundary |
|---|---|---|
| Perception | Extract source-linked atomic observations from artifacts | No motive, protected-attribute, or personnel judgment inference |
| State | Estimate provisional fatigue, clarity, cohesion, decision, tempo, and challenge-gap state | State is an estimate with uncertainty, not an asserted fact |
| Scenario | Draft exactly three distinct options | Draft only; no autonomous approval |
| Critic | Review grounding, safety, diversity, fatigue overload, and governance | Deterministic safety gates cannot be weakened |

When OpenAI is unavailable or disabled, the same API keeps using the
deterministic runtime. Each run includes `agent_trace` entries that identify
the provider, model, stage status, input/output hashes, duration, and fallback
reason when one occurred.

## Roster Runtime Flow

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
decision-quality, utility, and reliance assessment
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
| `adaptation_store.py` | In-memory and Postgres repositories for adaptation lookup and mission history |
| `cognitive.py` | Evidence fusion, cognitive state estimation, scenario direction, safety/doctrine gating, and adaptation approval |
| `operational_twin.py` | Backend operational twin loop for multimodal evidence, state estimates, governed options, decisions, and lessons |
| `llm.py` | JSON LLM client boundary for OpenAI-backed agent stages without requiring an SDK dependency |
| `decision_quality.py` | Deterministic decision-quality, value-of-information, utility, and reliance guidance for recommendation responses |
| `service.py` | Orchestrates scoring, assignment, fairness, career forecast, trace metadata, and audit logging |
| `candidate_pool.py` | Resolves `candidate_pool_id` into canonical soldiers, role slots, and source references |
| `models.py` | Pydantic request/response contracts and enums |
| `data.py` | Default role requirements and deterministic synthetic candidate generation |
| `scoring.py` | Role-fit model, TabPFN-compatible estimate, Bayes-compatible estimate, blend, assignment solver |
| `fairness.py` | Counterfactual, proxy-feature, demographic-parity, and equalized-odds proxy audits |
| `narrative.py` | Deterministic explanation and risk-factor generation from fixed scores |
| `career.py` | Five-year forecast for the top selected candidate |
| `calibration.py` | Calibration bins and disagreement histogram trace summaries |
| `audit.py` | Redacted append-only JSONL audit log with hash-chain validation |
| `shared_data.py` | Shared update ledger, decision snapshot, source-reference, and Postgres sink helpers |
| `registry.py` | Model version strings, prompt hash, and DoD AI Ethics mapping |

## API Surface

Canonical endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/healthz` | Liveness and kill-switch state |
| `POST` | `/v1/adaptations` | Cognitive state estimation and scenario-inject recommendation |
| `POST` | `/v1/adaptations/{adaptation_id}/approval` | Instructor approval or rejection for a scenario inject |
| `POST` | `/v1/operational-twin/runs` | Multimodal operational twin run with evidence bundle and draft options |
| `GET` | `/v1/operational-twin/runs/{twin_run_id}` | Operational twin run lookup |
| `POST` | `/v1/operational-twin/runs/{twin_run_id}/options/{scenario_option_id}/decision` | Human approve/reject/escalate decision |
| `POST` | `/v1/operational-twin/runs/{twin_run_id}/outcome` | Outcome, rating, safety incident flag, and AAR capture |
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

- `CognitiveAdaptationRequest`
- `TrainingEvidence`
- `CognitiveStateSnapshot`
- `ScenarioInjectRecommendation`
- `CognitiveAdaptationResponse`
- `ScenarioApprovalRequest`
- `OperationalTwinRequest`
- `ArtifactInput`
- `ObservationInput`
- `StateEstimate`
- `EvidenceBundle`
- `ScenarioOption`
- `ScenarioOptionDecisionRequest`
- `LessonLearned`
- `ScoreRequest`
- `Soldier`
- `RoleRequirement`
- `RosterRecommendation`
- `CandidateAssessment`
- `RiskFactor`
- `FairnessAudit`
- `CareerForecast`
- `TraceMetadata`
- `DecisionContext`
- `DecisionQualityAssessment`
- `DecisionUtilityEstimate`
- `RelianceGuidance`

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

Every roster, adaptation, operational twin, scenario option, and agent-run
response includes decision-quality fields. These fields estimate framing
completeness, evidence sufficiency, uncertainty, reversibility, value of
information, expected utility, and recommended human reliance posture. They are
not approval decisions and do not change scoring or assignment order.

Every adaptation response includes:

- state estimates for sensemaking, critical thinking, systems thinking,
  leadership communication, execution reliability, cognitive load,
  sleep/fatigue, nutrition strain, and team trust
- the primary developmental dimension and likely failure mode
- candidate scenario injects with expected developmental effect, risk,
  confidence, safety checks, and doctrine rationale
- blocked recommendations when the safety/doctrine auditor rejects an option
- decision-quality, utility, and reliance guidance for the response and each
  scenario recommendation
- trace metadata and source hashes for replay

## Cognitive Adaptation

`cognitive.py` implements the hackathon-ready vertical slice of the
developmental loop. It accepts normalized `TrainingEvidence` from voice notes,
transcripts, OCR text, checklists, patrol summaries, AARs, weather, terrain, or
structured mission events. The estimator updates a multidimensional learner and
team state over Army-relevant dimensions:

- `sensemaking`
- `critical_thinking`
- `systems_thinking`
- `leadership_communication`
- `execution_reliability`
- `cognitive_load`
- `sleep_fatigue`
- `nutrition_strain`
- `team_trust`

The scenario director produces up to three options for the current weakest
skill dimension: a direct pressure inject, a lower-noise skill-isolation
repetition, and a transfer test in a different tactical context. The
safety/doctrine auditor blocks options that exceed `max_safety_risk`, violate
blocked inject types, or conflict with configured environmental-stress limits.
The service writes a hash-chained audit record and an `entity_update_events`
row for each generated adaptation and approval decision.

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
| Adaptations | `ADAPTATION_REPOSITORY_BACKEND=postgres` | Postgres adaptation repository | In-memory repository |
| Audit log | `AUDIT_BACKEND=postgres` | Postgres hash-chain audit table | JSONL file audit |
| Agent runs | `AGENT_REPOSITORY_BACKEND=postgres` | Postgres JSONB repository | In-memory repository |
| Agent state | `AGENT_STATE_BACKEND=redis` | Redis status and locks | In-memory state |
| Candidate pools | `CANDIDATE_POOL_BACKEND=postgres` | Postgres candidate and role projections | Synthetic/local resolver |
| Retrieval | `RETRIEVAL_BACKEND=pgvector` | Postgres/pgvector context chunks | Packaged local context |
| Graph | `GRAPH_BACKEND=falkordb` | FalkorDB graph queries | Request-local graph facts |
| Shared data | `SHARED_DATA_BACKEND=postgres` | Postgres update events and decision snapshots | In-memory sink |

Postgres remains canonical for durable agent runs and audit records. Redis is
only ephemeral coordination state. FalkorDB graph facts should be rebuildable
from canonical records.

Context ingestion accepts text chunks and optional precomputed embedding
vectors. This service stores and retrieves those chunks; it does not generate
embeddings. Graph ingestion accepts derived `(subject, predicate, object)` facts
for relationship lookup. Both ingestion routes append shared update events and
must be protected by the same external auth boundary as admin routes.

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
- source references for mission, candidate, role, retrieval, and graph inputs
- input source hashes keyed by source reference

When `candidate_pool_id` is provided, the Postgres resolver reads
`candidate_pools_current`, `soldiers_current`, `role_slots_current`,
`training_observations_current`, and `deployment_outcomes_current`. Training
and outcome projections enrich the in-memory `Soldier` feature payload before
scoring, while source refs preserve the exact projection hashes. A resolved
pool replaces synthetic fallback references with canonical Postgres source refs
and hashes. In Postgres mode, missing pools fail the request instead of silently
scoring generated candidates.

Direct `/v1/score` calls now also attach retrieval and graph source refs from
the configured pgvector and FalkorDB adapters. Agent runs attach those refs
through their context steps. Both paths convert supported context into bounded
scoring adjustments, currently graph `requires_skill` relationships and
fatigue/safety retrieval context, and record those transforms in
`trace.context_adjustments`.

Audit logging is hash-chained. The file backend writes redacted JSONL records.
The Postgres backend writes the same canonical record into `system2_audit_log`.
Both remove protected attributes and hash clear unit/MOS values before
persistence.

Direct scores and agent runs write `decision_snapshots` for drift checks when
shared-data persistence is enabled. Human approval or rejection, context
ingestion, graph fact ingestion, and kill-switch changes append
`entity_update_events` so Systems 1 and 3 can consume System 2 changes without
scraping mutable run payloads.

## Cross-System Boundaries

System 1 can supply longitudinal training features through canonical Postgres
projections, pgvector context, and FalkorDB relationships. System 3 can consume
approved recommendations and assignment evidence from `entity_update_events`
and `decision_snapshots`. This service should still score a request when those
systems are unavailable, as long as the request contains explicit candidate and
role data or the local candidate-pool backend is intentionally enabled.
