"""Reserved seating: release times and whether a session can be reserved.

Seats release in two phases on October 6, 2026: the first half of reservable seats
at 9:00 AM PDT, the second half at 5:00 PM PDT. Until then ReserveSessions answers 409.

Before release the Events API reports ``isReservable: false`` and no
``seatAvailability`` for every session, so "not reservable" is only known once the
catalog carries reservation data (some session reservable) or a session is marked
``walkUp``. Until then a session's status is ``unknown``, not "no".
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from reinvent_agent.events_api.models import SEAT_WALK_UP, Session

EVENT_TZ = ZoneInfo("America/Los_Angeles")
RELEASES = (
    ("first half of reservable seats", datetime(2026, 10, 6, 9, 0, tzinfo=EVENT_TZ)),
    ("second half of reservable seats", datetime(2026, 10, 6, 17, 0, tzinfo=EVENT_TZ)),
)

RESERVABLE, WALK_UP, NOT_RESERVABLE, UNKNOWN = "reservable", "walk-up", "not reservable", "unknown"


def _zone(tz: str | None) -> ZoneInfo | None:
    try:
        return ZoneInfo(tz) if tz else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def format_time(at: datetime, tz: str | None = None) -> str:
    """``9:00 AM PDT (9:30 PM IST)`` -- event time, plus the viewer's when it differs."""
    event = at.astimezone(EVENT_TZ)
    text = f"{event:%-I:%M %p %Z}"
    local_tz = _zone(tz)
    if local_tz is not None:
        local = at.astimezone(local_tz)
        if local.utcoffset() != event.utcoffset():
            day = "" if local.date() == event.date() else f" {local:%a %b %-d}"
            text += f" ({local:%-I:%M %p}{day} {local:%Z})"
    return text


def release_note(tz: str | None = None, now: datetime | None = None) -> str | None:
    """What to tell the attendee about reserved seating right now (None once all released)."""
    now = now or datetime.now(EVENT_TZ)
    pending = [(what, at) for what, at in RELEASES if at > now]
    if not pending:
        return None
    parts = [f"the {what} at {format_time(at, tz)}" for what, at in pending]
    if len(pending) == len(RELEASES):
        return (
            f"Reserved seats release in two phases on Tuesday, October 6, 2026: "
            f"{parts[0]} and {parts[1]}."
        )
    return f"The first half of reserved seats is out; {parts[0]} (October 6)."


class ReservationInfo:
    """Per-session reservation status from the catalog's own data."""

    def __init__(self, sessions: Iterable[Session]):
        self.by_id = {s.session_id: s for s in sessions}
        # Before release every session reads isReservable=false; only once some
        # session is reservable does "false" really mean "no reserved seating".
        self.published = any(s.is_reservable for s in self.by_id.values())

    def status(self, session_id: str) -> str:
        s = self.by_id.get(session_id)
        if s is None:
            return UNKNOWN
        if s.seat_availability == SEAT_WALK_UP:
            return WALK_UP
        if s.is_reservable:
            return RESERVABLE
        return NOT_RESERVABLE if self.published else UNKNOWN

    def can_reserve(self, session_id: str) -> bool:
        """False only when the catalog says so; unknown sessions are left to the API."""
        return self.status(session_id) not in (WALK_UP, NOT_RESERVABLE)


# The unattended run: one-time EventBridge Scheduler jobs (infra/stacks/reservation_stack).
# (name, when, action, label); "run" jobs start 2 minutes early and poll until release.
SCHEDULE = (
    ("reminder", datetime(2026, 10, 5, 18, 0, tzinfo=EVENT_TZ), "preflight", "day before"),
    ("check-1", datetime(2026, 10, 6, 8, 30, tzinfo=EVENT_TZ), "preflight", "9 AM check"),
    ("run-1", datetime(2026, 10, 6, 8, 58, tzinfo=EVENT_TZ), "run", "first release"),
    ("check-2", datetime(2026, 10, 6, 16, 30, tzinfo=EVENT_TZ), "preflight", "5 PM check"),
    ("run-2", datetime(2026, 10, 6, 16, 58, tzinfo=EVENT_TZ), "run", "second release"),
)
OPEN_GRACE_MINUTES = 10  # keep polling this long after a release time before giving up


def release_for(label: str) -> datetime:
    return RELEASES[0][1] if label == "first release" else RELEASES[1][1]
