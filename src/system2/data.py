from __future__ import annotations

import random

import numpy as np

from .models import RoleRequirement, Soldier


MILESTONES = [
    "patrolling",
    "land_nav",
    "peer_leadership",
    "physical_resilience",
    "sleep_dep_decision_making",
    "weapons",
    "comms",
    "casevac",
]

COMPETENCIES = [
    "tactical_proficiency",
    "communication",
    "leadership_team_cohesion",
    "decision_under_stress",
    "physical_resilience",
    "equipment_mastery",
    "cultural_language_awareness",
    "knowledge_application",
    "self_management",
]


def default_roles() -> list[RoleRequirement]:
    return [
        RoleRequirement(slot_id="TL-1", role="team_leader"),
        RoleRequirement(slot_id="ATL-1", role="assistant_team_leader"),
        RoleRequirement(slot_id="BR-1", role="breacher"),
        RoleRequirement(slot_id="BR-2", role="breacher"),
        RoleRequirement(slot_id="MED-1", role="medic", required_mos="68W"),
        RoleRequirement(slot_id="DM-1", role="marksman"),
        RoleRequirement(slot_id="COMMS-1", role="comms", required_mos="25U"),
        RoleRequirement(slot_id="A-1", role="assaulter"),
        RoleRequirement(slot_id="A-2", role="assaulter"),
        RoleRequirement(slot_id="A-3", role="assaulter"),
        RoleRequirement(slot_id="A-4", role="assaulter"),
        RoleRequirement(slot_id="A-5", role="assaulter"),
        RoleRequirement(slot_id="A-6", role="assaulter"),
        RoleRequirement(slot_id="A-7", role="assaulter"),
    ]


def generate_soldiers(count: int, seed: int) -> list[Soldier]:
    rng = np.random.default_rng(seed)
    py_rng = random.Random(seed)
    units = [f"U-{idx:02d}" for idx in range(1, 13)]
    mos_values = ["11B", "11C", "68W", "25U", "18B"]
    races = ["group_a", "group_b", "group_c"]
    genders = ["male", "female"]
    soldiers: list[Soldier] = []

    unit_density = {unit: float(np.clip(rng.beta(2.5, 3.0), 0.05, 0.95)) for unit in units}
    for idx in range(count):
        unit = py_rng.choice(units)
        density = unit_density[unit]
        age = int(np.clip(rng.normal(28, 4.5), 20, 42))
        two_mile = int(np.clip(rng.normal(790 + (age - 28) * 6 - density * 55, 70), 620, 1120))
        acft = int(np.clip(600 - (two_mile - 620) * 0.55 + rng.normal(0, 22), 380, 600))
        peer = float(np.clip(rng.normal((acft - 500) / 70 + density * 0.8, 0.75), -2.5, 2.5))
        readiness = float(np.clip(0.45 + (acft - 450) / 260 + peer * 0.08 + rng.normal(0, 0.08), 0, 1))
        medical_risk = float(np.clip(0.14 + max(age - 31, 0) * 0.018 + rng.normal(0, 0.06), 0, 0.75))
        asymmetry = float(np.clip(medical_risk + rng.normal(0.05, 0.12), 0, 1))
        fatigue = float(np.clip(0.65 - readiness * 0.35 + rng.normal(0, 0.12), 0, 1))
        mos = py_rng.choices(mos_values, weights=[0.58, 0.12, 0.1, 0.1, 0.1], k=1)[0]

        soldiers.append(
            Soldier(
                soldier_id=f"RGR-{idx + 1:04d}",
                unit_id=unit,
                mos=mos,
                age_years=age,
                two_mile_run_sec=two_mile,
                self_efficacy_score=float(np.clip(rng.normal(3.6 + density * 0.5, 0.55), 1, 5)),
                peer_rating_z=peer,
                home_unit_ranger_density=density,
                acft_score=acft,
                operational_readiness=readiness,
                prior_missions=int(np.clip(rng.poisson(4 + density * 5), 0, 20)),
                medical_risk=medical_risk,
                landing_asymmetry_score=asymmetry,
                hip_extension_power_w=float(np.clip(rng.normal(1450 + (acft - 500) * 3.2, 180), 850, 2200)),
                change_of_direction_index=float(np.clip(rng.normal(0.68 + readiness * 0.18, 0.1), 0, 1)),
                fatigue_index=fatigue,
                sandbox_score=float(np.clip(rng.normal(0.62 + peer * 0.08 + readiness * 0.15, 0.12), 0, 1)),
                protected_race=py_rng.choice(races),
                protected_gender=py_rng.choices(genders, weights=[0.87, 0.13], k=1)[0],
                milestones={name: int(np.clip(round(rng.normal(3.2 + readiness, 0.8)), 1, 5)) for name in MILESTONES},
                competencies={name: int(np.clip(round(rng.normal(3.0 + readiness, 0.75)), 1, 5)) for name in COMPETENCIES},
            )
        )
    return soldiers

