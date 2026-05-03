from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .agent_stack import build_adaptation_repository, build_agent_orchestrator, build_selection_service
from .agent_tools import graph_context, retrieval_context
from .config import InfraSettings
from .cognitive import CognitiveAdaptationService
from .models import (
    AgentApprovalRequest,
    AgentRun,
    AgentRunRequest,
    CognitiveAdaptationRequest,
    CognitiveAdaptationResponse,
    ContextIngestRequest,
    ContextIngestResult,
    GraphIngestRequest,
    GraphIngestResult,
    RosterRecommendation,
    ScenarioApprovalRequest,
    ScenarioApprovalResponse,
    ScoreRequest,
    SourceReference,
)
from .security import ApiKeyGuard
from .service import SelectionService
from .shared_data import (
    build_context_update_events,
    build_direct_score_decision_snapshot,
    build_graph_update_events,
    build_kill_switch_update_event,
    attach_source_refs,
)


api_settings = InfraSettings.from_env()
app = FastAPI(
    title="System 2 Cognitive Mission Adaptation Engine",
    version=__version__,
    description=(
        "Adapts live training scenarios from field evidence, cognitive state "
        "estimates, instructor approval, uncertainty, fairness, and trace outputs."
    ),
)

if api_settings.cors_allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(api_settings.cors_allowed_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

api_key_guard = ApiKeyGuard(api_key=api_settings.api_key, admin_api_key=api_settings.admin_api_key)
service = build_selection_service(api_settings)
agent_orchestrator = build_agent_orchestrator(settings=api_settings, selection_service=service)
cognitive_service = CognitiveAdaptationService(
    audit_log=service.audit_log,
    shared_data_sink=agent_orchestrator.shared_data_sink,
    retriever=agent_orchestrator.retriever,
    repository=build_adaptation_repository(agent_orchestrator.settings),
)


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "version": __version__,
        "disabled": service.disabled,
        "infrastructure": api_settings.status(),
    }


@app.get("/v1/healthz")
def healthz() -> dict[str, object]:
    return health()


@app.post("/score", response_model=RosterRecommendation)
def score(
    request: ScoreRequest,
    _auth: None = Depends(api_key_guard.require_api_key),
) -> RosterRecommendation:
    try:
        recommendation = service.score(request)
        recommendation = attach_source_refs(recommendation, _direct_score_context_refs(request))
        agent_orchestrator.shared_data_sink.record_decision_snapshot(
            build_direct_score_decision_snapshot(request, recommendation)
        )
        return recommendation
    except RuntimeError as exc:
        raise HTTPException(status_code=423, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/v1/score", response_model=RosterRecommendation)
def score_v1(
    request: ScoreRequest,
    _auth: None = Depends(api_key_guard.require_api_key),
) -> RosterRecommendation:
    return score(request)


@app.post("/v1/adaptations", response_model=CognitiveAdaptationResponse)
def create_adaptation(
    request: CognitiveAdaptationRequest,
    _auth: None = Depends(api_key_guard.require_api_key),
) -> CognitiveAdaptationResponse:
    try:
        return cognitive_service.adapt(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/v1/adaptations/{adaptation_id}", response_model=CognitiveAdaptationResponse)
def get_adaptation(
    adaptation_id: str,
    _auth: None = Depends(api_key_guard.require_api_key),
) -> CognitiveAdaptationResponse:
    adaptation = cognitive_service.get(adaptation_id)
    if adaptation is None:
        raise HTTPException(status_code=404, detail="adaptation not found")
    return adaptation


@app.get("/v1/missions/{mission_id}/adaptations", response_model=list[CognitiveAdaptationResponse])
def list_mission_adaptations(
    mission_id: str,
    limit: int = 50,
    _auth: None = Depends(api_key_guard.require_api_key),
) -> list[CognitiveAdaptationResponse]:
    return cognitive_service.list_by_mission(mission_id, limit=limit)


@app.post("/v1/adaptations/{adaptation_id}/approval", response_model=ScenarioApprovalResponse)
def record_adaptation_approval(
    adaptation_id: str,
    request: ScenarioApprovalRequest,
    _auth: None = Depends(api_key_guard.require_api_key),
) -> ScenarioApprovalResponse:
    try:
        approval = cognitive_service.record_approval(adaptation_id, request)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if approval is None:
        raise HTTPException(status_code=404, detail="adaptation not found")
    return approval


@app.post("/v1/agent-runs", response_model=AgentRun)
def create_agent_run(
    request: AgentRunRequest,
    _auth: None = Depends(api_key_guard.require_api_key),
) -> AgentRun:
    return agent_orchestrator.run(request)


@app.get("/v1/agent-runs/{run_id}", response_model=AgentRun)
def get_agent_run(
    run_id: str,
    _auth: None = Depends(api_key_guard.require_api_key),
) -> AgentRun:
    run = agent_orchestrator.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="agent run not found")
    return run


@app.post("/v1/agent-runs/{run_id}/approval", response_model=AgentRun)
def record_agent_run_approval(
    run_id: str,
    request: AgentApprovalRequest,
    _auth: None = Depends(api_key_guard.require_api_key),
) -> AgentRun:
    try:
        run = agent_orchestrator.record_approval(run_id, request)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=423, detail=str(exc)) from exc
    if run is None:
        raise HTTPException(status_code=404, detail="agent run not found")
    return run


@app.post("/v1/context/chunks", response_model=ContextIngestResult)
def ingest_context_chunks(
    request: ContextIngestRequest,
    _auth: None = Depends(api_key_guard.require_api_key),
) -> ContextIngestResult:
    count = agent_orchestrator.retriever.upsert(request.chunks)
    for event in build_context_update_events(request.chunks):
        agent_orchestrator.shared_data_sink.append_update_event(event)
    return ContextIngestResult(
        backend=agent_orchestrator.settings.retrieval_backend,
        chunk_count=count,
        chunk_ids=[chunk.chunk_id for chunk in request.chunks],
    )


@app.post("/v1/graph/facts", response_model=GraphIngestResult)
def ingest_graph_facts(
    request: GraphIngestRequest,
    _auth: None = Depends(api_key_guard.require_api_key),
) -> GraphIngestResult:
    count = agent_orchestrator.graph_provider.upsert(request.facts)
    for event in build_graph_update_events(request.facts):
        agent_orchestrator.shared_data_sink.append_update_event(event)
    return GraphIngestResult(backend=agent_orchestrator.settings.graph_backend, fact_count=count)


@app.post("/admin/disable")
def disable(_auth: None = Depends(api_key_guard.require_admin_key)) -> dict[str, object]:
    service.disable()
    agent_orchestrator.shared_data_sink.append_update_event(
        build_kill_switch_update_event(disabled=True)
    )
    return {"disabled": service.disabled}


@app.post("/admin/enable")
def enable(_auth: None = Depends(api_key_guard.require_admin_key)) -> dict[str, object]:
    service.enable()
    agent_orchestrator.shared_data_sink.append_update_event(
        build_kill_switch_update_event(disabled=False)
    )
    return {"disabled": service.disabled}


def _direct_score_context_refs(request: ScoreRequest) -> list[SourceReference]:
    agent_request = AgentRunRequest(score_request=request, require_human_approval=False)
    refs: list[SourceReference] = []
    for _, evidence in (
        retrieval_context(api_settings, agent_orchestrator.retriever),
        graph_context(api_settings, agent_request, agent_orchestrator.graph_provider),
    ):
        raw_refs = evidence.get("source_refs", [])
        if not isinstance(raw_refs, list):
            continue
        for raw_ref in raw_refs:
            refs.append(SourceReference.model_validate(raw_ref))
    return refs
