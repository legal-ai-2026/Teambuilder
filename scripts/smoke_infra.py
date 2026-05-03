from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Any, Callable

from system2.config import InfraSettings


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test System 2 infrastructure connections.")
    parser.add_argument("--env-file", help="Path to generated graph-stack env file.")
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Initialize System 2 Postgres tables after connectivity checks.",
    )
    args = parser.parse_args()

    if args.env_file:
        os.environ["SYSTEM2_ENV_FILE"] = args.env_file

    settings = InfraSettings.from_env()
    print(json.dumps(settings.status(), indent=2, sort_keys=True))

    checks: list[Callable[[InfraSettings], CheckResult]] = [
        check_postgres,
        check_pgvector,
        check_redis,
        check_falkordb,
    ]
    results = [check(settings) for check in checks]

    if args.migrate:
        results.append(migrate_postgres(settings))

    for result in results:
        status = "ok" if result.ok else "fail"
        print(f"{status}: {result.name}: {result.detail}")

    if not all(result.ok for result in results):
        raise SystemExit(1)


def check_postgres(settings: InfraSettings) -> CheckResult:
    if not settings.database_url:
        return CheckResult("postgres", False, "DATABASE_URL is not configured")
    try:
        with _psycopg_connect(settings.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                database = cursor.fetchone()[0]
        return CheckResult("postgres", True, f"connected to {database}")
    except Exception as exc:
        return CheckResult("postgres", False, str(exc))


def check_pgvector(settings: InfraSettings) -> CheckResult:
    if not settings.pgvector_url:
        return CheckResult("pgvector", False, "PGVECTOR_CONNECTION_STRING or DATABASE_URL is not configured")
    try:
        with _psycopg_connect(settings.pgvector_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                row = cursor.fetchone()
        if row is None:
            return CheckResult("pgvector", False, "vector extension is not installed in this database")
        return CheckResult("pgvector", True, f"vector extension {row[0]}")
    except Exception as exc:
        return CheckResult("pgvector", False, str(exc))


def check_redis(settings: InfraSettings) -> CheckResult:
    if not settings.redis_url:
        return CheckResult("redis", False, "REDIS_URL is not configured")
    try:
        client = _redis_client(settings.redis_url)
        pong = client.ping()
        return CheckResult("redis", bool(pong), "PING succeeded" if pong else "PING failed")
    except Exception as exc:
        return CheckResult("redis", False, str(exc))


def check_falkordb(settings: InfraSettings) -> CheckResult:
    if not settings.falkordb_url:
        return CheckResult("falkordb", False, "FALKORDB_URL is not configured")
    try:
        client = _redis_client(settings.falkordb_url)
        client.execute_command("GRAPH.QUERY", "system2_smoke", "RETURN 1")
        return CheckResult("falkordb", True, "GRAPH.QUERY succeeded")
    except Exception as exc:
        return CheckResult("falkordb", False, str(exc))


def migrate_postgres(settings: InfraSettings) -> CheckResult:
    if not settings.database_url:
        return CheckResult("postgres_migration", False, "DATABASE_URL is not configured")
    try:
        from system2.agent_stack import build_adaptation_repository, build_agent_orchestrator, build_selection_service

        build_adaptation_repository(settings)
        service = build_selection_service(settings)
        build_agent_orchestrator(settings=settings, selection_service=service)
        return CheckResult(
            "postgres_migration",
            True,
            "adaptation, agent, audit, candidate-pool, shared-data, and context tables initialized",
        )
    except Exception as exc:
        return CheckResult("postgres_migration", False, str(exc))


def _psycopg_connect(url: str) -> Any:
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("Install infra dependencies first: pip install -e '.[infra]'") from exc
    return psycopg.connect(url)


def _redis_client(url: str) -> Any:
    try:
        import redis
    except ImportError as exc:
        raise RuntimeError("Install infra dependencies first: pip install -e '.[infra]'") from exc
    return redis.Redis.from_url(url)


if __name__ == "__main__":
    main()
