import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

from reinvent_agent import config  # noqa: E402


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # no stored tokens
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setattr(config, "stack_outputs", lambda region, client=None: {})
    config.settings.cache_clear()
    yield AppTest.from_file("../ui/app.py", default_timeout=30)
    config.settings.cache_clear()


def test_renders_signed_out_without_deployment(app):
    at = app.run()
    assert not at.exception
    assert at.title[0].value == "re:Invent 2026 planner"
    assert [t.label for t in at.tabs] == ["Ask", "Search", "My schedule"]
    assert any(b.label == "Sign in with AWS Builder ID" for b in at.sidebar.button)
    assert any("isn't deployed yet" in w.value for w in at.warning)
