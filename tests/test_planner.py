from reinvent_agent.events_api.models import Session
from reinvent_agent.planner import best_non_overlapping, plan_one_venue_per_day


def s(sid, day, start, minutes, venue):
    return Session(
        sessionId=sid,
        title=f"Title {sid}",
        abbreviation=sid,
        venue=venue,
        sessionTime={"date": day, "time": start, "length": str(minutes)},
    )


def venue(x):
    return x.venue


D1, D2 = "2026-12-01", "2026-12-02"


def test_picks_venue_with_most_sessions_and_reports_the_rest():
    sessions = [
        s("A1", D1, "09:00", 60, "MGM Grand"),
        s("A2", D1, "11:00", 60, "MGM Grand"),
        s("A3", D1, "14:00", 60, "MGM Grand"),
        s("B1", D1, "10:00", 60, "Venetian"),
        s("B2", D1, "13:00", 60, "Venetian"),
    ]
    plan = plan_one_venue_per_day(sessions, venue)
    [day] = plan.days
    assert (day.weekday, day.venue) == ("Tuesday", "MGM Grand")
    assert [x.code for x in day.sessions] == ["A1", "A2", "A3"]
    assert {x["code"]: x["reason"] for x in day.not_scheduled} == {
        "B1": "other venue",
        "B2": "other venue",
    }
    assert not plan.feasible and plan.summary.startswith("3 of 5")
    gaps = [(g.start, g.end) for g in day.free_slots]
    assert gaps == [("08:00", "09:00"), ("10:00", "11:00"), ("12:00", "14:00"), ("15:00", "18:00")]
    assert day.free_slots[1].elsewhere == ["B1"]


def test_overlaps_at_same_venue_keep_the_maximum_set():
    sessions = [
        s("L", D1, "09:00", 180, "Wynn"),  # blocks both shorter ones
        s("X", D1, "09:00", 60, "Wynn"),
        s("Y", D1, "10:30", 60, "Wynn"),
    ]
    assert [x.session_id for x in best_non_overlapping(sessions, set())] == ["X", "Y"]
    # a reservation outranks count
    assert [x.session_id for x in best_non_overlapping(sessions, {"L"})] == ["L"]


def test_back_to_back_is_allowed_and_all_fit_is_feasible():
    sessions = [
        s("A", D1, "09:00", 60, "ARIA"),
        s("B", D1, "10:00", 60, "ARIA"),
        s("C", D2, "09:00", 60, "Venetian"),
    ]
    plan = plan_one_venue_per_day(sessions, venue)
    assert plan.feasible and plan.summary.startswith("All of 3")
    assert [d.venue for d in plan.days] == ["ARIA", "Venetian"]


def test_unplaceable_sessions_are_reported():
    sessions = [Session(sessionId="T", title="TBD"), s("V", D1, "09:00", 60, None)]
    plan = plan_one_venue_per_day(sessions, venue)
    assert plan.days == []
    assert {x["code"]: x["reason"] for x in plan.unplaceable} == {
        "T": "no fixed time",
        "V": "venue unknown",
    }


def test_any_venue_plan_leaves_travel_time_between_venues():
    sessions = [
        s("MGM1", D1, "13:30", 60, "MGM Grand"),  # ends 14:30
        s("WYNN", D1, "14:30", 60, "Wynn"),  # 0 min after, ~40 min away
        s("MGM2", D1, "14:30", 60, "MGM Grand"),  # same venue, back to back is fine
    ]
    no_travel = best_non_overlapping(sessions[:2], set())
    assert [x.code for x in no_travel] == ["MGM1", "WYNN"]  # the old behaviour
    with_travel = best_non_overlapping(sessions, set(), venue_of=venue)
    assert [x.code for x in with_travel] == ["MGM1", "MGM2"]
    far = [s("A", D1, "09:00", 60, "MGM Grand"), s("B", D1, "10:45", 60, "Wynn")]  # 45 min gap
    assert len(best_non_overlapping(far, set(), venue_of=venue)) == 2


def test_travel_estimates_are_symmetric_with_safe_defaults():
    from reinvent_agent.travel import UNKNOWN, travel_minutes

    assert travel_minutes("Wynn", "MGM Grand") == travel_minutes("MGM Grand", "Wynn") == 40
    assert travel_minutes("Venetian", "Venetian") == 0
    assert travel_minutes("Venetian", None) == UNKNOWN


def test_edited_travel_times_roundtrip_and_apply():
    from reinvent_agent import travel

    try:
        rows = travel.rows()
        assert rows[-2]["from"] == "(same venue)" and rows[-1]["from"] == "(unknown venue)"
        for r in rows:
            if {r["from"], r["to"]} == {"Wynn", "MGM Grand"}:
                r["minutes"] = 50
            if r["from"] == "(same venue)":
                r["minutes"] = 5
        settings = travel.from_json(travel.to_json(travel.from_rows(rows)))
        travel.apply(settings)
        assert travel.travel_minutes("MGM Grand", "Wynn") == 50  # both directions
        assert travel.travel_minutes("Venetian", "Venetian") == 5
        assert travel.current() == settings != travel.DEFAULTS
    finally:
        travel.apply(None)
    assert travel.travel_minutes("Wynn", "MGM Grand") == 40


def test_travel_table_is_sorted_from_then_to():
    from reinvent_agent import travel

    pairs = [(r["from"], r["to"]) for r in travel.rows()[:-2]]
    assert pairs == sorted(pairs, key=lambda p: (p[0].lower(), p[1].lower()))
    assert all(a < b for a, b in pairs)  # each pair once, alphabetical within it
    assert pairs[0] == ("Caesars Forum", "Caesars Palace")
