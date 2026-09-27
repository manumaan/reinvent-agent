"""re:Invent planner: local Streamlit app.

Run from the repo root:  uv run --extra ui streamlit run ui/app.py

Runs on your machine on purpose: AWS Builder ID sign-in only redirects to
localhost (ports 8484-8489), so the app signs you in itself.
"""

from __future__ import annotations

import json
import threading
import time

import streamlit as st

from reinvent_agent.catalog import source
from reinvent_agent.config import settings
from reinvent_agent.events_api import EventsApiClient, FileTokenStore, Session, TokenProvider
from reinvent_agent.events_api.auth import AuthError, interactive_login, revoke
from reinvent_agent.llm import get_provider
from reinvent_agent.schedule import MySchedule

st.set_page_config(page_title="re:Invent planner", page_icon="🗓️", layout="wide")
cfg = settings()
store = FileTokenStore()


# --- sign-in ------------------------------------------------------------------


def _login_worker(state: dict) -> None:
    try:
        interactive_login(
            store, timeout=300, open_browser=True, on_url=lambda u: state.update(url=u)
        )
        state["status"] = "done"
    except Exception as e:  # surfaced in the sidebar
        state["status"] = f"error: {e}"


@st.fragment(run_every=2)
def _login_pending() -> None:
    """Polls the background sign-in and reruns the whole app once it finishes."""
    login = st.session_state["login"]
    if login["status"] != "running":
        st.rerun(scope="app")
    st.info("Finish signing in in the browser tab that just opened…")
    if url := login.get("url"):
        st.link_button("Open the sign-in page", url)


def model_caption() -> None:
    llm = get_provider(cfg.llm_provider)
    st.sidebar.divider()
    st.sidebar.caption(
        f"Answers: {llm.label}, `{cfg.model}`. Switch with `reinvent-agent config set-provider`."
    )


def sidebar() -> bool:
    st.sidebar.header("AWS Builder ID")
    login = st.session_state.setdefault("login", {"status": "idle"})
    tokens = store.load()

    if login["status"] == "running":
        with st.sidebar:
            _login_pending()
    elif login["status"].startswith("error"):
        st.sidebar.error(login["status"])
        login["status"] = "idle"

    if tokens is None:
        st.sidebar.write(
            "Not signed in. Sign in with the AWS Builder ID you registered for re:Invent "
            "to load the full catalog and your schedule."
        )
        if login["status"] != "running" and st.sidebar.button(
            "Sign in with AWS Builder ID", type="primary"
        ):
            login.update(status="running", url=None)
            threading.Thread(target=_login_worker, args=(login,), daemon=True).start()
            st.rerun()
        return False

    just_signed_in = login["status"] == "done"
    login["status"] = "idle"
    st.sidebar.success("Signed in")
    schedule_status(refresh=just_signed_in)
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
        my_schedule().path.unlink(missing_ok=True)
        st.rerun()
    st.sidebar.caption(
        "Signing out here does not end your Builder ID browser session; "
        "use profile.aws.amazon.com for that."
    )
    return True


def schedule_status(refresh: bool) -> None:
    """Sync GetSchedule into the local snapshot (on sign-in, when stale, or on demand)."""
    sched = my_schedule()
    manual = st.sidebar.button("Refresh my schedule")
    try:
        data = sched.load(refresh=refresh or manual)
    except Exception as e:
        return st.sidebar.warning(f"Could not load your schedule: {e}")
    when = time.strftime("%H:%M", time.localtime(sched.fetched_at() or time.time()))
    st.sidebar.caption(
        f"Schedule synced {when}: {len(data.favorites)} favorites, "
        f"{len(data.reserved)} reserved, {len(data.personal_time)} personal time."
    )


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


def events_client() -> EventsApiClient | None:
    return EventsApiClient(TokenProvider(store)) if store.load() else None


@st.cache_resource
def my_schedule() -> MySchedule:
    return MySchedule.with_inferred_venues(
        cfg.event_id, events_client, list(local_catalog().values())
    )


@st.cache_resource
def qa_backend():
    from reinvent_agent.qa import CatalogQA, make_client

    search = search_backend()
    if search is None:
        return None
    client = make_client(cfg.region, cfg.llm_provider, cfg.anthropic_key_secret_arn)
    return CatalogQA(search, client, cfg.model, cfg.event_id, schedule=my_schedule())


@st.cache_data
def local_catalog() -> dict[str, Session]:
    try:
        sessions = source.load_sessions(source.resolve(None, cfg.event_id, cfg.catalog_bucket))
    except Exception:  # no catalog yet
        return {}
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
            try:
                answer = qa.ask(question, history=[dict(m) for m in history])
            except Exception as e:  # e.g. model not enabled for the account
                return st.error(f"Claude call failed ({cfg.llm_provider}): {e}")
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


def index_catalog(sessions: list[Session], bar) -> None:
    """Embed into S3 Vectors (Bedrock Titan) and upsert the DynamoDB sessions table."""
    import boto3

    from reinvent_agent.catalog.embeddings import BedrockTitanEmbedder
    from reinvent_agent.catalog.ingest import ingest
    from reinvent_agent.catalog.vector_store import S3VectorsStore

    table = (
        boto3.resource("dynamodb", region_name=cfg.region).Table(cfg.sessions_table)
        if cfg.sessions_table
        else None
    )
    bar.progress(0.0, text="Embedding and indexing…")
    report = ingest(
        sessions,
        cfg.event_id,
        S3VectorsStore(cfg.vector_bucket, cfg.vector_index, region=cfg.region),
        BedrockTitanEmbedder(region=cfg.region),
        table=table,
        progress=lambda d, t: bar.progress(d / t, text=f"Indexed {d}/{t}…"),
    )
    st.success(f"Indexed {report.indexed} sessions for search and Q&A.")


def refresh_catalog(do_index: bool) -> None:
    """Events API -> local JSONL -> catalog bucket -> (optionally) vectors + DynamoDB."""
    bar = st.progress(0.0, text="Downloading sessions…")

    def on_page(n, total):
        bar.progress(min(n / total, 1.0) if total else 0.0, text=f"Downloaded {n} sessions…")

    client = EventsApiClient(TokenProvider(store))
    items, total = source.download_catalog(client, cfg.event_id, progress=on_page)
    body = source.to_jsonl(items)
    path = source.local_path(cfg.event_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    st.success(f"Downloaded {len(items)} sessions (API totalCount {total}) to `{path}`.")
    if cfg.catalog_bucket:
        st.success(f"Uploaded to `{source.upload(body, cfg.catalog_bucket, cfg.event_id)}`.")
    local_catalog.clear()
    my_schedule.clear()
    qa_backend.clear()
    if do_index and cfg.vector_bucket:
        index_catalog([Session.model_validate(x) for x in items], bar)
    bar.empty()


def catalog_tab(signed_in: bool):
    catalog = local_catalog()
    where = source.resolve(None, cfg.event_id, cfg.catalog_bucket)
    if catalog:
        st.write(f"**{len(catalog)} sessions** loaded from `{where}`.")
    else:
        st.write("No catalog downloaded yet.")
    if cfg.catalog_bucket:
        st.caption(
            f"Shared copy: `{source.s3_uri(cfg.catalog_bucket, cfg.event_id)}` "
            "(what `reinvent-agent catalog index` reads when there is no local file)."
        )
    if catalog and cfg.vector_bucket and st.button("Index this catalog for search and Q&A"):
        bar = st.progress(0.0)
        try:
            index_catalog(list(catalog.values()), bar)
        except Exception as e:
            st.error(f"Indexing failed: {e}")
        bar.empty()
    if not signed_in:
        return st.info(
            "The session catalog needs a re:Invent registration: sign in with AWS Builder ID "
            "in the sidebar to download it."
        )
    do_index = st.checkbox(
        "Also index for search and Q&A (Bedrock Titan embeddings)", value=bool(cfg.vector_bucket)
    )
    if st.button("Download full catalog from the AWS Events API", type="primary"):
        try:
            refresh_catalog(do_index)
        except Exception as e:
            st.error(f"Catalog refresh failed: {e}")


def schedule_tab(signed_in: bool):
    if not signed_in:
        return st.info("Sign in to see your favorites, reservations and personal time.")
    sched_ = my_schedule()
    try:
        sched = sched_.load()
    except Exception as e:
        return st.error(f"Could not load your schedule: {e}")

    def rows(ids):
        out = [sched_.describe(sid) for sid in ids]
        cols = ("code", "title", "weekday", "day", "start", "end", "venue", "level")
        out = [{c: r.get(c) for c in cols} for r in out]
        return sorted(out, key=lambda r: (r["day"] or "9", r["start"] or ""))

    c1, c2, c3 = st.columns(3)
    c1.metric("Reserved", len(sched.reserved))
    c2.metric("Favorites", len(sched.favorites))
    c3.metric("Personal time", len(sched.personal_time))
    with st.expander("One venue per day plan (favorites + reserved)"):
        venue_plan(sched_, sched)
    st.subheader("Reserved")
    st.dataframe(rows(sched.reserved), hide_index=True, use_container_width=True)
    st.subheader("Favorites")
    st.dataframe(rows(sched.favorites), hide_index=True, use_container_width=True)
    if sched.personal_time:
        st.subheader("Personal time")
        st.dataframe(
            [json.loads(p.model_dump_json()) for p in sched.personal_time], hide_index=True
        )


def venue_plan(sched_: MySchedule, sched) -> None:
    from reinvent_agent.planner import plan_one_venue_per_day

    ids = list(dict.fromkeys(sched.favorites + sched.reserved))
    plan = plan_one_venue_per_day(sched_.sessions(ids), sched_.venue_of, sched.reserved)
    st.write(plan.summary)
    for d in plan.days:
        left = "" if d.all_fit else f", {len(d.not_scheduled)} left out"
        st.markdown(f"**{d.weekday} {d.day} · {d.venue}** ({len(d.sessions)} sessions{left})")
        timeline = [
            {"start": x.start, "end": x.end, "session": f"{x.code} {x.title}"} for x in d.sessions
        ] + [{"start": g.start, "end": g.end, "session": "— free —"} for g in d.free_slots]
        st.dataframe(sorted(timeline, key=lambda r: r["start"]), hide_index=True)
        if d.not_scheduled:
            st.caption(
                "Left out: "
                + "; ".join(f"{x['code']} ({x['venue']}, {x['reason']})" for x in d.not_scheduled)
            )
    if plan.unplaceable:
        st.caption("Not plannable: " + ", ".join(x["code"] for x in plan.unplaceable))


signed_in = sidebar()
model_caption()
st.title("re:Invent 2026 planner")
tab_ask, tab_search, tab_sched, tab_cat = st.tabs(["Ask", "Search", "My schedule", "Catalog"])
with tab_ask:
    ask_tab()
with tab_search:
    search_tab()
with tab_sched:
    schedule_tab(signed_in)
with tab_cat:
    catalog_tab(signed_in)
