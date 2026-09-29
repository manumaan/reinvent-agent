import json
import time

import pytest

from reinvent_agent.events_api.models import Schedule, Session
from reinvent_agent.schedule import MySchedule, NotSignedIn
from tests.conftest import load


class FakeClient:
    def __init__(self, favorites):
        self.calls = 0
        self.favorites = favorites

    def get_schedule(self, event_id):
        self.calls += 1
        return Schedule(favorites=self.favorites)

    def get_session(self, event_id, sid):
        return Session(sessionId=sid, title="Fetched")


def catalog():
    items = load("sessions_page1.json")["items"] + load("sessions_page2.json")["items"]
    return [Session.model_validate(x) for x in items]


def test_snapshot_is_written_reused_and_refreshed(tmp_path):
    client = FakeClient(["ANT305"])
    ms = MySchedule.with_inferred_venues("ev", lambda: client, catalog(), path=tmp_path / "s.json")
    assert ms.load().favorites == ["ANT305"] and client.calls == 1
    assert ms.load().favorites == ["ANT305"] and client.calls == 1  # fresh snapshot
    client.favorites = ["ANT305", "SVS401"]
    assert len(ms.load(refresh=True).favorites) == 2 and client.calls == 2
    assert json.loads((tmp_path / "s.json").read_text())["fetchedAt"] <= time.time()


def test_signed_out_uses_snapshot_or_raises(tmp_path):
    ms = MySchedule("ev", lambda: None, {}, path=tmp_path / "s.json")
    with pytest.raises(NotSignedIn):
        ms.load()
    MySchedule("ev", lambda: FakeClient(["X"]), {}, path=tmp_path / "s.json").load()
    assert ms.load(refresh=True).favorites == ["X"]


def test_describe_uses_catalog_then_get_session(tmp_path):
    ms = MySchedule.with_inferred_venues(
        "ev", lambda: FakeClient([]), catalog(), path=tmp_path / "s.json"
    )
    d = ms.describe("SVS401")
    assert (d["code"], d["weekday"], d["start"], d["end"], d["venue"]) == (
        "SVS401",
        "Tuesday",
        "09:00",
        "10:00",
        "Venetian",
    )
    assert ms.describe("NEW1")["title"] == "Fetched"


class WriteClient(FakeClient):
    def __init__(self):
        super().__init__([])
        self.reserved = []
        self.closed = False

    def get_schedule(self, event_id):
        self.calls += 1
        return Schedule(favorites=list(self.favorites), reserved=list(self.reserved))

    def associate_favorites(self, event_id, ids):
        from reinvent_agent.events_api.models import BulkResult

        failed = [{"sessionId": i, "code": "alreadyFavorited"} for i in ids if i in self.favorites]
        new = [i for i in ids if i not in self.favorites]
        self.favorites += new
        return BulkResult.model_validate({"successful": new, "failed": failed})

    def disassociate_favorite(self, event_id, sid):
        self.favorites.remove(sid)
        return True

    def reserve_sessions(self, event_id, ids):
        from reinvent_agent.events_api.client import OperationClosedError
        from reinvent_agent.events_api.models import BulkResult

        if self.closed:
            raise OperationClosedError(409, {"message": "closed"}, "POST", "/reservations")
        self.reserved += ids[:1]
        return BulkResult.model_validate(
            {
                "successful": ids[:1],
                "failed": [
                    {"sessionId": i, "code": "scheduleConflict", "conflictsWith": ["SVS401"]}
                    for i in ids[1:]
                ],
            }
        )

    def cancel_reservation(self, event_id, sid):
        self.reserved.remove(sid)
        return True


def test_writes_report_outcomes_and_refresh_snapshot(tmp_path):
    client = WriteClient()
    ms = MySchedule.with_inferred_venues("ev", lambda: client, catalog(), path=tmp_path / "s.json")
    r = ms.favorite(["SVS401", "ANT305"])
    assert r.done == ["SVS401", "ANT305"] and not r.failed
    assert ms.load().favorites == ["SVS401", "ANT305"]  # snapshot re-read after the write
    assert ms.favorite(["SVS401"]).done == ["SVS401"]  # alreadyFavorited counts as done
    ms.unfavorite(["SVS401"])
    assert ms.load().favorites == ["ANT305"]

    r = ms.reserve(["SVS401", "SVS310"])
    assert r.done == ["SVS401"]
    assert r.failed == {"SVS310": "conflicts with another reservation (SVS401)"}
    ms.cancel_reservation(["SVS401"])
    assert ms.load().reserved == []

    client.closed = True
    r = ms.reserve(["SVS401"])
    assert r.note and "October 6" in r.note and r.failed == {"SVS401": "reservations not open"}


def test_writes_need_sign_in(tmp_path):
    ms = MySchedule("ev", lambda: None, {}, path=tmp_path / "s.json")
    with pytest.raises(NotSignedIn):
        ms.favorite(["X"])


def test_timeline_groups_real_schedule_by_day_and_flags_overlaps(tmp_path):
    from datetime import datetime

    from reinvent_agent.events_api.models import PersonalTime

    ms = MySchedule.with_inferred_venues("ev", lambda: None, catalog(), path=tmp_path / "s.json")
    sched = Schedule(
        reserved=["SVS401"],
        favorites=["SVS401", "SVS201", "SVS310"],
        personal_time=[
            PersonalTime(
                personalTimeId="pt-1",
                title="Lunch",
                description="x",
                startDateTime=datetime(2026, 12, 1, 20, 0),  # UTC = 12:00 Las Vegas
                endDateTime=datetime(2026, 12, 1, 21, 0),
            )
        ],
    )
    days = ms.timeline(sched)
    assert list(days) == ["2026-12-01"]
    rows = {e["code"] or e["title"]: e for e in days["2026-12-01"]}
    assert [e["start"] for e in days["2026-12-01"]] == ["09:00", "09:00", "10:30", "12:00"]
    assert rows["SVS401"]["reserved"] and rows["SVS401"]["favorite"]
    assert rows["SVS401"]["overlaps"] == ["SVS201"]
    assert rows["Lunch"]["kind"] == "personal time" and rows["Lunch"]["end"] == "13:00"
    assert rows["Lunch"]["overlaps"] == ["SVS310"]  # SVS310 runs 10:30-12:30
