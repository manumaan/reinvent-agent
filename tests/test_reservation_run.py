import json
import subprocess
import sys

import pytest

from reinvent_agent.events_api.client import EventsApiError, OperationClosedError
from reinvent_agent.events_api.models import BulkResult, Schedule, Session
from reinvent_agent.reservation_plan import (
    BACKUP,
    PRIMARY,
    PlanItem,
    PlanStore,
    ReservationPlan,
    build_plan,
    dynamo_lease,
)
from reinvent_agent.reservation_runner import ListNotifier, RateLimiter, ReservationRunner

DAY = "2026-12-01"


def item(sid, start, end, role=PRIMARY, priority=2, day=DAY):
    return PlanItem(sid, sid, f"Title {sid}", day, start, end, "Venetian", role, priority)


def make_plan(*items):
    plan = ReservationPlan("ev", list(items))
    plan.link_backups()
    return plan


class Clock:
    def __init__(self):
        self.t = 1_000_000.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class FakeApi:
    """ReserveSessions/GetSchedule with scripted behaviour."""

    def __init__(self, clock, closed_polls=0, full=(), conflict=(), lose_response=()):
        self.clock = clock
        self.closed_polls = closed_polls
        self.full, self.conflict = set(full), set(conflict)
        self.lose_response = set(lose_response)
        self.held: list[str] = []
        self.calls: list[tuple[float, list[str]]] = []

    def get_schedule(self, event_id):
        return Schedule(reserved=list(self.held))

    def reserve_sessions(self, event_id, ids):
        self.calls.append((self.clock(), list(ids)))
        if self.closed_polls:
            self.closed_polls -= 1
            raise OperationClosedError(409, {"message": "closed"}, "POST", "/reservations")
        ok, failed = [], []
        for sid in ids:
            if sid in self.full:
                failed.append({"sessionId": sid, "code": "sessionFull"})
            elif sid in self.conflict:
                failed.append(
                    {"sessionId": sid, "code": "scheduleConflict", "conflictsWith": ["X"]}
                )
            elif sid in self.held:
                failed.append({"sessionId": sid, "code": "alreadyScheduled"})
            else:
                self.held.append(sid)
                ok.append(sid)
        if any(sid in self.lose_response for sid in ids):
            self.lose_response -= set(ids)
            raise ConnectionError("response lost")  # the write happened anyway
        return BulkResult.model_validate({"successful": ok, "failed": failed})


def runner_for(api, clock):
    notifier = ListNotifier()
    return ReservationRunner(api, "ev", notifier, clock=clock, sleep=clock.sleep), notifier


def test_lost_response_on_the_first_request_after_release():
    clock = Clock()
    api = FakeApi(clock, closed_polls=2, lose_response={"A"})
    plan = make_plan(item("A", "09:00", "10:00", priority=1), item("B", "11:00", "12:00"))
    report = runner_for(api, clock)[0].run(plan, "first release", clock.t + 600)
    assert report.status == "done" and sorted(api.held) == ["A", "B"]
    assert sum(call[1].count("A") for call in api.calls) == 3  # 2 closed + the lost one


def test_waits_for_release_then_reserves_in_priority_order():
    clock = Clock()
    api = FakeApi(clock, closed_polls=3)
    plan = make_plan(
        item("LOW", "13:00", "14:00", priority=3),
        item("MUST", "15:00", "16:00", priority=1),
        item("WANT", "09:00", "10:00"),
    )
    runner, notifier = runner_for(api, clock)
    report = runner.run(plan, "first release", open_deadline=clock.t + 600)
    assert report.status == "done"
    assert api.calls[0][1] == ["MUST"] and len(api.calls) == 5  # 3 closed probes + 1 + batch
    assert api.calls[-1][1] == ["WANT", "LOW"]
    assert [r["code"] for r in report.reserved] == ["MUST", "WANT", "LOW"]
    assert notifier.messages[-1][0] == "re:Invent reservations (first release): 3 reserved"


def test_full_session_falls_back_to_best_overlapping_backup():
    clock = Clock()
    api = FakeApi(clock, full={"P"})
    plan = make_plan(
        item("P", "10:00", "11:00"),
        item("B_SMALL", "10:45", "11:30", BACKUP),
        item("B_BIG", "10:00", "11:00", BACKUP),
        item("OTHER", "13:00", "14:00"),
    )
    report, _ = runner_for(api, clock)[0].run(plan, "first release", clock.t), None
    assert "B_BIG" in api.held and "B_SMALL" not in api.held
    how = {r["code"]: r["how"] for r in report.reserved}
    assert how["B_BIG"] == "backup for P (full)" and "OTHER" in how
    assert report.failed == []


def test_backup_that_clashes_with_a_held_session_is_skipped():
    clock = Clock()
    api = FakeApi(clock, full={"P"})
    plan = make_plan(
        item("P", "10:00", "11:00", priority=2),
        item("KEEP", "11:00", "12:00", priority=1),
        item("B", "10:30", "11:30", BACKUP),  # overlaps KEEP, which is reserved first
    )
    report = runner_for(api, clock)[0].run(plan, "first release", clock.t)
    assert "B" not in api.held
    assert report.failed[0]["code"] == "P"
    assert "no backup could be reserved" in report.failed[0]["reason"]


def test_conflict_is_reported_not_retried():
    clock = Clock()
    api = FakeApi(clock, conflict={"C"})
    report = runner_for(api, clock)[0].run(make_plan(item("C", "09:00", "10:00")), "x", clock.t)
    assert report.failed[0]["reason"] == "conflicts with a session you already hold"
    assert len(api.calls) == 1


def test_lost_response_is_reconciled_from_get_schedule():
    clock = Clock()
    api = FakeApi(clock, lose_response={"B"})
    plan = make_plan(
        item("A", "09:00", "10:00", priority=1),
        item("B", "11:00", "12:00"),
        item("C", "13:00", "14:00"),
    )
    report = runner_for(api, clock)[0].run(plan, "x", clock.t)
    assert sorted(api.held) == ["A", "B", "C"]
    assert {r["code"] for r in report.reserved} == {"A", "B", "C"}
    # B and C were not sent twice: the read-back showed both already held.
    assert sum(call[1].count("B") for call in api.calls) == 1


def test_still_closed_at_deadline_reserves_nothing_and_says_so():
    clock = Clock()
    api = FakeApi(clock, closed_polls=10_000)
    runner, notifier = runner_for(api, clock)
    report = runner.run(make_plan(item("A", "09:00", "10:00")), "first release", clock.t + 30)
    assert report.status == "closed" and api.held == []
    assert "closed" in notifier.messages[-1][0]
    assert clock.t - 1_000_000 < 60


def test_second_run_keeps_backup_and_skips_held():
    clock = Clock()
    api = FakeApi(clock)
    api.held = ["B", "OTHER"]  # phase 1 got the backup for P, and OTHER
    plan = make_plan(
        item("P", "10:00", "11:00"),
        item("B", "10:00", "11:00", BACKUP),
        item("OTHER", "13:00", "14:00"),
    )
    report = runner_for(api, clock)[0].run(plan, "second release", clock.t)
    assert api.calls == [] and report.status == "done"
    assert {r["code"]: r["how"] for r in report.reserved} == {
        "B": "backup for P, reserved earlier",
        "OTHER": "already reserved",
    }


def test_no_plan_and_failed_sign_in_notify():
    clock = Clock()
    runner, notifier = runner_for(FakeApi(clock), clock)
    assert runner.run(None, "x", clock.t).status == "no_plan"

    class Broken:
        def get_schedule(self, event_id):
            raise RuntimeError("refresh failed (400); sign in again")

    runner, notifier = runner_for(Broken(), clock)
    ok, msg = runner.preflight("9 AM check")
    assert not ok and notifier.messages[0][0].startswith("ACTION NEEDED")
    assert "auth push-secret" in msg


def test_rate_limiter_keeps_30_sessions_per_rolling_minute():
    clock = Clock()
    limiter = RateLimiter(clock, clock.sleep)
    stamps = []
    for _ in range(9):
        limiter.acquire(10)
        stamps.append(clock.t)
    for i in range(len(stamps)):
        in_window = [t for t in stamps[i:] if t - stamps[i] < 60]
        assert len(in_window) * 10 <= 30


# --- plan building and storage ------------------------------------------------


def s(sid, start, minutes, venue="Venetian", day=DAY):
    return Session(
        sessionId=sid, title=sid, abbreviation=sid, venue=venue,
        sessionTime={"date": day, "time": start, "length": str(minutes)},
    )  # fmt: skip


def test_build_plan_picks_non_overlapping_primaries_and_links_backups():
    sessions = [s("A", "09:00", 60), s("B", "09:30", 60), s("C", "11:00", 60, "Wynn")]
    plan = build_plan("ev", sessions, lambda x: x.venue, strategy="max_sessions")
    roles = {i.code: i.role for i in plan.items}
    assert roles == {"A": PRIMARY, "B": BACKUP, "C": PRIMARY}
    assert plan.item("B").backup_for == ["A"] and not plan.problems()
    one = build_plan("ev", sessions, lambda x: x.venue, strategy="one_venue")
    assert {i.code for i in one.primaries} == {"A"}  # Venetian keeps A; C is at Wynn
    # existing reservations stay primaries, as must-haves
    kept = build_plan("ev", sessions, lambda x: x.venue, reserved=["B"])
    assert kept.item("B").role == PRIMARY and kept.item("B").priority == 1


def test_overlapping_primaries_block_approval():
    plan = make_plan(item("A", "09:00", "10:00"), item("B", "09:30", "10:30"))
    assert plan.problems() == ["A and B overlap on 2026-12-01: make one of them a backup."]


class FakeTable:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):  # noqa: N803
        found = self.items.get((Key["userId"], Key["sk"]))
        return {"Item": found} if found else {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):  # noqa: N803
        key = (Item["userId"], Item["sk"])
        now = (ExpressionAttributeValues or {}).get(":now")
        if ConditionExpression and key in self.items and self.items[key]["expires"] >= now:
            raise type("ConditionalCheckFailedException", (Exception,), {})()
        self.items[key] = dict(Item)

    def delete_item(self, Key, **kw):  # noqa: N803
        self.items.pop((Key["userId"], Key["sk"]), None)

    def query(self, **kw):
        runs = [v for (u, sk), v in self.items.items() if sk.startswith("run#")]
        return {"Items": sorted(runs, key=lambda v: v["sk"], reverse=True)}


def test_plan_store_versions_and_runs():
    store = PlanStore(FakeTable(), "user-1", "ev")
    plan = make_plan(item("A", "09:00", "10:00"))
    with pytest.raises(ValueError):
        store.approve(make_plan(item("A", "09:00", "10:00"), item("B", "09:30", "10:30")))
    assert store.approve(plan).version == 1
    assert store.approve(plan).version == 2
    assert store.approved().primaries[0].code == "A"
    store.save_run({"started_at": 1.0, "label": "first release", "status": "done"})
    assert store.runs()[0]["status"] == "done"


def test_lease_excludes_a_second_holder():
    table, clock = FakeTable(), Clock()
    lease = dynamo_lease(table, wait=0, sleep=clock.sleep)
    with lease(), pytest.raises(Exception, match="busy"), lease():
        pass
    with lease():  # released after the first holder exits
        pass


# --- Lambda --------------------------------------------------------------------


def test_lambda_package_does_not_import_heavy_modules():
    code = (
        "import sys, reinvent_agent.lambda_handler as h, reinvent_agent.reservation_runner, "
        "reinvent_agent.reservation_plan, reinvent_agent.events_api.client;"
        "print([m for m in ('anthropic','typer','streamlit','boto3') if m in sys.modules])"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout.replace("'", '"')) == []


def test_lambda_handler_run_uses_approved_plan(monkeypatch):
    from reinvent_agent import lambda_handler

    clock = Clock()
    api = FakeApi(clock)
    runner, notifier = runner_for(api, clock)
    store = PlanStore(FakeTable(), "u", "ev")
    store.approve(make_plan(item("A", "09:00", "10:00")))
    monkeypatch.setattr(lambda_handler, "_deps", lambda: (runner, store, object()))
    out = lambda_handler.handler({"action": "run", "label": "first release"}, None)
    assert out == {"ok": True, "status": "done"} and api.held == ["A"]
    assert store.runs()[0]["reserved"][0]["code"] == "A"

    # A second run while one holds the lease stops instead of reserving twice.
    import time as _time

    store.table.put_item(
        Item={"userId": "_lock", "sk": "reservation-run", "owner": "x",
              "expires": int(_time.time()) + 600}
    )  # fmt: skip
    busy = lambda_handler.handler({"action": "run", "label": "first release"}, None)
    assert busy == {"ok": False, "status": "busy"}
    assert notifier.messages[-1][0].endswith("skipped")


def test_poll_is_silent_while_closed_then_runs_once_when_open(monkeypatch):
    from reinvent_agent import lambda_handler

    clock = Clock()
    api = FakeApi(clock, closed_polls=2)
    runner, notifier = runner_for(api, clock)
    store = PlanStore(FakeTable(), "u", "ev")
    store.approve(make_plan(item("A", "09:00", "10:00")))
    monkeypatch.setattr(lambda_handler, "_deps", lambda: (runner, store, object()))
    poll = {"action": "poll", "label": "API opening"}
    for _ in range(2):  # API still closed: one probe each, no email, nothing saved
        assert lambda_handler.handler(poll, None) == {"ok": False, "status": "closed"}
    assert notifier.messages == [] and store.runs() == []
    assert lambda_handler.handler(poll, None) == {"ok": True, "status": "done"}
    assert api.held == ["A"] and len(notifier.messages) == 1
    calls = len(api.calls)
    assert lambda_handler.handler(poll, None)["status"] == "already done"
    assert len(api.calls) == calls
    # Approving a new plan re-arms the poll.
    store.approve(make_plan(item("A", "09:00", "10:00"), item("B", "11:00", "12:00")))
    assert lambda_handler.handler(poll, None)["status"] == "done" and "B" in api.held


def test_poll_alerts_on_sign_in_problems_at_most_hourly(monkeypatch):
    from reinvent_agent import lambda_handler

    class Broken:
        def get_schedule(self, event_id):
            raise RuntimeError("refresh failed")

    clock = Clock()
    runner, notifier = runner_for(Broken(), clock)
    store = PlanStore(FakeTable(), "u", "ev")
    store.approve(make_plan(item("A", "09:00", "10:00")))
    monkeypatch.setattr(lambda_handler, "_deps", lambda: (runner, store, object()))
    poll = {"action": "poll", "label": "API opening"}
    for _ in range(5):
        lambda_handler.handler(poll, None)
        clock.t += 120
    assert len(notifier.messages) == 1 and "auth failed" in notifier.messages[0][0]
    clock.t += 3600
    lambda_handler.handler(poll, None)
    assert len(notifier.messages) == 2


def test_errors_outside_the_runner_still_email(monkeypatch):
    from reinvent_agent import lambda_handler

    clock = Clock()
    runner, notifier = runner_for(FakeApi(clock), clock)

    class BrokenTable(FakeTable):
        def put_item(self, **kw):
            raise RuntimeError("AccessDenied")

    store = PlanStore(BrokenTable(), "u", "ev")
    monkeypatch.setattr(lambda_handler, "_deps", lambda: (runner, store, object()))
    with pytest.raises(RuntimeError):
        lambda_handler.handler({"action": "run", "label": "cloud test"}, None)
    assert notifier.messages[-1][0] == "ERROR: re:Invent reservations (cloud test)"


def test_backups_prefer_the_same_venue():
    other = item("FAR", "10:00", "11:00", BACKUP)
    other.venue = "Wynn"
    near = item("NEAR", "10:30", "11:00", BACKUP)  # less overlap, but same venue
    plan = make_plan(item("P", "10:00", "11:00"), other, near)
    assert [b.code for b in plan.backups_for("P")] == ["NEAR", "FAR"]


def test_tight_transfers_are_flagged_and_backups_must_be_reachable():
    mgm = item("MGM", "13:30", "14:30")
    mgm.venue = "MGM Grand"
    wynn = item("WYNN", "14:45", "15:45")
    wynn.venue = "Wynn"
    plan = make_plan(mgm, wynn)
    [(a, b, gap, travel)] = plan.tight_transfers()
    assert (a.code, b.code, gap, travel) == ("MGM", "WYNN", 15, 40)

    clock = Clock()
    api = FakeApi(clock, full={"P"})
    held = item("HELD", "13:30", "14:30", priority=1)
    held.venue = "MGM Grand"
    far_backup = item("FAR", "14:30", "15:30", BACKUP)
    far_backup.venue = "Wynn"  # free in time, but ~40 min away from HELD
    p = item("P", "14:30", "15:30")
    p.venue = "MGM Grand"
    report = runner_for(api, clock)[0].run(make_plan(held, p, far_backup), "x", clock.t)
    assert "FAR" not in api.held and report.failed[0]["code"] == "P"


def test_plan_lists_only_the_chosen_sessions_and_works_around_other_reservations():
    held = s("HELD", "09:00", 60, venue="MGM Grand")  # reserved, not a favorite
    both = s("BOTH", "16:00", 60, venue="MGM Grand")  # reserved and a favorite
    clash = s("CLASH", "09:30", 60, venue="MGM Grand")  # overlaps HELD
    far = s("FAR", "10:15", 60, venue="Wynn")  # free, but ~40 min from HELD
    near = s("NEAR", "10:00", 60, venue="MGM Grand")
    plan = build_plan(
        "ev", [both, clash, far, near], lambda x: x.venue, reserved=["HELD", "BOTH"],
        strategy="max_sessions", held=[held, both],
    )  # fmt: skip
    assert {i.code for i in plan.items} == {"BOTH", "CLASH", "FAR", "NEAR"}  # no HELD
    assert {i.code for i in plan.primaries} == {"BOTH", "NEAR"}
    assert plan.item("BOTH").priority == 1 and plan.item("CLASH").role == BACKUP
    assert [h.code for h in plan.held] == ["HELD"]  # BOTH is in the list already
    assert not plan.problems()

    plan.item("CLASH").role = PRIMARY  # the user forces it in the table
    assert any("CLASH overlaps HELD" in p for p in plan.problems())
    back = ReservationPlan.from_json(plan.to_json())
    assert [h.code for h in back.held] == ["HELD"]


def test_one_venue_day_stays_at_the_venue_of_a_held_reservation():
    held = s("HELD", "09:00", 60, venue="Wynn")
    a = s("A", "11:00", 60, venue="MGM Grand")
    b = s("B", "13:00", 60, venue="MGM Grand")
    c = s("C", "11:00", 60, venue="Wynn")
    plan = build_plan(
        "ev", [a, b, c], lambda x: x.venue, reserved=["HELD"], strategy="one_venue", held=[held]
    )
    assert {i.code for i in plan.primaries} == {"C"}  # not the bigger MGM Grand set


# --- step log and dry run ------------------------------------------------------


def _steps(report):
    return [(r["action"], r["code"], r["result"]) for r in report.log]


def test_real_run_logs_every_step():
    clock = Clock()
    api = FakeApi(clock, closed_polls=1, full={"A"}, conflict={"C"})
    api.held = ["HAD"]
    plan = make_plan(
        item("A", "09:00", "10:00", priority=1),
        item("A2", "09:30", "10:30", role=BACKUP),
        item("HAD", "11:00", "12:00"),
        item("C", "13:00", "14:00"),
    )
    report = runner_for(api, clock)[0].run(plan, "t", clock.t + 60)
    assert _steps(report) == [
        ("skip", "HAD", "skipped"),
        ("wait", "A", "closed"),
        ("reserve", "A", "refused"),
        ("backup", "A2", "reserved"),
        ("reserve", "C", "refused"),
    ]
    assert report.log[-1]["detail"] == "conflicts with a session you already hold"
    assert report.log[1]["seconds"] == 3  # one poll wait
    json.dumps(report.to_dict())


class ReadOnlyApi:
    """What a dry run may touch: any other call fails the test."""

    def __init__(self, sessions, reserved=()):
        self.sessions = {s.session_id: s for s in sessions}
        self.reserved = list(reserved)

    def get_session(self, event_id, session_id):
        if session_id not in self.sessions:
            raise EventsApiError(404, {}, "GET", "/sessions")
        return self.sessions[session_id]

    def get_schedule(self, event_id):
        return Schedule(reserved=self.reserved)


def live(sid, start, end, seats="available", reservable=True):
    def minutes(hhmm):
        return int(hhmm[:2]) * 60 + int(hhmm[3:])

    length = str(minutes(end) - minutes(start))
    return Session.model_validate(
        {
            "sessionId": sid,
            "title": sid,
            "sessionTime": {"date": DAY, "time": start, "length": length},
            "isReservable": reservable,
            "seatAvailability": seats,
        }
    )


def test_dry_run_works_out_each_outcome_without_writing_or_emailing():
    from reinvent_agent.reservation_runner import dry_run

    api = ReadOnlyApi(
        [
            live("OK", "08:00", "08:30"),
            live("FULL", "09:00", "10:00", seats="unavailable"),  # the API's band for full
            live("BK", "09:00", "10:00"),
            live("WALK", "11:00", "12:00", seats=None, reservable=False),
            live("CLASH", "13:00", "14:00"),
            live("HAD", "13:30", "14:30"),
        ],
        reserved=["HAD"],
    )
    plan = make_plan(
        item("OK", "08:00", "08:30"),
        item("FULL", "09:00", "10:00"),
        item("BK", "09:00", "10:00", role=BACKUP),
        item("WALK", "11:00", "12:00"),
        item("CLASH", "13:00", "14:00"),
        item("GONE", "15:00", "16:00"),
    )
    report = dry_run(api, "ev", plan)
    assert report.dry_run and report.status == "done"
    assert _steps(report) == [
        ("reserve", "OK", "reserved"),
        ("reserve", "FULL", "refused"),
        ("backup", "BK", "reserved"),
        ("reserve", "WALK", "refused"),
        ("reserve", "CLASH", "refused"),
        ("reserve", "GONE", "refused"),
    ]
    assert {r["code"]: r["seats"] for r in report.log}["FULL"] == "unavailable"
    assert report.log[1]["detail"] == "full"
    assert {r["code"]: r["seats"] for r in report.log}["GONE"] == "not in the catalog"
    assert api.reserved == ["HAD"]
    assert "DRY RUN: nothing was reserved" in report.text()
    assert any("wasn't emailed" in n for n in report.notes)  # no notifier
    assert any("isn't approved" in n for n in report.notes)
    assert report.subject().startswith("DRY RUN, nothing reserved:")
    assert "would be refused" in report.log_text() and "seats now: unavailable" in report.log_text()


def test_dry_run_pretend_full_uses_the_backup():
    from reinvent_agent.reservation_runner import dry_run

    api = ReadOnlyApi([live("A", "09:00", "10:00"), live("B", "09:00", "10:00")])
    plan = make_plan(item("A", "09:00", "10:00"), item("B", "09:00", "10:00", role=BACKUP))
    report = dry_run(api, "ev", plan, pretend_full=["A"])
    assert _steps(report) == [("reserve", "A", "refused"), ("backup", "B", "reserved")]
    assert report.log[0]["seats"] == "pretending full"


def test_dry_run_simulates_pacing_without_really_waiting():
    from reinvent_agent.reservation_runner import dry_run

    times = [(f"{9 + i // 6:02d}:{i % 6}0", f"{9 + i // 6:02d}:{i % 6}5") for i in range(35)]
    ids = [f"S{i}" for i in range(35)]
    api = ReadOnlyApi([live(s, a, b) for s, (a, b) in zip(ids, times, strict=True)])
    plan = make_plan(*[item(s, a, b) for s, (a, b) in zip(ids, times, strict=True)])
    report = dry_run(api, "ev", plan)  # would hang for a minute if the pacing were real
    assert len(report.reserved) == 35 and report.log[-1]["seconds"] >= 60


def test_dry_run_emails_the_full_log():
    from reinvent_agent.reservation_runner import dry_run

    api = ReadOnlyApi([live("A", "09:00", "10:00"), live("B", "09:00", "10:00")])
    plan = make_plan(item("A", "09:00", "10:00"), item("B", "09:00", "10:00", role=BACKUP))
    notifier = ListNotifier()
    report = dry_run(api, "ev", plan, ["A"], notifier)
    (subject, body), = notifier.messages
    assert subject.startswith("DRY RUN") and body.count("DRY RUN") == 2
    assert "Full log:" in body and "Would reserve" in body
    assert "reserve  A" in body and "would be refused" in body
    assert "backup   B" in body and "Title B | for A | seats now: available" in body
    assert any("sent to your notification email" in n for n in report.notes)


def test_dry_run_keeps_the_log_when_the_email_fails():
    from reinvent_agent.reservation_runner import Notifier, dry_run

    class Broken(Notifier):
        def notify(self, subject, message):
            raise RuntimeError("SNS down")

    api = ReadOnlyApi([live("A", "09:00", "10:00")])
    report = dry_run(api, "ev", make_plan(item("A", "09:00", "10:00")), notifier=Broken())
    assert report.log and any("Couldn't email the log: SNS down" in n for n in report.notes)


def test_dry_run_reports_a_failed_sign_in():
    from reinvent_agent.reservation_runner import dry_run

    class Expired:
        def get_schedule(self, event_id):
            raise RuntimeError("invalid_grant")

    report = dry_run(Expired(), "ev", make_plan(item("A", "09:00", "10:00")))
    assert report.status == "auth_failed" and "invalid_grant" in report.notes[0]


def test_lambda_dry_run_uses_the_plan_and_emails_the_log(monkeypatch):
    from reinvent_agent import lambda_handler

    clock = Clock()
    api = ReadOnlyApi([live("A", "09:00", "10:00")])
    runner, notifier = runner_for(api, clock)
    store = PlanStore(FakeTable(), "u", "ev")
    store.approve(make_plan(item("A", "09:00", "10:00")))
    monkeypatch.setattr(lambda_handler, "_deps", lambda: (runner, store, object()))
    out = lambda_handler.handler({"action": "dry_run"}, None)
    assert out["ok"] and out["report"]["log"][0]["result"] == "reserved"
    assert len(notifier.messages) == 1 and "Full log:" in notifier.messages[0][1]
    (saved,) = store.runs()  # in the history, marked as a dry run
    assert saved["dry_run"] and store.flag("api-run-done") is None

    # Nobody subscribed: nothing sent, and the log says so.
    notifier.messages.clear()
    out = lambda_handler.handler({"action": "dry_run", "email": False}, None)
    assert notifier.messages == [] and any("wasn't emailed" in n for n in out["report"]["notes"])

    # No stored sign-in: says so on screen, no email.
    monkeypatch.setattr(lambda_handler, "_deps", lambda: (runner, store, None))
    out = lambda_handler.handler({"action": "dry_run"}, None)
    assert not out["ok"] and "No sign-in" in out["message"] and notifier.messages == []


def test_no_email_is_sent_until_someone_has_confirmed(monkeypatch):
    from types import SimpleNamespace

    from reinvent_agent import accounts

    cfg = SimpleNamespace(reservation_topic_arn="arn:topic")
    monkeypatch.setattr(accounts, "_sns", lambda cfg: object())
    for subs, expected in (
        ([], False),
        ([{"endpoint": "a@b.c", "protocol": "email", "confirmed": False}], False),
        ([{"endpoint": "a@b.c", "protocol": "email", "confirmed": True}], True),
    ):
        monkeypatch.setattr(accounts, "subscriptions", lambda cfg=None, s=subs: s)
        assert accounts.has_confirmed_email(cfg) is expected
        assert (accounts.notifier(cfg) is not None) is expected


def test_real_report_says_it_is_real_and_history_sees_it_start():
    clock = Clock()
    api = FakeApi(clock)
    seen = []
    report = runner_for(api, clock)[0].run(
        make_plan(item("A", "09:00", "10:00")), "t", clock.t, on_start=seen.append
    )
    assert seen[0]["status"] == "running" and seen[0]["reserved"] == []
    assert seen[0]["started_at"] == report.started_at  # same history entry, then updated
    assert "REAL run" in report.text() and "DRY" not in report.text()


def test_a_failed_history_write_does_not_stop_the_run():
    clock = Clock()
    api = FakeApi(clock)

    def broken(_):
        raise RuntimeError("table down")

    report = runner_for(api, clock)[0].run(
        make_plan(item("A", "09:00", "10:00")), "t", clock.t, on_start=broken
    )
    assert report.status == "done" and api.held == ["A"]


def test_lambda_run_is_in_history_from_the_start_and_keeps_a_closed_result(monkeypatch):
    from reinvent_agent import lambda_handler

    clock = Clock()
    api = FakeApi(clock, closed_polls=1)
    runner, _ = runner_for(api, clock)
    store = PlanStore(FakeTable(), "u", "ev")
    store.approve(make_plan(item("A", "09:00", "10:00")))
    saves = []
    real_save = store.save_run
    monkeypatch.setattr(store, "save_run", lambda r: (saves.append(r["status"]), real_save(r)))
    monkeypatch.setattr(lambda_handler, "_deps", lambda: (runner, store, object()))
    out = lambda_handler.handler({"action": "run", "label": "scheduled run"}, None)
    assert out["status"] == "closed" and saves == ["running", "closed"]
    assert [r["status"] for r in store.runs()] == ["closed"]  # one entry, not two
