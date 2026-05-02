# AGENT.md - Talent / Soldier Selection Engine

This file is the entry point for AI coding agents working in this repository.
Read it before making changes. Keep it up to date.

## What this project is

**Spire Talent Engine** is a backend microservice that ranks soldiers for an
upcoming mission and forecasts career trajectories over multi-year horizons. It
is **System 2 of three** in the Spire flywheel.

The primary use case and demo focus is **operational assignment**: given a pool
of available soldiers and a specific upcoming mission, return a ranked roster
with reasoning, predicted mission fit, risk factors, second choices, model
uncertainty, trace metadata, and a fairness audit.

The secondary use case is **career trajectory**: given a soldier's accumulated
decision data, recommend assignments, schools, and promotions over years.

## What this project is not

- Not a hiring tool. It does not recommend who to recruit, accession, or
  separate.
- Not an authoritative system of record. HRC retains AR 614-100 section 1-5
  slating authority. This service produces ranked recommendations; commanders
  and career managers decide.
- Not a free-form Q&A interface. There is no chat. Endpoints take typed inputs
  and return typed outputs.
- Not the frontend. Demo UI should be a thin Streamlit or Next.js app in a
  sibling repo driven by this service's REST API.

## Current implementation note

The current repository contains a runnable offline FastAPI scaffold under
`src/system2/`. It uses deterministic surrogate adapters for TabPFN, hierarchical
Bayes, and LLM reasoning so the demo works without GPU, MCMC, or API keys.

The target package layout below uses `src/spire_talent/`. When migrating from
the scaffold to production shape, keep API behavior and tests green while moving
modules incrementally. Do not delete fairness, confidence, disagreement, or kill
switch behavior during the migration.

## Read these before doing anything substantial

1. `README.md` - quickstart and demo API.
2. `docs/architecture.md` - scoring pipeline, Bayesian model, fairness audit,
   and API surface. Create or update this when architecture changes.
3. `docs/implementation.md` - build plan, synthetic data generation, cut list,
   and demo failure modes. Create or update this when implementation plans
   change.
4. `assets/feature-spec.md` - feature dictionary, what each feature means and
   where it comes from. Treat this as ground truth; changes to the feature set
   must update this file in the same commit.

## How to run the current scaffold locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn system2.api:app --reload --port 8002
```

Open `http://127.0.0.1:8002/docs`.

Example score request:

```bash
curl -X POST http://127.0.0.1:8002/score \
  -H 'content-type: application/json' \
  -d '{"mission_id":"raid-tonight","candidate_count":80,"seed":42}'
```

## Target local workflow

Once the production package layout is in place, prefer:

```bash
uv venv
source .venv/bin/activate
uv sync
uv run python -m scripts.generate_synthetic --n 5000 --seed 42
uv run uvicorn spire_talent.app:app --reload --port 8002
```

`make demo` should post a canned `ScoreRequest` with 80 candidates against a
14-slot direct-action raid and print the ranked roster with the fairness audit.

## How to run tests

Current scaffold:

```bash
pytest
```

Target full suite:

```bash
uv run pytest
uv run pytest tests/unit
uv run pytest tests/integration
uv run pytest tests/fairness -v
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

The fairness suite is mandatory. It must run counterfactual checks, mutual
information audits on the feature matrix, and equalized-odds reports. CI should
fail if any fairness guardrail regresses.

## Target directory layout

```text
src/spire_talent/
  features/         # feature engineering; one file per feature family
  models/
    registry.py     # model version constants
    tabpfn.py       # TabPFN wrapper
    bayes.py        # PyMC hierarchical model with NumPyro NUTS backend
    blender.py      # disagreement-aware blend
  assignment/
    hungarian.py    # scipy linear_sum_assignment with role constraints
    second_choice.py
  fairness/
    counterfactual.py
    mutual_info.py
    equalized_odds.py
  reasoning/
    llm_explain.py  # Claude structured-output narrative only
  trajectory/
    thompson.py
    optimal_stopping.py
  api/              # FastAPI routes
  contracts/        # Pydantic models
  obs/              # logging and audit
scripts/
  generate_synthetic.py
tests/
  unit/
  integration/
  fairness/
  trajectory/
assets/
  feature-spec.md
  cohort_synthetic/  # generated, gitignored
docs/
  architecture.md
  implementation.md
```

## Coding conventions

- Python 3.11 is the target runtime.
- Use 4-space indentation and type hints everywhere.
- Use Pydantic v2 for contracts.
- Inbound request models must use `extra="forbid"`.
- Numerical code uses NumPy and Pandas where appropriate; vectorize before
  optimizing.
- Statistical models live under the model package. Never inline Bayes logic in
  an endpoint handler.
- Tests for model code use `numpy.testing.assert_allclose` with explicit
  tolerances.
- Random seeds are explicit. Anything stochastic takes a `seed: int` argument.
  Default `None` is forbidden in non-test code.
- Keep LLM calls out of the scoring decision path. LLM output explains a
  recommendation; it never selects a soldier.

## Scoring pipeline

The intended deterministic flow is:

1. `POST /score` receives typed mission, role, and candidate inputs.
2. Build a feature matrix with protected attributes excluded.
3. Run TabPFN and Bayesian inference in parallel.
4. Blend per `(soldier, role)` scores using disagreement-aware confidence.
5. Solve role assignment with Hungarian optimization and hard role constraints.
6. Produce second-choice rosters by blocking chosen pairs and re-solving.
7. Run fairness audits.
8. Generate narrative explanations from strict structured output.
9. Return recommendations, risk factors, confidence, audit, and trace metadata.

Confidence policy:

- `abs(p_tabpfn - p_bayes) < 0.10`: high confidence.
- `0.10 <= abs(p_tabpfn - p_bayes) <= 0.25`: medium confidence with explicit
  model disagreement explanation.
- `abs(p_tabpfn - p_bayes) > 0.25`: low confidence and demote the candidate.

## Fairness and bias guardrails

- Protected attributes such as race, gender, and religion never enter the
  feature matrix passed to TabPFN or Bayes.
- The audit module may read protected attributes for counterfactual and group
  fairness reporting.
- Age may remain only with an explicit BFOQ rationale for physical-risk
  prediction in mission or course contexts.
- Mutual information above `0.05` between a feature and a protected attribute
  must be flagged for projection, reweighting, removal, or documented BFOQ.
- Counterfactual audit flips race, gender, and religion across the cohort and
  re-scores. If `abs(p_original - p_counterfactual) > 0.05` for more than 5% of
  candidates, halt and flag.
- Report demographic parity and equalized odds. Operational fitness, not
  headcount matching, is the core mission-assignment concern.

## Things that will get a PR rejected

1. Adding a feature that correlates above MI `0.05` with a protected attribute
   without an explicit BFOQ rationale and a test for that rationale.
2. Removing or weakening any test in `tests/fairness/`.
3. Calling an LLM in the scoring hot path. The LLM is for narrative explanation
   only.
4. Logging a soldier's full name plus rank plus unit. Use synthetic roster
   tokens or canonical IDs; do not leak identity into traces.
5. Hard-coding model versions in handler code. Use model registry constants.
6. Returning a recommendation without `confidence` and `model_disagreement`
   fields populated.
7. Adding protected-attribute columns into any model feature matrix.
8. Bypassing `POST /admin/disable` or returning scored recommendations while
   the kill switch is active.

## DoD AI Ethics mapping

| Principle | What this means here |
|---|---|
| Responsible | The system advises; HRC and the commander decide. Never auto-publishes. |
| Equitable | Counterfactual audit and equalized-odds report on every response. |
| Traceable | Every recommendation carries feature hash, model version, prompt version, and seed. |
| Reliable | TabPFN / Bayes disagreement above `0.25` demotes the candidate to low confidence. |
| Governable | `POST /admin/disable` kills the engine; per-recommendation override should be logged. |

## Cross-system contract reminders

- **Inbound from System 1:** `GET /soldier/{id}/training-trajectory` is
  read-only on System 1. System 2 polls and caches.
- **Inbound from System 3:** `POST /v1/deployment-outcomes` adds outcome rows
  per `(soldier_id, mission_id)`. It must be idempotent on that pair. It updates
  the feature warehouse only and does not retrain on receipt.
- **Outbound to System 3:** `POST /v1/mission/assignment` pushes the finalized
  roster after a commander accepts it.
- **Canonical IDs:** `soldier_id`, `mission_id`. Never mint local IDs for
  either.

## Operational safety

- This service returns recommendations, not commitments.
- Every response must be traceable.
- Every recommendation must carry uncertainty and model disagreement.
- Kill-switch behavior must remain covered by tests.
- Offline demo scenarios should run without LLM or internet access.
- Do not make personnel-policy claims without checking the relevant source.

## When in doubt

Ask for domain review before making claims about Army personnel doctrine. The
fastest way to lose credibility with the judging panel is to make a personnel
policy claim that AR 600-20 or another controlling reference contradicts.

