import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from reinvent_agent.events_api import EventsApiError, NotRegisteredError, OperationClosedError
from reinvent_agent.events_api.client import personal_time_body
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
    lunch = sched.personal_time[0]
    assert (lunch.title, lunch.personal_time_id, lunch.start.hour) == ("Lunch", "pt-1", 20)
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
    full, dup, clash = result.failed
    assert full.is_full and not full.is_already_reserved
    assert dup.is_already_reserved
    assert clash.is_conflict and clash.model_extra["conflictsWith"]["id"] == "pt-1"


def test_errors_raise(make_client):
    client = make_client(lambda req: httpx.Response(403, json={"message": "not registered"}))
    with pytest.raises(EventsApiError) as exc:
        client.get_session("reinvent2026", "x")
    assert exc.value.status == 403


def test_list_events_include_past(make_client):
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        return httpx.Response(200, json=load("events.json"))

    make_client(handler).list_events(include_past=True)
    assert seen == [{"includePast": "true"}]


def test_403_with_json_is_not_registered(make_client):
    client = make_client(lambda req: httpx.Response(403, json={"message": "not registered"}))
    with pytest.raises(NotRegisteredError):
        client.get_schedule("reinvent2026")


def test_403_without_body_is_edge_refusal(make_client):
    client = make_client(lambda req: httpx.Response(403))
    with pytest.raises(EventsApiError) as exc:
        client.get_schedule("reinvent2026")
    assert not isinstance(exc.value, NotRegisteredError)


def test_409_reservations_closed(make_client):
    client = make_client(lambda req: httpx.Response(409, json={"message": "closed"}))
    with pytest.raises(OperationClosedError):
        client.reserve_sessions("reinvent2026", ["s1"])


def test_html_error_body_does_not_crash(make_client):
    client = make_client(lambda req: httpx.Response(502, text="<html>bad gateway</html>"))
    with pytest.raises(EventsApiError) as exc:
        client.list_events()
    assert exc.value.status == 502


def test_removals_treat_404_as_done(make_client):
    client = make_client(lambda req: httpx.Response(404, json={"message": "gone"}))
    assert client.cancel_reservation("reinvent2026", "s1") is False
    assert client.disassociate_favorite("reinvent2026", "s1") is False
    assert client.delete_personal_time("reinvent2026", "pt") is False
    ok = make_client(lambda req: httpx.Response(204))
    assert ok.cancel_reservation("reinvent2026", "s1") is True


def test_create_personal_time_sends_utc_minutes(make_client):
    sent = []

    def handler(req):
        sent.append((req.method, req.url.path, json.loads(req.content)))
        return httpx.Response(201)

    pst = timezone(timedelta(hours=-8))
    make_client(handler).create_personal_time(
        "reinvent2026",
        "Lunch",
        "Keep lunch free",
        datetime(2026, 12, 1, 12, 0, tzinfo=pst),
        datetime(2026, 12, 1, 13, 0, tzinfo=pst),
    )
    method, path, body = sent[0]
    assert (method, path) == ("POST", "/v1/events/reinvent2026/personal-time")
    assert body == {
        "title": "Lunch",
        "description": "Keep lunch free",
        "startDateTime": "2026-12-01T20:00:00",
        "endDateTime": "2026-12-01T21:00:00",
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"title": ""},
        {"description": "x" * 251},
        {"end": datetime(2026, 12, 1, 12, 7)},  # not a 5-minute multiple
        {"end": datetime(2026, 12, 1, 11, 0)},  # before start
        {"start": datetime(2026, 12, 1, 12, 0, 30)},  # seconds
    ],
)
def test_personal_time_validation(kwargs):
    args = {
        "title": "t",
        "description": "d",
        "start": datetime(2026, 12, 1, 12, 0),
        "end": datetime(2026, 12, 1, 13, 0),
    } | kwargs
    with pytest.raises(ValueError):
        personal_time_body(**args)
