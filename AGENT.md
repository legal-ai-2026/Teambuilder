# AGENT.md - Cognitive Mission Adaptation and Deployment Engine

This file is the entry point for AI coding agents working in this repository.
Read it before making changes and keep it aligned with the actual service.

## What This Project Is

**Spire System 2** is an operational backend service for cognitive mission
adaptation, deployment recommendation, and downstream roster scoring. It
estimates cognitive/team state from processed field evidence, drafts
instructor-approved scenario injects, recommends individual or platoon
deployment posture from processed System 1 outputs plus mission context,
terrain, weather, and readiness, and keeps the existing talent scoring lane for
mission roster slots.

The service is advisory. It produces recommendations for commanders and career
managers; it does not publish orders, replace command judgment, or act as a
system of record.

## What This Project Is Not

- Not a hiring, accession, separation, or disciplinary tool.
- Not a free-form Q&A or chat interface.
- Not a frontend application.
- Not a speech-to-text, OCR, raw media extraction, or KG-ingest service. System
  1 owns those steps; System 2 consumes the processed outputs.
- Not an autonomous deployment authority.
- Not a personnel-policy authority. Do not make doctrine claims without a
  controlling source.

## Current Package

The operational package is `src/system2/`.

It intentionally keeps model adapters dependency-light and deterministic so the
service can run in restricted environments while preserving the production API
contract:

- TabPFN-compatible probability estimate
- hierarchical-Bayes-compatible pooled estimate
- disagreement-aware blending
- Hungarian role assignment
- second-choice roster generation
- fairness audit
- narrative explanation
- five-year career forecast
- operational twin agent loop
- individual/platoon deployment recommendation wrapper
- decision-quality, utility, and reliance guidance
- append-only hash-chained audit log
- API kill switch

Heavier adapters may replace the deterministic implementations behind the same
contracts, but endpoint behavior, fairness outputs, uncertainty outputs, trace
metadata, and tests must stay green.

## Read Before Substantial Changes

1. `README.md` - run instructions and API examples.
2. `docs/architecture.md` - operational flow, contracts, and governance.
3. `docs/implementation.md` - module inventory, runbook, and test commands.
4. `assets/feature-spec.md` - feature dictionary. Update it in the same commit
   as any feature contract change.

## Run Locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn system2.api:app --reload --port 8002
```

Open `http://127.0.0.1:8002/docs`.

Example score request:

```bash
curl -X POST http://127.0.0.1:8002/v1/score \
  -H 'content-type: application/json' \
  -d '{"mission_id":"raid-tonight","candidate_count":80,"seed":42}'
```

## Tests

```bash
pytest
```

The tests must cover:

- typed request validation
- scoring response shape
- kill-switch behavior
- protected-attribute exclusion from feature hashes
- counterfactual fairness invariance
- proxy-feature audit behavior
- group fairness metrics
- high-disagreement low-confidence behavior
- audit-log hash-chain validation
- operational twin and deployment recommendation contracts

## Coding Conventions

- Python 3.11+ is supported.
- Use 4-space indentation and type hints.
- Use Pydantic v2 for contracts.
- Inbound request models must use `extra="forbid"`.
- Random seeds are explicit. Anything stochastic takes a `seed: int` argument.
- Keep model logic out of FastAPI handlers.
- Keep LLM or narrative logic out of the scoring decision path.
- Never route protected attributes into success scoring or assignment.
- Do not hard-code model versions in handler code; use `registry.py`.

## Scoring Pipeline

1. `POST /v1/score` receives a typed `ScoreRequest`.
2. Candidates are supplied directly or generated deterministically for local
   synthetic operation.
3. Role features are scored by TabPFN-compatible and Bayes-compatible adapters.
4. Scores are blended using model disagreement as a confidence signal.
5. The assignment solver fills role slots with hard constraints.
6. A second roster is solved after blocking primary `(soldier, role)` pairs.
7. Fairness audits run on protected attributes that are excluded from scoring.
8. Narrative explanations are generated from deterministic outputs.
9. Decision-quality assessment estimates evidence sufficiency, uncertainty,
   reversibility, value of information, utility, and reliance posture.
10. The response returns recommendations, audit, career forecast, and trace
   metadata.

## Deployment Recommendation Pipeline

1. `POST /v1/deployment-recommendations` receives a typed
   `DeploymentRecommendationRequest`.
2. The request must contain mission context and may include processed System 1
   observations, transcripts, OCR text, terrain, weather, readiness signals,
   constraints, and target soldier IDs.
3. The service wraps those inputs into an `OperationalTwinRequest` in `mission`
   mode.
4. The operational twin normalizes evidence, estimates latent state, drafts
   three governed options, and applies critic review.
5. `deployment.py` converts that twin run into platoon and individual
   deployment posture, required controls, option recommendations, source refs,
   and decision-quality outputs.
6. The response remains human-gated unless the caller explicitly disables
   `require_human_approval`.
7. Deployment recommendations are persisted and can be fetched by ID or mission.
8. Approval records approve/reject/escalate decisions with a named human actor.
9. Outcome capture records AAR signals, near misses, safety incidents,
   recommendation usefulness, missed factors, and lesson drafts.
10. Shared update events record recommendation, decision, and outcome lifecycle
    transitions for cross-system consumption.

Confidence policy:

- `abs(p_tabpfn - p_bayes) < 0.10`: high confidence.
- `0.10 <= abs(p_tabpfn - p_bayes) <= 0.25`: medium confidence.
- `abs(p_tabpfn - p_bayes) > 0.25`: low confidence and demotion.

## Fairness Guardrails

- Protected attributes such as race and gender never enter the model feature
  matrix or assignment objective.
- The audit module may read protected attributes only for measurement.
- Counterfactual protected-attribute flips must not change scores.
- Proxy-feature flags must be surfaced in the response.
- Demographic-parity and equalized-odds proxy metrics remain populated.
- Age and physical-risk fields require job-related rationale in
  `assets/feature-spec.md`.

## Operational Safety

- Every recommendation carries uncertainty and model disagreement.
- Every consequential response carries decision-quality, utility, and reliance
  guidance. This guidance is advisory and never auto-approves action.
- Every response carries trace metadata.
- Deployment recommendations must be based on processed evidence and mission
  context, not raw audio/images or unreviewed extraction.
- Deployment recommendations must preserve required controls and named human
  accountability.
- The kill switch must block all scoring routes.
- Admin routes must be protected by the deployment gateway or service mesh.
- Audit records must not include protected attributes or clear unit/MOS values.
- Use canonical IDs such as `soldier_id` and `mission_id`; do not mint local
  replacement IDs.

## PR Rejection Triggers

1. Adding protected attributes to scoring or assignment.
2. Removing confidence or model-disagreement fields from recommendations.
3. Weakening kill-switch behavior.
4. Removing fairness checks from the scoring response.
5. Logging protected attributes, full names, or clear unit/MOS identifiers.
6. Changing the feature set without updating `assets/feature-spec.md`.
7. Mutating rank/order in narrative code.
8. Removing decision-quality, utility, or reliance outputs from consequential
   recommendation responses.
