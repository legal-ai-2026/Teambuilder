# Architecture - Talent / Soldier Selection Engine

## Purpose

Spire Talent Engine is an operational FastAPI service for mission roster
recommendations. Given a mission, roles, and a candidate pool, it returns a
primary roster, second-choice roster, model disagreement, confidence, risk
factors, fairness audit, career forecast, and trace metadata.

The service is advisory. It does not publish orders, slate personnel, or replace
commander and career-manager judgment.

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

`AuditLog` writes redacted JSONL records and links them with hashes. It removes
protected attributes and hashes clear unit/MOS values before persistence.

## Cross-System Boundaries

System 1 can supply longitudinal training features through a read model.
System 3 can receive finalized assignments and deployment outcomes. This service
should still score a request when those systems are unavailable, as long as the
request contains the candidate and role data needed for scoring.
