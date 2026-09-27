import io

from reinvent_agent.catalog import source
from tests.conftest import load


class StubS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType):  # noqa: N803
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):  # noqa: N803
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


class PagedClient:
    def iter_session_pages(self, event_id, include_abstracts=True):
        assert include_abstracts
        yield load("sessions_page1.json")
        yield load("sessions_page2.json")


def test_download_upload_and_reload_roundtrip():
    seen = []
    items, _ = source.download_catalog(
        PagedClient(), "reinvent2026", progress=lambda n, t: seen.append(n)
    )
    assert seen[-1] == len(items) > 0
    s3 = StubS3()
    uri = source.upload(source.to_jsonl(items), "bkt", "reinvent2026", s3=s3)
    assert uri == "s3://bkt/catalog/reinvent2026/catalog.jsonl"
    sessions = source.load_sessions(uri, s3=s3)
    assert [s.session_id for s in sessions] == [x["sessionId"] for x in items]


def test_resolve_prefers_explicit_then_local_then_bucket(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert source.resolve("x.jsonl", "ev", "bkt") == "x.jsonl"
    assert source.resolve(None, "ev", "bkt") == "s3://bkt/catalog/ev/catalog.jsonl"
    assert source.resolve(None, "ev", None) == "fixtures/ev/catalog.jsonl"
    local = tmp_path / "fixtures" / "ev" / "catalog.jsonl"
    local.parent.mkdir(parents=True)
    local.write_text("")
    assert source.resolve(None, "ev", "bkt") == "fixtures/ev/catalog.jsonl"
