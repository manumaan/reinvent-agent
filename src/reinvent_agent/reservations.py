"""Reserved seating: when and where seats can be reserved, and per-session status.

Updated 2026-10-05 (AWS): seat reservations open in the **re:Invent portal** on
October 6, 2026 in two phases -- the first half of reservable seats at 9:00 AM PDT, the
second half at 5:00 PM PDT. The **Events API** (ReserveSessions / CancelReservation,
which this app and the unattended run use) opens on **October 8**; no time was given,
so the unattended run polls through that day. Until then ReserveSessions answers 409.

Before release the Events API reports ``isReservable: false`` and no
``seatAvailability`` for every session, so "not reservable" is only known once the
catalog carries reservation data (some session reservable) or a session is marked
``walkUp``. Until then a session's status is ``unknown``, not "no".
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from reinvent_agent.events_api.models import SEAT_WALK_UP, Session

EVENT_TZ = ZoneInfo("America/Los_Angeles")
# Portal (manual) releases on Oct 6.
RELEASES = (
    ("first half of reservable seats", datetime(2026, 10, 6, 9, 0, tzinfo=EVENT_TZ)),
    ("second half of reservable seats", datetime(2026, 10, 6, 17, 0, tzinfo=EVENT_TZ)),
)
# Events API (this app, the unattended run): the day it opens; the hour is unannounced.
API_OPENS = datetime(2026, 10, 8, 0, 0, tzinfo=EVENT_TZ)
API_POLL_UNTIL = API_OPENS + timedelta(days=1)

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
    """What to tell the attendee about reserving right now (None once the API is open)."""
    now = now or datetime.now(EVENT_TZ)
    api = (
        f"The API this app uses (Reserve button, unattended run) opens on "
        f"{API_OPENS:%A, %B %-d} (time not announced)."
    )
    pending = [(what, at) for what, at in RELEASES if at > now]
    if len(pending) == len(RELEASES):
        a, b = (f"the {w} at {format_time(at, tz)}" for w, at in RELEASES)
        return (
            f"Seat reservations open in the **re:Invent portal** on Tuesday, October 6: "
            f"{a} and {b}. Reserve there by hand. {api}"
        )
    if pending:
        what, at = pending[0]
        return (
            f"The re:Invent portal is open for the first half of seats; the {what} "
            f"release at {format_time(at, tz)} today. {api}"
        )
    if now < API_OPENS:
        return f"Reserve in the re:Invent portal. {api}"
    if now < API_POLL_UNTIL:
        return (
            "The Events API opens today (time not announced); the unattended run checks "
            "every 2 minutes and reserves your approved plan as soon as it opens."
        )
    return None


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


@dataclass(frozen=True)
class Job:
    """One EventBridge Scheduler job for the unattended run (reservation_stack)."""

    name: str
    action: str  # preflight | poll
    label: str
    at: datetime | None = None  # one-time
    start: datetime | None = None  # recurring window [start, end)
    end: datetime | None = None
    every_minutes: int | None = None

    def describe(self, tz: str | None = None) -> str:
        if self.at:
            return f"{self.at:%a %b %-d}, {format_time(self.at, tz)}"
        return (
            f"{self.start:%a %b %-d}, every {self.every_minutes} min from "
            f"{format_time(self.start, tz)} until {format_time(self.end, tz)} the next day"
        )


SCHEDULE = (
    Job("reminder", "preflight", "day before the API opens",
        at=datetime(2026, 10, 7, 18, 0, tzinfo=EVENT_TZ)),
    Job("api-poll", "poll", "API opening", start=API_OPENS, end=API_POLL_UNTIL,
        every_minutes=2),
)  # fmt: skip
