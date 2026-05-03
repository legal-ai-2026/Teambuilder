# Shared Data Contract - System 1, System 2, and System 3

This document is the integration contract for the three applications sharing
Postgres, pgvector, Redis, and FalkorDB.

The frontend should send canonical IDs. Backend services resolve details from
shared infrastructure, produce outputs with source references, and store every
update as an append-only event so drift can be detected over time.

## Core Rules

1. Canonical IDs are stable across all apps.
2. Frontend requests should pass IDs, not full person records, unless creating
   or correcting data.
3. Postgres is the canonical durable store.
4. Redis is ephemeral only: cache, locks, run status, and rate limits.
5. FalkorDB holds derived relationship facts that can be rebuilt from Postgres.
6. pgvector holds retrievable context chunks and embeddings, not authority.
7. Every agent output must cite the exact data it used.
8. Every write is append-only first. Current-state tables are projections.
9. Drift is measured by comparing snapshots, source hashes, and output
   explanations over time.

## Systems

| System | Responsibility | Writes | Reads |
|---|---|---|---|
| System 1 | Training/adversarial trajectory | training events, skill observations, simulation outcomes | soldiers, missions, graph facts |
| System 2 | Roster recommendation and career forecast | agent runs, recommendations, approvals, audit records, retrieval/graph context | soldiers, missions, roles, training, outcomes, policy context |
| System 3 | Deployment outcomes and final assignments | accepted assignments, mission outcomes, after-action observations | recommendations, soldiers, missions, graph facts |

No system should overwrite another system's canonical records. Cross-system
corrections must be written as new update events with provenance.

## System 2 Project Details

Repository:

```text
c2d2-teambuilder-model
```

Runtime service:

```text
FastAPI app: system2.api:app
Default local URL: http://127.0.0.1:8000
```

System 2 is the roster recommendation and agentic decision-support service. It
does not own soldier identity, training authority, final deployment outcomes, or
published assignment authority. It consumes those records, produces
recommendations, records its own reasoning and audit trail, and waits for human
approval before finalization.

### What System 2 Does

System 2 can:

- score soldiers against mission role slots
- produce a primary roster
- produce a second-choice roster
- explain confidence and model disagreement
- run fairness/proxy audits
- record agent steps and evidence
- ingest retrieval context into pgvector
- ingest relationship facts into FalkorDB
- record human approval or rejection
- write hash-chained audit records

System 2 must not:

- mutate soldier-of-record data owned by another system
- mutate training observations owned by System 1
- mutate deployment outcomes owned by System 3
- publish final orders or assignments by itself
- treat Redis, pgvector, or FalkorDB as canonical authority

### System 2 Endpoints Other Apps Can Use

| Endpoint | Purpose | Mutates shared state |
|---|---|---|
| `GET /v1/healthz` | Check service and backend selection | no |
| `POST /v1/score` | Direct roster scoring | audit only |
| `POST /v1/agent-runs` | Agentic recommendation workflow | yes |
| `GET /v1/agent-runs/{run_id}` | Fetch run and recommendation | no |
| `POST /v1/agent-runs/{run_id}/approval` | Record approve/reject decision | yes |
| `POST /v1/context/chunks` | Ingest retrievable policy/SOP/context chunks | yes |
| `POST /v1/graph/facts` | Ingest derived relationship facts | yes |
| `POST /admin/disable` | Disable scoring | audit/control only |
| `POST /admin/enable` | Re-enable scoring | audit/control only |

### System 2 Inputs

Current implemented score input:

```json
{
  "mission_id": "raid-tonight",
  "candidate_count": 80,
  "seed": 42,
  "candidates": [],
  "roles": []
}
```

For integrated operation, other apps should move toward ID-only inputs:

```json
{
  "mission_id": "raid-tonight",
  "candidate_pool_id": "pool-2026-05-02-a"
}
```

System 2 still needs the same underlying facts after resolving IDs:

- mission requirements
- role slots
- candidate soldiers
- current soldier profile projection
- training and competency projections
- deployment/outcome history if available
- policy/SOP context chunks
- graph relationships such as required roles, skills, and qualifications

Candidate fields currently consumed by the scorer:

| Field | Usage |
|---|---|
| `soldier_id` | trace and roster identity |
| `unit_id` | Bayes-compatible pooled unit effect; hashed in audit |
| `mos` | role qualification and pooled MOS effect; hashed in audit |
| `age_years` | proxy/fairness context; not directly scored |
| `two_mile_run_sec` | physical readiness |
| `self_efficacy_score` | readiness signal |
| `peer_rating_z` | leadership/cohesion signal |
| `home_unit_ranger_density` | experience/context signal |
| `acft_score` | readiness signal and hard role gate |
| `operational_readiness` | mission success signal |
| `prior_missions` | experience and uncertainty |
| `medical_risk` | safety/risk penalty |
| `landing_asymmetry_score` | safety/risk penalty |
| `hip_extension_power_w` | currently recorded, adapter expansion field |
| `change_of_direction_index` | local pattern signal |
| `fatigue_index` | safety/risk penalty |
| `sandbox_score` | simulation performance signal |
| `milestones` | readiness terms |
| `competencies` | role-specific fit terms |
| `protected_race` | fairness audit only |
| `protected_gender` | fairness audit only |

Role fields currently consumed:

| Field | Usage |
|---|---|
| `slot_id` | assignment slot identity |
| `role` | role-specific scoring weights |
| `required_mos` | hard disqualifier when set |
| `min_acft` | hard disqualifier |

### System 2 Outputs

Direct score output:

```json
{
  "mission_id": "raid-tonight",
  "roster": [],
  "second_choice_roster": [],
  "fairness_audit": {},
  "career_forecast": {},
  "trace": {}
}
```

Each roster item contains:

- `slot_id`
- `role`
- `soldier_id`
- `fit_score`
- `p_success_tabpfn`
- `p_success_bayes_mean`
- `model_disagreement`
- `p_success_bayes_ci`
- `confidence`
- `narrative`
- `key_strengths`
- `risk_factors`
- `second_choice_id`

Agent run output contains:

- `run_id`
- `status`
- original request
- ordered agent steps
- recommendation payload
- approval payload if decided
- error if failed
- timestamps

Other apps should treat `run_id` as the durable handle for System 2 decisions.
The frontend should fetch details by `run_id` instead of recomputing local
decision state.

### How System 2 Makes Recommendations

System 2 scores every `(soldier, role)` pair rather than ranking soldiers
globally.

1. `role_fit` computes deterministic role fit from physical readiness,
   operational readiness, experience, simulation score, milestones, and
   role-specific competencies.
2. Medical risk, landing asymmetry, and fatigue reduce fit.
3. Protected attributes are excluded from scoring and assignment.
4. A TabPFN-compatible deterministic probability is computed.
5. A Bayes-compatible pooled probability is computed from unit and MOS context.
6. The two probabilities are blended.
7. Model disagreement determines confidence:
   - `< 0.10`: `high`
   - `0.10` to `0.25`: `medium`
   - `> 0.25`: `low`, with score demotion
8. Required MOS and minimum ACFT create hard disqualifiers.
9. The roster is solved with Hungarian assignment.
10. The second-choice roster is solved by blocking primary `(soldier, role)`
    pairs and solving again.
11. Fairness audit, narrative, career forecast, trace metadata, and audit
    records are generated.

### System 2 Backend Ownership

| Store | System 2 objects |
|---|---|
| Postgres | `system2_agent_runs`, `system2_audit_log`, `system2_context_chunks` |
| pgvector | `system2_context_chunks.embedding` |
| Redis | `system2:agent-run:{run_id}:status`, `system2:agent-run:{run_id}:lock` |
| FalkorDB | derived graph facts accepted through `/v1/graph/facts` |

System 2 table creation is handled by its adapters when Postgres/pgvector
backends are enabled. Shared tables such as `soldiers_current`,
`entity_update_events`, `decision_snapshots`, and `drift_observations` are
defined by this contract and should be migrated consistently across all apps.

### What Other Projects Should Provide To System 2

System 1 should provide:

- training observations by `soldier_id`
- competency and milestone projections
- simulation outcomes
- graph facts for skills, qualifications, and training relationships
- source hashes for every projection

System 3 should provide:

- prior assignments
- deployment outcomes
- mission outcome observations
- graph facts linking assignments, missions, soldiers, and outcomes
- source hashes for every projection

Frontend should provide:

- `mission_id`
- `candidate_pool_id` once implemented
- `run_id` for fetching decision details
- approval/rejection decision, approver ID, and rationale

### What Other Projects Can Rely On From System 2

Other systems can rely on System 2 to produce:

- a durable `run_id`
- recommendation status
- selected roster and second-choice roster
- confidence and disagreement fields for each assignment
- fairness audit payload
- trace metadata
- audit records
- approval/rejection details
- context chunk records accepted through ingestion
- graph facts accepted through ingestion

They should not treat a recommendation as final until the agent run status is
`completed` and an approval payload exists.

## Canonical IDs

| Entity | ID field | Format guidance |
|---|---|---|
| Soldier | `soldier_id` | Canonical personnel identifier or stable synthetic token |
| Mission | `mission_id` | Stable mission/task identifier |
| Role slot | `slot_id` | Unique within a mission, such as `MED-1` |
| Unit | `unit_id` | Canonical unit code |
| Agent run | `run_id` | UUID generated by System 2 |
| Recommendation | `recommendation_id` | UUID or deterministic ID tied to `run_id` |
| Assignment | `assignment_id` | UUID or downstream mission assignment ID |
| Context chunk | `chunk_id` | Stable document chunk ID |
| Graph fact | `fact_id` | Hash of `(subject, predicate, object, source_event_id)` |
| Update event | `event_id` | UUID |

Frontend calls should normally contain:

```json
{
  "mission_id": "raid-tonight",
  "candidate_pool_id": "pool-2026-05-02-a"
}
```

or:

```json
{
  "soldier_id": "RGR-0001"
}
```

System 2 can still accept explicit candidate payloads for disconnected/local
operation, but integrated operation should resolve by ID.

## How System 2 Changes Shared Data

System 2 is mostly a decision-support service. It should not overwrite soldier,
mission, training, or deployment outcome authority owned by other systems. It
does write its own decision records, audit records, retrieval context, graph
facts, approvals, and drift snapshots.

### Write Summary

| System 2 operation | Endpoint | Postgres writes | pgvector writes | FalkorDB writes | Redis writes |
|---|---|---|---|---|---|
| Direct score | `POST /v1/score` | `system2_audit_log`; future `decision_snapshots` | none | none | none |
| Agent run create | `POST /v1/agent-runs` | `system2_agent_runs`, `system2_audit_log`; future `decision_snapshots` | none | none | `system2:agent-run:{run_id}:status`, lock |
| Agent approval | `POST /v1/agent-runs/{run_id}/approval` | `system2_agent_runs`, `system2_audit_log`, `entity_update_events`; future `decision_snapshots` | none | optional assignment facts after approval | status update |
| Context ingest | `POST /v1/context/chunks` | `system2_context_chunks`, `entity_update_events` | `system2_context_chunks.embedding` | none | optional cache invalidation |
| Graph fact ingest | `POST /v1/graph/facts` | `entity_update_events` | none | graph facts in `system2` | optional cache invalidation |
| Kill switch | `POST /admin/disable`, `POST /admin/enable` | `system2_audit_log` | none | none | none |

### Direct Score

When System 2 scores a request directly:

1. It reads candidate and role data from the request today. In integrated mode
   it should resolve IDs from Postgres projections.
2. It computes a recommendation.
3. It appends audit records:
   - `score_request_received`
   - `fairness_outcome`
   - `recommendations_returned`
4. It should write a `decision_snapshots` row containing request hash, source
   hashes, output hash, and fairness hash.

Direct scoring must not mutate:

- `soldiers_current`
- `missions_current`
- `role_slots_current`
- `training_observations_current`
- `deployment_outcomes_current`

### Agent Run Create

When System 2 creates an agent run:

1. It creates a `system2_agent_runs` row with status `queued`.
2. It writes Redis status `system2:agent-run:{run_id}:status`.
3. It acquires Redis lock `system2:agent-run:{run_id}:lock`.
4. It records run steps:
   - `request_context`
   - `retrieval_context`
   - `graph_context`
   - `roster_recommendation`
   - `human_approval`
5. It saves the final run payload with status `awaiting_approval` by default.
6. It releases the Redis lock.

The agent run payload should include source refs for each output. The current
implementation records step evidence; integrated mode should expand that
evidence to include concrete Postgres row hashes, pgvector chunk IDs, and
FalkorDB fact IDs.

### Agent Approval Or Rejection

When a human approves or rejects a run:

1. System 2 reads `system2_agent_runs` by `run_id`.
2. It verifies the run is `awaiting_approval`.
3. It appends an `approval_recorded` step.
4. It stores:
   - `decision`
   - `approver_id`
   - `rationale`
   - `decided_at`
5. It changes the run status:
   - `approved` -> `completed`
   - `rejected` -> `rejected`
6. It updates Redis run status.
7. It should append an `entity_update_events` row.

Recommended approval event:

```json
{
  "entity_type": "recommendation",
  "entity_id": "run_id-or-recommendation_id",
  "source_app": "system2",
  "source_record_id": "run_id",
  "operation": "approve",
  "event_payload": {
    "run_id": "run-123",
    "mission_id": "raid-tonight",
    "decision": "approved",
    "approver_id": "commander-1",
    "selected_soldier_ids": ["RGR-0001", "RGR-0002"],
    "slot_ids": ["TL-1", "MED-1"]
  },
  "previous_source_hash": "hash-before-approval",
  "new_source_hash": "hash-after-approval",
  "actor_id": "commander-1",
  "reason": "Reviewed recommendation, fairness audit, and second choices."
}
```

Rejected recommendations use `operation = "reject"` and should preserve the
same source references so future drift reviews can explain why the rejected
recommendation was produced.

### Context Chunk Ingestion

When System 2 ingests context chunks:

1. It upserts rows in `system2_context_chunks`.
2. It stores externally supplied embeddings in `embedding`.
3. It should append one `entity_update_events` row per chunk.
4. It should invalidate any context-cache Redis keys if those are introduced.

Recommended context event:

```json
{
  "entity_type": "policy",
  "entity_id": "sop-001",
  "source_app": "system2",
  "source_record_id": "sop-001",
  "operation": "observe",
  "event_payload": {
    "chunk_id": "sop-001",
    "source": "unit-sop",
    "title": "Roster approval",
    "content_hash": "hash-of-content",
    "embedding_model": "external-model-name"
  },
  "new_source_hash": "hash-of-chunk-record",
  "actor_id": "system2-context-ingest",
  "reason": "Context chunk ingested for retrieval."
}
```

System 2 does not create embeddings. The embedding producer must include enough
metadata to identify the embedding model and source text version.

### Graph Fact Ingestion

When System 2 ingests graph facts:

1. It writes or merges the fact into FalkorDB graph `system2`.
2. It should append an `entity_update_events` row for each graph fact.
3. The event payload must include source metadata sufficient to rebuild the
   graph from Postgres if FalkorDB is lost.

Recommended graph event:

```json
{
  "entity_type": "graph_fact",
  "entity_id": "fact-hash",
  "source_app": "system2",
  "source_record_id": "fact-hash",
  "operation": "observe",
  "event_payload": {
    "subject": "raid-tonight",
    "predicate": "requires_role",
    "object": "medic",
    "metadata": {
      "slot_id": "MED-1",
      "source_app": "system2"
    }
  },
  "new_source_hash": "hash-of-fact",
  "actor_id": "system2-graph-ingest",
  "reason": "Derived graph fact ingested for relationship lookup."
}
```

### Kill Switch Changes

When the kill switch changes:

1. System 2 writes a hash-chained audit record:
   - `kill_switch_changed`
2. It should append an `entity_update_events` row with:
   - `entity_type = "system_control"`
   - `entity_id = "system2.kill_switch"`
   - `operation = "disable"` or `"enable"` once those operation values are
     added to the shared enum.

Kill-switch records are operational control events. They do not alter source
data for soldiers, missions, or outcomes.

### Current Implementation Gap

The current System 2 implementation already writes:

- `system2_agent_runs`
- `system2_audit_log`
- `system2_context_chunks`
- FalkorDB facts through `/v1/graph/facts`
- Redis status and lock keys

The following are contract requirements still to implement:

- `entity_update_events` writes for approval, context ingest, graph ingest, and
  kill-switch changes.
- `decision_snapshots` writes for direct recommendations and approved agent
  recommendations.
- richer `source_refs` attached to each recommendation and agent step.
- ID-only request resolution through `candidate_pool_id`.

## Postgres Tables

Use separate tables for current projections and append-only updates.

### Current Projections

`soldiers_current`

| Column | Notes |
|---|---|
| `soldier_id` | Primary key |
| `unit_id` | Current unit |
| `mos` | Current MOS |
| `profile_json` | Current non-sensitive profile projection |
| `protected_json` | Protected attributes; access restricted to fairness audit paths |
| `source_hash` | Hash of projection inputs |
| `updated_at` | Projection timestamp |

`missions_current`

| Column | Notes |
|---|---|
| `mission_id` | Primary key |
| `mission_json` | Mission details |
| `source_hash` | Hash of projection inputs |
| `updated_at` | Projection timestamp |

`role_slots_current`

| Column | Notes |
|---|---|
| `mission_id` | Mission ID |
| `slot_id` | Slot ID |
| `role` | Role name |
| `required_mos` | Optional hard constraint |
| `min_acft` | Physical gate |
| `source_hash` | Hash of projection inputs |

`training_observations_current`

| Column | Notes |
|---|---|
| `soldier_id` | Soldier ID |
| `observation_json` | Current training/skill projection |
| `source_hash` | Hash of projection inputs |
| `updated_at` | Projection timestamp |

`deployment_outcomes_current`

| Column | Notes |
|---|---|
| `soldier_id` | Soldier ID |
| `mission_id` | Mission ID |
| `outcome_json` | Current outcome projection |
| `source_hash` | Hash of projection inputs |
| `updated_at` | Projection timestamp |

### Append-Only Updates

`entity_update_events`

| Column | Notes |
|---|---|
| `event_id` | Primary key |
| `entity_type` | `soldier`, `mission`, `role_slot`, `training`, `outcome`, `policy`, `graph_fact` |
| `entity_id` | Canonical ID |
| `source_app` | `system1`, `system2`, `system3`, `frontend`, `operator` |
| `source_record_id` | Upstream record ID |
| `operation` | `create`, `correct`, `observe`, `approve`, `reject`, `supersede` |
| `event_payload` | JSONB update payload |
| `previous_source_hash` | Hash before update, if known |
| `new_source_hash` | Hash after update |
| `observed_at` | When the fact was true/observed |
| `recorded_at` | When this update was written |
| `actor_id` | User/service identity |
| `reason` | Human-readable rationale |

Never update this table in place.

`system2_agent_runs`

Already implemented by System 2. Stores agent run payloads as JSONB with status
and mission indexes.

`system2_audit_log`

Already implemented by System 2. Stores hash-chained audit records. Protected
attributes are removed and clear unit/MOS values are hashed before persistence.

`system2_context_chunks`

Already implemented by System 2 for pgvector retrieval.

| Column | Notes |
|---|---|
| `chunk_id` | Primary key |
| `source` | Document/system source |
| `title` | Chunk title |
| `content` | Chunk body |
| `metadata` | JSONB metadata |
| `embedding` | pgvector embedding supplied by an external embedder |

## Output Provenance

Every agentic output must include references to the data it used.

Use this shape inside output `evidence` or recommendation metadata:

```json
{
  "source_refs": [
    {
      "store": "postgres",
      "table": "soldiers_current",
      "entity_type": "soldier",
      "entity_id": "RGR-0001",
      "source_hash": "abc123",
      "fields": ["acft_score", "operational_readiness", "prior_missions"]
    },
    {
      "store": "falkordb",
      "graph": "system2",
      "subject": "raid-tonight",
      "predicate": "REQUIRES_ROLE",
      "object": "medic",
      "fact_id": "fact-123"
    },
    {
      "store": "pgvector",
      "chunk_id": "sop-001",
      "source": "unit-sop",
      "score": 0.82
    }
  ]
}
```

Minimum provenance for a System 2 recommendation:

- `mission_id`
- candidate `soldier_id`s considered
- selected `soldier_id`
- selected `slot_id`
- source hashes for soldier profile, role slot, mission, training, and outcome
  projections
- context chunk IDs used
- graph fact IDs or `(subject, predicate, object)` triples used
- model/adapter versions
- run ID and audit record hash

## Drift Detection

Drift means one of these changed after a previous recommendation:

- Soldier profile or training projection changed.
- Mission or role-slot projection changed.
- Outcome data arrived or changed.
- Policy/context chunk changed.
- Graph facts changed.
- Same request now produces materially different output.
- Fairness/proxy metrics changed materially.

Store drift inputs separately from current records.

`decision_snapshots`

| Column | Notes |
|---|---|
| `snapshot_id` | Primary key |
| `run_id` | Agent run |
| `mission_id` | Mission |
| `request_hash` | Hash of normalized request |
| `input_source_hashes` | JSONB map of entity IDs to source hashes |
| `output_hash` | Hash of recommendation payload |
| `fairness_hash` | Hash of fairness audit payload |
| `created_at` | Snapshot timestamp |

`drift_observations`

| Column | Notes |
|---|---|
| `drift_id` | Primary key |
| `baseline_snapshot_id` | Prior snapshot |
| `comparison_snapshot_id` | New snapshot |
| `drift_type` | `input`, `output`, `fairness`, `policy`, `graph`, `retrieval` |
| `severity` | `low`, `medium`, `high` |
| `details` | JSONB explanation |
| `created_at` | Timestamp |

System 2 should write a `decision_snapshots` row when a recommendation is
returned or approved. A drift job can compare new snapshots against prior
snapshots for the same `mission_id`, `soldier_id`, or request hash.

## FalkorDB Contract

Graph name:

```text
system2
```

Recommended node labels:

- `Soldier`
- `Unit`
- `Mission`
- `Role`
- `Skill`
- `Qualification`
- `Policy`
- `Assignment`
- `Outcome`

Recommended relationships:

- `(Soldier)-[:HAS_SKILL]->(Skill)`
- `(Soldier)-[:ASSIGNED_TO]->(Unit)`
- `(Soldier)-[:HAS_QUALIFICATION]->(Qualification)`
- `(Mission)-[:REQUIRES_ROLE]->(Role)`
- `(Role)-[:REQUIRES_SKILL]->(Skill)`
- `(Role)-[:REQUIRES_QUALIFICATION]->(Qualification)`
- `(Policy)-[:CONSTRAINS]->(Role)`
- `(Assignment)-[:ASSIGNS]->(Soldier)`
- `(Assignment)-[:FILLS]->(Role)`
- `(Outcome)-[:OBSERVED_FOR]->(Assignment)`

Every graph fact should include metadata:

```json
{
  "source_app": "system1",
  "source_event_id": "event-123",
  "source_hash": "abc123",
  "observed_at": "2026-05-02T00:00:00Z"
}
```

## Redis Contract

Redis keys are namespaced and disposable.

| Key pattern | Owner | Purpose |
|---|---|---|
| `system2:agent-run:{run_id}:status` | System 2 | Ephemeral run status |
| `system2:agent-run:{run_id}:lock` | System 2 | Distributed lock |
| `system1:training:{soldier_id}:cache` | System 1 | Training projection cache |
| `system3:outcome:{mission_id}:cache` | System 3 | Outcome cache |
| `shared:entity:{entity_type}:{entity_id}:etag` | Any | Optional projection ETag |

Redis data must be rebuildable from Postgres or upstream systems.

## API Pattern For Frontend

Preferred frontend flow:

1. User selects a mission by `mission_id`.
2. Frontend calls System 2 with only IDs:

```json
{
  "score_request": {
    "mission_id": "raid-tonight",
    "candidate_pool_id": "pool-001"
  },
  "require_human_approval": true
}
```

3. System 2 resolves candidates, roles, training, context, and graph facts from
   shared infrastructure.
4. Frontend displays recommendation and source refs.
5. Authorized user approves or rejects.
6. Approval/rejection writes a separate update event.

The current System 2 API still accepts explicit candidate arrays. Add
`candidate_pool_id` support before relying on ID-only frontend requests in
production.

## Implementation Checklist For All Apps

- Write canonical projection rows in Postgres.
- Write append-only `entity_update_events` for every update.
- Include `source_app`, `source_record_id`, `actor_id`, and `reason`.
- Compute and persist `source_hash`.
- Upsert derived graph facts to FalkorDB with source metadata.
- Upsert context chunks to pgvector with stable `chunk_id`s.
- Use Redis only for cache/locks/status.
- Return source references for every agentic output.
- Never overwrite an update event.
- Never depend on graph or vector stores as the sole source of truth.
