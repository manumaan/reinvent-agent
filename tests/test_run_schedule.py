import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from reinvent_agent.run_schedule import PREFIX, cancel_run, list_runs, schedule_run

IST = "Asia/Kolkata"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


class FakeScheduler:
    def __init__(self, fail_on=None):
        self.items, self.fail_on = {}, fail_on

    def create_schedule(self, **kw):
        if self.fail_on and self.fail_on in kw["Name"]:
            raise RuntimeError("boom")
        self.items[kw["Name"]] = kw

    def list_schedules(self, NamePrefix):  # noqa: N803
        return {"Schedules": [{"Name": n} for n in self.items if n.startswith(NamePrefix)]}

    def get_schedule(self, Name):  # noqa: N803
        return self.items[Name]

    def delete_schedule(self, Name):  # noqa: N803
        if Name not in self.items:
            raise type("ResourceNotFoundException", (Exception,), {})()
        del self.items[Name]


def test_run_and_check_are_one_time_self_deleting_jobs():
    client = FakeScheduler()
    when = datetime(2026, 11, 10, 21, 30, tzinfo=ZoneInfo(IST))
    run = schedule_run(when, IST, "arn:fn", "arn:role", client, now=NOW)
    assert run.stamp == "20261110T1600" and run.has_check
    job = client.items[f"{PREFIX}20261110T1600-run"]
    assert job["ScheduleExpression"] == "at(2026-11-10T21:30:00)"
    assert job["ScheduleExpressionTimezone"] == IST
    assert job["ActionAfterCompletion"] == "DELETE"
    assert job["Target"]["RetryPolicy"]["MaximumRetryAttempts"] == 0
    assert json.loads(job["Target"]["Input"])["action"] == "run"
    check = client.items[f"{PREFIX}20261110T1600-check"]
    assert check["ScheduleExpression"] == "at(2026-11-10T21:00:00)"
    assert json.loads(check["Target"]["Input"])["action"] == "preflight"


def test_past_times_are_refused_and_soon_runs_skip_the_check():
    client = FakeScheduler()
    with pytest.raises(ValueError):
        schedule_run(NOW + timedelta(minutes=1), IST, "f", "r", client, now=NOW)
    assert client.items == {}
    soon = schedule_run(NOW + timedelta(minutes=10), IST, "f", "r", client, now=NOW)
    assert not soon.has_check and len(client.items) == 1


def test_list_and_cancel():
    client = FakeScheduler()
    later = datetime(2026, 11, 20, 9, 0, tzinfo=ZoneInfo(IST))
    sooner = datetime(2026, 11, 10, 9, 0, tzinfo=ZoneInfo(IST))
    schedule_run(later, IST, "f", "r", client, now=NOW)
    schedule_run(sooner, IST, "f", "r", client, now=NOW)
    runs = list_runs(client)
    assert [r.when for r in runs] == [sooner, later] and all(r.has_check for r in runs)
    cancel_run(runs[0].stamp, client)
    cancel_run(runs[0].stamp, client)  # already gone is fine
    assert [r.when for r in list_runs(client)] == [later]


def test_a_failed_check_leaves_nothing_behind():
    client = FakeScheduler(fail_on="-check")
    with pytest.raises(RuntimeError):
        schedule_run(NOW + timedelta(days=1), IST, "f", "r", client, now=NOW)
    assert client.items == {}


def test_list_skips_a_run_that_fired_while_listing():
    """AWS deletes a run as it fires, which can fall between the list and the details."""
    from datetime import UTC, datetime, timedelta

    from reinvent_agent.run_schedule import list_runs, schedule_run

    class Firing(FakeScheduler):
        def get_schedule(self, Name):  # noqa: N803
            raise type("ResourceNotFoundException", (Exception,), {})()

    client = Firing()
    now = datetime(2026, 11, 4, tzinfo=UTC)
    schedule_run(now + timedelta(minutes=5), "UTC", "arn:fn", "arn:role", client, now=now)
    assert list_runs(client) == []

    class Broken(FakeScheduler):
        def get_schedule(self, Name):  # noqa: N803
            raise RuntimeError("throttled")

    client = Broken()
    schedule_run(now + timedelta(minutes=5), "UTC", "arn:fn", "arn:role", client, now=now)
    with pytest.raises(RuntimeError):  # anything else is still an error
        list_runs(client)
