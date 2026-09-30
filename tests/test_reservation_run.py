import json
import subprocess
import sys

import pytest

from reinvent_agent.events_api.client import OperationClosedError
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
