# AGENT.md - Talent / Soldier Selection Engine

This file is the entry point for AI coding agents working in this repository.
Read it before making changes and keep it aligned with the actual service.

## What This Project Is

**Spire Talent Engine** is an operational backend service that ranks soldiers
for mission roster slots and returns uncertainty, second-choice options,
fairness audit results, trace metadata, and a career forecast for the strongest
selected candidate. It is System 2 in the Spire flywheel.

The service is advisory. It produces recommendations for commanders and career
managers; it does not publish orders, replace command judgment, or act as a
system of record.

## What This Project Is Not

- Not a hiring, accession, separation, or disciplinary tool.
- Not a free-form Q&A or chat interface.
- Not a frontend application.
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
9. The response returns recommendations, audit, career forecast, and trace
   metadata.

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
- Every response carries trace metadata.
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
