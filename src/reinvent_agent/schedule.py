"""The signed-in attendee's schedule (GetSchedule), joined with catalog details.

``MySchedule`` keeps a local snapshot (``~/.config/reinvent-agent/schedule-<event>.json``)
so the app and agent can answer from it without a network call. It is refreshed on
sign-in, on demand, and whenever the snapshot is missing or older than ``MAX_AGE``.
GetSchedule is the source of truth; the snapshot is only a cache.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from reinvent_agent.events_api.models import Schedule, Session

MAX_AGE_SECONDS = 15 * 60


class NotSignedIn(RuntimeError):
    pass


def snapshot_path(event_id: str) -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "reinvent-agent" / f"schedule-{event_id}.json"


class MySchedule:
    def __init__(
        self,
        event_id: str,
        client_factory: Callable[[], object | None],
        catalog: Mapping[str, Session],
        venue_of: Callable[[Session], str | None] | None = None,
        path: Path | None = None,
    ):
        """``client_factory`` returns an EventsApiClient, or None when not signed in."""
        self.event_id = event_id
        self.client_factory = client_factory
        self.catalog = catalog
        self.venue_of = venue_of or (lambda s: s.venue)
        self.path = path or snapshot_path(event_id)
        self._extra: dict[str, Session] = {}  # sessions fetched with GetSession

    @classmethod
    def with_inferred_venues(
        cls, event_id: str, client_factory, sessions: list[Session], **kwargs
    ) -> MySchedule:
        """Catalog lookup plus the same room-name venue inference used for search."""
        from reinvent_agent.catalog.venues import VenueInferrer

        inferrer = VenueInferrer.learn(sessions)
        return cls(
            event_id,
            client_factory,
            {s.session_id: s for s in sessions},
            venue_of=lambda s: inferrer.infer(s)[0],
            **kwargs,
        )

    # --- snapshot ----------------------------------------------------------

    def _read_snapshot(self) -> tuple[Schedule, float] | None:
        try:
            body = json.loads(self.path.read_text())
            return Schedule.model_validate(body["schedule"]), body["fetchedAt"]
        except (OSError, ValueError, KeyError):
            return None

    def refresh(self) -> Schedule:
        client = self.client_factory()
        if client is None:
            raise NotSignedIn("Not signed in with AWS Builder ID.")
        sched = client.get_schedule(self.event_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = {"fetchedAt": time.time(), "schedule": sched.model_dump(mode="json", by_alias=True)}
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(body, f)
        return sched

    def load(self, refresh: bool = False) -> Schedule:
        """Snapshot if fresh; otherwise GetSchedule (falling back to a stale snapshot
        when the API cannot be reached)."""
        snap = self._read_snapshot()
        if snap and not refresh and time.time() - snap[1] < MAX_AGE_SECONDS:
            return snap[0]
        try:
            return self.refresh()
        except NotSignedIn:
            if snap:
                return snap[0]
            raise

    def fetched_at(self) -> float | None:
        snap = self._read_snapshot()
        return snap[1] if snap else None

    # --- details -------------------------------------------------------------

    def session(self, session_id: str) -> Session | None:
        if s := self.catalog.get(session_id) or self._extra.get(session_id):
            return s
        client = self.client_factory()
        if client is None:
            return None
        try:
            s = client.get_session(self.event_id, session_id)
        except Exception:  # withdrawn session or API error: report as unknown
            return None
        self._extra[session_id] = s
        return s

    def describe(self, session_id: str) -> dict:
        s = self.session(session_id)
        if s is None:
            return {"sessionId": session_id, "title": "(not found in catalog)"}
        return {
            "sessionId": s.session_id,
            "code": s.code,
            "title": s.title,
            "type": s.type,
            "level": s.level_number,
            "day": s.day.isoformat() if s.day else None,
            "weekday": s.day.strftime("%A") if s.day else None,
            "start": s.start.strftime("%H:%M") if s.start else None,
            "end": s.end.strftime("%H:%M") if s.end else None,
            "venue": self.venue_of(s),
            "room": s.room,
        }

    def sessions(self, ids: list[str]) -> list[Session]:
        return [s for sid in ids if (s := self.session(sid)) is not None]
