from __future__ import annotations

from .models import CareerForecast, CareerYear, Soldier


def career_forecast(soldier: Soldier) -> CareerForecast:
    readiness = soldier.operational_readiness
    leadership = min(max((soldier.peer_rating_z + 2.5) / 5, 0), 1)
    assignments = [
        ("Advanced marksmanship or breacher sustainment", 0.62 + readiness * 0.18),
        ("Squad-level assistant leadership rotation", 0.58 + leadership * 0.22),
        ("Ranger School or specialty recertification window", 0.55 + soldier.self_efficacy_score / 25),
        ("Platoon training NCO pathway", 0.52 + leadership * 0.2),
        ("Senior team leader screening", 0.48 + (readiness + leadership) * 0.18),
    ]
    return CareerForecast(
        soldier_id=soldier.soldier_id,
        path=[
            CareerYear(
                year=idx,
                recommended_assignment=name,
                p_success=min(probability, 0.93),
                rationale="Balances mission performance, development value, and retention of calibrated uncertainty.",
            )
            for idx, (name, probability) in enumerate(assignments, start=1)
        ],
    )

