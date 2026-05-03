from __future__ import annotations

from .config import InfraSettings
from .graph import GraphContextProvider, GraphFact
from .models import AgentRunRequest, RosterRecommendation
from .retrieval import ContextRetriever, RetrievedContext
from .service import SelectionService
from .shared_data import canonical_hash


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
            "source_refs": [_context_source_ref(context) for context in contexts],
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
            "source_refs": [_graph_source_ref(fact) for fact in facts],
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


def _context_source_ref(context: RetrievedContext) -> dict[str, object]:
    chunk_id = str(context.metadata.get("chunk_id", f"{context.source}:{context.title}"))
    source_hash = context.metadata.get("source_hash") or canonical_hash(
        {
            "source": context.source,
            "title": context.title,
            "content": context.content,
        }
    )
    return {
        "ref": f"pgvector://system2_context_chunks/{chunk_id}",
        "role": "retrieval_context",
        "source_hash": str(source_hash),
        "metadata": {
            "source": context.source,
            "title": context.title,
            "score": context.score,
        },
    }


def _graph_source_ref(fact: GraphFact) -> dict[str, object]:
    fact_id = str(
        fact.metadata.get("fact_id")
        or canonical_hash(
            {
                "subject": fact.subject,
                "predicate": fact.predicate,
                "object": fact.object,
                "metadata": fact.metadata,
            }
        ).removeprefix("sha256:")[:16]
    )
    return {
        "ref": f"falkordb://system2/facts/{fact_id}",
        "role": "graph_fact",
        "source_hash": canonical_hash(
            {
                "subject": fact.subject,
                "predicate": fact.predicate,
                "object": fact.object,
                "metadata": fact.metadata,
            }
        ),
        "metadata": {
            "subject": fact.subject,
            "predicate": fact.predicate,
            "object": fact.object,
        },
    }
