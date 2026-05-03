from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .graph import GraphFact
from .models import RoleRequirement, Soldier
from .retrieval import RetrievedContext
from .shared_data import canonical_hash


MAX_CONTEXT_DELTA = 0.10

_SKILL_COMPETENCIES = {
    "systems_thinking": ("knowledge_application", "decision_under_stress", "leadership_team_cohesion"),
    "sensemaking": ("knowledge_application", "decision_under_stress"),
    "critical_thinking": ("knowledge_application", "decision_under_stress"),
    "comms_coordination": ("communication", "equipment_mastery"),
    "communication": ("communication",),
    "medical": ("knowledge_application", "self_management"),
    "breaching": ("equipment_mastery", "physical_resilience"),
    "marksmanship": ("tactical_proficiency", "self_management"),
}

_SKILL_ROLES = {
    "systems_thinking": ("team_leader", "assistant_team_leader"),
    "sensemaking": ("team_leader", "assistant_team_leader", "marksman"),
    "critical_thinking": ("team_leader", "assistant_team_leader", "breacher"),
    "comms_coordination": ("team_leader", "assistant_team_leader", "comms"),
    "communication": ("team_leader", "assistant_team_leader", "medic", "comms"),
    "medical": ("medic",),
    "breaching": ("breacher",),
    "marksmanship": ("marksman",),
}


@dataclass(frozen=True)
class ContextAdjustment:
    adjustment_id: str
    category: str
    source: str
    base_delta: float
    reason: str
    target_roles: tuple[str, ...] = ()
    target_competencies: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def trace_dict(self) -> dict[str, Any]:
        return {
            "adjustment_id": self.adjustment_id,
            "category": self.category,
            "source": self.source,
            "base_delta": self.base_delta,
            "reason": self.reason,
            "target_roles": list(self.target_roles),
            "target_competencies": list(self.target_competencies),
            "metadata": self.metadata,
        }


def extract_context_adjustments(
    contexts: list[RetrievedContext],
    facts: list[GraphFact],
) -> list[ContextAdjustment]:
    adjustments: list[ContextAdjustment] = []
    for fact in facts:
        adjustment = _adjustment_from_fact(fact)
        if adjustment is not None:
            adjustments.append(adjustment)
    for context in contexts:
        adjustments.extend(_adjustments_from_context(context))
    return _dedupe_adjustments(adjustments)


def pair_context_delta(
    soldier: Soldier,
    role: RoleRequirement,
    adjustments: list[ContextAdjustment],
) -> tuple[float, list[str]]:
    applied: list[str] = []
    total = 0.0
    for adjustment in adjustments:
        delta = _pair_delta(soldier, role, adjustment)
        if abs(delta) < 0.0001:
            continue
        total += delta
        applied.append(adjustment.adjustment_id)
    return _clamp(total, -MAX_CONTEXT_DELTA, MAX_CONTEXT_DELTA), applied


def context_adjustment_trace(adjustments: list[ContextAdjustment]) -> list[dict[str, Any]]:
    return [adjustment.trace_dict() for adjustment in adjustments]


def context_adjustment_hash(adjustments: list[ContextAdjustment]) -> str:
    return canonical_hash(context_adjustment_trace(adjustments))


def _adjustment_from_fact(fact: GraphFact) -> ContextAdjustment | None:
    predicate = _normalize_token(fact.predicate)
    if predicate not in {"requires_skill", "requires"}:
        return None
    skill = _normalize_token(fact.object)
    competencies = _SKILL_COMPETENCIES.get(skill)
    roles = _SKILL_ROLES.get(skill)
    if not competencies or not roles:
        return None
    payload = {
        "subject": fact.subject,
        "predicate": fact.predicate,
        "object": fact.object,
        "metadata": fact.metadata,
    }
    return ContextAdjustment(
        adjustment_id="ctx-" + canonical_hash(payload).removeprefix("sha256:")[:16],
        category="skill_requirement",
        source="graph",
        base_delta=0.06,
        reason=f"Mission graph requires skill '{fact.object}'.",
        target_roles=roles,
        target_competencies=competencies,
        metadata={
            "subject": fact.subject,
            "predicate": fact.predicate,
            "object": fact.object,
            "source_hash": canonical_hash(payload),
        },
    )


def _adjustments_from_context(context: RetrievedContext) -> list[ContextAdjustment]:
    text = f"{context.title} {context.content}".lower()
    adjustments: list[ContextAdjustment] = []
    if "fatigue" in text and ("unsafe" in text or "sleep" in text or "difficulty escalation" in text):
        adjustments.append(
            ContextAdjustment(
                adjustment_id="ctx-" + canonical_hash(
                    {
                        "source": context.source,
                        "title": context.title,
                        "category": "fatigue_caution",
                    }
                ).removeprefix("sha256:")[:16],
                category="fatigue_caution",
                source="retrieval",
                base_delta=-0.05,
                reason="Retrieved context warns against unsafe escalation under fatigue.",
                metadata={
                    "source": context.source,
                    "title": context.title,
                    "score": context.score,
                },
            )
        )
    return adjustments


def _pair_delta(soldier: Soldier, role: RoleRequirement, adjustment: ContextAdjustment) -> float:
    if adjustment.target_roles and role.role not in adjustment.target_roles:
        return 0.0
    if adjustment.category == "skill_requirement":
        return _skill_requirement_delta(soldier, adjustment)
    if adjustment.category == "fatigue_caution":
        return adjustment.base_delta * soldier.fatigue_index
    return 0.0


def _skill_requirement_delta(soldier: Soldier, adjustment: ContextAdjustment) -> float:
    if not adjustment.target_competencies:
        return 0.0
    ratings = [
        float(soldier.competencies.get(competency, 3))
        for competency in adjustment.target_competencies
    ]
    average_rating = sum(ratings) / len(ratings)
    centered = _clamp((average_rating - 3.0) / 2.0, -1.0, 1.0)
    return adjustment.base_delta * centered


def _dedupe_adjustments(adjustments: list[ContextAdjustment]) -> list[ContextAdjustment]:
    by_id: dict[str, ContextAdjustment] = {}
    for adjustment in adjustments:
        by_id[adjustment.adjustment_id] = adjustment
    return list(by_id.values())


def _normalize_token(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))
