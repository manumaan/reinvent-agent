import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

from reinvent_agent import config  # noqa: E402


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # no stored tokens
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(config, "stack_outputs", lambda region, client=None: {})
    config.settings.cache_clear()
    yield AppTest.from_file("../ui/app.py", default_timeout=30)
    config.settings.cache_clear()


def test_renders_signed_out_without_deployment(app):
    at = app.run()
    assert not at.exception
    assert at.title[0].value == "re:Invent 2026 planner"
    assert [t.label for t in at.tabs] == [
        "👤 Profile",
        "⭐ Short list",
        "Ask",
        "Search",
        "My schedule",
        "Plans",
        "Venues",
        "Catalog",
    ]
    assert any(b.label == "Sign in with AWS Builder ID" for b in at.sidebar.button)
    assert any("isn't deployed yet" in w.value for w in at.warning)


def test_pending_sign_in_shows_link_then_clears(app):
    app.session_state["login"] = {"status": "running", "url": "https://example.test/signin"}
    at = app.run()
    assert not at.exception
    assert any("Finish signing in" in i.value for i in at.sidebar.info)
    at.session_state["login"]["status"] = "done"  # what the sign-in thread does
    at = at.run()
    assert not at.exception
    assert not at.sidebar.info


def test_every_cleared_cache_is_a_cached_function():
    """`x.clear()` only exists on @st.cache_* functions (a refactor once broke this)."""
    import ast
    import re
    from pathlib import Path

    src = (Path(__file__).parent.parent / "ui" / "app.py").read_text()
    cached = {
        f.name
        for f in ast.parse(src).body
        if isinstance(f, ast.FunctionDef)
        and any("cache" in ast.unparse(d) for d in f.decorator_list)
    }
    cleared = set(re.findall(r"\b(\w+)\.clear\(\)", src)) - {"store"}  # store: tokens
    assert cleared <= cached, cleared - cached
