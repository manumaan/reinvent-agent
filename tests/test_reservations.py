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


def test_release_note_portal_then_api():
    before = release_note("Asia/Kolkata", now=datetime(2026, 10, 5, tzinfo=EVENT_TZ))
    assert "re:Invent portal** on Tuesday, October 6" in before
    assert "9:00 AM PDT (9:30 PM IST)" in before and "5:00 PM PDT" in before
    assert "opens on Thursday, October 8" in before
    between = release_note(now=datetime(2026, 10, 6, 12, 0, tzinfo=EVENT_TZ))
    assert "second half of reservable seats release at 5:00 PM PDT" in between
    gap = release_note(now=datetime(2026, 10, 7, tzinfo=EVENT_TZ))
    assert gap.startswith("Reserve in the re:Invent portal") and "October 8" in gap
    api_day = release_note(now=datetime(2026, 10, 8, 10, 0, tzinfo=EVENT_TZ))
    assert "every 2 minutes" in api_day
    assert release_note(now=datetime(2026, 10, 9, 1, 0, tzinfo=EVENT_TZ)) is None


def test_schedule_is_reminder_then_all_day_poll_on_oct_8():
    from reinvent_agent.reservations import API_OPENS, SCHEDULE

    reminder, poll = SCHEDULE
    assert reminder.action == "preflight" and reminder.at < API_OPENS
    assert (poll.action, poll.every_minutes) == ("poll", 2)
    assert poll.start == API_OPENS and (poll.end - poll.start).days == 1
    assert "every 2 min from 12:00 AM PDT (12:30 PM IST)" in poll.describe("Asia/Kolkata")


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
