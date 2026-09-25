"""Index a catalog: sessions -> documents -> embeddings -> vector store (+ DynamoDB)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from reinvent_agent.catalog.documents import SessionDoc, to_document
from reinvent_agent.catalog.embeddings import Embedder
from reinvent_agent.catalog.vector_store import VectorStore
from reinvent_agent.catalog.venues import VenueInferrer
from reinvent_agent.events_api.models import Session

EMBED_BATCH = 64


@dataclass
class IngestReport:
    sessions: int
    indexed: int
    venue_sources: dict[str, int]


def build_documents(sessions: Sequence[Session], event_id: str) -> list[SessionDoc]:
    inferrer = VenueInferrer.learn(sessions)
    return [to_document(s, event_id, inferrer) for s in sessions]


def write_sessions_table(table, sessions: Iterable[Session], docs: Iterable[SessionDoc]) -> None:
    """Upsert normalized sessions into the DynamoDB sessions table (DataStack)."""
    from decimal import Decimal

    def clean(v):
        if isinstance(v, float):
            return Decimal(str(v))
        if isinstance(v, list):
            return [clean(x) for x in v]
        return v

    with table.batch_writer(overwrite_by_pkeys=["eventId", "sessionId"]) as batch:
        for s, d in zip(sessions, docs, strict=True):
            item = {k: clean(v) for k, v in d.metadata.items() if v not in (None, "", [])}
            item["abstract"] = s.abstract or ""
            if "day" in item:
                item["eventDay"] = f"{item['eventId']}#{item['day']}"
                item["venueStart"] = f"{item['venue']}#{item.get('startMin', 0):04d}"
            batch.put_item(Item=item)


def ingest(
    sessions: Sequence[Session],
    event_id: str,
    store: VectorStore,
    embedder: Embedder,
    table=None,
    progress=None,
) -> IngestReport:
    docs = build_documents(sessions, event_id)
    for i in range(0, len(docs), EMBED_BATCH):
        chunk = docs[i : i + EMBED_BATCH]
        vectors = embedder.embed([d.text for d in chunk])
        store.put([(d.key, v, d.metadata) for d, v in zip(chunk, vectors, strict=True)])
        if progress:
            progress(min(i + EMBED_BATCH, len(docs)), len(docs))
    if table is not None:
        write_sessions_table(table, sessions, docs)
    sources: dict[str, int] = {}
    for d in docs:
        sources[d.metadata["venueSource"]] = sources.get(d.metadata["venueSource"], 0) + 1
    return IngestReport(sessions=len(sessions), indexed=len(docs), venue_sources=sources)
