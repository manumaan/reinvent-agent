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
