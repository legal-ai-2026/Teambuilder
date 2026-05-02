# Implementation - Talent / Soldier Selection Engine (System 2)

This is the operational build document. Read `docs/architecture.md` first.

## The 24-Hour Clock

Realistic focused build budget is 15-18 hours. Cut aggressively if behind; the
demo arc matters most at hour 22.

## Pre-Event Checklist

- API keys: Anthropic and OpenAI embeddings only.
- Model strings pinned.
- TabPFN weights pre-downloaded.
- PyMC, NumPyro, and JAX installed; sanity model fits in under 60 seconds.
- Synthetic cohort generator pre-tested on 500 rows.
- Three pre-cached canned scenarios committed under `assets/canned/`:
  operational, trajectory, fairness edge case.

## Hours 0-3 - Scaffolding, Contracts, Synthetic Data

- Create `uv` project and `pyproject.toml` with pins.
- Add contracts: `ScoreRequest`, `RosterRecommendation`,
  `CandidateAssessment`, `RiskFactor`, `FairnessAudit`.
- Put `extra="forbid"` on inbound request contracts.
- Add `scripts/generate_synthetic.py`.
- Generate 5,000 synthetic soldiers.
- Enforce core correlations:
  - ACFT vs 2-mile run: approximately `r=-0.65`
  - peer rating vs operational readiness: approximately `r=0.55`
  - unit Ranger density vs graduation probability
  - biomechanical asymmetry vs injury: approximately `r=0.40`
- Output to `assets/cohort_synthetic/cohort.parquet`.
- Add FastAPI skeleton and `/v1/healthz`.
- Initialize DuckDB with cohort plus stubs for trajectory and outcomes.

Exit criterion:

```bash
python scripts/generate_synthetic.py --n 5000 --seed 42
```

The parquet should pass `sdv.evaluation.evaluate_quality` at `>= 0.85`.

## Hours 3-7 - TabPFN and Bayes

- Add `src/spire_talent/models/tabpfn.py`.
- Use a fixed feature ordering from `assets/feature-spec.md`.
- Return calibrated probabilities and feature attributions.
- Add `src/spire_talent/models/bayes.py`.
- Implement PyMC non-centered hierarchical model with NumPyro NUTS backend.
- Wire ADVI fallback but keep it disabled for the demo path.
- Add `src/spire_talent/models/blender.py`.
- Blend TabPFN and Bayes outputs into:
  - `fit_score`
  - `model_disagreement`
  - `confidence`
  - Bayesian credible interval
- Add `tests/unit/test_blender.py` for all three confidence bands.

Exit criterion:

```bash
python -m spire_talent.models.smoke
```

It scores 50 random soldiers against a stub mission and prints both
probabilities plus disagreement.

## Hours 7-11 - Assignment and Fairness

- Add `src/spire_talent/assignment/hungarian.py`.
- Build cost matrix with hard disqualifiers and role replication.
- Solve with SciPy.
- Add second-choice solver.
- Add `src/spire_talent/fairness/counterfactual.py`.
- Flip race, gender, and religion across the synthetic cohort.
- Re-score and compute max delta per candidate.
- Halt if fairness thresholds are violated.
- Add `src/spire_talent/fairness/mutual_info.py`.
- Precompute MI against the synthetic cohort at startup.
- Add `src/spire_talent/fairness/equalized_odds.py`.
- Add tests under `tests/fairness/`.

Exit criterion:

`POST /v1/score` against a canned payload returns a typed
`RosterRecommendation` with the fairness audit populated. Narratives can still
be placeholders.

## Hours 11-14 - LLM Narrative and Structured Outputs

- Add `src/spire_talent/reasoning/llm_explain.py`.
- Use Claude Sonnet 4.5 with Anthropic structured outputs.
- The LLM output schema is exactly `list[CandidateAssessment]` minus score
  fields.
- Scores and order are immutable.
- Put the system prompt in `src/spire_talent/prompts/explain.md`.
- Version prompts and add prompt regression tests.
- Add a unit test proving rank is preserved.
- Add evals that flag narrative contradictions, such as "high fit" when
  `fit_score < 0.5`.

Exit criterion:

The canned payload returns full narratives with strengths and risk factors, and
the rank is unchanged from deterministic upstream.

## Hours 14-17 - Career Trajectory, Demo UI, Polish

- Add `src/spire_talent/trajectory/thompson.py`.
- Implement assignment-as-exploration with Beta posteriors per
  `(soldier, role)`.
- Add `src/spire_talent/trajectory/optimal_stopping.py`.
- Implement finite-horizon school timing decisions.
- Add `POST /v1/trajectory/forecast`.
- Build a small Streamlit app in a sibling repo or `tools/demo_ui/`.
- Render:
  - ranking
  - narrative
  - fairness panel
  - second-choice list
  - career forecast page
- Update README with demo-day quickstart.

Exit criterion:

Open the demo UI, select "Mountain Phase Direct Action Raid (n=80, slots=14)",
click Run, and see the full ranking with fairness audit in under 8 seconds.

## Hours 17-20 - Failure Modes and Dress Rehearsal

- Add calibration plots: predicted vs observed on held-out synthetic data.
- Show 80 percent predictions landing near 78-82 percent.
- Add disagreement histogram.
- Demo kill switch:
  - `POST /admin/disable`
  - scoring returns disabled status
  - UI shows system offline
  - manual override form remains available
- Run three full operational dry runs.
- Run two trajectory dry runs.
- Run one deliberately bad model-disagreement scenario above `0.30`.

## Hours 20-22 - Pitch Prep

- Prepare 90-second System 2 demo script.
- Make the fairness panel the rhetorical anchor.
- Practice naming the studies:
  - Medland and Yates, 1974
  - Benedict 2023
- Pre-record OBS backup video.

## Hours 22-24 - Buffer

Submit, stabilize, and sleep.

## Cut List

1. Career trajectory entirely. Operational assignment is the demo.
2. Equalized-odds report. Keep counterfactual plus MI.
3. Streamlit UI. Fall back to CLI table.
4. Bayes ADVI fallback.
5. CatBoost sanity ensemble.
6. LLM narrative quality. Keep short factual narratives.
7. README screenshots.

## Demo Arc

1. "An operational commander needs to fill a 14-slot direct-action raid tonight.
   There are 80 candidates."
2. Click Run.
3. Show ranking, narrative, and fairness panel.
4. Point at disagreement: "TabPFN says high; Bayes is uncertain because this
   soldier has sparse observations from a low-density Ranger unit. We surface
   that, we do not hide it."
5. Point at fairness: "Counterfactual delta below 0.05. MI flags zero. This is
   more auditable than the current process."
6. Point at citations and feature rationale.
7. Click second choices and show a different coherent roster.
8. Optional trajectory slide.

## Failure-Mode Talk Track

| Probe | Answer |
|---|---|
| What if it is wrong? | Calibration plot, confidence enum, demotion on disagreement, commander override, audit log. |
| What about bias? | Counterfactual, MI, and equalized odds in the response. Protected attributes are excluded from features. |
| AR 600-20? | Advisory only. HRC retains slating authority. Audit makes the process more transparent. |
| What if it picks the same person every time? | Second-choice solver. Demonstrate live. |
| Why both TabPFN and Bayes? | Disagreement is a confidence signal. Either alone hides uncertainty. |
| Why not Anthropic everything? | LLMs explain. They do not decide. |
| IPPS-A integration? | Phase II read-only pilot with HRC and downstream recommendation table. |

## Score Endpoint Pattern

```python
@router.post("/v1/score", response_model=RosterRecommendation)
async def score(req: ScoreRequest) -> RosterRecommendation:
    if not enabled():
        raise HTTPException(503, "engine disabled")

    features = warehouse.lookup(req.candidate_pool, req.mission)
    p_tabpfn = tabpfn.score(features, req.mission)
    p_bayes = bayes.score(features, req.mission, seed=req.seed)
    blended = blender.blend(p_tabpfn, p_bayes)
    fairness = fairness_module.audit(features, p_tabpfn, p_bayes, req.mission)

    if fairness.halt_recommended:
        return RosterRecommendation(
            request_id=req.request_id,
            assignments=[],
            fairness=fairness,
            blended_at=datetime.utcnow(),
            seed=req.seed,
        )

    assignments = hungarian.assign(blended, req.roles, req.mission)
    narrated = await llm_explain.narrate(assignments, features, blended)
    audit.write(req, narrated, fairness)
    return RosterRecommendation(..., assignments=narrated, fairness=fairness, ...)
```

## Counterfactual Audit Pattern

```python
def counterfactual_max_delta(features: pd.DataFrame, score_fn, protected_cols: list[str]) -> float:
    base = score_fn(features)
    deltas = []
    for col in protected_cols:
        for level in PROTECTED_LEVELS[col]:
            flipped = features.copy()
            flipped[col] = level
            cf = score_fn(flipped)
            deltas.append(np.abs(base - cf).max())
    return float(max(deltas))
```

## Bayes Model Pattern

```python
import pymc as pm


def build_model(features, outcomes, unit_idx, mos_idx, n_units, n_mos):
    with pm.Model() as model:
        global_skill = pm.Normal("global_skill", 0.0, 1.0)
        tau_unit = pm.HalfNormal("tau_unit", 1.0)
        tau_mos = pm.HalfNormal("tau_mos", 1.0)
        unit_z = pm.Normal("unit_z", 0.0, 1.0, shape=n_units)
        unit_mean = pm.Deterministic("unit_mean", global_skill + tau_unit * unit_z)
        mos_z = pm.Normal("mos_z", 0.0, 1.0, shape=n_mos)
        mos_eff = pm.Deterministic("mos_eff", tau_mos * mos_z)
        beta = pm.Normal("beta", 0.0, 1.0, shape=features.shape[1])
        logit = unit_mean[unit_idx] + mos_eff[mos_idx] + features @ beta
        pm.Bernoulli("y", logit_p=logit, observed=outcomes)
    return model
```

## Anti-Patterns

- Calling the LLM in a loop over candidates.
- Putting fairness checks behind a feature flag.
- Putting an LLM in the scoring path.
- Letting an LLM reorder assignments.
- Mutating the synthetic cohort at request time.
- Logging predicted probabilities with soldier names.
- Weakening kill-switch behavior.
- Dropping model disagreement from responses.

## Open Questions for HRC Mentor

1. What is the right operational definition of mission success?
2. Are there branch-specific BFOQ exceptions for physical features?
3. What is the canonical IPPS-A field name for home-unit Ranger density, if one
   exists?
4. Should Phase I be a read-only recommendation feed or a side-by-side
   comparison with the current process?

## When Stuck

Read `docs/architecture.md`. If it does not say what to do, the feature spec at
`assets/feature-spec.md` overrides. If neither says, ask before inventing.

