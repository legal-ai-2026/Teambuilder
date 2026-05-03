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
| System 2 | Cognitive mission adaptation, roster recommendation, and career forecast | adaptations, scenario recommendations, agent runs, recommendations, approvals, audit records, retrieval/graph context | soldiers, missions, roles, training, outcomes, policy context |
| System 3 | Deployment outcomes and final assignments | accepted assignments, mission outcomes, after-action observations | recommendations, soldiers, missions, graph facts |

No system should overwrite another system's canonical records. Cross-system
corrections must be written as new update events with provenance.

## System 1 Project Details

System 1 is the Ranger adversarial training agent. It is an API-only backend
that receives instructor ingest envelopes, extracts observations, writes graph
facts, drafts scenario recommendations, applies policy checks, waits for
instructor approval, and emits decision events for the other systems.

| Area | Detail |
|---|---|
| Service name | System 1 Ranger adversarial training agent |
| Public API root | `/v1` |
| Primary input | `IngestEnvelope` submitted to `POST /v1/ingest` |
| Primary output | `RunRecord` with observations, recommendations, audit events, dashboard summary, and outbox events |
| Human gate | `/v1/recommendations/{recommendation_id}/decision` |
| System-owned IDs | `run_id`, `observation_id`, `recommendation_id`, `event_id` |
| External IDs | `soldier_id`, `instructor_id`, `platoon_id`, `patrol_id`, `mission_id`, `task_code` |
| Owned durable state | run state, audit events, outbox events, observations, approved recommendation graph nodes |
| Not owned | roster/person profiles, mission plans, System 2 trajectory/recommendation records, System 3 lessons-learned records |

Other projects should treat System 1 as the owner of short-horizon scenario
observations and instructor-approved scenario recommendations. They should not
write directly into `ranger_runs`, `ranger_audit_events`, or
`ranger_outbox_events`.

### System 1 Endpoints Other Apps Can Use

| Endpoint | Purpose | Important response data |
|---|---|---|
| `GET /v1/healthz` | Service/provider/infra health | dependency and provider availability |
| `POST /v1/ingest` | Start a new instructor-ingest run | `run_id`, `status=accepted`, original ingest |
| `GET /v1/runs/{run_id}` | Fetch canonical run state | observations, recommendations, KG summary, errors |
| `GET /v1/dashboard/runs/{run_id}` | Presentation-neutral run summary | GO/NOGO counts, readiness score, active recommendations |
| `GET /v1/runs/{run_id}/audit` | Inspect lifecycle/decision events | ordered immutable audit events |
| `POST /v1/recommendations/{recommendation_id}/decision` | Approve or reject a recommendation | final recommendation status |
| `GET /v1/outbox` | Poll decision events | pending outbox events |
| `POST /v1/outbox/{event_id}/published` | Mark consumed outbox event | `event_id`, `status=published` |

Future endpoints named by System 1 but not implemented yet:

- `GET /v1/soldier/{id}/training-trajectory`
- `POST /v1/lessons-learned`

Until those exist, System 2 and System 3 should consume System 1 outputs through
shared stores and the outbox/update-ledger pattern.

### System 1 State Machine

Run statuses:

| Status | Meaning | Who acts |
|---|---|---|
| `accepted` | ingest validated and persisted | System 1 background processor |
| `processing` | STT/OCR/extraction/KG/reasoning/policy running | System 1 |
| `pending_approval` | recommendations ready for instructor decision | frontend/instructor |
| `completed` | all recommendations approved, rejected, or blocked | System 2/System 3 may consume outbox |
| `failed` | processing failed | operator/caller |

Recommendation statuses:

| Status | Meaning | Downstream rule |
|---|---|---|
| `pending` | policy allowed, instructor has not decided | not approved training intent |
| `approved` | instructor approved | Systems 2/3 may use as decision signal |
| `rejected` | instructor rejected | Systems 2/3 may use as negative feedback |
| `blocked` | policy rejected before approval | do not execute; use reasons for analysis |

### System 1 Inputs And Outputs

Inbound `IngestEnvelope` fields:

- `envelope_id`
- `instructor_id`
- `platoon_id`
- `mission_id`
- `phase`: `Benning`, `Mountain`, or `Florida`
- `timestamp_utc`
- `geo`
- optional `audio_b64`
- optional `image_b64[]`
- optional `free_text`

At least one of `audio_b64`, `image_b64`, or `free_text` is required.

Derived observation fields:

- `observation_id`
- `soldier_id`
- `task_code`
- redacted `note`
- `rating`: `GO`, `NOGO`, or `UNCERTAIN`
- `timestamp_utc`
- `source`: `audio`, `image`, `free_text`, or `synthetic`

Recommendation records include:

- `recommendation_id`
- `target_soldier_id`
- `rationale`
- `development_edge`
- `proposed_modification`
- `doctrine_refs`
- `safety_checks`
- `estimated_duration_min`
- `requires_resources`
- `risk_level`
- `fairness_score`
- `policy.allowed`
- `policy.reasons`
- final `status`

System 1 outbox events currently contain recommendation ID, decision status,
and target soldier ID. Consumers should resolve the full run and audit context
through `GET /v1/runs/{run_id}` and `GET /v1/runs/{run_id}/audit`.

### How System 1 Changes Shared Data

| Trigger | Store | Record changed | Data effect |
|---|---|---|---|
| `POST /v1/ingest` | Postgres `ranger_runs` | `RunRecord` | inserts accepted run with inbound envelope |
| `POST /v1/ingest` | Postgres `ranger_audit_events` | `run_accepted` | appends immutable lifecycle event |
| background start | Redis | `ranger:run-lease:{run_id}` | creates short-lived run lease |
| background start | Postgres `ranger_audit_events` | `run_processing_started` | appends immutable lifecycle event |
| extraction completes | Postgres `ranger_runs.record` | transcript/OCR/observations | updates materialized run state |
| observation graph write | FalkorDB graph `ranger` | mission/platoon/soldier/task/observation facts | `MERGE` canonical IDs and relationships |
| recommendation drafting | Postgres `ranger_runs.record` | recommendations/status | stores draft and policy outcomes |
| processing completes/fails | Postgres `ranger_audit_events` | status/failure event | appends immutable lifecycle event |
| approve/reject | Postgres `ranger_runs.record` | recommendation status | materialized status update |
| approve recommendation | FalkorDB graph `ranger` | `Recommendation` node | links recommendation to target soldier |
| approve/reject | Postgres `ranger_audit_events` | decision event | appends immutable approval/rejection audit |
| approve/reject | Postgres `ranger_outbox_events` | integration event | appends pending event for Systems 2/3 |
| outbox published | Postgres `ranger_outbox_events` | event status | marks event `published` after consumer applies it |
| vector upsert adapter | Postgres `ranger_vector_documents` | semantic document | upserts text, metadata, and embedding |

System 1 does not delete shared records and does not write System 2 or System 3
owned tables.

### System 1 Current Tables And Keys

| Table/key | Mutability | Purpose |
|---|---|---|
| `ranger_runs` | mutable materialized state | run status, ingest envelope, observations, KG summary, recommendation records, errors |
| `ranger_audit_events` | append-only | run lifecycle and instructor decisions |
| `ranger_outbox_events` | append-only except `status` | integration events for consumers |
| `ranger_vector_documents` | upsert by `(namespace, document_id)` | semantic text and embeddings |
| `ranger:run-lease:{run_id}` | TTL Redis key | active-run lease |

System 1 current gaps:

- dedicated update ledger table
- `evidence_refs` and `target_ids` on all output contracts
- graph edges from recommendations back to observations/tasks
- automatic pgvector ingestion in the ingest workflow
- cross-system detail lookup endpoints by canonical ID

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

System 2 is the Cognitive Mission Adaptation Engine and roster
decision-support service. Its live lane is developmental: it consumes field
evidence, estimates cognitive/team state, recommends scenario injects, records
its reasoning and audit trail, and waits for instructor approval. Its roster
scoring path remains a downstream talent lane. System 2 does not own soldier
identity, training authority, final deployment outcomes, or published
assignment authority.

### What System 2 Does

System 2 can:

- estimate cognitive and team state from live training evidence
- recommend scenario injects for instructor approval
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
| `POST /v1/adaptations` | Estimate cognitive/team state and propose scenario injects | yes |
| `GET /v1/adaptations/{adaptation_id}` | Fetch stored adaptation | no |
| `GET /v1/missions/{mission_id}/adaptations` | List stored adaptations for a mission | no |
| `POST /v1/adaptations/{adaptation_id}/approval` | Record instructor approve/reject decision | yes |
| `POST /v1/operational-twin/runs` | Normalize multimodal evidence, estimate operational state, and draft governed options | yes |
| `GET /v1/operational-twin/runs/{twin_run_id}` | Fetch operational twin run with evidence bundle and draft/decided options | no |
| `POST /v1/operational-twin/runs/{twin_run_id}/options/{scenario_option_id}/decision` | Record approve/reject/escalate decision and lesson draft | yes |
| `POST /v1/score` | Direct roster scoring | audit only |
| `POST /v1/agent-runs` | Agentic recommendation workflow | yes |
| `GET /v1/agent-runs/{run_id}` | Fetch run and recommendation | no |
| `POST /v1/agent-runs/{run_id}/approval` | Record approve/reject decision | yes |
| `POST /v1/context/chunks` | Ingest retrievable policy/SOP/context chunks | yes |
| `POST /v1/graph/facts` | Ingest derived relationship facts | yes |
| `POST /admin/disable` | Disable scoring | audit/control only |
| `POST /admin/enable` | Re-enable scoring | audit/control only |

### System 2 Inputs

Current implemented adaptation input:

```json
{
  "mission_id": "raid-tonight",
  "instructor_id": "instructor-1",
  "team_id": "alpha",
  "target_soldier_ids": ["RGR-0001"],
  "evidence": [
    {
      "evidence_id": "obs-001",
      "source_type": "voice_note",
      "text": "Missed second-order relationship between terrain, timing, support, civilian movement, and delayed comms relay under fatigue.",
      "soldier_ids": ["RGR-0001"],
      "tags": ["systems_thinking", "fatigue"],
      "metrics": {"sleep_hours": 4.5}
    }
  ]
}
```

Current adaptation evidence source types are `voice_note`, `transcript`,
`ocr_text`, `checklist`, `patrol_summary`, `aar`, `weather`, `terrain`, and
`structured_event`.

Current implemented operational twin input:

```json
{
  "mission_id": "foundry-twin-demo",
  "operator_id": "instructor-1",
  "mode": "training",
  "team_id": "alpha-1",
  "training_objective": "Train systems thinking under fatigue.",
  "artifacts": [
    {
      "kind": "audio",
      "content": "Two missed comms acknowledgements. Leader lost terrain, timing, support, civilian movement, and delayed comms relay relationships under fatigue."
    },
    {
      "kind": "sleep_food_log",
      "content": "Median team sleep was 4.1 hours.",
      "metadata": {"sleep_hours": 4.1}
    }
  ],
  "environment": {
    "weather": "cold wind",
    "terrain": "rough wooded draw",
    "temperature_c": 3,
    "wind_speed": 18
  }
}
```

Operational twin runs append `entity_update_events` with entity type
`operational_twin`. Option decisions append `operational_twin_option` events.
The payloads carry mission/team IDs, evidence bundle ID, state estimate ID,
scenario option IDs, decision IDs, and lesson IDs when an approved option emits
a lesson draft.

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
- `candidate_pool_id` for ID-only scoring
- `run_id` for fetching decision details
- approval/rejection decision, approver ID, and rationale

### What Other Projects Can Rely On From System 2

Other systems can rely on System 2 to produce:

- `adaptation_id` for developmental scenario-adaptation runs
- cognitive/team state snapshots with evidence references
- scenario inject recommendations with risk, confidence, and doctrine rationale
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

## System 3 Project Details

System 3 is the combat deployment intelligence and COA planning service. It
generates, validates, stores, retrieves, and reviews COA planning runs. It
consumes mission context and produces derived planning outputs; it must not
overwrite canonical person, unit, intel, ROE, terrain, or enemy-pattern records.

| Area | Detail |
|---|---|
| App ID | `system3` |
| Package | `spire_deploy` |
| API service | `spire_deploy.gateway.main:app` |
| API title | `System 3 Operations Gateway` |
| Primary responsibility | COA planning, rehearsal scenarios, planning guardrails, approval tracking |
| Current canonical read implementation | `SyntheticRepository` backed by `data/synthetic/` |
| Current checkpoint store | Redis when configured, otherwise in-memory |
| Current graph/vector usage | health-checkable, not yet in recommendation path |
| Primary run ID | `planning-run-{uuid}` |
| Derived output IDs | `coa-{missionId}-{seq}`, `scenario-{missionId}-{seq}` |
| Authentication | `X-API-Key` matching `SYSTEM3_API_KEY` |

System 3 owns these derived records:

- `PlanningRun`
- `AgentOutput`
- `COA`
- `RehearsalScenario`
- `COAApprovalResponse`
- future `DriftFinding`

System 3 consumes but does not own:

- `Person` / `Soldier`
- `Unit`
- `Mission`
- `MissionPhase`
- `ROERule`
- `IntelReport`
- `EnemyPattern`
- `EnemyEntity`
- `TerrainFeature`

### System 3 Endpoints Other Apps Can Use

| Endpoint | Purpose | Caller sends IDs |
|---|---|---|
| `GET /v1/healthz` | service health | no |
| `GET /v1/healthz/infrastructure` | redacted datastore reachability | no |
| `GET /v1/mission-context/{missionId}` | resolved mission context projection | `missionId` |
| `POST /v1/mission/assignment` | local mission assignment update | `missionId`, `soldierIds` |
| `POST /v1/coa/propose` | create a planning run | `missionId` |
| `GET /v1/coa/proposals/{runId}` | fetch stored planning run | `runId` |
| `POST /v1/coa/proposals/{runId}/approval` | approve or reject a COA | `runId`, `coaId` |

### System 3 Inputs And Outputs

COA proposal request:

```json
{
  "missionId": "mission-compound-iron",
  "requestedBy": "operator.id",
  "includeRehearsalScenarios": true
}
```

Mission assignment request:

```json
{
  "missionId": "mission-compound-iron",
  "soldierIds": ["soldier-viper-31-actual"],
  "requestedBy": "operator.id",
  "justification": "Assignment changed during planning."
}
```

COA approval request:

```json
{
  "coaId": "coa-mission-compound-iron-1",
  "reviewedBy": "commander.id",
  "decision": "Approve",
  "justification": "Approved after review."
}
```

COA proposal responses include:

- `runId`
- `missionId`
- `producedAt`
- proposed `coas`
- optional `rehearsalScenarios`
- ordered `agentTrace`

Infrastructure health responses must redact credentials and must not expose
full credential-bearing URLs.

### System 3 Agent Trace And Current Logic

Current agent names:

- `MissionContextAgent`
- `EnemyPatternMinerAgent`
- `ScenarioGeneratorAgent`
- `COACriticAgent`
- `PlanningGuardAgent`

Current deterministic logic:

- `EnemyPatternMinerAgent` sorts mission-linked enemy patterns by confidence.
- `ScenarioGeneratorAgent` creates `GroundConvoy`, `AirAssault`, and
  `DismountedPatrol` variants.
- `COACriticAgent` creates one COA per scenario.
- `riskScore` is deterministic by scenario index.
- `roeStatus` is derived from keywords in the COA summary.
- `PlanningGuardAgent` blocks invalid outputs before response persistence.

Current guardrails:

- classification cannot exceed mission ceiling
- COAs cannot be pre-approved before human review
- COAs must cite mission ROE
- COAs must cite only mission-context intel
- scenarios must cite enemy patterns
- scenarios must cite mission ROE

Current `agentTrace` references are object-level only. Shared-infra persistence
must enrich each reference with `recordVersion`, `recordHash`, and optionally
`fieldPath`.

### How System 3 Changes Shared Data

| Endpoint | Reads | Writes now | Target persistent writes |
|---|---|---|---|
| `GET /v1/mission-context/{missionId}` | mission context by ID | none | none |
| `POST /v1/mission/assignment` | mission and soldier IDs | in-memory mission assignment | update event, assignment version, graph edges |
| `POST /v1/coa/propose` | mission context, ROE, intel, patterns, terrain | Redis or memory planning run | planning runs, agent outputs, COAs, scenarios, graph edges, output versions |
| `GET /v1/coa/proposals/{runId}` | stored planning run | none | none |
| `POST /v1/coa/proposals/{runId}/approval` | run and COA | updates stored COA state | update event, COA state/version, approval event |
| `GET /v1/healthz/infrastructure` | env config and reachability | none | none |

System 3 Redis checkpoint key:

```text
system3:planning-run:{runId}
```

Current TTL:

```text
86400 seconds
```

Redis is a checkpoint/cache only. Other projects should use APIs now and
Postgres `planning_runs` / `agent_outputs` when persistence is added.

### System 3 Drift Triggers

System 3 outputs should be checked for drift when any of these source records
change:

- mission objective
- mission classification ceiling
- mission phases
- mission assignments
- ROE rules
- cited intel reports
- selected enemy patterns
- terrain features used by a scenario
- graph relationships connecting the mission to source data
- embeddings for cited intel, pattern, or lesson text

Drift response:

1. preserve the original run
2. write a `drift_findings` row
3. mark affected outputs `NeedsReview`
4. generate a new planning run if needed
5. never mutate the old output to appear current

System 3 current gaps:

- durable Postgres planning-run and agent-output persistence
- graph relationship persistence in the recommendation path
- pgvector retrieval in the recommendation path
- append-only update ledgers for mission assignment and approval changes
- source record versions/hashes on persisted agent outputs

## Canonical IDs

| Entity | ID field | Format guidance |
|---|---|---|
| Soldier | `soldier_id` | Canonical personnel identifier or stable synthetic token |
| Mission | `mission_id` | Stable mission/task identifier |
| Role slot | `slot_id` | Unique within a mission, such as `MED-1` |
| Unit | `unit_id` | Canonical unit code |
| Training evidence | `evidence_id` | Stable evidence ID from voice/OCR/checklist/AAR capture |
| Adaptation run | `adaptation_id` | UUID generated by System 2 for scenario adaptation |
| Scenario inject | `recommendation_id` | UUID generated by System 2 for a proposed inject |
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
| Cognitive adaptation | `POST /v1/adaptations` | `system2_adaptations`, `system2_audit_log`, `entity_update_events` | reads context chunks | none now | none |
| Adaptation approval | `POST /v1/adaptations/{adaptation_id}/approval` | `system2_audit_log`, `entity_update_events` | none | optional scenario facts after approval | none |
| Direct score | `POST /v1/score` | `system2_audit_log`, `decision_snapshots` | none | none | none |
| Agent run create | `POST /v1/agent-runs` | `system2_agent_runs`, `system2_audit_log`, `decision_snapshots` | none | none | `system2:agent-run:{run_id}:status`, lock |
| Agent approval | `POST /v1/agent-runs/{run_id}/approval` | `system2_agent_runs`, `system2_audit_log`, `entity_update_events` | none | optional assignment facts after approval | status update |
| Context ingest | `POST /v1/context/chunks` | `system2_context_chunks`, `entity_update_events` | `system2_context_chunks.embedding` | none | optional cache invalidation |
| Graph fact ingest | `POST /v1/graph/facts` | `entity_update_events` | none | graph facts in `system2` | optional cache invalidation |
| Kill switch | `POST /admin/disable`, `POST /admin/enable` | `system2_audit_log`, `entity_update_events` | none | none | none |

### Direct Score

When System 2 scores a request directly:

1. It reads candidate and role data from the request today. In integrated mode
   it should resolve IDs from Postgres projections.
2. It computes a recommendation.
3. It appends audit records:
   - `score_request_received`
   - `fairness_outcome`
   - `recommendations_returned`
4. It writes a `decision_snapshots` row containing request hash, source
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
5. It attaches source refs to the recommendation trace for request, retrieval,
   and graph inputs.
6. It saves the final run payload with status `awaiting_approval` by default.
7. It writes a `decision_snapshots` row with request hash, input source hashes,
   output hash, fairness hash, and source refs.
8. It releases the Redis lock.

The agent run payload includes source refs for each output. Request-local and
local fallback refs are explicit, so downstream systems can distinguish fully
resolved operational runs from disconnected/local runs.

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
7. It appends an `entity_update_events` row.

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
3. It appends one `entity_update_events` row per chunk.
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
2. It appends an `entity_update_events` row for each graph fact.
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
2. It appends an `entity_update_events` row with:
   - `entity_type = "system_control"`
   - `entity_id = "system2.kill_switch"`
   - `operation = "disable"` or `"enable"`

Kill-switch records are operational control events. They do not alter source
data for soldiers, missions, or outcomes.

### Current Implementation Gap

The current System 2 implementation already writes:

- `system2_agent_runs`
- `system2_audit_log`
- `system2_context_chunks`
- `decision_snapshots` for agent-run recommendations
- `decision_snapshots` for direct `/v1/score` recommendations
- `entity_update_events` for approval/rejection, context ingest, and graph
  ingest
- `entity_update_events` for kill-switch changes
- `entity_update_events` for cognitive adaptation recommendations and
  scenario-inject approval/rejection
- FalkorDB facts through `/v1/graph/facts`
- Redis status and lock keys
- recommendation `trace.source_refs` and `trace.input_source_hashes`
- ID-only candidate-pool scoring enrichment from `training_observations_current`
  and `deployment_outcomes_current`
- retrieval and graph source refs on direct `/v1/score` and agent-run
  recommendations
- bounded contextual scoring adjustments derived from retrieved fatigue/safety
  context and graph `requires_skill` relationships

The following are contract requirements still to implement:

- Broader validated context transforms for weather, terrain, qualifications,
  unit history, and prior assignment relationships.

## Postgres Tables

Use separate tables for current projections and append-only updates.

### Current Projections

`candidate_pools_current`

| Column | Notes |
|---|---|
| `pool_id` | Primary key for a reusable candidate pool |
| `mission_id` | Mission the pool applies to |
| `candidate_ids` | Ordered JSONB list of canonical `soldier_id` values |
| `payload` | Optional pool metadata |
| `source_hash` | Hash of projection inputs |
| `updated_at` | Projection timestamp |

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

`system2_adaptations`

Already implemented by System 2. Stores cognitive adaptation payloads as JSONB
with `adaptation_id`, `mission_id`, `team_id`, status, and timestamp indexes for
frontend lookup and mission timelines.

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

3. System 2 resolves candidates and role slots from Postgres, enriches
   candidate features from training and deployment projections, then attaches
   retrieval and graph source refs from the configured shared stores.
4. Frontend displays recommendation and source refs.
5. Authorized user approves or rejects.
6. Approval/rejection writes a separate update event.

The current System 2 API accepts `candidate_pool_id` and explicit candidate
arrays. With `CANDIDATE_POOL_BACKEND=postgres`, ID-only requests resolve
candidates and roles from `candidate_pools_current`, `soldiers_current`, and
`role_slots_current`; missing pools fail the request. Training, context, and
graph enrichment still depends on the corresponding shared projections,
pgvector chunks, and FalkorDB facts being populated.

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
