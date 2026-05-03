# Frontend Integration Guide

This document is the frontend contract for System 2, the Cognitive Mission
Adaptation Engine. It explains how a frontend should call the API, what data it
should send, what it should render, and which backend limitations it must handle
explicitly.

System 2 has two lanes:

- Developmental lane: live field evidence goes to `/v1/adaptations`, the
  backend estimates cognitive/team state, proposes scenario injects, and waits
  for instructor approval.
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
- Keep instructor approval explicit. The frontend must never auto-approve a
  scenario inject or roster decision.
- Treat developmental evidence as sensitive. Do not expose raw reflective notes,
  fatigue comments, or protected attributes beyond users with a need to know.
- Preserve the returned `adaptation_id` for refresh and timeline lookups.

The frontend should not:

- Assemble full soldier records for normal integrated operation.
- Treat recommendations as final orders.
- Treat synthetic fallback data as operational truth.
- Hide blocked recommendations; blocked cards are useful safety evidence.

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
- Evidence panel: voice notes, OCR text, checklist items, weather, terrain, AAR.
- State panel: primary developmental dimension, likely failure mode, estimates.
- Recommendation cards: proposed inject, expected effect, risk, confidence,
  rationale, doctrine refs, safety checks.
- Approval controls: approve, reject, and rationale text box.
- Trace drawer: source refs, model versions, hashes, generated timestamp.

### 2. Roster Scoring

Use this as a secondary or downstream talent workflow.

```text
Frontend sends mission_id and candidate_pool_id
  -> POST /v1/score
  -> render roster, second choices, fairness audit, risk factors
```

### 3. Agentic Roster Run

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
- Put raw text in `text`. For audio or images, first send a transcript/OCR text
  result or a short instructor-entered summary.
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

### Timeline

Recommended timeline event types:

- evidence captured
- adaptation generated
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

## Known Backend Gaps The Frontend Must Handle

- Built-in auth is API-key only; it does not provide user identity, roles, or
  per-mission authorization.
- Direct `/v1/score` does not yet write `decision_snapshots`.
- Kill-switch changes do not yet write `entity_update_events`.
- Training observation and deployment outcome enrichment require shared
  projections to be populated and further wired into scoring features.

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
