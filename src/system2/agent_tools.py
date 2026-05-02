from __future__ import annotations

from .config import InfraSettings
from .graph import GraphContextProvider
from .models import AgentRunRequest, RosterRecommendation
from .retrieval import ContextRetriever
from .service import SelectionService


def request_context(request: AgentRunRequest) -> tuple[str, dict[str, object]]:
    score_request = request.score_request
    candidate_source = "provided" if score_request.candidates else "synthetic"
    role_source = "provided" if score_request.roles else "default"
    return (
        "Loaded mission, candidate, and role context.",
        {
            "mission_id": score_request.mission_id,
            "candidate_source": candidate_source,
            "candidate_count": len(score_request.candidates) if score_request.candidates else score_request.candidate_count,
            "role_source": role_source,
            "role_count": len(score_request.roles) if score_request.roles else 14,
            "seed": score_request.seed,
        },
    )


def retrieval_context(settings: InfraSettings, retriever: ContextRetriever) -> tuple[str, dict[str, object]]:
    configured = settings.database_url is not None and settings.pgvector_enabled
    contexts = retriever.retrieve("mission roster recommendation protected attributes", limit=3)
    summary = (
        "pgvector retrieval is configured for doctrine, SOP, and prior-decision context."
        if configured
        else "pgvector retrieval is not configured; using request-local and packaged feature context."
    )
    return (
        summary,
        {
            "postgres_configured": settings.database_url is not None,
            "pgvector_enabled": settings.pgvector_enabled,
            "backend": settings.retrieval_backend,
            "retrieved_context_count": len(contexts),
            "sources": [context.source for context in contexts],
            "context_sources": ["assets/feature-spec.md", "request"],
        },
    )


def graph_context(
    settings: InfraSettings,
    request: AgentRunRequest,
    graph_provider: GraphContextProvider,
) -> tuple[str, dict[str, object]]:
    configured = settings.falkordb_url is not None
    facts = graph_provider.mission_context(request)
    summary = (
        "FalkorDB graph context is configured for relationship and constraint lookup."
        if configured
        else "FalkorDB graph context is not configured; using request-local role constraints."
    )
    return (
        summary,
        {
            "falkordb_configured": configured,
            "backend": settings.graph_backend,
            "fact_count": len(facts),
            "relationship_types": [
                "soldier_skill",
                "mission_role",
                "unit_history",
                "qualification_policy",
                "prior_assignment",
            ],
        },
    )


def roster_recommendation_tool(
    selection_service: SelectionService,
    request: AgentRunRequest,
) -> RosterRecommendation:
    return selection_service.score(request.score_request)
