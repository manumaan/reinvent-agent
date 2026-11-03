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
        "Plans",
        "My schedule",
        "Venues",
        "Setup",
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


def test_schedule_and_cancel_a_run_from_the_plans_tab(monkeypatch, tmp_path):
    import time
    from datetime import date, timedelta
    from pathlib import Path

    import streamlit as st

    from reinvent_agent import accounts
    from reinvent_agent.events_api import FileTokenStore, Tokens
    from reinvent_agent.events_api.models import Schedule
    from reinvent_agent.reservation_plan import PlanStore
    from reinvent_agent.run_schedule import PREFIX
    from tests.test_reservation_run import FakeTable, item, make_plan
    from tests.test_run_schedule import FakeScheduler

    app_file = Path(__file__).resolve().parent.parent / "ui" / "app.py"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # no local catalog
    FileTokenStore().save(Tokens("a", "r", time.time() + 3600))
    outputs = {
        "ReservationFunctionName": "fn",
        "ReservationFunctionArn": "arn:fn",
        "ReservationSchedulerRoleArn": "arn:role",
    }
    monkeypatch.setattr(config, "stack_outputs", lambda region, client=None: outputs)
    config.settings.cache_clear()
    st.cache_data.clear()
    st.cache_resource.clear()

    class Api:
        def get_schedule(self, event_id):
            return Schedule()

    pstore = PlanStore(FakeTable(), "u", "reinvent2026")
    pstore.approve(make_plan(item("A", "09:00", "10:00")))
    scheduler = FakeScheduler()
    monkeypatch.setattr(accounts, "plan_store", lambda cfg=None: pstore)
    monkeypatch.setattr(accounts, "events_client", lambda cfg=None: Api())
    monkeypatch.setattr(accounts, "unattended_enabled", lambda: True)
    monkeypatch.setattr(accounts, "subscriptions", lambda cfg=None: [])
    monkeypatch.setattr(accounts, "scheduler_client", lambda cfg=None: scheduler)
    try:
        at = AppTest.from_file(str(app_file), default_timeout=30).run()
        assert not at.exception
        at.date_input(key="run_date").set_value(date.today() + timedelta(days=7))
        next(b for b in at.button if b.label == "⏰ Schedule run").click()
        at = at.run()
        assert not at.exception
        assert any(n.startswith(PREFIX) and n.endswith("-run") for n in scheduler.items)
        assert any("Scheduled run for" in s.value for s in at.success)
        next(b for b in at.button if b.label == "✖ Cancel").click()
        at = at.run()
        assert not at.exception and scheduler.items == {}
        assert any("Cancelled the scheduled run" in s.value for s in at.success)
    finally:
        config.settings.cache_clear()
        st.cache_data.clear()
        st.cache_resource.clear()


def test_expired_sign_in_asks_to_sign_in_and_keeps_the_page_working(monkeypatch, tmp_path):
    import time
    from pathlib import Path

    import streamlit as st

    from reinvent_agent import accounts
    from reinvent_agent.events_api import FileTokenStore, Tokens
    from reinvent_agent.events_api.auth import AuthError

    app_file = Path(__file__).resolve().parent.parent / "ui" / "app.py"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    FileTokenStore().save(Tokens("a", "r", time.time() - 10))
    monkeypatch.setattr(config, "stack_outputs", lambda region, client=None: {})
    config.settings.cache_clear()
    st.cache_data.clear()
    st.cache_resource.clear()

    class Expired:
        def get_schedule(self, event_id):
            raise AuthError('refresh failed (400); sign in again. {"error":"invalid_grant"}')

    monkeypatch.setattr(accounts, "events_client", lambda cfg=None: Expired())
    try:
        at = AppTest.from_file(str(app_file), default_timeout=30).run()
        assert not at.exception
        assert any("sign-in has expired" in e.value for e in at.sidebar.error)
        assert any(b.label == "Sign in with AWS Builder ID" for b in at.sidebar.button)
        assert not any(s.value == "Signed in" for s in at.sidebar.success)
    finally:
        config.settings.cache_clear()
        st.cache_data.clear()
        st.cache_resource.clear()


def test_dry_run_shows_a_log_and_reserves_nothing(monkeypatch, tmp_path):
    import time
    from pathlib import Path

    import streamlit as st

    from reinvent_agent import accounts
    from reinvent_agent.events_api import FileTokenStore, Tokens
    from reinvent_agent.events_api.models import Schedule
    from reinvent_agent.reservation_plan import PlanStore
    from tests.test_reservation_run import FakeTable, item, live, make_plan
    from tests.test_run_schedule import FakeScheduler

    app_file = Path(__file__).resolve().parent.parent / "ui" / "app.py"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    FileTokenStore().save(Tokens("a", "r", time.time() + 3600))
    outputs = {
        "ReservationFunctionName": "fn",
        "ReservationFunctionArn": "arn:fn",
        "ReservationSchedulerRoleArn": "arn:role",
    }
    monkeypatch.setattr(config, "stack_outputs", lambda region, client=None: outputs)
    config.settings.cache_clear()
    st.cache_data.clear()
    st.cache_resource.clear()

    class Api:
        def get_schedule(self, event_id):
            return Schedule()

        def iter_sessions(self, event_id, include_abstracts=True):
            return iter([live("A", "09:00", "10:00")])

    pstore = PlanStore(FakeTable(), "u", "reinvent2026")
    pstore.approve(make_plan(item("A", "09:00", "10:00")))
    monkeypatch.setattr(accounts, "plan_store", lambda cfg=None: pstore)
    monkeypatch.setattr(accounts, "events_client", lambda cfg=None: Api())
    monkeypatch.setattr(accounts, "unattended_enabled", lambda: True)
    monkeypatch.setattr(accounts, "subscriptions", lambda cfg=None: [])
    monkeypatch.setattr(accounts, "scheduler_client", lambda cfg=None: FakeScheduler())
    try:
        at = AppTest.from_file(str(app_file), default_timeout=30).run()
        assert not at.exception
        next(b for b in at.button if b.label == "🧪 Dry run (this computer)").click()
        at = at.run()
        assert not at.exception
        report = at.session_state["dry_run_report"]
        assert report["dry_run"] and report["log"][0]["code"] == "A"
        assert any(m.label == "Reserved or already held" and m.value == "1" for m in at.metric)
        assert any("Nothing was reserved" in c.value for c in at.caption)
        assert pstore.runs() == []
    finally:
        config.settings.cache_clear()
        st.cache_data.clear()
        st.cache_resource.clear()
