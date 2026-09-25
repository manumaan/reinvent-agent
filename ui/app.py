"""re:Invent planner: local Streamlit app.

Run from the repo root:  uv run --extra ui streamlit run ui/app.py

Runs on your machine on purpose: AWS Builder ID sign-in only redirects to
localhost (ports 8484-8489), so the app signs you in itself.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import streamlit as st

from reinvent_agent.config import settings
from reinvent_agent.events_api import EventsApiClient, FileTokenStore, Session, TokenProvider
from reinvent_agent.events_api.auth import AuthError, interactive_login, revoke

CATALOG_FILE = Path("fixtures/reinvent2026/catalog.jsonl")

st.set_page_config(page_title="re:Invent planner", page_icon="🗓️", layout="wide")
cfg = settings()
store = FileTokenStore()


# --- sign-in ------------------------------------------------------------------


def _login_worker(state: dict) -> None:
    try:
        interactive_login(store, timeout=300, open_browser=True)
        state["status"] = "done"
    except Exception as e:  # surfaced in the sidebar
        state["status"] = f"error: {e}"


def sidebar() -> bool:
    st.sidebar.header("AWS Builder ID")
    login = st.session_state.setdefault("login", {"status": "idle"})
    tokens = store.load()

    if login["status"] == "running":
        st.sidebar.info("Finish signing in in the browser tab that just opened…")
        if st.sidebar.button("Check again"):
            st.rerun()
    elif login["status"].startswith("error"):
        st.sidebar.error(login["status"])
        login["status"] = "idle"

    if tokens is None:
        st.sidebar.write("Not signed in.")
        if login["status"] != "running" and st.sidebar.button(
            "Sign in with AWS Builder ID", type="primary"
        ):
            login["status"] = "running"
            threading.Thread(target=_login_worker, args=(login,), daemon=True).start()
            st.rerun()
        return False

    login["status"] = "idle"
    st.sidebar.success("Signed in")
    if cfg.token_secret_arn and st.sidebar.button("Enable unattended reservations"):
        import boto3

        from reinvent_agent.events_api.auth import SecretsManagerTokenStore

        client = boto3.client("secretsmanager", region_name=cfg.region)
        SecretsManagerTokenStore(cfg.token_secret_arn, client=client).save(tokens)
        st.sidebar.success("Tokens stored in Secrets Manager for the Oct 8 reservation run.")
    if st.sidebar.button("Sign out"):
        try:
            revoke(tokens.refresh_token)
        except AuthError as e:
            st.sidebar.warning(str(e))
        store.clear()
        st.rerun()
    st.sidebar.caption(
        "Signing out here does not end your Builder ID browser session; "
        "use profile.aws.amazon.com for that."
    )
    return True


# --- backends -----------------------------------------------------------------


@st.cache_resource
def search_backend():
    from reinvent_agent.catalog.embeddings import BedrockTitanEmbedder
    from reinvent_agent.catalog.search import CatalogSearch
    from reinvent_agent.catalog.vector_store import S3VectorsStore

    if not cfg.vector_bucket:
        return None
    store_ = S3VectorsStore(cfg.vector_bucket, cfg.vector_index, region=cfg.region)
    return CatalogSearch(store_, BedrockTitanEmbedder(region=cfg.region))


@st.cache_resource
def qa_backend():
    from reinvent_agent.qa import CatalogQA, make_client

    search = search_backend()
    if search is None:
        return None
    return CatalogQA(search, make_client(cfg.region), cfg.model, cfg.event_id)


@st.cache_data
def local_catalog() -> dict[str, Session]:
    if not CATALOG_FILE.exists():
        return {}
    sessions = [Session.model_validate_json(x) for x in CATALOG_FILE.open() if x.strip()]
    return {s.session_id: s for s in sessions}


def not_deployed():
    st.warning(
        "Search isn't deployed yet. Run `cd infra && npx aws-cdk@2 deploy --all`, then "
        "`uv run reinvent-agent catalog index`."
    )


# --- tabs -----------------------------------------------------------------------


def ask_tab():
    qa = qa_backend()
    if qa is None:
        return not_deployed()
    history = st.session_state.setdefault("chat", [])
    for msg in history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
    question = st.chat_input("e.g. Which sessions cover zero-ETL between Aurora and Redshift?")
    if question:
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"), st.spinner("Searching the catalog…"):
            answer = qa.ask(question, history=[dict(m) for m in history])
            st.markdown(answer.text)
            if answer.cited:
                with st.expander(f"{len(answer.cited)} cited sessions"):
                    st.dataframe(answer.cited, hide_index=True)
        history += [
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer.text},
        ]


def search_tab():
    from reinvent_agent.catalog.search import SearchFilters

    search = search_backend()
    if search is None:
        return not_deployed()
    q = st.text_input("Search sessions", placeholder="serverless event-driven architecture")
    c1, c2, c3, c4 = st.columns(4)
    min_level = c1.selectbox("Min level", [None, 200, 300, 400, 500])
    days = c2.multiselect(
        "Days", ["2026-11-30", "2026-12-01", "2026-12-02", "2026-12-03", "2026-12-04"]
    )
    venues = c3.multiselect("Venues", ["MGM Grand", "Caesars Forum", "Venetian", "Caesars Palace"])
    types = c4.multiselect(
        "Types",
        ["Breakout session", "Chalk talk", "Workshop", "Builders' session", "Code talk",
         "Lightning talk"],
    )  # fmt: skip
    if q:
        filters = SearchFilters(
            event_id=cfg.event_id, min_level=min_level, days=days, venues=venues, types=types
        )
        rows = [r.summary() for r in search.search(q, filters, k=25)]
        st.dataframe(rows, hide_index=True, use_container_width=True)


def schedule_tab(signed_in: bool):
    if not signed_in:
        return st.info("Sign in to see your favorites, reservations and personal time.")
    try:
        sched = EventsApiClient(TokenProvider(store)).get_schedule(cfg.event_id)
    except Exception as e:
        return st.error(f"Could not load your schedule: {e}")
    catalog = local_catalog()

    def rows(ids):
        out = []
        for sid in ids:
            s = catalog.get(sid)
            out.append(
                {
                    "code": s.code if s else sid,
                    "title": s.title if s else "(not in local catalog)",
                    "day": s.day.isoformat() if s and s.day else None,
                    "start": s.start.strftime("%H:%M") if s and s.start else None,
                    "venue": s.venue if s else None,
                    "level": s.level_number if s else None,
                }
            )
        return sorted(out, key=lambda r: (r["day"] or "9", r["start"] or ""))

    c1, c2, c3 = st.columns(3)
    c1.metric("Reserved", len(sched.reserved))
    c2.metric("Favorites", len(sched.favorites))
    c3.metric("Personal time", len(sched.personal_time))
    st.subheader("Reserved")
    st.dataframe(rows(sched.reserved), hide_index=True, use_container_width=True)
    st.subheader("Favorites")
    st.dataframe(rows(sched.favorites), hide_index=True, use_container_width=True)
    if sched.personal_time:
        st.subheader("Personal time")
        st.dataframe(
            [json.loads(p.model_dump_json()) for p in sched.personal_time], hide_index=True
        )


signed_in = sidebar()
st.title("re:Invent 2026 planner")
tab_ask, tab_search, tab_sched = st.tabs(["Ask", "Search", "My schedule"])
with tab_ask:
    ask_tab()
with tab_search:
    search_tab()
with tab_sched:
    schedule_tab(signed_in)
