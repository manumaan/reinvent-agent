"""Turn API sessions into search documents: embedding text + vector metadata.

S3 Vectors limits filterable metadata to 2 KB per vector, so only short fields
used in filters are filterable; long display fields (title, abstract snippet,
room, speakers) are declared non-filterable on the index (see
``NON_FILTERABLE_KEYS``, mirrored in ``infra/stacks/search_stack.py``).
"""

from __future__ import annotations

from dataclasses import dataclass

from reinvent_agent.catalog.venues import VenueInferrer
from reinvent_agent.events_api.models import Session

NON_FILTERABLE_KEYS = ["title", "snippet", "room", "speakers"]
SNIPPET_CHARS = 600
_MAX_LIST = 8  # cap taxonomy lists so filterable metadata stays well under 2 KB


@dataclass
class SessionDoc:
    key: str
    text: str
    metadata: dict


def minutes_of_day(session: Session, *, end: bool = False) -> int | None:
    t = session.end if end else session.start
    return t.hour * 60 + t.minute if t else None


def embedding_text(s: Session) -> str:
    parts = [
        f"{s.code}: {s.title}",
        f"{s.type or ''} · {s.level or ''}",
        "Tracks: " + ", ".join(s.tracks),
        "Topics: " + ", ".join(s.topics + s.areas_of_interest),
        "Services: " + ", ".join(s.services),
        "Speakers: " + "; ".join(s.speaker_names),
        s.abstract or "",
    ]
    return "\n".join(p for p in parts if p.strip(" :·"))


def to_document(s: Session, event_id: str, inferrer: VenueInferrer | None = None) -> SessionDoc:
    venue, venue_source = inferrer.infer(s) if inferrer else (s.venue, "api" if s.venue else "none")
    meta: dict = {
        "eventId": event_id,
        "sessionId": s.session_id,
        "code": s.code,
        "type": s.type or "unknown",
        "venue": venue or "unknown",
        "venueSource": venue_source,
        "reservable": s.is_reservable,
        "services": s.services[:_MAX_LIST],
        "topics": (s.topics + s.areas_of_interest)[:_MAX_LIST],
        "tracks": s.tracks[:_MAX_LIST],
        # non-filterable display fields
        "title": s.title,
        "snippet": (s.abstract or "")[:SNIPPET_CHARS],
        "room": s.room or "",
        "speakers": "; ".join(s.speaker_names)[:500],
    }
    # S3 Vectors rejects null metadata values, so only set what we have. A missing
    # key also makes range filters fail, so unlevelled sessions never pass a level filter.
    if s.level_number is not None:
        meta["levelNum"] = s.level_number
    if s.day:
        meta["day"] = s.day.isoformat()
    for key, value in (("startMin", minutes_of_day(s)), ("endMin", minutes_of_day(s, end=True))):
        if value is not None:
            meta[key] = value
    return SessionDoc(key=f"{event_id}#{s.session_id}", text=embedding_text(s), metadata=meta)
