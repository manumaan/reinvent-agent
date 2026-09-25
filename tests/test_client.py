import json

import httpx
import pytest

from reinvent_agent.events_api import EventsApiError
from tests.conftest import load


def test_list_events_needs_no_auth(make_client):
    def handler(req):
        assert "authorization" not in req.headers
        return httpx.Response(200, json=load("events.json"))

    events = make_client(handler).list_events()
    assert [e.event_id for e in events] == ["reinvent2026", "reinvent2025"]


def test_iter_sessions_paginates_and_parses(make_client):
    calls = []

    def handler(req):
        calls.append(dict(req.url.params))
        assert req.headers["authorization"] == "Bearer access-1"
        page = (
            "sessions_page2.json"
            if req.url.params.get("nextToken") == "page2"
            else "sessions_page1.json"
        )
        return httpx.Response(200, json=load(page))

    sessions = list(make_client(handler).iter_sessions("reinvent2026", include_abstracts=False))
    assert len(sessions) == 10
    assert calls[0]["includeAbstracts"] == "false"
    assert calls[1]["nextToken"] == "page2"
    s = sessions[0]
    assert (s.code, s.venue, s.level_number) == ("SVS401", "Venetian", 400)
    assert s.start.hour == 9 and s.reservable


def test_throttle_honours_retry_after(token_store):
    slept, n = [], {"calls": 0}

    def handler(req):
        n["calls"] += 1
        if n["calls"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json=load("events.json"))

    from reinvent_agent.events_api import EventsApiClient, TokenProvider

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = EventsApiClient(TokenProvider(token_store, http=http), http=http, sleep=slept.append)
    client.list_events()
    assert slept == [7.0]


def test_401_triggers_one_refresh(make_client, token_store):
    state = {"api": 0}

    def handler(req):
        if req.url.host == "oauth.awsevents.com":
            return httpx.Response(200, json={"access_token": "access-2", "expires_in": 3600})
        state["api"] += 1
        if req.headers["authorization"] == "Bearer access-1":
            return httpx.Response(401, json={"message": "expired"})
        return httpx.Response(200, json=load("schedule.json"))

    sched = make_client(handler).get_schedule("reinvent2026")
    assert state["api"] == 2
    assert token_store.load().access_token == "access-2"
    assert sched.personal_time[0].title == "Lunch"
    assert sched.reservations[0].code == "SVS401"


def test_reserve_batches_dedupes_and_reports_partial_failure(make_client):
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json=load("reserve_partial.json") if len(bodies) == 1 else {})

    ids = [f"s{i}" for i in range(12)] + ["s0"]
    result = make_client(handler).reserve_sessions("reinvent2026", ids)
    assert [len(b["sessionIds"]) for b in bodies] == [10, 2]
    assert result.succeeded == ["sess-0003"]
    full, dup = result.failed
    assert full.is_full and not full.is_already_reserved
    assert dup.is_already_reserved


def test_errors_raise(make_client):
    client = make_client(lambda req: httpx.Response(403, json={"message": "not registered"}))
    with pytest.raises(EventsApiError) as exc:
        client.get_session("reinvent2026", "x")
    assert exc.value.status == 403
