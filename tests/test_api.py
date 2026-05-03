import pytest

from fastapi import HTTPException
from pydantic import ValidationError

from system2.api import create_agent_run, disable, enable, get_agent_run, record_agent_run_approval, score, score_v1
from system2.agent_orchestrator import AgentOrchestrator
from system2.agent_state import InMemoryAgentStateStore, RedisAgentStateStore
from system2.agent_stack import build_agent_orchestrator
from system2.agent_store import InMemoryAgentRunRepository
from system2.audit import AuditLog, validate_hash_chain
from system2.config import InfraSettings, redact_url
from system2.data import default_roles, generate_soldiers
from system2.fairness import counterfactual_flip_audit, fairness_audit, mutual_information_proxy_audit
from system2.models import AgentApprovalDecision, AgentApprovalRequest, AgentRunRequest, AgentRunStatus, ScoreRequest
from system2.postgres_agent_store import AGENT_RUNS_SCHEMA_SQL, dump_agent_run, load_agent_run
from system2.registry import MODEL_VERSIONS
from system2.graph import LocalGraphContextProvider, cypher_quote, parse_falkordb_rows
from system2.retrieval import PGVECTOR_SCHEMA_SQL, LocalContextRetriever, embedding_literal
from system2.scoring import feature_hash, role_fit
from system2.service import SelectionService


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
            "REDIS_URL": "redis://:redis_secret@redis.internal:6379/0",
            "FALKORDB_URL": "redis://graph_user:graph_secret@falkordb.internal:6379",
            "PGVECTOR_ENABLED": "true",
            "AGENT_REPOSITORY_BACKEND": "postgres",
            "AGENT_STATE_BACKEND": "redis",
            "RETRIEVAL_BACKEND": "pgvector",
            "GRAPH_BACKEND": "falkordb",
            "SYSTEM2_AUDIT_LOG": "/var/log/system2/audit.jsonl",
        }
    )

    status = settings.status()

    assert status["postgres"]["configured"] is True
    assert status["postgres"]["pgvector_enabled"] is True
    assert status["postgres"]["url"] == "postgresql://app_user:***@pgbouncer.internal:6432/system2"
    assert status["redis"]["url"] == "redis://redis.internal:6379/0"
    assert status["falkordb"]["url"] == "redis://graph_user:***@falkordb.internal:6379"
    assert status["backends"] == {
        "agent_repository": "postgres",
        "agent_state": "redis",
        "retrieval": "pgvector",
        "graph": "falkordb",
    }
    assert redact_url(None) is None


def test_infra_settings_default_to_local_backends() -> None:
    settings = InfraSettings.from_env({})

    assert settings.agent_repository_backend == "memory"
    assert settings.agent_state_backend == "memory"
    assert settings.retrieval_backend == "local"
    assert settings.graph_backend == "local"


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


def test_pgvector_schema_and_embedding_literal_are_stable() -> None:
    assert "CREATE EXTENSION IF NOT EXISTS vector" in PGVECTOR_SCHEMA_SQL
    assert "embedding vector(1536)" in PGVECTOR_SCHEMA_SQL
    assert embedding_literal([0.1, 0.25, 1]) == "[0.1,0.25,1]"


def test_local_graph_context_provider_returns_request_facts() -> None:
    facts = LocalGraphContextProvider().mission_context(
        AgentRunRequest(score_request=ScoreRequest(mission_id="mission-1", candidate_count=80))
    )

    assert facts[0].subject == "mission-1"
    assert facts[0].predicate == "uses_role_source"
    assert cypher_quote("a'b") == "'a\\'b'"


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
    orchestrator = AgentOrchestrator(repository=InMemoryAgentRunRepository())
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
