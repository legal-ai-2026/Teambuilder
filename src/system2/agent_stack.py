from __future__ import annotations

from .agent_orchestrator import AgentOrchestrator
from .agent_state import InMemoryAgentStateStore, RedisAgentStateStore
from .agent_store import AgentRunRepository, InMemoryAgentRunRepository
from .audit import AuditLog, AuditSink, PostgresAuditLog
from .config import InfraSettings
from .graph import FalkorDBGraphContextProvider, LocalGraphContextProvider
from .postgres_agent_store import PostgresAgentRunRepository
from .retrieval import LocalContextRetriever, PgVectorContextRetriever
from .service import SelectionService


def build_agent_orchestrator(
    *,
    settings: InfraSettings | None = None,
    selection_service: SelectionService | None = None,
) -> AgentOrchestrator:
    resolved_settings = settings or InfraSettings.from_env()
    resolved_service = selection_service or SelectionService(build_audit_log(resolved_settings))
    return AgentOrchestrator(
        repository=build_agent_run_repository(resolved_settings),
        state_store=(
            RedisAgentStateStore(resolved_settings.redis_url)
            if resolved_settings.agent_state_backend == "redis" and resolved_settings.redis_url
            else InMemoryAgentStateStore()
        ),
        retriever=(
            PgVectorContextRetriever(resolved_settings.database_url)
            if resolved_settings.retrieval_backend == "pgvector" and resolved_settings.database_url
            else LocalContextRetriever()
        ),
        graph_provider=(
            FalkorDBGraphContextProvider(resolved_settings.falkordb_url)
            if resolved_settings.graph_backend == "falkordb" and resolved_settings.falkordb_url
            else LocalGraphContextProvider()
        ),
        selection_service=resolved_service,
        settings=resolved_settings,
    )


def build_agent_run_repository(settings: InfraSettings) -> AgentRunRepository:
    if settings.agent_repository_backend == "postgres" and settings.database_url:
        return PostgresAgentRunRepository(settings.database_url)
    return InMemoryAgentRunRepository()


def build_selection_service(settings: InfraSettings | None = None) -> SelectionService:
    resolved_settings = settings or InfraSettings.from_env()
    return SelectionService(build_audit_log(resolved_settings))


def build_audit_log(settings: InfraSettings) -> AuditSink:
    if settings.audit_backend == "postgres" and settings.database_url:
        return PostgresAuditLog(settings.database_url)
    return AuditLog(settings.audit_log_path)
