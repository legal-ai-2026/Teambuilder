# Frontend Integration Guide

This document is the frontend contract for System 2, the Cognitive Mission
Adaptation Engine. It explains how a frontend should call the API, what data it
should send, what it should render, and which backend limitations it must handle
explicitly.

System 2 has three lanes:

- Developmental lane: live field evidence goes to `/v1/adaptations`, the
  backend estimates cognitive/team state, proposes scenario injects, and waits
  for instructor approval.
- Deployment lane: processed System 1 evidence plus mission context, terrain,
  weather, and readiness go to `/v1/deployment-recommendations`, the backend
  returns human-gated individual/platoon deployment posture and required
  controls.
- Talent lane: roster scoring and agent-run approval remain available through
  `/v1/score` and `/v1/agent-runs`, but they should be treated as downstream
  decision support rather than the main live training loop.

## Base Contract

Default local API:

```text
http://127.0.0.1:8000
```

Interactive OpenAPI docs:

```text
http://127.0.0.1:8000/docs
```

Machine-readable schema:

```text
http://127.0.0.1:8000/openapi.json
```

All request models are strict. Unknown JSON fields are rejected. Timestamps are
ISO 8601 strings. Numeric scores and risks are usually `0.0` to `1.0`.

## Authentication And CORS

Local development can run without API keys. In shared or deployed environments,
configure the backend with:

```env
SYSTEM2_CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
SYSTEM2_API_KEY=replace-me-service-key
SYSTEM2_ADMIN_API_KEY=replace-me-admin-key
```

When `SYSTEM2_API_KEY` is set, protected routes require one of these headers:

```http
X-API-Key: replace-me-service-key
Authorization: Bearer replace-me-service-key
```

`GET /health` and `GET /v1/healthz` stay public. `/admin/disable` and
`/admin/enable` require `SYSTEM2_ADMIN_API_KEY`; if that is unset, the service
uses `SYSTEM2_API_KEY` as the admin fallback.

For a purely browser-based hackathon frontend, the API key is only a coarse
demo gate because browser secrets are visible to users. For production, put this
service behind a gateway or backend-for-frontend that handles user identity,
role-based authorization, and short-lived session tokens.

## Frontend Responsibilities

The frontend should:

- Send canonical IDs whenever possible: `mission_id`, `team_id`, `soldier_id`,
  `candidate_pool_id`, `adaptation_id`, `recommendation_id`, and `run_id`.
- Display source references, confidence, risk, safety checks, and rationale for
  every recommendation.
- Display `decision_quality`, `utility_estimate`, and `reliance_guidance` on
  recommendation, operational twin, deployment, adaptation, and roster review
  surfaces.
- Keep instructor approval explicit. The frontend must never auto-approve a
  scenario inject, deployment posture, or roster decision.
- Treat developmental evidence as sensitive. Do not expose raw reflective notes,
  fatigue comments, or protected attributes beyond users with a need to know.
- Preserve the returned `adaptation_id` for refresh and timeline lookups.

The frontend should not:

- Assemble full soldier records for normal integrated operation.
- Treat recommendations as final orders.
- Send raw audio/images directly to System 2; STT/OCR belongs in System 1.
- Treat synthetic fallback data as operational truth.
- Hide blocked recommendations; blocked cards are useful safety evidence.

## Rich Dashboard Blueprint

Build the frontend as a working command dashboard, not a form collection. The
first screen should show the current mission, evidence freshness, system health,
active recommendations, approval state, and outcome follow-up queue without
requiring navigation.

### Dashboard Shell

Use a dense, operations-oriented layout:

- Top command bar: mission selector, active team, current phase, backend
  health, kill-switch state, OpenAI/fallback status, last refresh time, and
  user role.
- Left rail: mission timeline filters for evidence, adaptation, deployment,
  approval, outcome, and lesson events.
- Main workspace: tabbed mission views for `Overview`, `Deployment`,
  `Training Adaptation`, `Operational Twin`, `Roster`, and `AAR`.
- Right inspection drawer: source refs, agent trace, decision-quality details,
  utility estimate, reliance guidance, and raw JSON for debugging.
- Bottom status strip: pending approvals, escalations, failed/fallback agent
  stages, stale evidence warnings, and unresolved controls.

Avoid a marketing-style landing page. The default view should be a usable
mission dashboard with actionable records and review controls.

### Overview Tab

Show a mission-level summary that helps a reviewer decide where attention is
needed first:

- Mission card: `mission_id`, team, phase, mission context summary, terrain,
  weather, constraints, and readiness.
- Readiness strip: platoon readiness, fatigue burden, mission tempo risk,
  situational clarity, cohesion, and leader decision quality.
- Active decisions: latest deployment recommendation, latest adaptation,
  pending roster run, and any operational twin option awaiting decision.
- Risk board: medium/high risk recommendations, critic `modify/escalate/reject`
  counts, safety incidents, near misses, stale-source warnings, and
  `decision_quality.readiness = "escalate"`.
- Evidence freshness: newest processed System 1 observation, oldest source
  used by a live recommendation, missing terrain/weather/readiness indicators,
  and duplicate/conflicting evidence warnings.
- Outcome queue: approved deployment recommendations or twin options that do
  not yet have an outcome/AAR record.

### Deployment Tab

Make the deployment lane the richest workflow because it now has a complete
recommendation lifecycle:

- Request panel: mission context, terrain, weather, readiness, constraints, and
  processed System 1 observations.
- Platoon posture panel: posture, readiness score, risk level, rationale,
  required controls, recommended option ID, and decision status.
- Individual table: soldier ID, posture, readiness, risk, recommended role,
  required controls, evidence refs, and exception badges.
- Option comparison: three option cards side by side with title, option type,
  recommendation, confidence, risk, critic status, critic reasons, utility, and
  reliance posture.
- Control checklist: every required control must be checked, overridden with a
  reason, or escalated before approval.
- Decision panel: approve, reject, escalate, selected option, approved posture,
  actor ID, and required comment.
- Outcome panel: observed outcome summary, commander rating, safety incident,
  near miss, mission effectiveness estimate, accepted/overridden flag,
  helpful/not-helpful flag, missed factor, should-have-escalated flag, and AAR
  notes.
- Lifecycle timeline: recommendation created, decision recorded, outcome
  recorded, lesson drafted, with timestamps and actor IDs.

Status behavior:

- `pending_approval`: show decision controls and block outcome capture.
- `approved`: show approved posture, lock the decision form, and enable outcome
  capture.
- `rejected`: lock outcome capture and keep the record visible for audit.
- `escalated`: route to higher review and do not show a normal approve affordance.
- `outcome_recorded`: show lessons learned and outcome metrics as the primary
  record state.

### Training Adaptation Tab

Use this for instructor-led training changes:

- Cognitive state matrix: all dimensions, current score, priority, trend,
  confidence, and evidence refs.
- Primary failure mode: render prominently with fatigue/load caveats so the UI
  does not imply fixed trait judgments.
- Recommendation cards: inject type, target dimension, proposed inject, risk,
  learning gain, transfer value, safety checks, doctrine refs, and status.
- Approval queue: pending, approved, rejected, and blocked injects with
  rationale and approver.
- AAR link: show how approved injects relate to later deployment outcomes and
  lesson drafts when mission IDs match.

### Operational Twin Tab

Expose this as an advanced evidence and trace view for operators and engineers:

- Evidence bundle: artifacts, observations, policy checks, hash chain, and
  controls.
- State estimate: state vector, uncertainty, state summary, and source refs.
- Scenario options: draft/approved/rejected/escalated status, critic reasons,
  decision quality, utility estimate, and reliance guidance.
- Agent trace: stage, provider, model, status, latency, fallback reason,
  input/output hashes, and validation failures.
- Outcome capture: selected option outcome, instructor rating, safety incident,
  targeted improvement, AAR notes, and lesson draft.

### Roster Tab

Keep this secondary to the live mission workflow:

- Candidate pool and mission context.
- Primary assignment table and second-choice table.
- Fairness audit status, protected-attribute exclusion notes, risk factors, and
  model disagreement.
- Decision quality and reliance guidance.
- Agent-run status if the durable workflow is used.
- Approval panel for roster runs only when `status = "awaiting_approval"`.

### AAR And Lessons Tab

This tab should turn captured outcomes into actionable review material:

- Deployment outcomes grouped by mission and team.
- Operational twin outcomes grouped by source twin run.
- Lesson drafts with severity, category, summary, root cause, recommended
  training delta, and recommended mission delta.
- Outcome quality metrics: commander/instructor rating, helpfulness, accepted
  versus overridden, near misses, safety incidents, and should-have-escalated.
- Export-friendly AAR view with source refs and timestamps.

### Visual System

Use compact, legible controls:

- Badges for `status`, posture, critic status, risk level, confidence, and
  readiness.
- Segmented tabs for major mission views.
- Tables for individual recommendations, roster assignments, source refs, and
  agent traces.
- Cards only for repeated recommendation/option/outcome items.
- Checkboxes for required controls.
- Selects for decision values and approved posture.
- Text areas for rationale, comments, missed factors, and AAR notes.
- Progress bars for readiness, confidence, risk, utility, and uncertainty.
- Alert banners for kill switch, fallback, stale evidence, escalation, and
  missing outcome records.

Do not hide governance details behind decorative UI. The dashboard should make
the review burden clear and fast to inspect.

## Recommended User Flows

### 1. Live Training Adaptation

Use this as the primary hackathon/product workflow.

```text
Instructor creates or selects mission
  -> frontend captures evidence
  -> POST /v1/adaptations
  -> render cognitive/team state
  -> render recommended scenario injects
  -> instructor approves or rejects one inject
  -> POST /v1/adaptations/{adaptation_id}/approval
  -> render approved inject and timeline update
```

The frontend should have these screens or panels:

- Mission timeline: ordered evidence, recommendations, approvals, outcomes.
- Evidence panel: processed System 1 observations, transcripts, OCR text,
  mission context, weather, terrain, AAR, and readiness signals.
- State panel: primary developmental dimension, likely failure mode, estimates.
- Recommendation cards: proposed inject, expected effect, risk, confidence,
  rationale, doctrine refs, safety checks, decision readiness, value of
  information, and reliance posture.
- Approval controls: approve, reject, and rationale text box.
- Trace drawer: source refs, model versions, hashes, generated timestamp.

### 2. Deployment Recommendation

Use this when the mission UI needs a deployment recommendation for an
individual or platoon from already processed evidence.

```text
Commander selects mission
  -> frontend loads processed System 1 observations, mission context, terrain, weather, readiness
  -> POST /v1/deployment-recommendations
  -> render platoon posture, individual postures, options, controls, and trace
  -> POST /v1/deployment-recommendations/{deployment_recommendation_id}/approval
  -> after execution or rehearsal, POST /v1/deployment-recommendations/{deployment_recommendation_id}/outcome
  -> render lesson draft and AAR timeline update
```

The frontend should have these panels:

- Mission context: mission summary, terrain, weather, constraints, readiness.
- Evidence panel: processed System 1 observations, transcripts, OCR text, and
  source refs.
- Deployment posture: `deploy`, `deploy_with_controls`, `hold`, or
  `escalate_review`.
- Individual recommendations: soldier ID, inherited posture, readiness score,
  risk level, required controls, evidence refs.
- Option cards: twin scenario/COA option, risk, confidence, critic status,
  critic reasons, decision quality, utility, and reliance posture.
- Trace drawer: `source_twin_run_id`, source refs, agent trace hashes, fallback
  reasons, and generated timestamps.
- Approval/outcome panel: decision form while pending, outcome/AAR form after
  approval, and lesson draft after outcome capture.

### 3. Roster Scoring

Use this as a secondary or downstream talent workflow.

```text
Frontend sends mission_id and candidate_pool_id
  -> POST /v1/score
  -> render roster, second choices, fairness audit, risk factors, decision quality
```

### 4. Agentic Roster Run

Use this if the frontend needs durable run state and approval for roster
recommendations.

```text
POST /v1/agent-runs
  -> GET /v1/agent-runs/{run_id}
  -> POST /v1/agent-runs/{run_id}/approval
```

## Endpoint Summary

| Method | Path | Purpose | Frontend Use |
|---|---|---|---|
| `GET` | `/v1/healthz` | Health, kill switch, backend selection | startup checks |
| `POST` | `/v1/adaptations` | Estimate cognitive/team state and propose scenario injects | primary live workflow |
| `GET` | `/v1/adaptations/{adaptation_id}` | Fetch a stored adaptation | details/refresh |
| `GET` | `/v1/missions/{mission_id}/adaptations` | List adaptations for a mission | timeline/history |
| `POST` | `/v1/adaptations/{adaptation_id}/approval` | Approve or reject a scenario inject | instructor decision |
| `POST` | `/v1/operational-twin/runs` | Run operational twin and draft governed options | backend/advanced details |
| `GET` | `/v1/operational-twin/runs/{twin_run_id}` | Fetch operational twin run | backend/advanced details |
| `POST` | `/v1/operational-twin/runs/{twin_run_id}/options/{scenario_option_id}/decision` | Record twin option decision | operator decision |
| `POST` | `/v1/operational-twin/runs/{twin_run_id}/outcome` | Capture selected-option outcome and AAR | outcome/AAR capture |
| `POST` | `/v1/deployment-recommendations` | Recommend individual/platoon deployment posture | deployment workflow |
| `GET` | `/v1/deployment-recommendations/{deployment_recommendation_id}` | Fetch deployment recommendation lifecycle | details/refresh |
| `GET` | `/v1/missions/{mission_id}/deployment-recommendations` | List deployment recommendation history | timeline/history |
| `POST` | `/v1/deployment-recommendations/{deployment_recommendation_id}/approval` | Approve, reject, or escalate deployment recommendation | commander decision |
| `POST` | `/v1/deployment-recommendations/{deployment_recommendation_id}/outcome` | Capture deployment outcome and AAR | outcome/AAR capture |
| `POST` | `/v1/score` | Direct roster scoring | downstream roster view |
| `POST` | `/v1/agent-runs` | Durable roster recommendation workflow | roster approval workflow |
| `GET` | `/v1/agent-runs/{run_id}` | Fetch roster agent run | polling/details |
| `POST` | `/v1/agent-runs/{run_id}/approval` | Approve or reject roster recommendation | commander/instructor decision |
| `POST` | `/v1/context/chunks` | Ingest retrievable SOP/doctrine/context text | admin/data setup |
| `POST` | `/v1/graph/facts` | Ingest derived graph facts | admin/data setup |
| `POST` | `/admin/disable` | Disable scoring | protected admin |
| `POST` | `/admin/enable` | Re-enable scoring | protected admin |

Compatibility aliases also exist:

- `GET /health`
- `POST /score`

Prefer the `/v1/*` routes in new frontend code.

## Dashboard Data Loading

Use one mission-scoped loader that hydrates the dashboard shell, then lazy-load
dense detail drawers.

Initial mission load:

```text
GET /v1/healthz
GET /v1/missions/{mission_id}/deployment-recommendations?limit=20
GET /v1/missions/{mission_id}/adaptations?limit=20
optional: POST /v1/score when the roster tab is opened
optional: GET /v1/agent-runs/{run_id} when a roster run is selected
```

On deployment selection:

```text
GET /v1/deployment-recommendations/{deployment_recommendation_id}
GET /v1/operational-twin/runs/{source_twin_run_id}
```

On adaptation selection:

```text
GET /v1/adaptations/{adaptation_id}
```

Polling and refresh:

- Poll health every 15-30 seconds in operator views.
- Refresh mission deployment/adaptation lists after any create, approval, or
  outcome action.
- Avoid polling long historical records; refresh selected details only when the
  user opens the drawer or performs an action.
- Preserve the selected tab, selected recommendation, and drawer state across
  refreshes.

Derived dashboard state:

- `pendingApprovals`: deployment recommendations with `pending_approval`,
  adaptations with `pending_approval`, agent runs with `awaiting_approval`, and
  twin options with `draft`.
- `needsOutcome`: deployment recommendations with `approved` and no outcomes,
  plus operational twin runs with approved options and no outcomes.
- `escalations`: deployment recommendations with `escalated`, posture
  `escalate_review`, critic `escalate/reject`, or decision quality
  `escalate`.
- `fallbacks`: agent trace rows where `status` is `fallback` or `failed`.
- `reviewRisks`: medium/high risk level, low confidence, high uncertainty,
  missing source refs, stale evidence, near misses, and safety incidents.

Frontend state should store IDs, not copies of mutable records. Re-fetch by ID
after approval/outcome so the UI uses the backend lifecycle record.

## Cognitive Adaptation

### Create Adaptation

```http
POST /v1/adaptations
Content-Type: application/json
```

Minimal request:

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
      "text": "Soldier handled direct contact, but missed the second-order relationship between terrain, timing, support, civilian movement, and a delayed comms relay under moderate fatigue.",
      "soldier_ids": ["RGR-0001"],
      "tags": ["systems_thinking", "fatigue"],
      "metrics": {
        "sleep_hours": 4.5
      }
    }
  ]
}
```

Full request shape:

```json
{
  "mission_id": "raid-tonight",
  "instructor_id": "instructor-1",
  "team_id": "alpha",
  "target_soldier_ids": ["RGR-0001", "RGR-0002"],
  "phase": "Mountain",
  "evidence": [
    {
      "evidence_id": "obs-001",
      "source_type": "voice_note",
      "text": "Observation text, transcript, OCR result, checklist note, weather note, terrain note, or AAR excerpt.",
      "observed_at": "2026-05-03T18:30:00Z",
      "soldier_ids": ["RGR-0001"],
      "team_id": "alpha",
      "task_code": "PATROL-LEAD",
      "tags": ["systems_thinking", "fatigue"],
      "metrics": {
        "sleep_hours": 4.5,
        "cognitive_load": 0.7
      },
      "source_ref": "postgres://training_observations_current/obs-001"
    }
  ],
  "constraints": {
    "max_safety_risk": "medium",
    "allow_environmental_stress": true,
    "blocked_inject_types": []
  },
  "require_human_approval": true
}
```

Allowed `source_type` values:

- `voice_note`
- `transcript`
- `ocr_text`
- `checklist`
- `patrol_summary`
- `aar`
- `weather`
- `terrain`
- `structured_event`

Allowed cognitive dimensions:

- `sensemaking`
- `critical_thinking`
- `systems_thinking`
- `leadership_communication`
- `execution_reliability`
- `cognitive_load`
- `sleep_fatigue`
- `nutrition_strain`
- `team_trust`

Useful `metrics` keys:

| Metric | Meaning | Range |
|---|---|---|
| `sensemaking` | Direct score for sensemaking evidence | `0.0` to `1.0` |
| `critical_thinking` | Direct score for critical-thinking evidence | `0.0` to `1.0` |
| `systems_thinking` | Direct score for systems-thinking evidence | `0.0` to `1.0` |
| `leadership_communication` | Direct communication score | `0.0` to `1.0` |
| `execution_reliability` | Direct execution reliability score | `0.0` to `1.0` |
| `team_trust` | Direct team trust/cohesion score | `0.0` to `1.0` |
| `sleep_hours` | Hours slept before or during event window | positive number |
| `hours_awake` | Hours awake before observation | positive number |
| `cognitive_load` | Load/stress value, where higher means more load | `0.0` to `1.0` |
| `stress_level` | Alias for cognitive load | `0.0` to `1.0` |
| `hydration` | Hydration readiness | `0.0` to `1.0` |
| `nutrition` | Nutrition readiness | `0.0` to `1.0` |

Frontend capture guidance:

- Generate a stable `evidence_id` client-side if the backend source does not
  provide one yet.
- Put processed text in `text`. Audio/image ingestion and STT/OCR belong to
  System 1; System 2 should receive the resulting transcript, OCR text,
  observation, or summary with a `source_ref`.
- Use `source_ref` when evidence already exists in a shared store.
- Keep evidence records small. One observation per evidence object is easier to
  trace and replay than one large blob.

### Adaptation Response

Important top-level fields:

```json
{
  "adaptation_id": "adapt-...",
  "mission_id": "raid-tonight",
  "team_id": "alpha",
  "status": "pending_approval",
  "state": {},
  "recommendations": [],
  "blocked_recommendations": [],
  "trace": {},
  "approval_required": true
}
```

Statuses:

- `pending_approval`: scenario options are ready for instructor decision.
- `completed`: approval is not required or an inject was approved.
- `rejected`: instructor rejected the selected inject.
- `failed`: reserved for future persistence/workflow failure handling.

State fields:

```json
{
  "snapshot_id": "state-...",
  "mission_id": "raid-tonight",
  "team_id": "alpha",
  "target_soldier_ids": ["RGR-0001"],
  "primary_development_dimension": "systems_thinking",
  "likely_failure_mode": "Likely failure mode is missing second-order relationships across people, terrain, timing, or support. Fatigue or cognitive load is elevated, so avoid interpreting this as fixed talent.",
  "state_summary": "Primary developmental target is systems thinking with medium confidence...",
  "generated_at": "2026-05-03T18:30:00Z",
  "estimates": []
}
```

Each estimate:

```json
{
  "dimension": "systems_thinking",
  "current_score": 0.16,
  "development_priority": 0.73,
  "confidence": "medium",
  "trend": "stable",
  "rationale": "Evidence indicates a near-term developmental weakness in systems thinking.",
  "evidence_refs": ["evidence://voice_note/obs-001"]
}
```

Recommendation card fields:

```json
{
  "recommendation_id": "scenario-...",
  "title": "Target the weak cognitive dimension directly",
  "inject_type": "direct_pressure",
  "target_dimension": "systems_thinking",
  "proposed_inject": "Delay the comms relay while adding civilian movement on the flank and a timing change for support.",
  "expected_developmental_effect": "Develops the currently weakest cognitive dimension under realistic mission pressure.",
  "rationale": "Adaptive training should target measured understanding instead of raising difficulty blindly.",
  "doctrine_refs": [
    "ADP 6-22 Army Leadership Requirements Model",
    "FM 7-0 training assessment and AAR cycle"
  ],
  "evidence_refs": ["evidence://voice_note/obs-001"],
  "safety_checks": [
    "Instructor approval required before execution."
  ],
  "risk_level": "medium",
  "safety_risk": 0.44,
  "fatigue_risk": 0.48,
  "unfair_exposure_risk": 0.18,
  "expected_learning_gain": 0.78,
  "transfer_value": 0.58,
  "confidence": "medium",
  "status": "pending_approval",
  "block_reason": null
}
```

Recommendation statuses:

- `pending_approval`: render approve/reject controls.
- `blocked`: render as disabled with `block_reason`; do not show approve.

Inject types:

- `direct_pressure`: targets the weak dimension under mission pressure.
- `skill_isolation`: lower-noise repetition to separate skill gap from fatigue
  or overload.
- `transfer_test`: tests whether the skill transfers to a different tactical
  context.

Trace fields:

```json
{
  "model_versions": {
    "cognitive_state_estimator": "deterministic-cognitive-state-0.1",
    "scenario_director": "deterministic-scenario-director-0.1",
    "safety_doctrine_auditor": "deterministic-safety-doctrine-auditor-0.1"
  },
  "feature_hash": "sha256:...",
  "prompt_hash": "...",
  "seed": 0,
  "generated_at": "2026-05-03T18:30:00Z",
  "dod_ai_principles": {},
  "source_refs": [],
  "input_source_hashes": {}
}
```

Render trace data in a drawer or details panel. It is too dense for the main
card, but it is important for instructor trust and audit.

### Fetch Adaptation

```http
GET /v1/adaptations/{adaptation_id}
```

Use this after page refresh, from mission timeline cards, and after approval to
reload the updated adaptation status. A missing adaptation returns `404`.

### List Mission Adaptations

```http
GET /v1/missions/{mission_id}/adaptations?limit=50
```

Use this to build a mission timeline or history panel. The response is an array
of `CognitiveAdaptationResponse` objects ordered newest first by the repository.
The optional `limit` defaults to `50`.

### Approve Or Reject Scenario Inject

```http
POST /v1/adaptations/{adaptation_id}/approval
Content-Type: application/json
```

Request:

```json
{
  "recommendation_id": "scenario-...",
  "decision": "approved",
  "approver_id": "instructor-1",
  "rationale": "Targets systems thinking without increasing general difficulty."
}
```

Use `decision: "rejected"` for rejection.

Response:

```json
{
  "adaptation_id": "adapt-...",
  "recommendation_id": "scenario-...",
  "status": "completed",
  "decision": "approved",
  "approved_inject": {
    "recommendation_id": "scenario-...",
    "title": "Target the weak cognitive dimension directly"
  },
  "decided_at": "2026-05-03T18:35:00Z"
}
```

Frontend behavior:

- Disable approval buttons while the request is pending.
- Require a non-empty rationale before sending approval or rejection.
- On approval, add the inject to the mission timeline.
- On rejection, keep the recommendation visible and mark it rejected in the UI.
- If approval returns `404`, the `adaptation_id` is invalid or points to a
  backend repository that does not contain the adaptation.

## Deployment Recommendations

### Create Deployment Recommendation

```http
POST /v1/deployment-recommendations
Content-Type: application/json
```

Minimal request:

```json
{
  "mission_id": "deployment-demo",
  "requester_id": "commander-1",
  "team_id": "platoon-alpha",
  "scope": "platoon",
  "target_soldier_ids": ["RGR-0001", "RGR-0002"],
  "mission_context": "Move platoon to observation position with time-sensitive support coordination.",
  "terrain": "Rough wooded draw with constrained visibility.",
  "weather": {
    "condition": "cold wind",
    "temperature_c": 3,
    "wind_speed": 18
  },
  "readiness": {
    "sleep_hours": 4.2,
    "hydration": 0.62
  },
  "processed_observations": [
    {
      "kind": "system1_observation",
      "content": "System 1 observation: missed two comms acknowledgements; leader recovered after terrain-support timing cue.",
      "metadata": {
        "observation_id": "s1-obs-001"
      }
    }
  ],
  "constraints": ["no avoidable fatigue load"],
  "require_human_approval": true
}
```

Use `scope: "individual"` for a single-soldier recommendation surface and
`scope: "platoon"` for team-level posture. `processed_observations` accepts the
same processed `ArtifactInput` kinds as the operational twin, such as
`system1_observation`, `transcript`, `ocr_text`, `telemetry`, and
`manual_note`. Raw media should already have been processed by System 1.

Important response fields:

```json
{
  "deployment_recommendation_id": "deploy-...",
  "mission_id": "deployment-demo",
  "team_id": "platoon-alpha",
  "scope": "platoon",
  "status": "pending_approval",
  "source_twin_run_id": "twin-...",
  "platoon_recommendation": {
    "posture": "deploy_with_controls",
    "readiness_score": 0.66,
    "risk_level": "medium",
    "required_controls": []
  },
  "individual_recommendations": [],
  "options": [],
  "decisions": [],
  "outcomes": [],
  "lessons_learned": [],
  "agent_trace": [],
  "source_refs": [],
  "decision_quality": {},
  "utility_estimate": {},
  "reliance_guidance": {}
}
```

Posture values:

- `deploy`: evidence supports deployment with normal human review.
- `deploy_with_controls`: deployment may proceed only after listed controls.
- `hold`: readiness or fatigue indicates the element should not deploy as-is.
- `escalate_review`: the evidence or option set needs higher-level review.

### Deployment Lifecycle Actions

Fetch or refresh a stored recommendation:

```http
GET /v1/deployment-recommendations/{deployment_recommendation_id}
```

List a mission history:

```http
GET /v1/missions/{mission_id}/deployment-recommendations?limit=50
```

Record a decision:

```http
POST /v1/deployment-recommendations/{deployment_recommendation_id}/approval
Content-Type: application/json
```

```json
{
  "decision": "approved",
  "actor_id": "commander-1",
  "selected_option_id": "option-...",
  "approved_posture": "deploy_with_controls",
  "comment": "Approved after reviewing controls, critic reasons, and source refs."
}
```

Use `decision: "rejected"` when the recommendation should not be used. Use
`decision: "escalated"` when the evidence, risk, or command context requires
higher review. The UI should require a comment for every decision.

Record the outcome/AAR after an approved recommendation:

```http
POST /v1/deployment-recommendations/{deployment_recommendation_id}/outcome
Content-Type: application/json
```

```json
{
  "observed_outcome_summary": "Platoon completed the movement with controls and no safety incident.",
  "commander_rating": 4,
  "safety_incident": false,
  "near_miss": false,
  "mission_effectiveness_estimate": 0.35,
  "recommendation_accepted": true,
  "recommendation_helpful": true,
  "missed_factor": null,
  "should_have_escalated": false,
  "aar_notes": "Comms confirmation before movement prevented a repeat relay error.",
  "actor_id": "commander-1"
}
```

Frontend behavior:

- Treat `pending_approval` as non-final.
- Render `required_controls` as blocking checklist items before any downstream
  approval action.
- Approve only one selected option. Use `platoon_recommendation.recommended_option_id`
  as the default selected option, but let the reviewer choose another option
  with rationale.
- When approving `deploy_with_controls`, require each control to be checked or
  explicitly overridden with rationale.
- When the posture is `hold` or `escalate_review`, route the decision form to
  reject/escalate-first behavior instead of a single-click approval.
- After a named decision, refresh the record with
  `GET /v1/deployment-recommendations/{deployment_recommendation_id}`.
- Use the outcome endpoint after execution or rehearsal AAR to capture
  commander rating, safety/near-miss signals, recommendation usefulness, and
  missed factors.
- Show `source_twin_run_id` so operators can open the underlying twin evidence.
- Keep all three `options` visible, including critic-modified or escalated
  options, because they explain the posture.
- Render `reliance_guidance.human_accountability` near the final action button.

## Roster Scoring

### Direct Score

```http
POST /v1/score
Content-Type: application/json
```

ID-only operational request:

```json
{
  "mission_id": "raid-tonight",
  "candidate_pool_id": "pool-2026-05-02-a"
}
```

Local synthetic request:

```json
{
  "mission_id": "raid-tonight",
  "candidate_count": 80,
  "seed": 42
}
```

Explicit request shape:

```json
{
  "mission_id": "raid-tonight",
  "candidate_pool_id": "pool-2026-05-02-a",
  "candidate_count": 80,
  "seed": 42,
  "candidates": [],
  "roles": [
    {
      "slot_id": "MED-1",
      "role": "medic",
      "required_mos": "68W",
      "min_acft": 450
    }
  ]
}
```

Allowed `role` values:

- `team_leader`
- `assistant_team_leader`
- `breacher`
- `medic`
- `marksman`
- `comms`
- `assaulter`

When `CANDIDATE_POOL_BACKEND=postgres`, a missing `candidate_pool_id` returns
HTTP `422`. When local mode is active, the backend can fall back to synthetic
candidates and marks that fallback in `trace.source_refs`.

Important response fields:

- `roster`: primary assignments.
- `second_choice_roster`: backup assignments.
- `fairness_audit`: pass/halt status and fairness metrics.
- `career_forecast`: five-year forecast for the top selected candidate.
- `trace.source_refs`: mission, candidate, role, retrieval, and graph refs.

Each roster item includes:

```json
{
  "slot_id": "MED-1",
  "role": "medic",
  "soldier_id": "RGR-0001",
  "fit_score": 0.81,
  "p_success_tabpfn": 0.79,
  "p_success_bayes_mean": 0.83,
  "model_disagreement": 0.04,
  "p_success_bayes_ci": [0.73, 0.9],
  "narrative": "...",
  "key_strengths": [],
  "risk_factors": [],
  "second_choice_id": "RGR-0007",
  "confidence": "high"
}
```

Roster UI guidance:

- Show primary and second-choice assignments side by side.
- Highlight `confidence = "low"` or high `model_disagreement`.
- Show `fairness_audit.status`. If it is `halt`, prevent final approval in the
  frontend until a human resolves the issue.
- Always show that the roster scorer is advisory.

## Agentic Roster Workflow

Use this when the frontend needs durable roster workflow state and explicit
approval.

### Create Agent Run

```http
POST /v1/agent-runs
Content-Type: application/json
```

Request:

```json
{
  "objective": "mission_roster_recommendation",
  "score_request": {
    "mission_id": "raid-tonight",
    "candidate_pool_id": "pool-2026-05-02-a"
  },
  "require_human_approval": true
}
```

Response top-level fields:

- `run_id`
- `status`: `queued`, `running`, `awaiting_approval`, `completed`, `rejected`,
  or `failed`
- `steps`
- `recommendation`
- `approval`
- `error`

If `status = "awaiting_approval"`, show the recommendation and approval form.

### Fetch Agent Run

```http
GET /v1/agent-runs/{run_id}
```

Use this for refresh or details screens. If the response is `404`, the run is
unknown to the current backend repository.

### Approve Or Reject Agent Run

```http
POST /v1/agent-runs/{run_id}/approval
Content-Type: application/json
```

Request:

```json
{
  "decision": "approved",
  "approver_id": "commander-1",
  "rationale": "Reviewed recommendation, fairness audit, and second choices."
}
```

Possible `decision` values:

- `approved`
- `rejected`

## Context And Graph Setup

These routes are normally admin/data setup routes, not high-frequency UI routes.

### Ingest Context Chunks

```http
POST /v1/context/chunks
Content-Type: application/json
```

Request:

```json
{
  "chunks": [
    {
      "chunk_id": "sop-roster-001",
      "source": "unit-sop",
      "title": "Roster review policy",
      "content": "Roster recommendations require human approval.",
      "metadata": {
        "entity_type": "policy",
        "actor_id": "operator-1",
        "reason": "Initial SOP load."
      },
      "embedding": null
    }
  ]
}
```

Response:

```json
{
  "backend": "local",
  "chunk_count": 1,
  "chunk_ids": ["sop-roster-001"]
}
```

Embeddings must be generated outside this service. If an embedding is provided,
it should match the configured pgvector dimension.

### Ingest Graph Facts

```http
POST /v1/graph/facts
Content-Type: application/json
```

Request:

```json
{
  "facts": [
    {
      "subject": "raid-tonight",
      "predicate": "requires_role",
      "object": "medic",
      "metadata": {
        "fact_id": "fact-raid-tonight-medic",
        "actor_id": "operator-1",
        "reason": "Mission role setup."
      }
    }
  ]
}
```

Response:

```json
{
  "backend": "local",
  "fact_count": 1
}
```

## Health And Admin

### Health

```http
GET /v1/healthz
```

Response:

```json
{
  "status": "ok",
  "version": "0.1.0",
  "disabled": false,
  "infrastructure": {
    "postgres": {
      "configured": true,
      "url": "postgresql://app_user:***@pgbouncer.internal:6432/system2",
      "pgvector_enabled": true,
      "pgvector_url": "postgresql://app_user:***@pgbouncer.internal:6432/system2"
    },
    "redis": {
      "configured": true,
      "url": "redis://redis.internal:6379/0"
    },
    "falkordb": {
      "configured": true,
      "url": "redis://falkordb.internal:6379"
    },
    "audit_log_path": "/tmp/system2_audit.jsonl",
    "backends": {
      "audit": "postgres",
      "agent_repository": "postgres",
      "agent_state": "redis",
      "candidate_pool": "postgres",
      "retrieval": "pgvector",
      "graph": "falkordb",
      "shared_data": "postgres"
    }
  }
}
```

Frontend behavior:

- If `disabled` is true, disable scoring and approval controls and show an
  operational banner.
- Show backend status in an operator diagnostics panel, not in the normal
  instructor view.

### Disable Or Enable

```http
POST /admin/disable
POST /admin/enable
```

These routes are not self-authenticating. They must be protected before any
shared environment or public frontend uses them.

## Error Handling

Common statuses:

| Status | Meaning | Frontend Behavior |
|---|---|---|
| `200` | Success | render response |
| `404` | Unknown `run_id` or `adaptation_id` | show not found and offer refresh/recreate |
| `409` | Invalid approval state or recommendation ID | keep form open and show message |
| `422` | Validation error or invalid request | show field errors or backend detail |
| `423` | Selection engine disabled | disable action and show kill-switch banner |

FastAPI validation errors use the standard shape:

```json
{
  "detail": [
    {
      "type": "missing",
      "loc": ["body", "mission_id"],
      "msg": "Field required",
      "input": {}
    }
  ]
}
```

Backend-raised request errors often use:

```json
{
  "detail": "candidate_pool_id 'missing-pool' was not found for mission 'raid-tonight'"
}
```

## Suggested TypeScript Types

These are intentionally partial. Generate exact types from `/openapi.json` when
you wire the frontend build.

```ts
type Confidence = "high" | "medium" | "low";
type Decision = "approved" | "rejected";
type RiskLevel = "low" | "medium" | "high";
type InjectType = "direct_pressure" | "skill_isolation" | "transfer_test";
type DeploymentScope = "individual" | "platoon";
type DeploymentPosture =
  | "deploy"
  | "deploy_with_controls"
  | "hold"
  | "escalate_review";

type CognitiveDimension =
  | "sensemaking"
  | "critical_thinking"
  | "systems_thinking"
  | "leadership_communication"
  | "execution_reliability"
  | "cognitive_load"
  | "sleep_fatigue"
  | "nutrition_strain"
  | "team_trust";

type EvidenceSourceType =
  | "voice_note"
  | "transcript"
  | "ocr_text"
  | "checklist"
  | "patrol_summary"
  | "aar"
  | "weather"
  | "terrain"
  | "structured_event";

interface TrainingEvidence {
  evidence_id: string;
  source_type: EvidenceSourceType;
  text: string;
  observed_at?: string;
  soldier_ids?: string[];
  team_id?: string | null;
  task_code?: string | null;
  tags?: string[];
  metrics?: Record<string, number>;
  source_ref?: string | null;
}

interface CognitiveAdaptationRequest {
  mission_id: string;
  instructor_id: string;
  team_id: string;
  target_soldier_ids?: string[];
  phase?: string | null;
  evidence: TrainingEvidence[];
  constraints?: {
    max_safety_risk?: RiskLevel;
    allow_environmental_stress?: boolean;
    blocked_inject_types?: InjectType[];
  };
  require_human_approval?: boolean;
}

interface ScenarioInjectRecommendation {
  recommendation_id: string;
  title: string;
  inject_type: InjectType;
  target_dimension: CognitiveDimension;
  proposed_inject: string;
  expected_developmental_effect: string;
  rationale: string;
  doctrine_refs: string[];
  evidence_refs: string[];
  safety_checks: string[];
  risk_level: RiskLevel;
  safety_risk: number;
  fatigue_risk: number;
  unfair_exposure_risk: number;
  expected_learning_gain: number;
  transfer_value: number;
  confidence: Confidence;
  status: "pending_approval" | "blocked";
  block_reason?: string | null;
}

interface DeploymentRecommendationRequest {
  mission_id: string;
  requester_id: string;
  team_id: string;
  scope?: DeploymentScope;
  target_soldier_ids?: string[];
  mission_context: string;
  terrain?: string | null;
  weather?: Record<string, unknown>;
  readiness?: Record<string, unknown>;
  processed_observations?: Array<{
    kind: string;
    content: string;
    artifact_id?: string | null;
    source_system?: string | null;
    metadata?: Record<string, unknown>;
  }>;
  constraints?: string[];
  require_human_approval?: boolean;
}

interface DeploymentRecommendationResponse {
  deployment_recommendation_id: string;
  mission_id: string;
  team_id: string;
  scope: DeploymentScope;
  status:
    | "pending_approval"
    | "approved"
    | "rejected"
    | "escalated"
    | "completed"
    | "outcome_recorded";
  source_twin_run_id: string;
  platoon_recommendation: {
    team_id: string;
    posture: DeploymentPosture;
    readiness_score: number;
    risk_level: RiskLevel;
    recommended_option_id?: string | null;
    rationale: string;
    required_controls: string[];
    evidence_refs: string[];
  };
  individual_recommendations: Array<{
    soldier_id: string;
    posture: DeploymentPosture;
    readiness_score: number;
    risk_level: RiskLevel;
    recommended_role?: string | null;
    rationale: string;
    required_controls: string[];
    evidence_refs: string[];
  }>;
  decisions: Array<{
    decision_id: string;
    selected_option_id?: string | null;
    actor_id: string;
    decision: "approved" | "rejected" | "escalated";
    approved_posture?: DeploymentPosture | null;
    comment: string;
    timestamp_utc: string;
  }>;
  outcomes: Array<{
    outcome_id: string;
    selected_option_id?: string | null;
    observed_outcome_summary: string;
    commander_rating: number;
    safety_incident: boolean;
    near_miss: boolean;
    mission_effectiveness_estimate: number;
    recommendation_accepted: boolean;
    recommendation_helpful: boolean;
    missed_factor?: string | null;
    should_have_escalated: boolean;
    recorded_at_utc: string;
  }>;
  lessons_learned: Array<Record<string, unknown>>;
}

interface DeploymentApprovalResponse {
  deployment_recommendation_id: string;
  status: "approved" | "rejected" | "escalated";
  decision: {
    decision_id: string;
    selected_option_id?: string | null;
    actor_id: string;
    decision: "approved" | "rejected" | "escalated";
    approved_posture?: DeploymentPosture | null;
    comment: string;
    timestamp_utc: string;
  };
  lesson_learned?: Record<string, unknown> | null;
  decided_at_utc: string;
}
```

## Suggested Fetch Wrapper

```ts
async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const apiKey = import.meta.env.VITE_SYSTEM2_API_KEY;
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      "content-type": "application/json",
      ...(apiKey ? {"x-api-key": apiKey} : {}),
      ...(init?.headers ?? {}),
    },
  });

  const body = await response.json().catch(() => null);

  if (!response.ok) {
    const message =
      typeof body?.detail === "string"
        ? body.detail
        : `Request failed with status ${response.status}`;
    throw new Error(message);
  }

  return body as T;
}
```

Create an adaptation:

```ts
const adaptation = await apiFetch<CognitiveAdaptationResponse>("/v1/adaptations", {
  method: "POST",
  body: JSON.stringify(request),
});
```

Create a deployment recommendation:

```ts
const deployment = await apiFetch<DeploymentRecommendationResponse>(
  "/v1/deployment-recommendations",
  {
    method: "POST",
    body: JSON.stringify(deploymentRequest),
  },
);
```

Approve a deployment recommendation:

```ts
const deploymentApproval = await apiFetch<DeploymentApprovalResponse>(
  `/v1/deployment-recommendations/${deployment.deployment_recommendation_id}/approval`,
  {
    method: "POST",
    body: JSON.stringify({
      decision: "approved",
      actor_id: currentUser.id,
      selected_option_id: deployment.platoon_recommendation.recommended_option_id,
      comment: approvalComment,
    }),
  },
);
```

Approve an inject:

```ts
const approval = await apiFetch<ScenarioApprovalResponse>(
  `/v1/adaptations/${adaptation.adaptation_id}/approval`,
  {
    method: "POST",
    body: JSON.stringify({
      recommendation_id: selectedRecommendationId,
      decision: "approved",
      approver_id: currentUserId,
      rationale,
    }),
  },
);
```

## UI Mapping

### Adaptation State Panel

Render:

- `state.primary_development_dimension`
- `state.state_summary`
- `state.likely_failure_mode`
- table of `state.estimates`

For each estimate:

- dimension label
- score bar from `current_score`
- priority bar from `development_priority`
- confidence badge
- evidence count from `evidence_refs.length`

### Recommendation Cards

Render:

- title
- inject type badge
- target dimension
- proposed inject
- expected developmental effect
- risk level and numeric risks
- confidence
- rationale
- doctrine refs
- safety checks
- evidence refs

Button logic:

- If `status = "blocked"`, disable approve and show `block_reason`.
- If `approval_required = true`, show approve/reject buttons.
- Require rationale before either decision.

### Deployment Posture Panel

Render:

- `platoon_recommendation.posture`
- `readiness_score` and `risk_level`
- `recommended_option_id`
- `required_controls`
- `individual_recommendations`
- `options` with critic status and reasons
- `decision_quality`, `utility_estimate`, and `reliance_guidance`
- `decisions`, `outcomes`, and `lessons_learned` as the lifecycle record

Button logic:

- If `status = "pending_approval"`, require named human review before
  downstream execution.
- If `status = "approved"`, lock the decision form and show the outcome/AAR
  form.
- If `status = "rejected"` or `status = "escalated"`, lock outcome capture and
  keep the record visible in timeline/history views.
- If `status = "outcome_recorded"`, show the outcome and lesson draft before
  the original recommendation.
- If posture is `hold` or `escalate_review`, block any simple approve action
  and route to the escalation workflow.
- If posture is `deploy_with_controls`, require every control to be checked or
  explicitly overridden with rationale.

### Dashboard Alert Rules

Show prominent alerts when any of these are true:

- `health.disabled = true`
- any agent trace has `status = "fallback"` or `status = "failed"`
- `decision_quality.readiness = "escalate"`
- `reliance_guidance.posture = "defer_for_more_info"` or `"escalate"`
- deployment posture is `hold` or `escalate_review`
- critic status is `escalate` or `reject`
- source refs are missing for a recommendation
- a deployment recommendation is `approved` but has no outcome
- an outcome has `safety_incident`, `near_miss`, or `should_have_escalated`

### Timeline

Recommended timeline event types:

- evidence captured
- adaptation generated
- deployment recommendation generated
- recommendation blocked
- recommendation approved
- recommendation rejected
- scenario injected
- outcome captured
- AAR summary created

## Privacy And Governance

For hackathon/demo:

- Use synthetic or sanitized records.
- Avoid raw PII in visible screenshots.
- Keep fatigue, reflection, and coaching notes in the developmental UI lane.
- Do not label soldiers as permanently weak. Render state as current,
  evidence-based, and uncertain.

For production:

- Add user identity and role-based access control at the gateway or BFF layer.
- Add retention and purpose limitations for developmental evidence.
- Add explicit frontend affordances for source refs, uncertainty, and human
  approval.
- Render readiness `escalate` and reliance posture `escalate` as blocking UI
  states for final approval until an authorized human resolves the issue.

## Known Backend Gaps The Frontend Must Handle

- Built-in auth is API-key only; it does not provide user identity, roles, or
  per-mission authorization.
- Training and deployment enrichment depends on shared projections being
  populated. Retrieval and graph context can create bounded scoring adjustments,
  but broader weather, terrain, qualification, and unit-history transforms are
  still future work.
- Individual deployment recommendations currently inherit the team readiness
  posture unless the request and future model path provide soldier-specific
  readiness evidence.

## Minimum Frontend Demo Cut

Build this first:

1. Mission header with `mission_id`, `team_id`, instructor ID, and status.
2. Evidence form with source type, text, tags, soldier IDs, and metrics.
3. Submit to `POST /v1/adaptations`.
4. State panel showing primary dimension and estimates.
5. Three recommendation cards plus blocked cards.
6. Approval modal with rationale.
7. Timeline event after approval.
8. Trace drawer with source refs and model versions.

This shows the core product: the system helps instructors detect weak signals,
choose targeted scenario pressure, keep a human gate, and preserve evidence for
AAR and later lessons learned.
