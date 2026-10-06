from types import SimpleNamespace

from reinvent_agent.events_api.models import Session
from reinvent_agent.profile import Profile, load_local, save_local
from reinvent_agent.shortlist import refine_with_claude, score_sessions


def s(code, level, day="2026-12-01", start="09:00", type_="Breakout session", **kw):
    return Session(
        sessionId=code, title=f"Title {code}", abbreviation=code, venue="MGM Grand",
        level=f"{level} – x", type=type_,
        sessionTime={"date": day, "time": start, "length": "60"}, **kw,
    )  # fmt: skip


CATALOG = [
    s("DAT101", 100, services=["Amazon Aurora"]),
    s("DAT401", 400, services=["Amazon Aurora"]),
    s("SVS201", 200, services=["AWS Lambda"]),
    s("SVS301", 300, services=["AWS Lambda"]),
    s("AIM300", 300, topics=["Artificial Intelligence"], areasOfInterest=["Agentic AI"]),
    s("NET300", 300, topics=["Networking & Content Delivery"]),
    s("FRI200", 200, day="2026-12-04", topics=["Artificial Intelligence"]),
    s("WKS300", 300, type_="Workshop", topics=["Artificial Intelligence"]),
]


def codes(picks):
    return [p.session.code for p in picks]


def test_services_use_deep_sessions_learn_intro_sessions():
    prof = Profile(aws_using=["Amazon Aurora"], aws_learning=["AWS Lambda"])
    picks = score_sessions(prof, CATALOG)
    assert codes(picks)[:2] == ["DAT401", "SVS201"]  # 300-400 for use, 100-200 to learn
    assert "DAT101" not in codes(picks)  # intro to something you already use: no match
    by = {p.session.code: p for p in picks}
    assert by["SVS301"].score == 2  # 300 for a service you're learning: small bonus
    assert by["DAT401"].reasons == ["Amazon Aurora, which you use, at 400"]


def test_hard_filters_and_irrelevant_topics():
    prof = Profile(
        topics=["Artificial Intelligence", "Networking & Content Delivery"],
        days=["2026-12-01"],
        session_types=["Breakout session"],
        irrelevant_topics=["Networking & Content Delivery"],
    )
    got = codes(score_sessions(prof, CATALOG))
    assert got == ["AIM300"]  # FRI200 wrong day, WKS300 wrong type, NET300 irrelevant


def test_semantic_ranks_and_experience_band():
    prof = Profile(projects="agents that call tools", experience="experienced")
    picks = score_sessions(prof, CATALOG, {"projects": ["AIM300", "NET300"]})
    by = {p.session.code: p for p in picks}
    assert codes(picks) == ["AIM300", "NET300"]  # level alone never lists a session
    assert by["AIM300"].reasons == ["Matches your projects", "Level 300 fits your experience"]


def test_profile_roundtrip_and_describe(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    prof = Profile(roles=["Developer"], constraints="no 8am sessions", experience="some")
    save_local(prof, "ev")
    back = load_local("ev")
    assert back == prof and back.answered() == 3
    assert "Preferences and constraints: no 8am sessions" in back.describe()


def test_refine_with_claude_keeps_only_known_codes():
    class FakeClaude:
        def __init__(self):
            self.messages = SimpleNamespace(create=self.create)

        def create(self, **kw):
            assert kw["tool_choice"] == {"type": "tool", "name": "shortlist"}
            assert "no 8am" in kw["messages"][0]["content"]
            picks = [{"code": "AIM300", "reason": "fits"}, {"code": "BOGUS", "reason": "x"}]
            return SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", input={"picks": picks})]
            )

    prof = Profile(topics=["Artificial Intelligence"], constraints="no 8am")
    picks = score_sessions(prof, CATALOG)
    got = refine_with_claude(FakeClaude(), "m", prof, picks, lambda x: x.venue, limit=5)
    assert got == [("AIM300", "fits")]
