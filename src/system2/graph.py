from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .models import AgentRunRequest, GraphFactInput


@dataclass(frozen=True)
class GraphFact:
    subject: str
    predicate: str
    object: str
    metadata: dict[str, object] = field(default_factory=dict)


class GraphContextProvider(Protocol):
    def mission_context(self, request: AgentRunRequest) -> list[GraphFact]:
        ...

    def upsert(self, facts: Sequence[GraphFactInput]) -> int:
        ...


class LocalGraphContextProvider:
    def __init__(self, facts: Sequence[GraphFact] | None = None) -> None:
        self._facts = list(facts or [])

    def mission_context(self, request: AgentRunRequest) -> list[GraphFact]:
        score_request = request.score_request
        roles = score_request.roles or []
        facts = [
            GraphFact(
                subject=score_request.mission_id,
                predicate="uses_role_source",
                object="provided" if roles else "default",
                metadata={"backend": "local"},
            )
        ]
        for role in roles:
            facts.append(
                GraphFact(
                    subject=score_request.mission_id,
                    predicate="requires_role",
                    object=role.role,
                    metadata={"slot_id": role.slot_id, "required_mos": role.required_mos},
                )
            )
        stored = [fact for fact in self._facts if fact.subject == score_request.mission_id]
        return [*facts, *stored]

    def upsert(self, facts: Sequence[GraphFactInput]) -> int:
        existing = {(fact.subject, fact.predicate, fact.object): fact for fact in self._facts}
        for fact in facts:
            existing[(fact.subject, fact.predicate, fact.object)] = GraphFact(
                subject=fact.subject,
                predicate=fact.predicate,
                object=fact.object,
                metadata={**fact.metadata, "backend": "local"},
            )
        self._facts = list(existing.values())
        return len(facts)


class FalkorDBGraphContextProvider:
    def __init__(
        self,
        falkordb_url: str,
        *,
        client: Any | None = None,
        graph_name: str = "system2",
    ) -> None:
        self.falkordb_url = falkordb_url
        self._client = client
        self.graph_name = graph_name

    def mission_context(self, request: AgentRunRequest) -> list[GraphFact]:
        mission_id = cypher_quote(request.score_request.mission_id)
        query = (
            "MATCH (m:Mission {mission_id: "
            f"{mission_id}"
            "})-[rel]->(node) "
            "RETURN m.mission_id, type(rel), coalesce(node.name, node.role, node.id) "
            "LIMIT 50"
        )
        raw = self._redis().execute_command("GRAPH.QUERY", self.graph_name, query)
        return parse_falkordb_rows(raw)

    def upsert(self, facts: Sequence[GraphFactInput]) -> int:
        client = self._redis()
        for fact in facts:
            query = (
                f"MERGE (subject:System2Entity {{id: {cypher_quote(fact.subject)}}}) "
                f"MERGE (object:System2Entity {{id: {cypher_quote(fact.object)}}}) "
                f"MERGE (subject)-[rel:{cypher_identifier(fact.predicate)}]->(object) "
                f"SET rel.metadata_json = {cypher_quote(json.dumps(fact.metadata, sort_keys=True))}"
            )
            client.execute_command("GRAPH.QUERY", self.graph_name, query)
        return len(facts)

    def _redis(self) -> Any:
        if self._client is not None:
            return self._client

        try:
            import redis
        except ImportError as exc:
            raise RuntimeError(
                "FalkorDB graph context requires the 'infra' optional dependencies. "
                "Install with: pip install -e '.[infra]'"
            ) from exc

        self._client = redis.Redis.from_url(self.falkordb_url, decode_responses=True)
        return self._client


def cypher_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def cypher_identifier(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", value.strip()).upper()
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized or normalized[0].isdigit():
        raise ValueError("Cypher identifiers must contain letters and cannot start with a digit")
    return normalized


def parse_falkordb_rows(raw: object) -> list[GraphFact]:
    if not isinstance(raw, list) or len(raw) < 2:
        return []
    rows = raw[1]
    if not isinstance(rows, list):
        return []

    facts: list[GraphFact] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 3:
            continue
        subject = _cell_to_str(row[0])
        predicate = _cell_to_str(row[1])
        obj = _cell_to_str(row[2])
        facts.append(GraphFact(subject=subject, predicate=predicate, object=obj, metadata={"backend": "falkordb"}))
    return facts


def _cell_to_str(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)
