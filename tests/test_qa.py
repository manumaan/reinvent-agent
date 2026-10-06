import json
from types import SimpleNamespace

from reinvent_agent.catalog.embeddings import HashingEmbedder
from reinvent_agent.catalog.ingest import ingest
from reinvent_agent.catalog.search import CatalogSearch
from reinvent_agent.catalog.vector_store import InMemoryVectorStore
from reinvent_agent.events_api.models import Session
from reinvent_agent.qa import CatalogQA
from tests.conftest import load


def _search():
    items = load("sessions_page1.json")["items"] + load("sessions_page2.json")["items"]
    store, emb = InMemoryVectorStore(), HashingEmbedder()
    ingest([Session.model_validate(x) for x in items], "reinvent2026", store, emb)
    return CatalogSearch(store, emb)


class FakeClient:
    """Stands in for AnthropicBedrockMantle: the runner calls the tool once, then
    'answers' by citing what came back."""

    def __init__(self, tool_input):
        self.tool_input = tool_input
        self.kwargs = None
        self.beta = SimpleNamespace(messages=SimpleNamespace(tool_runner=self._runner))

    def _runner(self, **kwargs):
        self.kwargs = kwargs
        [tool] = kwargs["tools"]
        results = json.loads(tool.call(self.tool_input))
        text = "Try " + ", ".join(f"[{r['code']}] {r['title']}" for r in results[:2])
        yield SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn"
        )


def test_ask_runs_tool_and_collects_citations():
    client = FakeClient({"query": "DynamoDB data modeling", "min_level": 400, "limit": 3})
    qa = CatalogQA(_search(), client, "anthropic.claude-opus-4-8")
    answer = qa.ask("Deep dives on DynamoDB modeling?")
    assert "[DAT401]" in answer.text
    assert answer.cited and answer.cited[0]["code"] == "DAT401"
    assert all(c["level"] >= 400 for c in answer.cited)
    assert client.kwargs["model"] == "anthropic.claude-opus-4-8"
    assert "reinvent2026" in client.kwargs["system"]
    assert client.kwargs["thinking"] == {"type": "adaptive"}
    assert client.kwargs["messages"][-1] == {
        "role": "user",
        "content": "Deep dives on DynamoDB modeling?",
    }


def test_tool_schema_exposes_filters():
    [tool] = CatalogQA(_search(), None, "m")._tools({})
    props = tool.to_dict()["input_schema"]["properties"]
    assert {"query", "min_level", "days", "venues", "session_types", "services"} <= set(props)
    assert tool.to_dict()["input_schema"]["required"] == ["query"]


def test_no_results_message():
    [tool] = CatalogQA(_search(), None, "m")._tools({})
    assert tool.call({"query": "anything", "venues": ["Nowhere"]}) == "No matching sessions."


def test_haiku_runs_without_thinking():
    client = FakeClient({"query": "serverless"})
    CatalogQA(_search(), client, "anthropic.claude-haiku-4-5").ask("serverless?")
    assert "thinking" not in client.kwargs


class ScheduleClient:
    def get_schedule(self, event_id):
        from reinvent_agent.events_api.models import Schedule

        return Schedule(favorites=["SVS401", "SVS310", "ANT305", "SVS320"], reserved=[])


def test_schedule_tools_plan_from_favorites(tmp_path):
    from reinvent_agent.schedule import MySchedule

    items = load("sessions_page1.json")["items"] + load("sessions_page2.json")["items"]
    ms = MySchedule.with_inferred_venues(
        "reinvent2026",
        ScheduleClient,
        [Session.model_validate(x) for x in items],
        path=tmp_path / "s.json",
    )
    seen = {}
    tools = {t.name: t for t in CatalogQA(_search(), None, "m", schedule=ms)._tools(seen)}
    assert set(tools) == {
        "catalog_search",
        "get_my_schedule",
        "plan_one_venue_per_day",
        "venue_concurrency",
    }
    parallel = json.loads(tools["venue_concurrency"].call({"day": "2026-12-01", "at": "09:30"}))
    assert parallel["Venetian"]["codes"] == ["SVS401"]
    assert parallel["Wynn"]["codes"] == ["SVS201"]
    venetian = json.loads(
        tools["venue_concurrency"].call({"day": "2026-12-01", "venue": "Venetian"})
    )
    assert venetian["venue_days"][0]["peak_sessions"] == 1 and venetian["slots"]
    mine = json.loads(tools["get_my_schedule"].call({}))
    assert [f["code"] for f in mine["favorites"]] == ["SVS401", "SVS310", "ANT305", "SVS320"]
    plan = json.loads(tools["plan_one_venue_per_day"].call({}))
    [day] = plan["days"]
    assert day["venue"] == "Venetian"
    assert [x["code"] for x in day["sessions"]] == ["SVS401", "SVS310", "SVS320"]
    assert day["not_scheduled"][0]["code"] == "ANT305"
    assert "SVS401" in seen


def test_schedule_tools_when_signed_out(tmp_path):
    from reinvent_agent.schedule import MySchedule

    ms = MySchedule("reinvent2026", lambda: None, {}, path=tmp_path / "s.json")
    tools = {t.name: t for t in CatalogQA(_search(), None, "m", schedule=ms)._tools({})}
    assert tools["get_my_schedule"].call({}).startswith("Not signed in")
    assert tools["plan_one_venue_per_day"].call({}).startswith("Not signed in")
