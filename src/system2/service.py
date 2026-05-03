from __future__ import annotations

from .audit import AuditLog, AuditSink
from .calibration import calibration_bins, disagreement_histogram
from .candidate_pool import CandidatePoolResolver, InMemoryCandidatePoolResolver
from .career import career_forecast
from .data import default_roles, generate_soldiers
from .fairness import fairness_audit
from .models import RoleRequirement, RosterRecommendation, ScoreRequest, Soldier, SourceReference, TraceMetadata
from .narrative import build_assessment
from .registry import DOD_AI_PRINCIPLES, MODEL_VERSIONS, prompt_hash
from .scoring import feature_hash, score_matrix, solve_assignment
from .shared_data import input_source_hashes, score_request_source_refs


class SelectionService:
    def __init__(
        self,
        audit_log: AuditSink | None = None,
        candidate_pool_resolver: CandidatePoolResolver | None = None,
    ) -> None:
        self.disabled = False
        self.audit_log = audit_log or AuditLog()
        self.candidate_pool_resolver = candidate_pool_resolver or InMemoryCandidatePoolResolver()

    def disable(self) -> None:
        self.disabled = True
        self.audit_log.append("kill_switch_changed", {"disabled": True})

    def enable(self) -> None:
        self.disabled = False
        self.audit_log.append("kill_switch_changed", {"disabled": False})

    def score(self, request: ScoreRequest) -> RosterRecommendation:
        if self.disabled:
            self.audit_log.append(
                "score_request_blocked",
                {"mission_id": request.mission_id, "candidate_count": request.candidate_count},
            )
            raise RuntimeError("selection engine is disabled")

        soldiers, roles, candidate_pool_resolved, resolved_source_refs = self._resolve_inputs(request)
        self.audit_log.append(
            "score_request_received",
            {
                "mission_id": request.mission_id,
                "candidate_count": len(soldiers),
                "requested_candidate_count": request.candidate_count,
                "role_count": len(roles),
                "has_candidates": request.candidates is not None,
                "candidate_pool_id": request.candidate_pool_id,
                "candidate_pool_resolved": candidate_pool_resolved,
                "seed": request.seed,
            },
        )
        if len(soldiers) < len(roles):
            raise ValueError(
                f"candidate pool has {len(soldiers)} candidates but {len(roles)} roles must be filled"
            )
        scores = score_matrix(soldiers, roles)
        primary_pairs = solve_assignment(soldiers, roles, scores)
        secondary_pairs = solve_assignment(soldiers, roles, scores, blocked_pairs=set(primary_pairs))

        secondary_by_slot = {slot_idx: soldier_idx for soldier_idx, slot_idx in secondary_pairs}
        primary = [
            build_assessment(
                soldiers[soldier_idx],
                roles[slot_idx],
                scores[(soldier_idx, slot_idx)],
                soldiers[secondary_by_slot[slot_idx]].soldier_id,
            )
            for soldier_idx, slot_idx in primary_pairs
        ]
        secondary = [
            build_assessment(
                soldiers[soldier_idx],
                roles[slot_idx],
                scores[(soldier_idx, slot_idx)],
                None,
            )
            for soldier_idx, slot_idx in secondary_pairs
        ]

        best_by_soldier: dict[str, float] = {}
        for soldier_idx, soldier in enumerate(soldiers):
            best_by_soldier[soldier.soldier_id] = max(float(scores[(soldier_idx, slot_idx)]["fit_score"]) for slot_idx in range(len(roles)))

        selected = max(primary, key=lambda item: item.fit_score)
        selected_soldier = next(soldier for soldier in soldiers if soldier.soldier_id == selected.soldier_id)
        fairness = fairness_audit(soldiers, best_by_soldier)
        predictions = [best_by_soldier[soldier.soldier_id] for soldier in soldiers]
        outcomes = [
            int(soldier.operational_readiness >= 0.62 and soldier.medical_risk <= 0.32 and soldier.acft_score >= 500)
            for soldier in soldiers
        ]
        trace = TraceMetadata(
            model_versions=MODEL_VERSIONS,
            feature_hash=feature_hash(soldiers, roles),
            prompt_hash=prompt_hash(),
            seed=request.seed,
            dod_ai_principles=DOD_AI_PRINCIPLES,
            calibration_bins=calibration_bins(predictions, outcomes),
            disagreement_histogram=disagreement_histogram(primary + secondary),
        )
        source_refs = score_request_source_refs(
            request,
            soldiers,
            roles,
            candidate_pool_resolved=candidate_pool_resolved,
            additional_refs=resolved_source_refs,
        )
        trace = trace.model_copy(
            update={
                "source_refs": source_refs,
                "input_source_hashes": input_source_hashes(source_refs),
            }
        )
        recommendation = RosterRecommendation(
            mission_id=request.mission_id,
            roster=primary,
            second_choice_roster=secondary,
            fairness_audit=fairness,
            career_forecast=career_forecast(selected_soldier),
            trace=trace,
        )
        self.audit_log.append(
            "fairness_outcome",
            {
                "mission_id": request.mission_id,
                "status": fairness.status,
                "proxy_features": fairness.proxy_features,
                "counterfactual_violation_rate": fairness.counterfactual_violation_rate,
            },
        )
        self.audit_log.append(
            "recommendations_returned",
            {
                "mission_id": request.mission_id,
                "feature_hash": trace.feature_hash,
                "primary_ids": [item.soldier_id for item in primary],
                "second_choice_ids": [item.soldier_id for item in secondary],
            },
        )
        return recommendation

    def _resolve_inputs(
        self,
        request: ScoreRequest,
    ) -> tuple[list[Soldier], list[RoleRequirement], bool, list[SourceReference]]:
        if request.candidates is not None:
            return (
                request.candidates,
                request.roles or default_roles(),
                False,
                [],
            )

        if request.candidate_pool_id is not None:
            resolved = self.candidate_pool_resolver.resolve(request)
            if resolved is not None:
                selected_roles = request.roles or resolved.roles or default_roles()
                resolved_source_refs = resolved.source_refs
                if request.roles is not None:
                    resolved_source_refs = [
                        ref for ref in resolved_source_refs if ref.role != "role_slot"
                    ]
                return (
                    resolved.candidates,
                    selected_roles,
                    True,
                    resolved_source_refs,
                )
            if self.candidate_pool_resolver.requires_resolution:
                raise ValueError(
                    f"candidate_pool_id {request.candidate_pool_id!r} was not found for mission "
                    f"{request.mission_id!r}"
                )

        return (
            generate_soldiers(request.candidate_count, request.seed),
            request.roles or default_roles(),
            False,
            [],
        )
