from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .models import ContextChunkInput


PGVECTOR_SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS system2_context_chunks (
    chunk_id text PRIMARY KEY,
    source text NOT NULL,
    title text NOT NULL,
    content text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    embedding vector(1536)
);

CREATE INDEX IF NOT EXISTS idx_system2_context_chunks_source
    ON system2_context_chunks (source);

CREATE INDEX IF NOT EXISTS idx_system2_context_chunks_embedding
    ON system2_context_chunks
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
"""


@dataclass(frozen=True)
class RetrievedContext:
    source: str
    title: str
    content: str
    score: float
    metadata: dict[str, object] = field(default_factory=dict)


class ContextRetriever(Protocol):
    def retrieve(
        self,
        query: str,
        *,
        query_embedding: Sequence[float] | None = None,
        limit: int = 5,
    ) -> list[RetrievedContext]:
        ...

    def upsert(self, chunks: Sequence[ContextChunkInput]) -> int:
        ...


class LocalContextRetriever:
    def __init__(self, contexts: Sequence[RetrievedContext] | None = None) -> None:
        self._contexts = list(contexts) if contexts is not None else _default_contexts()

    def retrieve(
        self,
        query: str,
        *,
        query_embedding: Sequence[float] | None = None,
        limit: int = 5,
    ) -> list[RetrievedContext]:
        query_terms = query.lower().split()
        contexts = sorted(
            self._contexts,
            key=lambda context: _text_score(query_terms, context),
            reverse=True,
        )
        return contexts[:limit]

    def upsert(self, chunks: Sequence[ContextChunkInput]) -> int:
        by_id = {
            str(context.metadata.get("chunk_id", f"{context.source}:{context.title}")): context
            for context in self._contexts
        }
        for chunk in chunks:
            by_id[chunk.chunk_id] = RetrievedContext(
                source=chunk.source,
                title=chunk.title,
                content=chunk.content,
                score=1.0,
                metadata={**chunk.metadata, "chunk_id": chunk.chunk_id, "backend": "local"},
            )
        self._contexts = list(by_id.values())
        return len(chunks)


class PgVectorContextRetriever:
    def __init__(
        self,
        database_url: str,
        *,
        connection_factory: Any | None = None,
        table_name: str = "system2_context_chunks",
        auto_migrate: bool = True,
    ) -> None:
        self.database_url = database_url
        self._connection_factory = connection_factory
        self.table_name = table_name
        if auto_migrate:
            self.migrate()

    def migrate(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(PGVECTOR_SCHEMA_SQL)
            connection.commit()

    def retrieve(
        self,
        query: str,
        *,
        query_embedding: Sequence[float] | None = None,
        limit: int = 5,
    ) -> list[RetrievedContext]:
        if query_embedding:
            return self._retrieve_by_embedding(query_embedding, limit=limit)
        return self._retrieve_by_text(query, limit=limit)

    def upsert(self, chunks: Sequence[ContextChunkInput]) -> int:
        sql = f"""
            INSERT INTO {self.table_name} (
                chunk_id, source, title, content, metadata, embedding
            )
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::vector)
            ON CONFLICT (chunk_id) DO UPDATE SET
                source = EXCLUDED.source,
                title = EXCLUDED.title,
                content = EXCLUDED.content,
                metadata = EXCLUDED.metadata,
                embedding = EXCLUDED.embedding
        """
        with self._connect() as connection:
            with connection.cursor() as cursor:
                for chunk in chunks:
                    cursor.execute(
                        sql,
                        (
                            chunk.chunk_id,
                            chunk.source,
                            chunk.title,
                            chunk.content,
                            json.dumps(chunk.metadata, sort_keys=True),
                            embedding_literal(chunk.embedding) if chunk.embedding is not None else None,
                        ),
                    )
            connection.commit()
        return len(chunks)

    def _retrieve_by_embedding(self, query_embedding: Sequence[float], *, limit: int) -> list[RetrievedContext]:
        embedding = embedding_literal(query_embedding)
        sql = f"""
            SELECT source, title, content, metadata, 1.0 - (embedding <=> %s::vector) AS score
            FROM {self.table_name}
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """
        return self._fetch(sql, (embedding, embedding, limit))

    def _retrieve_by_text(self, query: str, *, limit: int) -> list[RetrievedContext]:
        sql = f"""
            SELECT source, title, content, metadata, 0.5 AS score
            FROM {self.table_name}
            WHERE title ILIKE %s OR content ILIKE %s
            ORDER BY source, title
            LIMIT %s
        """
        pattern = f"%{query}%"
        return self._fetch(sql, (pattern, pattern, limit))

    def _fetch(self, sql: str, params: tuple[object, ...]) -> list[RetrievedContext]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
        return [
            RetrievedContext(
                source=row["source"] if isinstance(row, dict) else row[0],
                title=row["title"] if isinstance(row, dict) else row[1],
                content=row["content"] if isinstance(row, dict) else row[2],
                metadata=row["metadata"] if isinstance(row, dict) else row[3],
                score=float(row["score"] if isinstance(row, dict) else row[4]),
            )
            for row in rows
        ]

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory()

        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "pgvector retrieval requires the 'infra' optional dependencies. "
                "Install with: pip install -e '.[infra]'"
            ) from exc

        return psycopg.connect(self.database_url)


def embedding_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(value):.8g}" for value in values) + "]"


def _default_contexts() -> list[RetrievedContext]:
    return [
        RetrievedContext(
            source="docs/architecture.md",
            title="Cognitive adaptation contract",
            content=(
                "The live adaptation loop estimates sensemaking, critical "
                "thinking, systems thinking, readiness pressure, and team state; "
                "scenario recommendations require instructor approval."
            ),
            score=1.0,
            metadata={"backend": "local", "chunk_id": "cognitive-adaptation-contract"},
        ),
        RetrievedContext(
            source="assets/feature-spec.md",
            title="Feature contract",
            content=(
                "Protected attributes are fairness-only and must not enter "
                "success scoring or assignment. Physical and medical fields "
                "require job-related rationale."
            ),
            score=1.0,
            metadata={"backend": "local", "chunk_id": "feature-contract"},
        ),
        RetrievedContext(
            source="docs/architecture.md",
            title="Operational scoring contract",
            content=(
                "The agent workflow sequences deterministic tools, preserves "
                "traceable outputs, and requires human approval before finalization."
            ),
            score=0.9,
            metadata={"backend": "local", "chunk_id": "architecture-agent-workflow"},
        ),
    ]


def _text_score(query_terms: list[str], context: RetrievedContext) -> int:
    haystack = f"{context.title} {context.content} {context.source}".lower()
    return sum(term in haystack for term in query_terms)
