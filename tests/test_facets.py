from datetime import time

from reinvent_agent.catalog import facets as fx
from reinvent_agent.events_api.models import Session


def s(code, start, venue, **kw):
    return Session(
        sessionId=code, title=f"Title {code}", abbreviation=code, venue=venue,
        sessionTime={"date": "2026-12-01", "time": start, "length": "60"}, **kw,
    )  # fmt: skip


SESSIONS = [
    s("DAT401", "09:00", "MGM Grand", level="400 – Expert", type="Chalk talk",
      topics=["Databases"], areasOfInterest=["Resilience", "Cost Optimization"],
      services=["Amazon Aurora"], abstract="Aurora global database failover"),
    s("SVS310", "13:00", "Wynn", level="300 – Advanced", type="Workshop",
      topics=["Serverless"], areasOfInterest=["Cost Optimization"],
      services=["AWS Lambda"], speakers=[{"name": "Jane Doe"}]),
    s("AIM201", "16:00", "MGM Grand", level="200 – Intermediate", type="Breakout session",
      topics=["Artificial Intelligence"], areasOfInterest=["Agentic AI"]),
]  # fmt: skip


def venue(x):
    return x.venue


def codes(filters):
    return [x.code for x in fx.apply(SESSIONS, filters, venue)]


def test_any_of_within_a_facet_all_of_across():
    f = fx.Filters(selected={"Interests": ["Resilience", "Agentic AI"]})
    assert codes(f) == ["DAT401", "AIM201"]
    f.selected["Venue"] = ["MGM Grand"]
    f.selected["Level"] = [400]
    assert codes(f) == ["DAT401"]
    assert f.active == 3


def test_text_time_services_speakers_and_favorites():
    assert codes(fx.Filters(abstract="FAILOVER")) == ["DAT401"]
    assert codes(fx.Filters(code="svs")) == ["SVS310"]
    assert codes(fx.Filters(start_from=time(12, 0), start_to=time(15, 0))) == ["SVS310"]
    assert codes(fx.Filters(selected={"Services": ["Amazon Aurora"]})) == ["DAT401"]
    assert codes(fx.Filters(selected={"Speakers": ["Jane Doe"]})) == ["SVS310"]
    assert codes(fx.Filters(only_ids={"AIM201"})) == ["AIM201"]
    assert codes(fx.Filters(selected={"Track": ["DAT", "AIM"]})) == ["DAT401", "AIM201"]


def test_options_count_sessions_per_value():
    interests = fx.options(SESSIONS, "Interests", venue)
    assert interests["Cost Optimization"] == 2 and interests["Agentic AI"] == 1
    assert fx.options(SESSIONS, "Level", venue) == {400: 1, 300: 1, 200: 1}
    assert fx.GROUPS["Venue"](SESSIONS[1], venue) == "Wynn"
