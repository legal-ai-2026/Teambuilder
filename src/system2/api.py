from __future__ import annotations

from fastapi import FastAPI, HTTPException

from . import __version__
from .agent_stack import build_agent_orchestrator
from .config import InfraSettings
from .models import AgentApprovalRequest, AgentRun, AgentRunRequest, RosterRecommendation, ScoreRequest
from .service import SelectionService


app = FastAPI(
    title="System 2 Talent Selection Engine",
    version=__version__,
    description="Ranks soldiers into mission rosters with uncertainty, fairness, and trace outputs.",
)

service = SelectionService()
agent_orchestrator = build_agent_orchestrator(selection_service=service)


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "version": __version__,
        "disabled": service.disabled,
        "infrastructure": InfraSettings.from_env().status(),
    }


@app.get("/v1/healthz")
def healthz() -> dict[str, object]:
    return health()


@app.post("/score", response_model=RosterRecommendation)
def score(request: ScoreRequest) -> RosterRecommendation:
    try:
        return service.score(request)
    except RuntimeError as exc:
        raise HTTPException(status_code=423, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/v1/score", response_model=RosterRecommendation)
def score_v1(request: ScoreRequest) -> RosterRecommendation:
    return score(request)


@app.post("/v1/agent-runs", response_model=AgentRun)
def create_agent_run(request: AgentRunRequest) -> AgentRun:
    return agent_orchestrator.run(request)


@app.get("/v1/agent-runs/{run_id}", response_model=AgentRun)
def get_agent_run(run_id: str) -> AgentRun:
    run = agent_orchestrator.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="agent run not found")
    return run


@app.post("/v1/agent-runs/{run_id}/approval", response_model=AgentRun)
def record_agent_run_approval(run_id: str, request: AgentApprovalRequest) -> AgentRun:
    try:
        run = agent_orchestrator.record_approval(run_id, request)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=423, detail=str(exc)) from exc
    if run is None:
        raise HTTPException(status_code=404, detail="agent run not found")
    return run


@app.post("/admin/disable")
def disable() -> dict[str, object]:
    service.disable()
    return {"disabled": service.disabled}


@app.post("/admin/enable")
def enable() -> dict[str, object]:
    service.enable()
    return {"disabled": service.disabled}
