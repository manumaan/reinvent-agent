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
    assert at.title[0].value == "re:Invent planner"
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
        assert any("Scheduled the run for" in s.value for s in at.success)
        assert any("starts in" in m.value for m in at.markdown)
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


def _plans_with_a_run(monkeypatch, tmp_path, subs=()):
    """The Plans tab, signed in, with an approved plan and the unattended run on."""
    import time
    from pathlib import Path

    import streamlit as st

    from reinvent_agent import accounts
    from reinvent_agent.events_api import FileTokenStore, Tokens
    from reinvent_agent.events_api.models import Schedule
    from reinvent_agent.reservation_plan import PlanStore
    from tests.test_reservation_run import FakeTable, item, live, make_plan
    from tests.test_run_schedule import FakeScheduler

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

        def get_session(self, event_id, session_id):
            return live(session_id, "09:00", "10:00")

    pstore = PlanStore(FakeTable(), "u", "reinvent2026")
    pstore.approve(make_plan(item("A", "09:00", "10:00")))
    monkeypatch.setattr(accounts, "plan_store", lambda cfg=None: pstore)
    monkeypatch.setattr(accounts, "events_client", lambda cfg=None: Api())
    monkeypatch.setattr(accounts, "unattended_enabled", lambda: True)
    monkeypatch.setattr(accounts, "subscriptions", lambda cfg=None: list(subs))
    monkeypatch.setattr(accounts, "scheduler_client", lambda cfg=None: FakeScheduler())
    app_file = Path(__file__).resolve().parent.parent / "ui" / "app.py"
    return AppTest.from_file(str(app_file), default_timeout=30), pstore


def test_schedule_time_says_when_it_will_run_and_refuses_a_past_time(monkeypatch, tmp_path):
    from datetime import datetime, time, timedelta
    from zoneinfo import ZoneInfo

    import streamlit as st

    def schedule_button(at):
        return next(b for b in at.button if b.label == "⏰ Schedule run")

    at, _ = _plans_with_a_run(monkeypatch, tmp_path)
    at = at.run()
    today = datetime.now(ZoneInfo("America/Los_Angeles")).date()  # the app's fallback timezone
    try:
        assert not at.exception
        assert any("Not scheduled yet" in c.value for c in at.caption)  # starts ten minutes ahead

        at.date_input(key="run_date").set_value(today + timedelta(days=30))
        at.time_input(key="run_time").set_value(time(21, 5))
        at = at.run()
        assert any("Not scheduled yet" in c.value and "9:05 PM" in c.value for c in at.caption)

        # A time earlier today (12:00 AM has always passed): says why, before and on click.
        at.date_input(key="run_date").set_value(today)
        at.time_input(key="run_time").set_value(time(0, 0))
        at = at.run()
        assert any("already past" in w.value and "12:00 AM" in w.value for w in at.warning)
        schedule_button(at).click()
        at = at.run()
        assert any(w.value.startswith("Not scheduled:") for w in at.warning)
        assert any("No runs scheduled" in c.value for c in at.caption)

        at.session_state["run_date"] = today - timedelta(days=1)  # tab left open overnight
        at = at.run()
        assert not at.exception and at.session_state["run_date"] >= today
    finally:
        config.settings.cache_clear()
        st.cache_data.clear()
        st.cache_resource.clear()


def test_dry_run_in_the_background_shows_a_log_labelled_as_dry(monkeypatch, tmp_path):
    import time

    import streamlit as st

    at, pstore = _plans_with_a_run(monkeypatch, tmp_path)
    at = at.run()
    try:
        assert not at.exception
        next(b for b in at.button if b.label == "🧪 Dry run (this computer)").click()
        at = at.run()
        assert not at.exception
        deadline = time.time() + 10
        while "finished" not in at.session_state["dry_run_job"] and time.time() < deadline:
            time.sleep(0.05)
        at = at.run()
        assert not at.exception
        assert at.session_state["dry_run_job"]["report"]["dry_run"]
        assert any("Nothing was reserved" in s.value for s in at.success)
        assert any(m.label == "Would be reserved or kept" and m.value == "1" for m in at.metric)
        assert any("wasn't emailed" in c.value for c in at.caption)  # nobody subscribed
        assert sum(b.label == "🧪 Dry run (this computer)" for b in at.button) == 1

        # The history says plainly that this was a dry run.
        (run,) = pstore.runs()
        assert run["label"] == "dry run (this computer)" and run["dry_run"]
        assert any("DRY RUN, nothing reserved" in m.value for m in at.markdown)
        assert not any("REAL RUN" in m.value for m in at.markdown)
    finally:
        config.settings.cache_clear()
        st.cache_data.clear()
        st.cache_resource.clear()


def test_history_shows_a_real_run_while_it_runs_and_after(monkeypatch, tmp_path):
    import time

    import streamlit as st

    at, pstore = _plans_with_a_run(monkeypatch, tmp_path)
    started = {
        "label": "scheduled run",
        "started_at": time.time(),
        "status": "running",
        "finished_at": None,
        "reserved": [],
        "failed": [],
        "notes": [],
        "log": [],
    }
    pstore.save_run(started)
    at = at.run()
    try:
        assert any("REAL RUN" in m.value and "running now" in m.value for m in at.markdown)
        assert any("Running now" in i.value for i in at.info)
        reserved = [{"code": "A", "title": "T", "how": "reserved"}]
        finished = {"status": "done", "finished_at": time.time(), "reserved": reserved}
        pstore.save_run(started | finished)
        at = at.run()
        assert any("REAL RUN" in m.value and "done: 1 reserved" in m.value for m in at.markdown)
        assert any("finished at" in s.value for s in at.success)
    finally:
        config.settings.cache_clear()
        st.cache_data.clear()
        st.cache_resource.clear()


def test_pending_email_says_to_go_confirm_it(monkeypatch, tmp_path):
    import streamlit as st

    pending = {"endpoint": "me@gmail.com", "protocol": "email", "confirmed": False}
    subs = [pending]
    at, _ = _plans_with_a_run(monkeypatch, tmp_path, subs)
    at = at.run()
    try:
        assert not at.exception
        assert any("confirm your email" in w.value for w in at.warning)
        assert any("me@gmail.com" in w.value for w in at.warning)
        assert any(b.label == "Send the email again" for b in at.button)
    finally:
        config.settings.cache_clear()
        st.cache_data.clear()
        st.cache_resource.clear()

    at, _ = _plans_with_a_run(monkeypatch, tmp_path, [pending | {"confirmed": True}])
    at = at.run()
    try:
        assert any("Subscribed:" in s.value for s in at.success)
        assert not any("confirm your email" in w.value for w in at.warning)
        # The box shows the subscribed address; subscribing it again would do nothing.
        assert at.text_input(key="sub_email").value == "me@gmail.com"
        subscribe = next(b for b in at.button if b.label == "Subscribe")
        assert subscribe.disabled
        at.text_input(key="sub_email").set_value("other@gmail.com")
        at = at.run()
        assert not next(b for b in at.button if b.label == "Subscribe").disabled
    finally:
        config.settings.cache_clear()
        st.cache_data.clear()
        st.cache_resource.clear()


def test_cloud_dry_run_says_why_it_is_disabled(monkeypatch, tmp_path):
    import streamlit as st

    from reinvent_agent import accounts

    note = "Turn on the unattended run"
    at, _ = _plans_with_a_run(monkeypatch, tmp_path)
    at = at.run()
    try:
        assert not any(note in c.value for c in at.caption)  # on: enabled, nothing to explain
        monkeypatch.setattr(accounts, "unattended_enabled", lambda: False)
        at = at.run()
        assert not at.exception
        assert next(b for b in at.button if b.label == "🧪 Dry run (cloud)").disabled
        assert any(note in c.value for c in at.caption)
    finally:
        config.settings.cache_clear()
        st.cache_data.clear()
        st.cache_resource.clear()


def test_plans_tab_no_longer_has_the_portal_checklist(monkeypatch, tmp_path):
    from pathlib import Path

    src = (Path(__file__).parent.parent / "ui" / "app.py").read_text()
    assert "portal_checklist" not in src and "reserve by hand" not in src
