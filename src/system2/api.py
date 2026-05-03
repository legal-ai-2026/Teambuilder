from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .agent_stack import (
    build_adaptation_repository,
    build_agent_orchestrator,
    build_deployment_repository,
    build_operational_twin_repository,
    build_selection_service,
)
from .agent_tools import ContextualScoringInput, contextual_scoring_input
from .config import InfraSettings
from .cognitive import CognitiveAdaptationService
from .deployment import DeploymentRecommendationService
from .models import (
    AgentApprovalRequest,
    AgentRun,
    AgentRunRequest,
    CognitiveAdaptationRequest,
    CognitiveAdaptationResponse,
    ContextIngestRequest,
    ContextIngestResult,
    DeploymentApprovalRequest,
    DeploymentApprovalResponse,
    DeploymentOutcomeRequest,
    DeploymentOutcomeResponse,
    DeploymentRecommendationRequest,
    DeploymentRecommendationResponse,
    GraphIngestRequest,
    GraphIngestResult,
    OperationalTwinOutcomeRequest,
    OperationalTwinOutcomeResponse,
    OperationalTwinRequest,
    OperationalTwinResponse,
    RosterRecommendation,
    ScenarioApprovalRequest,
    ScenarioApprovalResponse,
    ScenarioOptionDecisionRequest,
    ScenarioOptionDecisionResponse,
    ScoreRequest,
    SourceReference,
)
from .llm import OpenAIJsonAgentClient
from .operational_twin import OperationalTwinService
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
operational_twin_service = OperationalTwinService(
    audit_log=service.audit_log,
    shared_data_sink=agent_orchestrator.shared_data_sink,
    repository=build_operational_twin_repository(agent_orchestrator.settings),
    agent_provider=api_settings.agentic_provider,
    llm_client=(
        OpenAIJsonAgentClient(
            api_key=api_settings.openai_api_key,
            model=api_settings.openai_model,
            base_url=api_settings.openai_base_url,
            timeout_seconds=api_settings.agentic_timeout_seconds,
        )
        if api_settings.openai_api_key
        and api_settings.agentic_provider in {"auto", "openai"}
        else None
    ),
    llm_model=api_settings.openai_model,
    agent_max_retries=api_settings.agentic_max_retries,
)
deployment_service = DeploymentRecommendationService(
    operational_twin_service=operational_twin_service,
    audit_log=service.audit_log,
    shared_data_sink=agent_orchestrator.shared_data_sink,
    repository=build_deployment_repository(agent_orchestrator.settings),
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
        context_input = _direct_score_context_input(request)
        recommendation = service.score(request, context_adjustments=context_input.adjustments)
        recommendation = attach_source_refs(
            recommendation,
            [SourceReference.model_validate(ref) for ref in context_input.source_refs],
        )
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


@app.post("/v1/operational-twin/runs", response_model=OperationalTwinResponse)
def create_operational_twin_run(
    request: OperationalTwinRequest,
    _auth: None = Depends(api_key_guard.require_api_key),
) -> OperationalTwinResponse:
    try:
        return operational_twin_service.run(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/v1/operational-twin/runs/{twin_run_id}", response_model=OperationalTwinResponse)
def get_operational_twin_run(
    twin_run_id: str,
    _auth: None = Depends(api_key_guard.require_api_key),
) -> OperationalTwinResponse:
    run = operational_twin_service.get(twin_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="operational twin run not found")
    return run


@app.post(
    "/v1/operational-twin/runs/{twin_run_id}/options/{scenario_option_id}/decision",
    response_model=ScenarioOptionDecisionResponse,
)
def record_operational_twin_option_decision(
    twin_run_id: str,
    scenario_option_id: str,
    request: ScenarioOptionDecisionRequest,
    _auth: None = Depends(api_key_guard.require_api_key),
) -> ScenarioOptionDecisionResponse:
    if request.scenario_option_id != scenario_option_id:
        raise HTTPException(status_code=422, detail="scenario option id mismatch")
    try:
        decision = operational_twin_service.record_decision(twin_run_id, request)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if decision is None:
        raise HTTPException(status_code=404, detail="operational twin run not found")
    return decision


@app.post(
    "/v1/operational-twin/runs/{twin_run_id}/outcome",
    response_model=OperationalTwinOutcomeResponse,
)
def record_operational_twin_outcome(
    twin_run_id: str,
    request: OperationalTwinOutcomeRequest,
    _auth: None = Depends(api_key_guard.require_api_key),
) -> OperationalTwinOutcomeResponse:
    try:
        outcome = operational_twin_service.record_outcome(twin_run_id, request)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if outcome is None:
        raise HTTPException(status_code=404, detail="operational twin run not found")
    return outcome


@app.post("/v1/deployment-recommendations", response_model=DeploymentRecommendationResponse)
def create_deployment_recommendation(
    request: DeploymentRecommendationRequest,
    _auth: None = Depends(api_key_guard.require_api_key),
) -> DeploymentRecommendationResponse:
    try:
        return deployment_service.recommend(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(
    "/v1/deployment-recommendations/{deployment_recommendation_id}",
    response_model=DeploymentRecommendationResponse,
)
def get_deployment_recommendation(
    deployment_recommendation_id: str,
    _auth: None = Depends(api_key_guard.require_api_key),
) -> DeploymentRecommendationResponse:
    recommendation = deployment_service.get(deployment_recommendation_id)
    if recommendation is None:
        raise HTTPException(status_code=404, detail="deployment recommendation not found")
    return recommendation


@app.get(
    "/v1/missions/{mission_id}/deployment-recommendations",
    response_model=list[DeploymentRecommendationResponse],
)
def list_mission_deployment_recommendations(
    mission_id: str,
    limit: int = 50,
    _auth: None = Depends(api_key_guard.require_api_key),
) -> list[DeploymentRecommendationResponse]:
    return deployment_service.list_by_mission(mission_id, limit=limit)


@app.post(
    "/v1/deployment-recommendations/{deployment_recommendation_id}/approval",
    response_model=DeploymentApprovalResponse,
)
def record_deployment_recommendation_approval(
    deployment_recommendation_id: str,
    request: DeploymentApprovalRequest,
    _auth: None = Depends(api_key_guard.require_api_key),
) -> DeploymentApprovalResponse:
    try:
        approval = deployment_service.record_approval(deployment_recommendation_id, request)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if approval is None:
        raise HTTPException(status_code=404, detail="deployment recommendation not found")
    return approval


@app.post(
    "/v1/deployment-recommendations/{deployment_recommendation_id}/outcome",
    response_model=DeploymentOutcomeResponse,
)
def record_deployment_recommendation_outcome(
    deployment_recommendation_id: str,
    request: DeploymentOutcomeRequest,
    _auth: None = Depends(api_key_guard.require_api_key),
) -> DeploymentOutcomeResponse:
    try:
        outcome = deployment_service.record_outcome(deployment_recommendation_id, request)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if outcome is None:
        raise HTTPException(status_code=404, detail="deployment recommendation not found")
    return outcome


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


def _direct_score_context_input(request: ScoreRequest) -> ContextualScoringInput:
    agent_request = AgentRunRequest(score_request=request, require_human_approval=False)
    return contextual_scoring_input(
        agent_request,
        agent_orchestrator.retriever,
        agent_orchestrator.graph_provider,
    )
