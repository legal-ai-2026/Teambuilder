from __future__ import annotations

from .models import CandidateAssessment, Confidence, RiskCategory, RiskFactor, RoleRequirement, Soldier


def strengths(soldier: Soldier) -> list[str]:
    items: list[tuple[float, str]] = [
        (soldier.peer_rating_z, "peer-rated leadership signal"),
        (soldier.operational_readiness, "high operational readiness"),
        (soldier.home_unit_ranger_density, "trained in a high-density Ranger unit"),
        (soldier.self_efficacy_score / 5, "strong self-efficacy"),
        (soldier.sandbox_score, "strong simulation score"),
        ((600 - soldier.two_mile_run_sec / 2) / 600, "fast two-mile run time"),
    ]
    return [label for _, label in sorted(items, reverse=True)[:3]]


def risk_factors(soldier: Soldier, p_tabpfn: float, p_bayes: float, confidence: Confidence) -> list[RiskFactor]:
    risks: list[RiskFactor] = []
    if soldier.medical_risk > 0.32 or soldier.landing_asymmetry_score > 0.42:
        risks.append(
            RiskFactor(
                category=RiskCategory.medical,
                label="Elevated injury risk",
                detail="Biomechanical and medical-risk indicators should be reviewed before final tasking.",
            )
        )
    if soldier.prior_missions < 2:
        risks.append(
            RiskFactor(
                category=RiskCategory.experience,
                label="Limited mission history",
                detail="Sparse prior-mission record widens uncertainty in the hierarchical estimate.",
            )
        )
    if confidence is Confidence.low:
        risks.append(
            RiskFactor(
                category=RiskCategory.model_disagreement,
                label="Low model agreement",
                detail=f"TabPFN and Bayes differ by {abs(p_tabpfn - p_bayes):.2f}; demotion applied.",
            )
        )
    elif confidence is Confidence.medium:
        risks.append(
            RiskFactor(
                category=RiskCategory.model_disagreement,
                label="Moderate model disagreement",
                detail="TabPFN sees raw-feature pattern while Bayes pulls toward pooled unit and MOS means.",
            )
        )
    return risks


def build_assessment(
    soldier: Soldier,
    role: RoleRequirement,
    score: dict[str, object],
    second_choice_id: str | None,
) -> CandidateAssessment:
    p_tab = float(score["p_tabpfn"])
    p_bayes = float(score["p_bayes"])
    confidence = score["confidence"]
    assert isinstance(confidence, Confidence)
    key_strengths = strengths(soldier)
    narrative = (
        f"{soldier.soldier_id} is recommended for {role.role.replace('_', ' ')} with "
        f"{confidence.value} confidence. Primary signals are {', '.join(key_strengths[:2])}; "
        f"Bayesian pooling accounts for {soldier.unit_id} and MOS {soldier.mos}."
    )
    return CandidateAssessment(
        slot_id=role.slot_id,
        role=role.role,
        soldier_id=soldier.soldier_id,
        fit_score=float(score["fit_score"]),
        p_success_tabpfn=p_tab,
        p_success_bayes_mean=p_bayes,
        model_disagreement=abs(p_tab - p_bayes),
        p_success_bayes_ci=score["bayes_ci"],
        narrative=narrative,
        key_strengths=key_strengths,
        risk_factors=risk_factors(soldier, p_tab, p_bayes, confidence),
        second_choice_id=second_choice_id,
        confidence=confidence,
    )
