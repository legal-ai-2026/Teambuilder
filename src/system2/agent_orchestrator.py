from __future__ import annotations

from datetime import UTC, datetime

from .agent_state import AgentStateStore, InMemoryAgentStateStore
from .agent_store import AgentRunRepository, InMemoryAgentRunRepository
from .agent_tools import (
    contextual_scoring_input,
    graph_context,
    request_context,
    retrieval_context,
    roster_recommendation_tool,
)
from .config import InfraSettings
from .graph import GraphContextProvider, LocalGraphContextProvider
from .models import (
    AgentApproval,
    AgentApprovalDecision,
    AgentApprovalRequest,
    AgentRun,
    AgentRunRequest,
    AgentRunStatus,
    AgentStep,
    AgentStepStatus,
    SourceReference,
)
from .retrieval import ContextRetriever, LocalContextRetriever
from .service import SelectionService
from .shared_data import (
    InMemorySharedDataSink,
    SharedDataSink,
    attach_source_refs,
    build_approval_update_event,
    build_decision_snapshot,
)


class AgentOrchestrator:
    def __init__(
        self,
        repository: AgentRunRepository | None = None,
        state_store: AgentStateStore | None = None,
        retriever: ContextRetriever | None = None,
        graph_provider: GraphContextProvider | None = None,
        selection_service: SelectionService | None = None,
        shared_data_sink: SharedDataSink | None = None,
        settings: InfraSettings | None = None,
    ) -> None:
        self.repository = repository or InMemoryAgentRunRepository()
        self.state_store = state_store or InMemoryAgentStateStore()
        self.retriever = retriever or LocalContextRetriever()
        self.graph_provider = graph_provider or LocalGraphContextProvider()
        self.selection_service = selection_service or SelectionService()
        self.shared_data_sink = shared_data_sink or InMemorySharedDataSink()
        self.settings = settings or InfraSettings.from_env()

    def run(self, request: AgentRunRequest) -> AgentRun:
        run = self.repository.create(request)
        self.state_store.set_status(run.run_id, AgentRunStatus.queued)
        if not self.state_store.acquire_lock(run.run_id):
            raise RuntimeError("agent run is already locked")
        run = self.repository.save(run.model_copy(update={"status": AgentRunStatus.running}))
        self.state_store.set_status(run.run_id, AgentRunStatus.running)
        steps: list[AgentStep] = []

        try:
            summary, evidence = request_context(request)
            steps.append(_completed_step("request_context", summary, evidence))

            context_input = contextual_scoring_input(request, self.retriever, self.graph_provider)

            summary, evidence = retrieval_context(
                self.settings,
                self.retriever,
                contexts=context_input.contexts,
            )
            steps.append(_completed_step("retrieval_context", summary, evidence))

            summary, evidence = graph_context(
                self.settings,
                request,
                self.graph_provider,
                facts=context_input.facts,
            )
            steps.append(_completed_step("graph_context", summary, evidence))

            recommendation = roster_recommendation_tool(
                self.selection_service,
                request,
                context_adjustments=context_input.adjustments,
            )
            recommendation = attach_source_refs(recommendation, _step_source_refs(steps))
            steps.append(
                _completed_step(
                    "roster_recommendation",
                    "Generated primary roster, second-choice roster, fairness audit, career forecast, and trace metadata.",
                    {
                        "mission_id": recommendation.mission_id,
                        "primary_count": len(recommendation.roster),
                        "second_choice_count": len(recommendation.second_choice_roster),
                        "fairness_status": recommendation.fairness_audit.status,
                        "feature_hash": recommendation.trace.feature_hash,
                        "context_adjustment_count": len(context_input.adjustments),
                    },
                )
            )

            if request.require_human_approval:
                status = AgentRunStatus.awaiting_approval
                steps.append(
                    _completed_step(
                        "human_approval",
                        "Recommendation is ready for authorized human approval before finalization.",
                        {"required": True},
                    )
                )
            else:
                status = AgentRunStatus.completed

            stored = self.repository.save(
                run.model_copy(
                    update={
                        "status": status,
                        "steps": steps,
                        "recommendation": recommendation,
                        "error": None,
                    }
                )
            )
            self.shared_data_sink.record_decision_snapshot(build_decision_snapshot(stored))
            self.state_store.set_status(stored.run_id, status)
            return stored
        except Exception as exc:
            steps.append(
                _completed_step(
                    "agent_failure",
                    "Agent run failed before producing an approval-ready recommendation.",
                    {"error": str(exc)},
                    status=AgentStepStatus.failed,
                )
            )
            stored = self.repository.save(
                run.model_copy(
                    update={
                        "status": AgentRunStatus.failed,
                        "steps": steps,
                        "error": str(exc),
                    }
                )
            )
            self.state_store.set_status(stored.run_id, AgentRunStatus.failed)
            return stored
        finally:
            self.state_store.release_lock(run.run_id)

    def get(self, run_id: str) -> AgentRun | None:
        return self.repository.get(run_id)

    def record_approval(self, run_id: str, request: AgentApprovalRequest) -> AgentRun | None:
        run = self.repository.get(run_id)
        if run is None:
            return None
        if not self.state_store.acquire_lock(run.run_id):
            raise RuntimeError("agent run is already locked")

        try:
            if run.status is not AgentRunStatus.awaiting_approval:
                raise ValueError("agent run is not awaiting approval")
            if run.recommendation is None:
                raise ValueError("agent run has no recommendation to approve")

            approval = AgentApproval(
                decision=request.decision,
                approver_id=request.approver_id,
                rationale=request.rationale,
            )
            status = (
                AgentRunStatus.completed
                if request.decision is AgentApprovalDecision.approved
                else AgentRunStatus.rejected
            )
            step = _completed_step(
                "approval_recorded",
                f"Human decision recorded: {request.decision.value}.",
                {
                    "decision": request.decision.value,
                    "approver_id": request.approver_id,
                },
            )
            stored = self.repository.save(
                run.model_copy(
                    update={
                        "status": status,
                        "approval": approval,
                        "steps": [*run.steps, step],
                    }
                )
            )
            self.shared_data_sink.append_update_event(build_approval_update_event(stored))
            self.state_store.set_status(stored.run_id, status)
            return stored
        finally:
            self.state_store.release_lock(run.run_id)


def _completed_step(
    name: str,
    summary: str,
    evidence: dict[str, object],
    *,
    status: AgentStepStatus = AgentStepStatus.completed,
) -> AgentStep:
    now = datetime.now(UTC)
    return AgentStep(
        name=name,
        status=status,
        summary=summary,
        evidence=evidence,
        started_at=now,
        completed_at=now,
    )


def _step_source_refs(steps: list[AgentStep]) -> list[SourceReference]:
    refs: list[SourceReference] = []
    for step in steps:
        raw_refs = step.evidence.get("source_refs", [])
        if not isinstance(raw_refs, list):
            continue
        for raw_ref in raw_refs:
            if isinstance(raw_ref, SourceReference):
                refs.append(raw_ref)
            elif isinstance(raw_ref, dict):
                refs.append(SourceReference.model_validate(raw_ref))
    return refs
