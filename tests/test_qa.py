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
    qa = CatalogQA(_search(), client, "anthropic.claude-opus-5")
    answer = qa.ask("Deep dives on DynamoDB modeling?")
    assert "[DAT401]" in answer.text
    assert answer.cited and answer.cited[0]["code"] == "DAT401"
    assert all(c["level"] >= 400 for c in answer.cited)
    assert client.kwargs["model"] == "anthropic.claude-opus-5"
    assert "reinvent2026" in client.kwargs["system"]
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
