import pytest

from fastapi import HTTPException
from pydantic import ValidationError

from system2.api import create_agent_run, disable, enable, get_agent_run, score, score_v1
from system2.agent_orchestrator import AgentOrchestrator
from system2.agent_store import InMemoryAgentRunRepository
from system2.audit import AuditLog, validate_hash_chain
from system2.config import InfraSettings, redact_url
from system2.data import default_roles, generate_soldiers
from system2.fairness import counterfactual_flip_audit, fairness_audit, mutual_information_proxy_audit
from system2.models import AgentRunRequest, AgentRunStatus, ScoreRequest
from system2.registry import MODEL_VERSIONS
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
