from datetime import datetime

from reinvent_agent.events_api.models import Session
from reinvent_agent.reservations import (
    EVENT_TZ,
    NOT_RESERVABLE,
    RESERVABLE,
    UNKNOWN,
    WALK_UP,
    ReservationInfo,
    format_time,
    release_note,
)

FIRST = datetime(2026, 10, 6, 9, 0, tzinfo=EVENT_TZ)


def test_times_in_event_and_viewer_timezone():
    assert format_time(FIRST) == "9:00 AM PDT"
    assert format_time(FIRST, "America/Los_Angeles") == "9:00 AM PDT"
    assert format_time(FIRST, "Asia/Kolkata") == "9:00 AM PDT (9:30 PM IST)"
    second = datetime(2026, 10, 6, 17, 0, tzinfo=EVENT_TZ)
    assert format_time(second, "Asia/Kolkata") == "5:00 PM PDT (5:30 AM Wed Oct 7 IST)"
    assert format_time(FIRST, "Not/AZone") == "9:00 AM PDT"


def test_release_note_by_phase():
    before = release_note("Asia/Kolkata", now=datetime(2026, 9, 28, tzinfo=EVENT_TZ))
    assert "two phases on Tuesday, October 6, 2026" in before
    assert "9:00 AM PDT (9:30 PM IST)" in before and "5:00 PM PDT" in before
    between = release_note(now=datetime(2026, 10, 6, 12, 0, tzinfo=EVENT_TZ))
    assert between.startswith("The first half") and "5:00 PM PDT" in between
    assert release_note(now=datetime(2026, 10, 7, tzinfo=EVENT_TZ)) is None


def s(sid, reservable=False, seats=None):
    return Session(sessionId=sid, title=sid, isReservable=reservable, seatAvailability=seats)


def test_status_unknown_until_catalog_has_reservation_data():
    before = ReservationInfo([s("A"), s("B")])  # what the API returns today
    assert before.status("A") == UNKNOWN and before.can_reserve("A")
    after = ReservationInfo([s("A", True, "available"), s("B"), s("W", False, "walkUp")])
    assert after.status("A") == RESERVABLE
    assert after.status("B") == NOT_RESERVABLE and not after.can_reserve("B")
    assert after.status("W") == WALK_UP and not after.can_reserve("W")
    assert ReservationInfo([s("W", False, "walkUp")]).status("W") == WALK_UP
    assert after.status("missing") == UNKNOWN
