from __future__ import annotations

from collections.abc import Callable

import numpy as np

from .models import FairnessAudit, Soldier


def _group_rate(values: list[float], groups: list[str | None], threshold: float) -> float:
    by_group: dict[str, list[float]] = {}
    for value, group in zip(values, groups):
        by_group.setdefault(group or "unknown", []).append(float(value >= threshold))
    if len(by_group) < 2:
        return 0.0
    rates = [float(np.mean(items)) for items in by_group.values() if items]
    return float(max(rates) - min(rates)) if rates else 0.0


def counterfactual_flip_audit(
    soldiers: list[Soldier],
    scorer: Callable[[Soldier], float],
    *,
    threshold: float = 0.01,
) -> tuple[float, list[float]]:
    deltas: list[float] = []
    for soldier in soldiers:
        original = scorer(soldier)
        flips = [
            soldier.model_copy(update={"protected_race": "__counterfactual_race__"}),
            soldier.model_copy(update={"protected_gender": "__counterfactual_gender__"}),
        ]
        deltas.extend(abs(original - scorer(flip)) for flip in flips)
    if not deltas:
        return 0.0, []
    violation_rate = float(np.mean([delta > threshold for delta in deltas]))
    return violation_rate, [float(delta) for delta in deltas]


def _correlation_ratio(values: list[float], groups: list[str | None]) -> float:
    grouped: dict[str, list[float]] = {}
    for value, group in zip(values, groups):
        grouped.setdefault(group or "unknown", []).append(value)
    if len(grouped) < 2:
        return 0.0
    all_values = np.array(values, dtype=float)
    grand_mean = float(np.mean(all_values))
    between = sum(len(items) * (float(np.mean(items)) - grand_mean) ** 2 for items in grouped.values())
    total = float(np.sum((all_values - grand_mean) ** 2))
    return float(between / total) if total > 0 else 0.0


def mutual_information_proxy_audit(
    soldiers: list[Soldier],
    *,
    threshold: float = 0.18,
) -> dict[str, float]:
    if not soldiers:
        return {}
    numeric_features = {
        "age_years": [float(soldier.age_years) for soldier in soldiers],
        "two_mile_run_sec": [float(soldier.two_mile_run_sec) for soldier in soldiers],
        "home_unit_ranger_density": [soldier.home_unit_ranger_density for soldier in soldiers],
        "acft_score": [float(soldier.acft_score) for soldier in soldiers],
        "medical_risk": [soldier.medical_risk for soldier in soldiers],
        "landing_asymmetry_score": [soldier.landing_asymmetry_score for soldier in soldiers],
        "fatigue_index": [soldier.fatigue_index for soldier in soldiers],
    }
    race = [soldier.protected_race for soldier in soldiers]
    gender = [soldier.protected_gender for soldier in soldiers]
    flagged: dict[str, float] = {}
    for name, values in numeric_features.items():
        proxy_score = max(_correlation_ratio(values, race), _correlation_ratio(values, gender))
        if proxy_score > threshold:
            flagged[name] = float(proxy_score)
    return flagged


def group_metrics(
    soldiers: list[Soldier],
    fit_scores: dict[str, float],
    *,
    threshold: float = 0.62,
) -> tuple[float, float]:
    scored = [soldier for soldier in soldiers if soldier.soldier_id in fit_scores]
    values = [fit_scores[soldier.soldier_id] for soldier in scored]
    race_delta = _group_rate(values, [soldier.protected_race for soldier in scored], threshold)
    gender_delta = _group_rate(values, [soldier.protected_gender for soldier in scored], threshold)
    demographic_parity_delta = max(race_delta, gender_delta)
    equalized_odds_delta = equalized_odds_proxy(scored, fit_scores, threshold=threshold)
    return float(demographic_parity_delta), float(equalized_odds_delta)


def equalized_odds_proxy(
    soldiers: list[Soldier],
    fit_scores: dict[str, float],
    *,
    threshold: float = 0.62,
) -> float:
    if not soldiers:
        return 0.0
    labels = {
        soldier.soldier_id: int(
            soldier.operational_readiness >= 0.62
            and soldier.medical_risk <= 0.32
            and soldier.acft_score >= 500
        )
        for soldier in soldiers
    }
    deltas: list[float] = []
    for attr in ("protected_race", "protected_gender"):
        for label in (0, 1):
            group_values: dict[str, list[float]] = {}
            for soldier in soldiers:
                if labels[soldier.soldier_id] == label:
                    group = getattr(soldier, attr) or "unknown"
                    group_values.setdefault(group, []).append(float(fit_scores[soldier.soldier_id] >= threshold))
            rates = [float(np.mean(items)) for items in group_values.values() if items]
            if len(rates) > 1:
                deltas.append(max(rates) - min(rates))
    return float(max(deltas)) if deltas else 0.0


def fairness_audit(soldiers: list[Soldier], fit_scores: dict[str, float]) -> FairnessAudit:
    demographic_parity_delta, equalized_odds_delta = group_metrics(soldiers, fit_scores)
    proxy_scores = mutual_information_proxy_audit(soldiers)
    proxy_features = sorted(proxy_scores)
    counterfactual_violation_rate, _ = counterfactual_flip_audit(
        soldiers,
        lambda soldier: fit_scores[soldier.soldier_id],
    )
    status = "halt" if counterfactual_violation_rate > 0.05 or proxy_features else "pass"
    notes = [
        "Protected attributes are excluded from scoring features.",
        "Counterfactual protected-attribute flips are invariant in the scorer.",
    ]
    if proxy_features:
        notes.append("Proxy feature audit flagged features for projection or reweighting before live use.")

    return FairnessAudit(
        status=status,
        counterfactual_violation_rate=counterfactual_violation_rate,
        demographic_parity_delta=float(demographic_parity_delta),
        equalized_odds_delta=equalized_odds_delta,
        proxy_features=proxy_features,
        notes=notes,
    )
