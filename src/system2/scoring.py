from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict

import numpy as np
from scipy.optimize import linear_sum_assignment

from .models import Confidence, RoleRequirement, Soldier


ROLE_WEIGHTS: dict[str, dict[str, float]] = {
    "team_leader": {"leadership_team_cohesion": 0.22, "decision_under_stress": 0.18, "communication": 0.16},
    "assistant_team_leader": {"leadership_team_cohesion": 0.18, "knowledge_application": 0.16, "communication": 0.14},
    "breacher": {"equipment_mastery": 0.2, "physical_resilience": 0.18, "decision_under_stress": 0.14},
    "medic": {"knowledge_application": 0.2, "communication": 0.16, "self_management": 0.14},
    "marksman": {"tactical_proficiency": 0.22, "self_management": 0.16, "decision_under_stress": 0.16},
    "comms": {"communication": 0.2, "equipment_mastery": 0.16, "knowledge_application": 0.16},
    "assaulter": {"tactical_proficiency": 0.18, "physical_resilience": 0.16, "equipment_mastery": 0.12},
}


def sigmoid(value: float) -> float:
    return 1 / (1 + math.exp(-value))


def feature_hash(soldiers: list[Soldier], roles: list[RoleRequirement]) -> str:
    payload = {
        "soldiers": [soldier.model_dump(exclude={"protected_race", "protected_gender"}) for soldier in soldiers],
        "roles": [role.model_dump() for role in roles],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def role_fit(soldier: Soldier, role: RoleRequirement) -> float:
    weights = ROLE_WEIGHTS[role.role]
    competency_term = sum((soldier.competencies.get(name, 3) / 5) * weight for name, weight in weights.items())
    milestone_term = np.mean(list(soldier.milestones.values())) / 5 if soldier.milestones else 0.6
    base = (
        0.16 * ((soldier.acft_score - 300) / 300)
        + 0.12 * (1 - (soldier.two_mile_run_sec - 600) / 600)
        + 0.13 * soldier.self_efficacy_score / 5
        + 0.13 * ((soldier.peer_rating_z + 2.5) / 5)
        + 0.12 * soldier.home_unit_ranger_density
        + 0.12 * soldier.operational_readiness
        + 0.06 * min(soldier.prior_missions / 12, 1)
        + 0.08 * soldier.sandbox_score
        + 0.08 * milestone_term
        + competency_term
    )
    penalty = 0.16 * soldier.medical_risk + 0.08 * soldier.landing_asymmetry_score + 0.06 * soldier.fatigue_index
    return float(np.clip(base - penalty, 0.01, 0.99))


def tabpfn_probability(soldier: Soldier, role: RoleRequirement) -> float:
    raw = role_fit(soldier, role)
    local_pattern = 0.05 * math.sin(soldier.acft_score / 37 + soldier.peer_rating_z)
    raw_feature_bonus = 0.04 if soldier.change_of_direction_index > 0.78 and soldier.fatigue_index < 0.45 else 0.0
    return float(np.clip(sigmoid((raw - 0.55) * 5.4) + local_pattern + raw_feature_bonus, 0.01, 0.99))


def bayes_probability(soldier: Soldier, role: RoleRequirement, unit_means: dict[str, float], mos_effects: dict[str, float]) -> tuple[float, tuple[float, float]]:
    pooled = 0.54 + unit_means[soldier.unit_id] + mos_effects[soldier.mos]
    individual = (role_fit(soldier, role) - 0.55) * 0.52
    p = float(np.clip(sigmoid((pooled + individual - 0.55) * 4.2), 0.01, 0.99))
    width = float(np.clip(0.18 - min(soldier.prior_missions, 12) * 0.006 + (1 - soldier.home_unit_ranger_density) * 0.05, 0.08, 0.28))
    return p, (float(np.clip(p - width / 2, 0, 1)), float(np.clip(p + width / 2, 0, 1)))


def pooled_effects(soldiers: list[Soldier]) -> tuple[dict[str, float], dict[str, float]]:
    by_unit: dict[str, list[float]] = defaultdict(list)
    by_mos: dict[str, list[float]] = defaultdict(list)
    for soldier in soldiers:
        latent = (
            (soldier.peer_rating_z / 5)
            + (soldier.acft_score - 500) / 550
            + soldier.home_unit_ranger_density * 0.15
            - soldier.medical_risk * 0.14
        )
        by_unit[soldier.unit_id].append(latent)
        by_mos[soldier.mos].append(latent)
    unit_means = {unit: float(np.clip(np.mean(values) * 0.35, -0.18, 0.18)) for unit, values in by_unit.items()}
    mos_effects = {mos: float(np.clip(np.mean(values) * 0.22, -0.12, 0.12)) for mos, values in by_mos.items()}
    return unit_means, mos_effects


def blend(tabpfn: float, bayes: float) -> tuple[float, Confidence]:
    disagreement = abs(tabpfn - bayes)
    if disagreement < 0.10:
        return float((tabpfn + bayes) / 2), Confidence.high
    if disagreement <= 0.25:
        return float(tabpfn * 0.55 + bayes * 0.45), Confidence.medium
    return float(max(tabpfn * 0.45 + bayes * 0.55 - 0.10, 0.01)), Confidence.low


def score_matrix(soldiers: list[Soldier], roles: list[RoleRequirement]) -> dict[tuple[int, int], dict[str, object]]:
    unit_means, mos_effects = pooled_effects(soldiers)
    scores: dict[tuple[int, int], dict[str, object]] = {}
    for i, soldier in enumerate(soldiers):
        for j, role in enumerate(roles):
            p_tab = tabpfn_probability(soldier, role)
            p_bayes, ci = bayes_probability(soldier, role, unit_means, mos_effects)
            p_blended, confidence = blend(p_tab, p_bayes)
            disqualified = role.required_mos is not None and soldier.mos != role.required_mos
            disqualified = disqualified or soldier.acft_score < role.min_acft
            scores[(i, j)] = {
                "p_tabpfn": p_tab,
                "p_bayes": p_bayes,
                "bayes_ci": ci,
                "fit_score": p_blended,
                "confidence": confidence,
                "disqualified": disqualified,
            }
    return scores


def solve_assignment(
    soldiers: list[Soldier],
    roles: list[RoleRequirement],
    scores: dict[tuple[int, int], dict[str, object]],
    blocked_pairs: set[tuple[int, int]] | None = None,
) -> list[tuple[int, int]]:
    blocked_pairs = blocked_pairs or set()
    cost = np.zeros((len(soldiers), len(roles)))
    for i in range(len(soldiers)):
        for j in range(len(roles)):
            score = scores[(i, j)]
            if score["disqualified"] or (i, j) in blocked_pairs:
                cost[i, j] = 1e6
            else:
                cost[i, j] = -math.log(float(score["fit_score"]))
    row_ind, col_ind = linear_sum_assignment(cost)
    pairs = [(int(i), int(j)) for i, j in zip(row_ind, col_ind) if j < len(roles) and cost[i, j] < 1e6]
    if len(pairs) != len(roles):
        raise ValueError("Unable to fill all role slots with qualified candidates")
    return sorted(pairs, key=lambda item: item[1])

