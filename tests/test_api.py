import pytest

from fastapi import HTTPException
from pydantic import ValidationError

from system2.api import (
    create_agent_run,
    create_adaptation,
    disable,
    enable,
    get_adaptation,
    get_agent_run,
    ingest_context_chunks,
    ingest_graph_facts,
    list_mission_adaptations,
    record_adaptation_approval,
    record_agent_run_approval,
    score,
    score_v1,
)
from system2.adaptation_store import (
    ADAPTATION_SCHEMA_SQL,
    InMemoryAdaptationRepository,
    dump_adaptation,
    load_adaptation,
)
from system2.agent_orchestrator import AgentOrchestrator
from system2.agent_state import InMemoryAgentStateStore, RedisAgentStateStore
from system2.agent_stack import build_adaptation_repository, build_agent_orchestrator, build_audit_log, build_shared_data_sink
from system2.agent_store import InMemoryAgentRunRepository
from system2.audit import (
    POSTGRES_AUDIT_SCHEMA_SQL,
    AuditLog,
    PostgresAuditLog,
    build_audit_record,
    validate_audit_records,
    validate_hash_chain,
)
from system2.candidate_pool import (
    CANDIDATE_POOL_SCHEMA_SQL,
    InMemoryCandidatePoolResolver,
    _soldier_from_row,
    build_local_candidate_pool_source_refs,
)
from system2.config import InfraSettings, redact_url
from system2.cognitive import CognitiveAdaptationService
from system2.data import default_roles, generate_soldiers
from system2.fairness import counterfactual_flip_audit, fairness_audit, mutual_information_proxy_audit
from system2.models import (
    AgentApprovalDecision,
    AgentApprovalRequest,
    AgentRunRequest,
    AgentRunStatus,
    AdaptationConstraints,
    CognitiveAdaptationRequest,
    ContextChunkInput,
    ContextIngestRequest,
    GraphFactInput,
    GraphIngestRequest,
    RoleRequirement,
    ScenarioApprovalRequest,
    ScoreRequest,
    TrainingEvidence,
)
from system2.postgres_agent_store import AGENT_RUNS_SCHEMA_SQL, dump_agent_run, load_agent_run
from system2.registry import MODEL_VERSIONS
from system2.graph import LocalGraphContextProvider, cypher_identifier, cypher_quote, parse_falkordb_rows
from system2.retrieval import PGVECTOR_SCHEMA_SQL, LocalContextRetriever, PgVectorContextRetriever, embedding_literal
from system2.scoring import feature_hash, role_fit
from system2.security import ApiKeyGuard
from system2.service import SelectionService
from system2.shared_data import (
    SHARED_DATA_SCHEMA_SQL,
    InMemorySharedDataSink,
    build_context_update_events,
    build_graph_update_events,
)


def test_score_returns_roster_and_audit() -> None:
    enable()

    payload = score(ScoreRequest(mission_id="raid-tonight", candidate_count=80, seed=7))

    assert payload.mission_id == "raid-tonight"
    assert len(payload.roster) == 14
    assert len(payload.second_choice_roster) == 14
    assert payload.fairness_audit.status in {"pass", "halt"}
    assert payload.career_forecast.horizon_years == 5
    assert payload.trace.model_versions["assignment"] == MODEL_VERSIONS["assignment"]
    assert payload.trace.model_versions["fairness_metrics"] == MODEL_VERSIONS["fairness_metrics"]
    assert len(payload.trace.calibration_bins) == 10
    assert len(payload.trace.disagreement_histogram) == 10

    for assessment in payload.roster + payload.second_choice_roster:
        assert assessment.model_disagreement == pytest.approx(
            abs(assessment.p_success_tabpfn - assessment.p_success_bayes_mean)
        )


def test_cognitive_adaptation_recommends_instructor_approved_scenario_changes() -> None:
    payload = create_adaptation(
        CognitiveAdaptationRequest(
            mission_id="raid-tonight",
            instructor_id="instructor-1",
            team_id="alpha",
            target_soldier_ids=["RGR-0001"],
            evidence=[
                TrainingEvidence(
                    evidence_id="obs-001",
                    source_type="voice_note",
                    text=(
                        "Soldier handled direct contact, but missed the second-order "
                        "relationship between terrain, timing, support, civilian movement, "
                        "and a delayed comms relay under moderate fatigue."
                    ),
                    soldier_ids=["RGR-0001"],
                    tags=["systems_thinking", "fatigue"],
                    metrics={"sleep_hours": 4.5},
                )
            ],
        )
    )

    assert payload.status == "pending_approval"
    assert payload.state.primary_development_dimension == "systems_thinking"
    assert len(payload.recommendations) == 3
    assert payload.blocked_recommendations == []
    assert payload.trace.input_source_hashes
    assert any("comms relay" in item.proposed_inject for item in payload.recommendations)
    assert get_adaptation(payload.adaptation_id).adaptation_id == payload.adaptation_id
    assert any(
        adaptation.adaptation_id == payload.adaptation_id
        for adaptation in list_mission_adaptations("raid-tonight")
    )

    approval = record_adaptation_approval(
        payload.adaptation_id,
        ScenarioApprovalRequest(
            recommendation_id=payload.recommendations[0].recommendation_id,
            decision=AgentApprovalDecision.approved,
            approver_id="instructor-1",
            rationale="Good targeted pressure for the next lane.",
        ),
    )

    assert approval.status == "completed"
    assert approval.approved_inject is not None
    assert approval.approved_inject.recommendation_id == payload.recommendations[0].recommendation_id
    assert get_adaptation(payload.adaptation_id).status == "completed"


def test_cognitive_adaptation_safety_auditor_blocks_excess_risk(tmp_path) -> None:
    sink = InMemorySharedDataSink()
    service = CognitiveAdaptationService(
        audit_log=AuditLog(tmp_path / "audit.jsonl"),
        shared_data_sink=sink,
    )

    payload = service.adapt(
        CognitiveAdaptationRequest(
            mission_id="raid-tonight",
            instructor_id="instructor-1",
            team_id="alpha",
            constraints=AdaptationConstraints(max_safety_risk="low"),
            evidence=[
                TrainingEvidence(
                    evidence_id="obs-002",
                    source_type="checklist",
                    text="Leader made a single option assumption and failed to question a contradictory report.",
                    tags=["critical_thinking"],
                )
            ],
        )
    )

    assert len(payload.recommendations) == 1
    assert payload.recommendations[0].inject_type == "skill_isolation"
    assert len(payload.blocked_recommendations) == 2
    assert all(item.status == "blocked" for item in payload.blocked_recommendations)
    assert sink.update_events[-1]["entity_type"] == "scenario_adaptation"


def test_adaptation_repository_round_trips_serialized_payload(tmp_path) -> None:
    service = CognitiveAdaptationService(audit_log=AuditLog(tmp_path / "audit.jsonl"))
    payload = service.adapt(
        CognitiveAdaptationRequest(
            mission_id="serialization-mission",
            instructor_id="instructor-1",
            team_id="alpha",
            evidence=[
                TrainingEvidence(
                    evidence_id="obs-serialize",
                    source_type="aar",
                    text="Improving systems thinking after terrain and timing review.",
                    tags=["improving", "systems_thinking"],
                )
            ],
        )
    )

    loaded = load_adaptation(dump_adaptation(payload))

    assert loaded == payload


def test_kill_switch_blocks_scoring() -> None:
    disabled = disable()
    assert disabled == {"disabled": True}

    with pytest.raises(HTTPException) as exc_info:
        score(ScoreRequest(candidate_count=80))

    assert exc_info.value.status_code == 423
    assert "disabled" in exc_info.value.detail

    enable()


def test_kill_switch_blocks_versioned_scoring() -> None:
    disable()

    with pytest.raises(HTTPException) as exc_info:
        score_v1(ScoreRequest(candidate_count=80))

    assert exc_info.value.status_code == 423
    enable()


def test_inbound_contract_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ScoreRequest(candidate_count=80, unknown_field=True)


def test_infra_settings_redacts_connection_urls() -> None:
    settings = InfraSettings.from_env(
        {
            "DATABASE_URL": "postgresql://app_user:secret@pgbouncer.internal:6432/system2",
            "PGVECTOR_CONNECTION_STRING": "postgresql+psycopg://app_user:secret@pgbouncer.internal:6432/system2",
            "REDIS_URL": "redis://:redis_secret@redis.internal:6379/0",
            "FALKORDB_URL": "redis://graph_user:graph_secret@falkordb.internal:6379",
            "PGVECTOR_ENABLED": "true",
            "AUDIT_BACKEND": "postgres",
            "AGENT_REPOSITORY_BACKEND": "postgres",
            "AGENT_STATE_BACKEND": "redis",
            "RETRIEVAL_BACKEND": "pgvector",
            "GRAPH_BACKEND": "falkordb",
            "SHARED_DATA_BACKEND": "postgres",
            "SYSTEM2_AUDIT_LOG": "/var/log/system2/audit.jsonl",
            "SYSTEM2_CORS_ORIGINS": "http://localhost:3000, http://127.0.0.1:3000",
            "SYSTEM2_API_KEY": "service-secret",
            "SYSTEM2_ADMIN_API_KEY": "admin-secret",
        }
    )

    status = settings.status()

    assert status["postgres"]["configured"] is True
    assert status["postgres"]["pgvector_enabled"] is True
    assert status["postgres"]["url"] == "postgresql://app_user:***@pgbouncer.internal:6432/system2"
    assert status["postgres"]["pgvector_url"] == "postgresql://app_user:***@pgbouncer.internal:6432/system2"
    assert status["redis"]["url"] == "redis://redis.internal:6379/0"
    assert status["falkordb"]["url"] == "redis://graph_user:***@falkordb.internal:6379"
    assert status["backends"] == {
        "adaptation_repository": "postgres",
        "audit": "postgres",
        "agent_repository": "postgres",
        "agent_state": "redis",
        "candidate_pool": "postgres",
        "retrieval": "pgvector",
        "graph": "falkordb",
        "shared_data": "postgres",
    }
    assert settings.cors_allowed_origins == ("http://localhost:3000", "http://127.0.0.1:3000")
    assert settings.api_key == "service-secret"
    assert settings.admin_api_key == "admin-secret"
    assert status["security"] == {
        "api_key_required": True,
        "admin_api_key_required": True,
        "cors_allowed_origins": ["http://localhost:3000", "http://127.0.0.1:3000"],
    }
    assert redact_url(None) is None


def test_infra_settings_default_to_local_backends() -> None:
    settings = InfraSettings.from_env({})

    assert settings.adaptation_repository_backend == "memory"
    assert settings.agent_repository_backend == "memory"
    assert settings.agent_state_backend == "memory"
    assert settings.audit_backend == "file"
    assert settings.candidate_pool_backend == "local"
    assert settings.retrieval_backend == "local"
    assert settings.graph_backend == "local"
    assert settings.shared_data_backend == "memory"
    assert settings.api_key is None
    assert settings.admin_api_key is None
    assert settings.cors_allowed_origins == ()


def test_infra_settings_treats_blank_security_values_as_unset() -> None:
    settings = InfraSettings.from_env(
        {
            "SYSTEM2_API_KEY": "   ",
            "SYSTEM2_ADMIN_API_KEY": "",
            "SYSTEM2_CORS_ORIGINS": " , ",
        }
    )

    assert settings.api_key is None
    assert settings.admin_api_key is None
    assert settings.cors_allowed_origins == ()


def test_infra_settings_uses_service_key_as_admin_fallback() -> None:
    settings = InfraSettings.from_env({"SYSTEM2_API_KEY": "service-secret"})

    assert settings.api_key == "service-secret"
    assert settings.admin_api_key == "service-secret"


def test_api_key_guard_allows_unconfigured_local_mode() -> None:
    guard = ApiKeyGuard()

    guard.require_api_key()
    guard.require_admin_key()


def test_api_key_guard_accepts_x_api_key_and_bearer_token() -> None:
    guard = ApiKeyGuard(api_key="service-secret", admin_api_key="admin-secret")

    guard.require_api_key(x_api_key="service-secret")
    guard.require_api_key(authorization="Bearer service-secret")
    guard.require_admin_key(x_api_key="admin-secret")


def test_api_key_guard_rejects_missing_or_wrong_keys() -> None:
    guard = ApiKeyGuard(api_key="service-secret", admin_api_key="admin-secret")

    with pytest.raises(HTTPException) as missing_service:
        guard.require_api_key()
    with pytest.raises(HTTPException) as wrong_admin:
        guard.require_admin_key(x_api_key="service-secret")

    assert missing_service.value.status_code == 401
    assert wrong_admin.value.status_code == 401


def test_infra_settings_supports_graph_stack_env_shape() -> None:
    settings = InfraSettings.from_env(
        {
            "FALKORDB_HOST": "192.168.0.245",
            "FALKORDB_PORT": "6379",
            "FALKORDB_URL": "redis://:secret@192.168.0.245:6379",
            "REDIS_URL": "redis://:secret@192.168.0.250:6379/0",
            "DATABASE_URL": "postgresql://graphmem:secret@192.168.0.251:5432/graphmem",
            "PGVECTOR_CONNECTION_STRING": "postgresql+psycopg://graphmem:secret@192.168.0.251:5432/graphmem",
        }
    )

    assert settings.database_url == "postgresql://graphmem:secret@192.168.0.251:5432/graphmem"
    assert settings.pgvector_url == "postgresql://graphmem:secret@192.168.0.251:5432/graphmem"
    assert settings.pgvector_enabled is True
    assert settings.adaptation_repository_backend == "postgres"
    assert settings.audit_backend == "postgres"
    assert settings.agent_repository_backend == "postgres"
    assert settings.agent_state_backend == "redis"
    assert settings.candidate_pool_backend == "postgres"
    assert settings.retrieval_backend == "pgvector"
    assert settings.graph_backend == "falkordb"
    assert settings.shared_data_backend == "postgres"


def test_infra_settings_can_load_env_file(tmp_path) -> None:
    env_path = tmp_path / "infra.env"
    env_path.write_text(
        "\n".join(
            [
                "# generated outside the repo",
                "DATABASE_URL=postgresql://graphmem:secret@192.168.0.251:5432/graphmem",
                "PGVECTOR_CONNECTION_STRING=postgresql+psycopg://graphmem:secret@192.168.0.251:5432/graphmem",
                "REDIS_URL='redis://:secret@192.168.0.250:6379/0'",
                "FALKORDB_URL=\"redis://:secret@192.168.0.245:6379\"",
            ]
        ),
        encoding="utf-8",
    )

    settings = InfraSettings.from_env({"SYSTEM2_ENV_FILE": str(env_path)})

    assert settings.database_url == "postgresql://graphmem:secret@192.168.0.251:5432/graphmem"
    assert settings.redis_url == "redis://:secret@192.168.0.250:6379/0"
    assert settings.falkordb_url == "redis://:secret@192.168.0.245:6379"
    assert settings.retrieval_backend == "pgvector"


def test_audit_records_redact_and_validate_hash_chain() -> None:
    first = build_audit_record(
        "candidate_seen",
        {"unit_id": "U-01", "mos": "11B", "protected_race": "group_a"},
        "0" * 64,
    )
    second = build_audit_record("decision_seen", {"mission_id": "m-1"}, first["record_hash"])

    assert "protected_race" not in first["payload"]
    assert first["payload"]["unit_id"] != "U-01"
    assert first["payload"]["mos"] != "11B"
    assert validate_audit_records([first, second])


def test_postgres_audit_schema_is_hash_chained() -> None:
    assert "CREATE TABLE IF NOT EXISTS system2_audit_log" in POSTGRES_AUDIT_SCHEMA_SQL
    assert "previous_hash text NOT NULL" in POSTGRES_AUDIT_SCHEMA_SQL
    assert "record_hash text NOT NULL UNIQUE" in POSTGRES_AUDIT_SCHEMA_SQL


def test_build_audit_log_uses_file_backend_by_default() -> None:
    audit_log = build_audit_log(InfraSettings.from_env({}))

    assert isinstance(audit_log, AuditLog)


def test_agent_run_repository_tracks_runs() -> None:
    repository = InMemoryAgentRunRepository()
    request = AgentRunRequest(score_request=ScoreRequest(candidate_count=80, seed=21))

    run = repository.create(request)

    assert run.status is AgentRunStatus.queued
    assert run.request.score_request.seed == 21
    assert repository.get(run.run_id) == run

    saved = repository.save(run.model_copy(update={"status": AgentRunStatus.running}))

    assert saved.status is AgentRunStatus.running
    assert saved.updated_at >= run.updated_at
    assert repository.get(run.run_id) == saved


def test_postgres_agent_run_payload_round_trips() -> None:
    repository = InMemoryAgentRunRepository()
    run = repository.create(AgentRunRequest(score_request=ScoreRequest(candidate_count=80, seed=22)))

    loaded = load_agent_run(dump_agent_run(run))

    assert loaded == run
    assert "CREATE TABLE IF NOT EXISTS system2_agent_runs" in AGENT_RUNS_SCHEMA_SQL
    assert "payload jsonb NOT NULL" in AGENT_RUNS_SCHEMA_SQL


def test_shared_data_schema_contains_update_and_snapshot_tables() -> None:
    assert "CREATE TABLE IF NOT EXISTS entity_update_events" in SHARED_DATA_SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS decision_snapshots" in SHARED_DATA_SCHEMA_SQL
    assert "input_source_hashes jsonb NOT NULL" in SHARED_DATA_SCHEMA_SQL


def test_candidate_pool_schema_contains_shared_projection_tables() -> None:
    assert "CREATE TABLE IF NOT EXISTS candidate_pools_current" in CANDIDATE_POOL_SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS soldiers_current" in CANDIDATE_POOL_SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS role_slots_current" in CANDIDATE_POOL_SCHEMA_SQL


def test_adaptation_schema_contains_lookup_indexes() -> None:
    assert "CREATE TABLE IF NOT EXISTS system2_adaptations" in ADAPTATION_SCHEMA_SQL
    assert "idx_system2_adaptations_mission_id" in ADAPTATION_SCHEMA_SQL
    assert "idx_system2_adaptations_status" in ADAPTATION_SCHEMA_SQL


def test_build_adaptation_repository_uses_memory_by_default() -> None:
    repository = build_adaptation_repository(InfraSettings.from_env({}))

    assert isinstance(repository, InMemoryAdaptationRepository)


def test_build_shared_data_sink_uses_memory_by_default() -> None:
    sink = build_shared_data_sink(InfraSettings.from_env({}))

    assert isinstance(sink, InMemorySharedDataSink)


def test_memory_agent_state_tracks_status_and_locks() -> None:
    state = InMemoryAgentStateStore()

    state.set_status("run-1", AgentRunStatus.running)

    assert state.get_status("run-1") is AgentRunStatus.running
    assert state.acquire_lock("run-1") is True
    assert state.acquire_lock("run-1") is False

    state.release_lock("run-1")

    assert state.acquire_lock("run-1") is True


def test_redis_agent_state_uses_scoped_keys() -> None:
    store = RedisAgentStateStore("redis://redis.internal:6379/0", client=object(), key_prefix="test")

    assert store._status_key("abc") == "test:agent-run:abc:status"
    assert store._lock_key("abc") == "test:agent-run:abc:lock"


def test_local_context_retriever_returns_packaged_context() -> None:
    contexts = LocalContextRetriever().retrieve("protected attributes", limit=1)

    assert len(contexts) == 1
    assert contexts[0].source == "assets/feature-spec.md"
    assert "Protected attributes" in contexts[0].content


def test_local_context_retriever_ingests_chunks() -> None:
    retriever = LocalContextRetriever()

    count = retriever.upsert(
        [
            ContextChunkInput(
                chunk_id="policy-1",
                source="policy",
                title="Commander approval",
                content="Human approval is required before finalizing recommendations.",
                metadata={"kind": "policy"},
            )
        ]
    )
    contexts = retriever.retrieve("approval finalizing", limit=1)

    assert count == 1
    assert contexts[0].metadata["chunk_id"] == "policy-1"


def test_context_ingest_api_uses_configured_retriever() -> None:
    result = ingest_context_chunks(
        ContextIngestRequest(
            chunks=[
                ContextChunkInput(
                    chunk_id="api-context-1",
                    source="operator-note",
                    title="Approval note",
                    content="Route approval through the authorized command reviewer.",
                )
            ]
        )
    )

    assert result.chunk_count == 1
    assert result.chunk_ids == ["api-context-1"]


def test_pgvector_schema_and_embedding_literal_are_stable() -> None:
    assert "CREATE EXTENSION IF NOT EXISTS vector" in PGVECTOR_SCHEMA_SQL
    assert "embedding vector(1536)" in PGVECTOR_SCHEMA_SQL
    assert embedding_literal([0.1, 0.25, 1]) == "[0.1,0.25,1]"


def test_pgvector_retriever_can_defer_migration() -> None:
    retriever = PgVectorContextRetriever("postgresql://db.internal/system2", auto_migrate=False)

    assert retriever.database_url == "postgresql://db.internal/system2"
    assert retriever.table_name == "system2_context_chunks"


def test_local_graph_context_provider_returns_request_facts() -> None:
    facts = LocalGraphContextProvider().mission_context(
        AgentRunRequest(score_request=ScoreRequest(mission_id="mission-1", candidate_count=80))
    )

    assert facts[0].subject == "mission-1"
    assert facts[0].predicate == "uses_role_source"
    assert cypher_quote("a'b") == "'a\\'b'"


def test_local_graph_context_provider_ingests_facts() -> None:
    provider = LocalGraphContextProvider()

    count = provider.upsert(
        [
            GraphFactInput(
                subject="mission-1",
                predicate="requires_skill",
                object="casevac",
                metadata={"source": "operator"},
            )
        ]
    )
    facts = provider.mission_context(
        AgentRunRequest(score_request=ScoreRequest(mission_id="mission-1", candidate_count=80))
    )

    assert count == 1
    assert any(fact.predicate == "requires_skill" and fact.object == "casevac" for fact in facts)
    assert cypher_identifier("requires-skill") == "REQUIRES_SKILL"


def test_graph_ingest_api_uses_configured_provider() -> None:
    result = ingest_graph_facts(
        GraphIngestRequest(
            facts=[
                GraphFactInput(
                    subject="mission-api",
                    predicate="requires_role",
                    object="medic",
                )
            ]
        )
    )

    assert result.fact_count == 1


def test_falkordb_row_parser_handles_graph_query_rows() -> None:
    raw = [["subject", "predicate", "object"], [["mission-1", "REQUIRES", "medic"]]]

    facts = parse_falkordb_rows(raw)

    assert len(facts) == 1
    assert facts[0].object == "medic"
    assert facts[0].metadata["backend"] == "falkordb"


def test_agent_stack_factory_uses_local_backends_by_default() -> None:
    orchestrator = build_agent_orchestrator(settings=InfraSettings.from_env({}))

    run = orchestrator.run(
        AgentRunRequest(score_request=ScoreRequest(mission_id="factory-local", candidate_count=80, seed=7))
    )

    assert run.status is AgentRunStatus.awaiting_approval
    assert run.steps[1].evidence["backend"] == "local"
    assert run.steps[2].evidence["backend"] == "local"


def test_agent_orchestrator_produces_approval_ready_recommendation() -> None:
    orchestrator = AgentOrchestrator(
        repository=InMemoryAgentRunRepository(),
        settings=InfraSettings.from_env(
            {
                "DATABASE_URL": "postgresql://app_user:secret@pgbouncer.internal:6432/system2",
                "PGVECTOR_ENABLED": "true",
                "FALKORDB_URL": "redis://falkordb.internal:6379",
            }
        ),
    )

    run = orchestrator.run(
        AgentRunRequest(
            score_request=ScoreRequest(mission_id="agent-roster", candidate_count=80, seed=7),
            require_human_approval=True,
        )
    )

    assert run.status is AgentRunStatus.awaiting_approval
    assert run.recommendation is not None
    assert len(run.recommendation.roster) == 14
    assert [step.name for step in run.steps] == [
        "request_context",
        "retrieval_context",
        "graph_context",
        "roster_recommendation",
        "human_approval",
    ]
    assert run.steps[1].evidence["pgvector_enabled"] is True
    assert run.steps[2].evidence["falkordb_configured"] is True


def test_agent_orchestrator_records_human_approval() -> None:
    shared_sink = InMemorySharedDataSink()
    orchestrator = AgentOrchestrator(
        repository=InMemoryAgentRunRepository(),
        shared_data_sink=shared_sink,
    )
    run = orchestrator.run(
        AgentRunRequest(score_request=ScoreRequest(mission_id="approval", candidate_count=80, seed=7))
    )

    approved = orchestrator.record_approval(
        run.run_id,
        AgentApprovalRequest(
            decision=AgentApprovalDecision.approved,
            approver_id="commander-1",
            rationale="Reviewed roster, fairness audit, and second choices.",
        ),
    )

    assert approved is not None
    assert approved.status is AgentRunStatus.completed
    assert approved.approval is not None
    assert approved.approval.approver_id == "commander-1"
    assert approved.steps[-1].name == "approval_recorded"
    assert len(shared_sink.decision_snapshots) == 1
    assert shared_sink.decision_snapshots[0]["mission_id"] == "approval"
    assert len(shared_sink.update_events) == 1
    assert shared_sink.update_events[0]["operation"] == "approve"
    assert shared_sink.update_events[0]["event_payload"]["selected_soldier_ids"]


def test_agent_orchestrator_records_human_rejection() -> None:
    orchestrator = AgentOrchestrator(repository=InMemoryAgentRunRepository())
    run = orchestrator.run(
        AgentRunRequest(score_request=ScoreRequest(mission_id="rejection", candidate_count=80, seed=7))
    )

    rejected = orchestrator.record_approval(
        run.run_id,
        AgentApprovalRequest(
            decision=AgentApprovalDecision.rejected,
            approver_id="commander-1",
            rationale="Mission constraints changed before finalization.",
        ),
    )

    assert rejected is not None
    assert rejected.status is AgentRunStatus.rejected
    assert rejected.approval is not None
    assert rejected.approval.decision is AgentApprovalDecision.rejected


def test_recommendation_trace_cites_input_sources() -> None:
    payload = SelectionService().score(
        ScoreRequest(mission_id="source-refs", candidate_count=80, seed=7, candidate_pool_id="pool-1")
    )

    refs = {ref.ref: ref for ref in payload.trace.source_refs}

    assert "postgres://missions_current/source-refs" in refs
    assert "postgres://candidate_pools_current/pool-1" in refs
    assert "synthetic://system2/generated-candidates/source-refs/7" in refs
    assert payload.trace.input_source_hashes["postgres://missions_current/source-refs"].startswith("sha256:")
    assert refs["synthetic://system2/generated-candidates/source-refs/7"].metadata["operational_source"] is False


def test_candidate_pool_resolver_replaces_synthetic_fallback_refs() -> None:
    mission_id = "resolved-mission"
    pool_id = "pool-resolved"
    soldiers = generate_soldiers(80, seed=7)
    roles = default_roles()
    resolver = InMemoryCandidatePoolResolver()
    resolver.add_pool(
        pool_id,
        mission_id,
        soldiers,
        roles,
        build_local_candidate_pool_source_refs(pool_id, mission_id, soldiers, roles),
    )

    payload = SelectionService(candidate_pool_resolver=resolver).score(
        ScoreRequest(mission_id=mission_id, candidate_pool_id=pool_id, candidate_count=80, seed=999)
    )
    refs = {ref.ref: ref for ref in payload.trace.source_refs}

    assert f"postgres://candidate_pools_current/{pool_id}" in refs
    assert refs[f"postgres://candidate_pools_current/{pool_id}"].role == "candidate_pool_resolved"
    assert refs[f"postgres://candidate_pools_current/{pool_id}"].metadata["candidate_count"] == 80
    assert "synthetic://system2/generated-candidates/resolved-mission/999" not in refs
    assert payload.trace.feature_hash == feature_hash(soldiers, roles)


def test_candidate_pool_request_roles_replace_resolved_role_refs() -> None:
    mission_id = "role-override-mission"
    pool_id = "pool-role-override"
    soldiers = generate_soldiers(80, seed=7)
    resolved_roles = default_roles()
    request_roles = [RoleRequirement(slot_id="CUSTOM-1", role="assaulter", min_acft=300)]
    resolver = InMemoryCandidatePoolResolver()
    resolver.add_pool(
        pool_id,
        mission_id,
        soldiers,
        resolved_roles,
        build_local_candidate_pool_source_refs(pool_id, mission_id, soldiers, resolved_roles),
    )

    payload = SelectionService(candidate_pool_resolver=resolver).score(
        ScoreRequest(mission_id=mission_id, candidate_pool_id=pool_id, roles=request_roles)
    )
    refs = {ref.ref: ref for ref in payload.trace.source_refs}

    assert len(payload.roster) == 1
    assert f"postgres://role_slots_current/{mission_id}/CUSTOM-1" in refs
    assert f"postgres://role_slots_current/{mission_id}/{resolved_roles[0].slot_id}" not in refs
    assert payload.trace.feature_hash == feature_hash(soldiers, request_roles)


def test_strict_candidate_pool_resolver_fails_when_pool_is_missing() -> None:
    resolver = InMemoryCandidatePoolResolver(requires_resolution=True)
    service = SelectionService(candidate_pool_resolver=resolver)

    with pytest.raises(ValueError, match="candidate_pool_id 'missing-pool' was not found"):
        service.score(ScoreRequest(mission_id="missing-mission", candidate_pool_id="missing-pool"))


def test_postgres_soldier_row_uses_projection_columns_for_unit_and_mos() -> None:
    soldier = generate_soldiers(1, seed=7)[0]
    profile = soldier.model_dump(
        mode="json",
        exclude={"soldier_id", "unit_id", "mos", "protected_race", "protected_gender"},
    )
    protected = {
        "protected_race": soldier.protected_race,
        "protected_gender": soldier.protected_gender,
    }

    parsed = _soldier_from_row(
        (
            soldier.soldier_id,
            soldier.unit_id,
            soldier.mos,
            profile,
            protected,
            "sha256:test",
        )
    )

    assert parsed.soldier_id == soldier.soldier_id
    assert parsed.unit_id == soldier.unit_id
    assert parsed.mos == soldier.mos


def test_context_and_graph_ingest_events_follow_shared_contract() -> None:
    context_events = build_context_update_events(
        [
            ContextChunkInput(
                chunk_id="sop-001",
                source="unit-sop",
                title="Roster review",
                content="Roster recommendations require approval.",
                metadata={"actor_id": "operator-1"},
            )
        ]
    )
    graph_events = build_graph_update_events(
        [
            GraphFactInput(
                subject="mission-1",
                predicate="requires_role",
                object="medic",
                metadata={"fact_id": "fact-1"},
            )
        ]
    )

    assert context_events[0]["entity_type"] == "policy"
    assert context_events[0]["operation"] == "observe"
    assert context_events[0]["actor_id"] == "operator-1"
    assert context_events[0]["new_source_hash"].startswith("sha256:")
    assert graph_events[0]["entity_type"] == "graph_fact"
    assert graph_events[0]["entity_id"] == "fact-1"
    assert graph_events[0]["event_payload"]["predicate"] == "requires_role"


def test_agent_run_api_creates_and_fetches_run() -> None:
    run = create_agent_run(
        AgentRunRequest(
            score_request=ScoreRequest(mission_id="agent-api", candidate_count=80, seed=31),
            require_human_approval=True,
        )
    )

    fetched = get_agent_run(run.run_id)

    assert fetched == run
    assert fetched.status is AgentRunStatus.awaiting_approval
    assert fetched.recommendation is not None


def test_agent_run_api_records_approval() -> None:
    run = create_agent_run(
        AgentRunRequest(
            score_request=ScoreRequest(mission_id="agent-api-approval", candidate_count=80, seed=32),
            require_human_approval=True,
        )
    )

    approved = record_agent_run_approval(
        run.run_id,
        AgentApprovalRequest(
            decision=AgentApprovalDecision.approved,
            approver_id="commander-2",
            rationale="Recommendation accepted after review.",
        ),
    )

    assert approved.status is AgentRunStatus.completed
    assert approved.approval is not None


def test_agent_run_api_returns_404_for_missing_run() -> None:
    with pytest.raises(HTTPException) as exc_info:
        get_agent_run("missing")

    assert exc_info.value.status_code == 404


def test_feature_hash_excludes_protected_attributes() -> None:
    soldiers = generate_soldiers(20, seed=11)
    roles = default_roles()
    baseline = feature_hash(soldiers, roles)
    flipped = [
        soldier.model_copy(update={"protected_race": "new_group", "protected_gender": "new_gender"})
        for soldier in soldiers
    ]

    assert feature_hash(flipped, roles) == baseline


def test_counterfactual_protected_flips_do_not_change_score() -> None:
    soldiers = generate_soldiers(12, seed=5)
    role = default_roles()[0]

    violation_rate, deltas = counterfactual_flip_audit(soldiers, lambda soldier: role_fit(soldier, role))

    assert violation_rate == 0.0
    assert max(deltas) == 0.0


def test_mutual_information_proxy_audit_flags_correlated_proxy() -> None:
    soldiers = generate_soldiers(30, seed=13)
    clean = [
        soldier.model_copy(
            update={
                "protected_race": "group_a" if idx % 2 == 0 else "group_b",
                "protected_gender": "male" if idx % 2 == 0 else "female",
                "age_years": 28,
                "two_mile_run_sec": 780,
                "home_unit_ranger_density": 0.5,
                "acft_score": 520,
                "medical_risk": 0.2,
                "landing_asymmetry_score": 0.2,
                "fatigue_index": 0.4,
            }
        )
        for idx, soldier in enumerate(soldiers)
    ]
    flagged = [
        soldier.model_copy(
            update={
                "protected_race": "group_a" if idx < 15 else "group_b",
                "home_unit_ranger_density": 0.85 if idx < 15 else 0.15,
            }
        )
        for idx, soldier in enumerate(clean)
    ]

    assert mutual_information_proxy_audit(clean) == {}
    assert "home_unit_ranger_density" in mutual_information_proxy_audit(flagged)


def test_fairness_group_metrics_are_populated() -> None:
    soldiers = generate_soldiers(40, seed=17)
    fit_scores = {soldier.soldier_id: 0.7 if idx % 3 else 0.5 for idx, soldier in enumerate(soldiers)}

    audit = fairness_audit(soldiers, fit_scores)

    assert audit.counterfactual_violation_rate == 0.0
    assert audit.demographic_parity_delta >= 0.0
    assert audit.equalized_odds_delta >= 0.0


def test_operational_request_returns_full_primary_and_secondary_rosters() -> None:
    payload = SelectionService().score(
        ScoreRequest(mission_id="operational-roster", candidate_count=80, seed=7)
    )

    assert len(payload.roster) == 14
    assert len(payload.second_choice_roster) == 14


def test_high_disagreement_seed_has_low_confidence_recommendation() -> None:
    payload = SelectionService().score(
        ScoreRequest(mission_id="high-disagreement", candidate_count=80, seed=137)
    )

    assert any(item.confidence.value == "low" for item in payload.roster + payload.second_choice_roster)


def test_audit_log_hash_chain_validates_after_score_and_kill_switch(tmp_path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    service = SelectionService(AuditLog(audit_path))

    service.score(ScoreRequest(candidate_count=80, seed=7))
    service.disable()
    with pytest.raises(RuntimeError):
        service.score(ScoreRequest(candidate_count=80, seed=7))
    service.enable()

    assert validate_hash_chain(audit_path)
