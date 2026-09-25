import io
import json

import pytest

from reinvent_agent.catalog.documents import to_document
from reinvent_agent.catalog.embeddings import BedrockTitanEmbedder, HashingEmbedder
from reinvent_agent.catalog.ingest import build_documents, ingest
from reinvent_agent.catalog.search import CatalogSearch, SearchFilters
from reinvent_agent.catalog.vector_store import InMemoryVectorStore, S3VectorsStore, matches
from reinvent_agent.events_api.models import Session
from tests.conftest import load

EVENT = "reinvent2026"


@pytest.fixture(scope="module")
def sessions() -> list[Session]:
    items = load("sessions_page1.json")["items"] + load("sessions_page2.json")["items"]
    return [Session.model_validate(x) for x in items]


@pytest.fixture(scope="module")
def search(sessions) -> CatalogSearch:
    store, embedder = InMemoryVectorStore(), HashingEmbedder()
    report = ingest(sessions, EVENT, store, embedder)
    assert report.indexed == len(sessions)
    return CatalogSearch(store, embedder)


def test_document_metadata_is_s3_vectors_safe(sessions):
    docs = build_documents(sessions, EVENT)
    for d in docs:
        assert None not in d.metadata.values()
        filterable = {
            k: v for k, v in d.metadata.items() if k not in ("title", "snippet", "room", "speakers")
        }
        assert len(json.dumps(filterable).encode()) < 2048
    d = next(d for d in docs if d.metadata["code"] == "SVS401")
    assert d.key == "reinvent2026#SVS401"
    assert (d.metadata["day"], d.metadata["startMin"], d.metadata["endMin"]) == (
        "2026-12-01",
        540,
        600,
    )
    assert d.metadata["levelNum"] == 400 and d.metadata["venue"] == "Venetian"
    assert "EventBridge" in d.text and "Example Speaker" in d.text


def test_unscheduled_session_has_no_time_metadata():
    d = to_document(Session(sessionId="x", title="TBD"), EVENT)
    assert "day" not in d.metadata and "startMin" not in d.metadata
    assert d.metadata["venue"] == "unknown" and "levelNum" not in d.metadata


def test_search_ranks_and_filters(search):
    top = search.search("DynamoDB data modeling", SearchFilters(min_level=400), k=3)
    assert top[0].code == "DAT401"
    assert all(r.metadata["levelNum"] >= 400 for r in top)

    intro = search.search("generative AI", SearchFilters(max_level=100), k=5)
    assert [r.code for r in intro] == ["AIM101"]

    at_venetian = search.search("serverless", SearchFilters(venues=["Venetian"]), k=10)
    assert at_venetian and {r.metadata["venue"] for r in at_venetian} == {"Venetian"}

    workshops = search.search("step functions", SearchFilters(types=["Workshop"]), k=10)
    assert [r.code for r in workshops] == ["SVS310"]

    lambda_ = search.search("performance", SearchFilters(services=["AWS Lambda"]), k=10)
    assert {r.code for r in lambda_} == {"SVS401", "SVS201", "SVS402", "SVS320"}

    afternoon = search.search("serverless", SearchFilters(start_after_min=13 * 60), k=10)
    assert all(r.metadata["startMin"] >= 780 for r in afternoon)

    other_event = search.search("serverless", SearchFilters(event_id="reinvent2025"), k=10)
    assert other_event == []


def test_summary_formats_times(search):
    r = search.search("EventBridge Pipes", SearchFilters(), k=1)[0].summary()
    assert (r["code"], r["start"], r["end"], r["day"]) == ("SVS401", "09:00", "10:00", "2026-12-01")


@pytest.mark.parametrize(
    ("flt", "expected"),
    [
        ({"a": 1}, True),
        ({"a": {"$ne": 1}}, False),
        ({"tags": {"$in": ["x", "z"]}}, True),
        ({"tags": {"$nin": ["x"]}}, False),
        ({"tags": "y"}, True),
        ({"a": {"$gte": 1, "$lt": 2}}, True),
        ({"missing": {"$eq": 1}}, False),
        ({"$or": [{"a": 2}, {"tags": "x"}]}, True),
        ({"$and": [{"a": 1}, {"tags": "q"}]}, False),
    ],
)
def test_filter_semantics(flt, expected):
    assert matches({"a": 1, "tags": ["x", "y"]}, flt) is expected


class _StubS3Vectors:
    def __init__(self):
        self.puts, self.queries = [], []

    def put_vectors(self, **kw):
        self.puts.append(kw)

    def query_vectors(self, **kw):
        self.queries.append(kw)
        return {"vectors": [{"key": "e#s1", "distance": 0.25, "metadata": {"code": "S1"}}]}


def test_s3_vectors_store_batches_and_queries():
    stub = _StubS3Vectors()
    store = S3VectorsStore("bucket", "sessions", client=stub)
    store.put([(f"k{i}", [0.1], {"i": i}) for i in range(1203)])
    assert [len(p["vectors"]) for p in stub.puts] == [500, 500, 203]
    assert stub.puts[0]["vectors"][0] == {
        "key": "k0",
        "data": {"float32": [0.1]},
        "metadata": {"i": 0},
    }
    [hit] = store.query([0.1], top_k=500, filter={"a": 1})
    q = stub.queries[0]
    assert (q["topK"], q["filter"], q["returnMetadata"]) == (100, {"a": 1}, True)
    assert (hit.key, hit.distance) == ("e#s1", 0.25)


def test_titan_embedder_request_shape():
    calls = []

    class Stub:
        def invoke_model(self, modelId, body):  # noqa: N803
            calls.append((modelId, json.loads(body)))
            return {"body": io.BytesIO(json.dumps({"embedding": [0.5] * 1024}).encode())}

    vecs = BedrockTitanEmbedder(client=Stub(), workers=2).embed(["a", "b"])
    assert len(vecs) == 2 and len(vecs[0]) == 1024
    model, body = calls[0]
    assert model == "amazon.titan-embed-text-v2:0"
    assert body["dimensions"] == 1024 and body["normalize"] is True
