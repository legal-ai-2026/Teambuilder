from __future__ import annotations

from datetime import UTC, datetime

from .agent_store import AgentRunRepository, InMemoryAgentRunRepository
from .agent_tools import graph_context, request_context, retrieval_context, roster_recommendation_tool
from .config import InfraSettings
from .models import AgentRun, AgentRunRequest, AgentRunStatus, AgentStep, AgentStepStatus
from .service import SelectionService


class AgentOrchestrator:
    def __init__(
        self,
        repository: AgentRunRepository | None = None,
        selection_service: SelectionService | None = None,
        settings: InfraSettings | None = None,
    ) -> None:
        self.repository = repository or InMemoryAgentRunRepository()
        self.selection_service = selection_service or SelectionService()
        self.settings = settings or InfraSettings.from_env()

    def run(self, request: AgentRunRequest) -> AgentRun:
        run = self.repository.create(request)
        run = self.repository.save(run.model_copy(update={"status": AgentRunStatus.running}))
        steps: list[AgentStep] = []

        try:
            summary, evidence = request_context(request)
            steps.append(_completed_step("request_context", summary, evidence))

            summary, evidence = retrieval_context(self.settings)
            steps.append(_completed_step("retrieval_context", summary, evidence))

            summary, evidence = graph_context(self.settings)
            steps.append(_completed_step("graph_context", summary, evidence))

            recommendation = roster_recommendation_tool(self.selection_service, request)
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

            return self.repository.save(
                run.model_copy(
                    update={
                        "status": status,
                        "steps": steps,
                        "recommendation": recommendation,
                        "error": None,
                    }
                )
            )
        except Exception as exc:
            steps.append(
                _completed_step(
                    "agent_failure",
                    "Agent run failed before producing an approval-ready recommendation.",
                    {"error": str(exc)},
                    status=AgentStepStatus.failed,
                )
            )
            return self.repository.save(
                run.model_copy(
                    update={
                        "status": AgentRunStatus.failed,
                        "steps": steps,
                        "error": str(exc),
                    }
                )
            )

    def get(self, run_id: str) -> AgentRun | None:
        return self.repository.get(run_id)


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
