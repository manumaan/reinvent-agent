from reinvent_agent.catalog import concurrency as cc
from reinvent_agent.events_api.models import Session

DAY = "2026-12-01"


def s(code, start, minutes, venue, topics=()):
    return Session(
        sessionId=code, title=code, abbreviation=code, venue=venue, room=f"{venue} {code}",
        topics=list(topics), sessionTime={"date": DAY, "time": start, "length": str(minutes)},
    )  # fmt: skip


SESSIONS = [
    s("AIM301", "09:00", 60, "MGM Grand", ["Artificial Intelligence"]),
    s("AIM302", "09:30", 60, "MGM Grand", ["Artificial Intelligence"]),
    s("SEC201", "09:00", 120, "MGM Grand", ["Security & Identity", "Artificial Intelligence"]),
    s("DAT401", "13:00", 60, "MGM Grand", ["Databases"]),
    s("SVS310", "09:15", 60, "Wynn", ["Serverless"]),
]


def venue(x):
    return x.venue


def test_track_is_code_prefix_and_label_is_distinctive_topic():
    assert cc.track_of(SESSIONS[0]) == "AIM"
    assert cc.track_of(Session(sessionId="x", title="x", abbreviation="DAT416-R")) == "DAT"
    labels = cc.track_labels(SESSIONS)
    assert labels["SEC"] == "SEC · Security & Identity"  # not the common AI topic
    assert labels["AIM"] == "AIM · Artificial Intelligence"


def test_overview_peak_parallel_sessions_and_tracks():
    [mgm, wynn] = cc.overview(SESSIONS, venue)
    assert (mgm.venue, mgm.sessions, mgm.tracks) == ("MGM Grand", 4, 3)
    assert (mgm.peak_sessions, mgm.peak_tracks, mgm.peak_at) == (3, 2, "09:30")
    assert (wynn.peak_sessions, wynn.rooms) == (1, 1)


def test_slots_count_every_session_in_each_slot_it_touches():
    slots = cc.venue_day_slots(SESSIONS, venue, DAY, "MGM Grand")
    by = {sl.start: sl for sl in slots}
    assert slots[0].start == "09:00" and slots[-1].end == "14:00"
    assert len(by["09:30"].sessions) == 3 and by["09:30"].tracks == {"AIM": 2, "SEC": 1}
    assert len(by["11:30"].sessions) == 0  # the gap before DAT401 still shows


def test_running_at_and_overlapping():
    now = cc.running_at(SESSIONS, venue, DAY, "09:20")
    assert {x.code for x, _ in now} == {"AIM301", "SEC201", "SVS310"}
    assert {x.code for x, _ in cc.running_at(SESSIONS, venue, DAY, "09:20", "Wynn")} == {"SVS310"}
    over = [x.code for x, _ in cc.overlapping(SESSIONS[0], SESSIONS, venue)]
    assert over == ["SEC201", "AIM302", "SVS310"]  # same venue first, then by start
