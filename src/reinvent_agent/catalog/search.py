"""Catalog search: semantic query + structured filters."""

from __future__ import annotations

from dataclasses import dataclass, field

from reinvent_agent.catalog.embeddings import Embedder
from reinvent_agent.catalog.vector_store import Hit, VectorStore


@dataclass
class SearchFilters:
    event_id: str = "reinvent2026"
    min_level: int | None = None
    max_level: int | None = None
    days: list[str] = field(default_factory=list)  # "2026-12-01"
    venues: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)  # "Chalk talk", "Workshop", ...
    services: list[str] = field(default_factory=list)  # any-of, exact AWS service names
    reservable_only: bool = False
    exclude_session_ids: list[str] = field(default_factory=list)
    start_after_min: int | None = None  # minutes since local midnight
    end_before_min: int | None = None

    def to_filter(self) -> dict:
        clauses: list[dict] = [{"eventId": {"$eq": self.event_id}}]
        if self.min_level is not None:
            clauses.append({"levelNum": {"$gte": self.min_level}})
        if self.max_level is not None:
            clauses.append({"levelNum": {"$lte": self.max_level}})
        if self.days:
            clauses.append({"day": {"$in": self.days}})
        if self.venues:
            clauses.append({"venue": {"$in": self.venues}})
        if self.types:
            clauses.append({"type": {"$in": self.types}})
        if self.services:
            clauses.append({"services": {"$in": self.services}})
        if self.reservable_only:
            clauses.append({"reservable": {"$eq": True}})
        if self.exclude_session_ids:
            clauses.append({"sessionId": {"$nin": self.exclude_session_ids}})
        if self.start_after_min is not None:
            clauses.append({"startMin": {"$gte": self.start_after_min}})
        if self.end_before_min is not None:
            clauses.append({"endMin": {"$lte": self.end_before_min}})
        return clauses[0] if len(clauses) == 1 else {"$and": clauses}


@dataclass
class SearchResult:
    session_id: str
    code: str
    title: str
    score: float
    metadata: dict

    def summary(self) -> dict:
        m = self.metadata
        return {
            "sessionId": self.session_id,
            "code": self.code,
            "title": self.title,
            "type": m.get("type"),
            "level": m.get("levelNum"),
            "day": m.get("day"),
            "start": _hhmm(m.get("startMin")),
            "end": _hhmm(m.get("endMin")),
            "venue": m.get("venue"),
            "room": m.get("room"),
            "services": m.get("services"),
            "speakers": m.get("speakers"),
            "snippet": m.get("snippet"),
            "score": round(self.score, 3),
        }


def _hhmm(minutes: int | None) -> str | None:
    return None if minutes is None else f"{minutes // 60:02d}:{minutes % 60:02d}"


class CatalogSearch:
    def __init__(self, store: VectorStore, embedder: Embedder):
        self.store, self.embedder = store, embedder

    def search(
        self, query: str, filters: SearchFilters | None = None, k: int = 10
    ) -> list[SearchResult]:
        filters = filters or SearchFilters()
        [vector] = self.embedder.embed([query])
        hits: list[Hit] = self.store.query(vector, k, filters.to_filter())
        return [
            SearchResult(
                session_id=h.metadata.get("sessionId", h.key.split("#")[-1]),
                code=h.metadata.get("code", ""),
                title=h.metadata.get("title", ""),
                score=1 - h.distance,
                metadata=h.metadata,
            )
            for h in hits
        ]
