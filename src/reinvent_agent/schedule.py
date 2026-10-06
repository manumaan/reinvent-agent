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
from dataclasses import dataclass, field
from pathlib import Path

from reinvent_agent.catalog.venues import sub_venue
from reinvent_agent.events_api.models import BulkResult, Schedule, Session

# Short, so favorites changed in the AWS portal show up here within a minute. Each
# refresh is one GetSchedule call (quota: 60 per minute per attendee).
MAX_AGE_SECONDS = 60
EVENT_TZ = "America/Los_Angeles"  # re:Invent, Las Vegas

FAILURE_TEXT = {
    "sessionNotReservable": "not reservable",
    "scheduleConflict": "conflicts with another reservation",
    "sessionFull": "session is full",
    "insufficientAccess": "your pass doesn't include it",
    "timePassed": "session already happened",
    "notFavorited": "wasn't a favorite",
}


@dataclass
class WriteResult:
    action: str
    done: list[str] = field(default_factory=list)  # session IDs now in the desired state
    failed: dict[str, str] = field(default_factory=dict)  # session ID -> reason
    note: str | None = None  # whole-request problem, e.g. reservations not open yet


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
        self._withdrawn: set[str] = set()  # GetSession 404: no longer in the catalog
        self.archive: dict[str, dict] = {}  # sessionId -> {code, title} ever seen

    @classmethod
    def with_inferred_venues(
        cls, event_id: str, client_factory, sessions: list[Session], **kwargs
    ) -> MySchedule:
        """Catalog lookup plus the same room-name venue inference used for search."""
        from reinvent_agent.catalog.source import load_archive
        from reinvent_agent.catalog.venues import VenueInferrer

        inferrer = VenueInferrer.learn(sessions)
        out = cls(
            event_id,
            client_factory,
            {s.session_id: s for s in sessions},
            venue_of=lambda s: inferrer.infer(s)[0],
            **kwargs,
        )
        out.archive = load_archive(event_id)
        return out

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

    # --- writes (each re-reads GetSchedule, the source of truth) ---------------

    def _client_or_raise(self):
        client = self.client_factory()
        if client is None:
            raise NotSignedIn("Not signed in with AWS Builder ID.")
        return client

    def _bulk(self, action: str, result: BulkResult) -> WriteResult:
        out = WriteResult(action, done=list(result.successful))
        for f in result.failed:
            if f.already_done:
                out.done.append(f.session_id)
                continue
            reason = FAILURE_TEXT.get(f.code, f.code)
            if f.conflicts_with:
                codes = [self.describe(c).get("code", c) for c in f.conflicts_with]
                reason += f" ({', '.join(codes)})"
            out.failed[f.session_id] = reason
        return out

    def _verified(self, result: WriteResult, field: str, present: bool) -> WriteResult:
        """Re-read GetSchedule and keep in ``done`` only what AWS now really shows.

        A write can report success (or a 404 "already gone") while AWS still lists the
        session - e.g. a stale ID or a change made elsewhere - so never trust the write
        response alone.
        """
        listed = set(getattr(self.refresh(), field))
        for sid in list(result.done):
            if (sid in listed) != present:
                result.done.remove(sid)
                result.failed[sid] = (
                    "AWS still lists it - try again or check the portal"
                    if not present
                    else "AWS doesn't list it - try again"
                )
        return result

    def _each(self, action: str, ids: list[str], call) -> WriteResult:
        """Single-session removals: one failure doesn't stop the rest."""
        out = WriteResult(action)
        for sid in ids:
            try:
                call(self.event_id, sid)  # False on 404: verified against GetSchedule below
                out.done.append(sid)
            except Exception as e:
                out.failed[sid] = str(e)
        return out

    def favorite(self, ids: list[str]) -> WriteResult:
        client = self._client_or_raise()
        try:
            result = self._bulk("favorite", client.associate_favorites(self.event_id, ids))
        except Exception:
            self.refresh()
            raise
        return self._verified(result, "favorites", present=True)

    def unfavorite(self, ids: list[str]) -> WriteResult:
        client = self._client_or_raise()
        result = self._each("unfavorite", ids, client.disassociate_favorite)
        return self._verified(result, "favorites", present=False)

    def reserve(self, ids: list[str], tz: str | None = None) -> WriteResult:
        """``tz``: the viewer's timezone, for the release times in the not-open note."""
        from reinvent_agent.events_api.client import OperationClosedError
        from reinvent_agent.reservations import release_note

        client = self._client_or_raise()
        try:
            result = self._bulk("reserve", client.reserve_sessions(self.event_id, ids))
        except OperationClosedError:
            self.refresh()
            return WriteResult(
                "reserve",
                failed={sid: "reservations not open" for sid in ids},
                note="Reservations aren't open yet. "
                + (release_note(tz) or "Try again shortly.")
                + " Favorite the sessions for now.",
            )
        except Exception:
            self.refresh()
            raise
        return self._verified(result, "reserved", present=True)

    def cancel_reservation(self, ids: list[str]) -> WriteResult:
        client = self._client_or_raise()
        result = self._each("cancel reservation", ids, client.cancel_reservation)
        return self._verified(result, "reserved", present=False)

    # --- details -------------------------------------------------------------

    def session(self, session_id: str) -> Session | None:
        if s := self.catalog.get(session_id) or self._extra.get(session_id):
            return s
        client = self.client_factory()
        if client is None:
            return None
        try:
            s = client.get_session(self.event_id, session_id)
        except Exception as e:  # 404: withdrawn from the catalog; else unavailable now
            if getattr(e, "status", None) == 404:
                self._withdrawn.add(session_id)
            return None
        self._extra[session_id] = s
        return s

    def describe(self, session_id: str) -> dict:
        s = self.session(session_id)
        if s is None:
            known = self.archive.get(session_id, {})
            withdrawn = session_id in self._withdrawn
            return {
                "sessionId": session_id,
                "code": known.get("code"),
                "title": known.get("title")
                or ("(no longer in the AWS catalog)" if withdrawn else "(details unavailable)"),
                "withdrawn": withdrawn,
            }
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
            "sub_venue": sub_venue(s.room),
            "room": s.room,
        }

    def timeline(self, sched: Schedule, tz: str = EVENT_TZ) -> dict[str, list[dict]]:
        """The real schedule, day by day: reserved and favorited sessions plus personal
        time, in start order, with overlapping entries flagged. Undated items go under
        ``"unscheduled"``."""
        from datetime import UTC
        from zoneinfo import ZoneInfo

        entries: dict[str, dict] = {}
        for sid in dict.fromkeys(sched.reserved + sched.favorites):
            d = self.describe(sid)
            entries[sid] = d | {
                "favorite": sid in sched.favorites,
                "reserved": sid in sched.reserved,
                "kind": "session",
            }
        local = ZoneInfo(tz)
        for p in sched.personal_time:  # API times are UTC without an offset
            start = p.start_date_time.replace(tzinfo=UTC).astimezone(local)
            end = p.end_date_time.replace(tzinfo=UTC).astimezone(local)
            entries[p.personal_time_id] = {
                "sessionId": p.personal_time_id,
                "code": "",
                "title": p.title,
                "day": start.date().isoformat(),
                "weekday": start.strftime("%A"),
                "start": start.strftime("%H:%M"),
                "end": end.strftime("%H:%M"),
                "venue": p.location,
                "sub_venue": None,
                "favorite": False,
                "reserved": False,
                "kind": "personal time",
            }
        days: dict[str, list[dict]] = {}
        for e in entries.values():
            bucket = "withdrawn" if e.get("withdrawn") else e.get("day") or "unscheduled"
            days.setdefault(bucket, []).append(e)
        for day, items in days.items():
            items.sort(key=lambda e: (e.get("start") or "99", e.get("code") or ""))
            for e in items:
                e["overlaps"] = [
                    o.get("code") or o["title"]
                    for o in items
                    if o is not e
                    and day != "unscheduled"
                    and e.get("start")
                    and o.get("start")
                    and e.get("end")
                    and o.get("end")
                    and o["start"] < e["end"]
                    and e["start"] < o["end"]
                ]
        return dict(sorted(days.items()))

    def sessions(self, ids: list[str]) -> list[Session]:
        return [s for sid in ids if (s := self.session(sid)) is not None]
