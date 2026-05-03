from __future__ import annotations

from .adaptation_store import AdaptationRepository, InMemoryAdaptationRepository, PostgresAdaptationRepository
from .agent_orchestrator import AgentOrchestrator
from .agent_state import InMemoryAgentStateStore, RedisAgentStateStore
from .agent_store import AgentRunRepository, InMemoryAgentRunRepository
from .audit import AuditLog, AuditSink, PostgresAuditLog
from .candidate_pool import CandidatePoolResolver, InMemoryCandidatePoolResolver, PostgresCandidatePoolResolver
from .config import InfraSettings
from .deployment import (
    DeploymentRecommendationRepository,
    InMemoryDeploymentRecommendationRepository,
    PostgresDeploymentRecommendationRepository,
)
from .graph import FalkorDBGraphContextProvider, LocalGraphContextProvider
from .operational_twin import (
    InMemoryOperationalTwinRepository,
    OperationalTwinRepository,
    PostgresOperationalTwinRepository,
)
from .postgres_agent_store import PostgresAgentRunRepository
from .retrieval import LocalContextRetriever, PgVectorContextRetriever
from .service import SelectionService
from .shared_data import InMemorySharedDataSink, PostgresSharedDataSink, SharedDataSink


def build_agent_orchestrator(
    *,
    settings: InfraSettings | None = None,
    selection_service: SelectionService | None = None,
) -> AgentOrchestrator:
    resolved_settings = settings or InfraSettings.from_env()
    resolved_service = selection_service or build_selection_service(resolved_settings)
    return AgentOrchestrator(
        repository=build_agent_run_repository(resolved_settings),
        state_store=(
            RedisAgentStateStore(resolved_settings.redis_url)
            if resolved_settings.agent_state_backend == "redis" and resolved_settings.redis_url
            else InMemoryAgentStateStore()
        ),
        retriever=(
            PgVectorContextRetriever(resolved_settings.pgvector_url)
            if resolved_settings.retrieval_backend == "pgvector" and resolved_settings.pgvector_url
            else LocalContextRetriever()
        ),
        graph_provider=(
            FalkorDBGraphContextProvider(resolved_settings.falkordb_url)
            if resolved_settings.graph_backend == "falkordb" and resolved_settings.falkordb_url
            else LocalGraphContextProvider()
        ),
        selection_service=resolved_service,
        shared_data_sink=build_shared_data_sink(resolved_settings),
        settings=resolved_settings,
    )


def build_agent_run_repository(settings: InfraSettings) -> AgentRunRepository:
    if settings.agent_repository_backend == "postgres" and settings.database_url:
        return PostgresAgentRunRepository(settings.database_url)
    return InMemoryAgentRunRepository()


def build_adaptation_repository(settings: InfraSettings) -> AdaptationRepository:
    if settings.adaptation_repository_backend == "postgres" and settings.database_url:
        return PostgresAdaptationRepository(settings.database_url)
    return InMemoryAdaptationRepository()


def build_operational_twin_repository(settings: InfraSettings) -> OperationalTwinRepository:
    if settings.operational_twin_repository_backend == "postgres" and settings.database_url:
        return PostgresOperationalTwinRepository(settings.database_url)
    return InMemoryOperationalTwinRepository()


def build_deployment_repository(settings: InfraSettings) -> DeploymentRecommendationRepository:
    if settings.deployment_repository_backend == "postgres" and settings.database_url:
        return PostgresDeploymentRecommendationRepository(settings.database_url)
    return InMemoryDeploymentRecommendationRepository()


def build_selection_service(settings: InfraSettings | None = None) -> SelectionService:
    resolved_settings = settings or InfraSettings.from_env()
    return SelectionService(
        build_audit_log(resolved_settings),
        build_candidate_pool_resolver(resolved_settings),
    )


def build_audit_log(settings: InfraSettings) -> AuditSink:
    if settings.audit_backend == "postgres" and settings.database_url:
        return PostgresAuditLog(settings.database_url)
    return AuditLog(settings.audit_log_path)


def build_shared_data_sink(settings: InfraSettings) -> SharedDataSink:
    if settings.shared_data_backend == "postgres" and settings.database_url:
        return PostgresSharedDataSink(settings.database_url)
    return InMemorySharedDataSink()


def build_candidate_pool_resolver(settings: InfraSettings) -> CandidatePoolResolver:
    if settings.candidate_pool_backend == "postgres" and settings.database_url:
        return PostgresCandidatePoolResolver(settings.database_url)
    return InMemoryCandidatePoolResolver()
