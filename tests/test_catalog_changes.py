import json
import time

from reinvent_agent.catalog import changes as cc
from reinvent_agent.catalog.source import to_jsonl

A = {"sessionId": "a", "title": "Aurora", "abbreviation": "DAT1", "room": "R1"}
B = {"sessionId": "b", "title": "Bedrock", "abbreviation": "AIM2", "abstract": "Old"}
C = {"sessionId": "c", "title": "Cloud", "abbreviation": "SVS3"}


class Pages:
    def __init__(self, items):
        self.items = items

    def iter_session_pages(self, event_id, include_abstracts=True):
        assert include_abstracts  # an edited description counts as a change
        yield {"items": self.items[:1]}
        yield {"items": self.items[1:]}


def test_compare_finds_new_removed_and_updated():
    ours = to_jsonl([A, B])
    theirs = to_jsonl([{**B, "abstract": "New", "room": "R9"}, C])
    found = cc.compare(ours, theirs, checked_at=5)
    assert found.added == ["SVS3"]
    assert found.removed == ["DAT1"]
    assert found.changed == {"AIM2": ["description", "room"]}
    assert found.found
    assert found.summary() == "1 new, 1 removed, 1 updated"


def test_compare_ignores_seats_and_order():
    ours = to_jsonl([A, {**B, "seatAvailability": "available", "isReservable": True}])
    theirs = to_jsonl([{**B, "seatAvailability": "limited", "isReservable": False}, A])
    assert not cc.compare(ours, theirs).found


def test_check_due_waits_for_the_interval(monkeypatch):
    monkeypatch.setenv("REINVENT_CATALOG_CHECK_HOURS", "6")
    assert cc.check_due(None)
    ok = cc.CatalogChanges(checked_at=1000)
    assert not cc.check_due(ok, now=1000 + 5 * 3600)
    assert cc.check_due(ok, now=1000 + 6 * 3600)
    failed = cc.CatalogChanges(checked_at=1000, error="sign-in")
    assert not cc.check_due(failed, now=1000 + cc.RETRY_SECONDS - 1)
    assert cc.check_due(failed, now=1000 + cc.RETRY_SECONDS)
    monkeypatch.setenv("REINVENT_CATALOG_CHECK_HOURS", "0")  # off
    assert not cc.check_due(None)


def test_check_saves_a_result_and_a_download_clears_it(tmp_path):
    ours = tmp_path / "catalog.jsonl"
    ours.write_text(to_jsonl([A]))
    found = cc.check_catalog(Pages([A, C]), "ev", ours)
    assert found.added == ["SVS3"]
    assert cc.load_changes("ev").added == ["SVS3"]
    cc.mark_up_to_date("ev")
    saved = cc.load_changes("ev")
    assert not saved.found and not saved.error


def _wait_for_check():
    for _ in range(200):
        if cc._running.acquire(blocking=False):
            cc._running.release()
            return
        time.sleep(0.01)
    raise AssertionError("the check didn't finish")


def test_background_check_records_a_failure(tmp_path):
    def no_client():
        raise RuntimeError("sign in again")

    assert cc.start_check(no_client, "ev", tmp_path / "missing.jsonl")
    _wait_for_check()
    assert cc.load_changes("ev").error == "sign in again"


def test_only_one_background_check_at_a_time(tmp_path):
    assert cc._running.acquire(blocking=False)
    try:
        assert not cc.start_check(lambda: None, "ev", tmp_path / "x")
    finally:
        cc._running.release()


def test_unreadable_result_counts_as_no_check():
    path = cc.changes_path("ev")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"unexpected": 1}))
    assert cc.load_changes("ev") is None
