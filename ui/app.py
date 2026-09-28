"""re:Invent planner: local Streamlit app.

Run from the repo root:  uv run --extra ui streamlit run ui/app.py

Runs on your machine on purpose: AWS Builder ID sign-in only redirects to
localhost (ports 8484-8489), so the app signs you in itself.
"""

from __future__ import annotations

import threading
import time

import streamlit as st

from reinvent_agent.catalog import source
from reinvent_agent.config import settings
from reinvent_agent.events_api import EventsApiClient, FileTokenStore, Session, TokenProvider
from reinvent_agent.events_api.auth import AuthError, interactive_login, revoke
from reinvent_agent.llm import get_provider
from reinvent_agent.schedule import MySchedule

FAV_ICON, RES_ICON = "⭐", "🎟️"

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


@st.cache_resource
def catalog_docs():
    """Per-session metadata exactly as indexed (inferred venues), for browsing."""
    from reinvent_agent.catalog.ingest import build_documents

    return build_documents(list(local_catalog().values()), cfg.event_id)


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


def search_tab(signed_in: bool):
    from datetime import date

    from reinvent_agent.catalog.search import SearchFilters, browse

    docs = catalog_docs()
    meta = [d.metadata for d in docs]
    q = st.text_input(
        "Search sessions",
        placeholder="serverless event-driven architecture (leave empty to browse all)",
    )
    c1, c2, c3, c4 = st.columns(4)
    min_level = c1.selectbox("Min level", [None, 100, 200, 300, 400, 500])
    days = c2.multiselect(
        "Days",
        sorted({m["day"] for m in meta if "day" in m}),
        format_func=lambda d: date.fromisoformat(d).strftime("%a %b %-d"),
    )
    venues = c3.multiselect("Venues", sorted({m["venue"] for m in meta}))
    types = c4.multiselect("Types", sorted({m["type"] for m in meta}))
    filters = SearchFilters(
        event_id=cfg.event_id, min_level=min_level, days=days, venues=venues, types=types
    )

    if q:
        search = search_backend()
        if search is None:
            return not_deployed()
        # Selecting rows reruns the script; keep results so we don't re-embed the query.
        key = (q, min_level, tuple(days), tuple(venues), tuple(types))
        cached = st.session_state.get("search_results")
        if not cached or cached[0] != key:
            cached = (key, [r.summary() for r in search.search(q, filters, k=25)])
            st.session_state["search_results"] = cached
        results = cached[1]
        st.caption(f"Top {len(results)} matches for “{q}”.")
    else:
        if not docs:
            return st.info("No catalog yet: download it from the Catalog tab.")
        key = ("", min_level, tuple(days), tuple(venues), tuple(types))
        results = [r.summary() for r in browse(docs, filters)]
        st.caption(f"{len(results)} of {len(docs)} sessions.")

    sched = None
    if signed_in:
        try:
            sched = my_schedule().load()
        except Exception as e:
            st.warning(f"Could not load your schedule: {e}")
    favorites = set(sched.favorites) if sched else set()
    reserved = set(sched.reserved) if sched else set()

    cols = ("code", "title", "type", "level", "weekday", "day", "start", "end", "venue")
    rows = [
        {
            "fav": FAV_ICON if r["sessionId"] in favorites else "",
            "res": RES_ICON if r["sessionId"] in reserved else "",
            **{c: r.get(c) for c in cols},
        }
        for r in results
    ]
    # Actions sit ABOVE the table so they are visible right after ticking rows. The
    # selection is read from widget state (set on the rerun the tick triggered); the
    # nonce gives a fresh, unselected table after each action.
    table_key = f"search_table_{hash(key)}_{st.session_state.get('table_nonce', 0)}"
    if signed_in:
        state = st.session_state.get(table_key)
        selected = list(state.selection.rows) if state is not None else []
        session_actions([results[i] for i in selected], favorites, reserved)
    st.dataframe(
        rows,
        hide_index=True,
        use_container_width=True,
        column_config={
            "fav": st.column_config.TextColumn(FAV_ICON, width=40, help="Favorited"),
            "res": st.column_config.TextColumn(RES_ICON, width=40, help="Reserved"),
        },
        on_select="rerun" if signed_in else "ignore",
        selection_mode="multi-row",
        key=table_key,
    )
    if not signed_in:
        st.caption("Sign in to favorite or reserve sessions from here.")


def session_actions(picked: list[dict], favorites: set[str], reserved: set[str]) -> None:
    """Toolbar for the selected search rows: favorite / unfavorite / reserve / cancel."""
    ids = [r["sessionId"] for r in picked]
    to_fav = [i for i in ids if i not in favorites]
    to_unfav = [i for i in ids if i in favorites]
    # The catalog's isReservable can be stale (false everywhere before seating opens),
    # so let the API decide; it answers 409 (not open yet) or sessionNotReservable.
    to_reserve = [i for i in ids if i not in reserved]
    to_cancel = [i for i in ids if i in reserved]
    sched = my_schedule()

    with st.container(border=True):
        if flash := st.session_state.pop("action_result", None):
            for kind, text in flash:
                getattr(st, kind)(text)
        if not ids:
            st.markdown(
                f"☑️ **Tick sessions in the table below**, then {FAV_ICON} favorite or "
                f"{RES_ICON} reserve them here.  \n{FAV_ICON} favorite · {RES_ICON} reserved"
            )
            return
        codes = ", ".join(r["code"] for r in picked[:6]) + ("…" if len(picked) > 6 else "")
        st.markdown(f"**{len(ids)} selected:** {codes}")
        b1, b2, b3, b4 = st.columns(4)
        actions = [
            (b1, FAV_ICON, "Favorite", to_fav, sched.favorite, "primary"),
            (b2, "☆", "Unfavorite", to_unfav, sched.unfavorite, "secondary"),
            (b3, RES_ICON, "Reserve", to_reserve, sched.reserve, "primary"),
            (b4, "✖", "Unreserve", to_cancel, sched.cancel_reservation, "secondary"),
        ]
        for col, icon, name, targets, fn, kind in actions:
            label = f"{icon} {name}" + (f" ({len(targets)})" if targets else "")
            if col.button(
                label,
                disabled=not targets,
                type=kind if targets else "secondary",
                use_container_width=True,
                help=None if targets else f"None of the selected sessions to {name.lower()}",
            ):
                run_action(name, fn, targets, picked)
        if to_reserve:
            st.caption("Reserved seating opens in the Events API on Oct 8, 2026.")


def run_action(name: str, fn, targets: list[str], picked: list[dict]) -> None:
    code = {r["sessionId"]: r["code"] for r in picked}
    try:
        result = fn(targets)
    except Exception as e:
        msgs = [("error", f"{name} failed: {e}")]
    else:
        msgs = []
        if result.done:
            names = ", ".join(code.get(i, i) for i in result.done)
            msgs.append(("success", f"{result.action.capitalize()}: {names}"))
        if result.note:
            msgs.append(("warning", result.note))
        elif result.failed:
            why = "; ".join(f"{code.get(i, i)} ({r})" for i, r in result.failed.items())
            msgs.append(("warning", f"Not done: {why}"))
    st.session_state["action_result"] = msgs
    st.session_state["table_nonce"] = st.session_state.get("table_nonce", 0) + 1
    st.rerun()


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
    catalog_docs.clear()
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
    """Only what is really in your AWS Events schedule: reservations, favorites and
    personal time, day by day."""
    from datetime import date

    if not signed_in:
        return st.info("Sign in to see your favorites, reservations and personal time.")
    sched_ = my_schedule()
    try:
        sched = sched_.load()
    except Exception as e:
        return st.error(f"Could not load your schedule: {e}")

    c1, c2, c3 = st.columns(3)
    c1.metric(f"{RES_ICON} Reserved", len(sched.reserved))
    c2.metric(f"{FAV_ICON} Favorites", len(sched.favorites))
    c3.metric("🕑 Personal time", len(sched.personal_time))
    days = sched_.timeline(sched)
    if not days:
        return st.info("Your schedule is empty: favorite or reserve sessions from the Search tab.")
    st.caption(
        f"{RES_ICON} reserved · {FAV_ICON} favorite · 🕑 personal time · "
        "⚠️ overlaps another entry the same day"
    )
    for day, items in days.items():
        label = (
            "No fixed time" if day == "unscheduled"
            else date.fromisoformat(day).strftime("%A, %b %-d")
        )  # fmt: skip
        n_res = sum(e["reserved"] for e in items)
        st.subheader(f"{label}  ·  {len(items)} entries" + (f", {n_res} reserved" if n_res else ""))
        rows = [
            {
                "": (RES_ICON if e["reserved"] else "")
                + (FAV_ICON if e["favorite"] else "")
                + ("🕑" if e["kind"] == "personal time" else ""),
                "start": e.get("start"),
                "end": e.get("end"),
                "code": e.get("code"),
                "title": e.get("title"),
                "venue": e.get("venue"),
                "⚠️ overlaps": ", ".join(e["overlaps"]),
            }
            for e in items
        ]
        st.dataframe(
            rows,
            hide_index=True,
            use_container_width=True,
            column_config={"": st.column_config.TextColumn(width=50)},
        )


def plans_tab(signed_in: bool):
    if not signed_in:
        return st.info("Sign in to plan from your favorites and reservations.")
    sched_ = my_schedule()
    try:
        sched = sched_.load()
    except Exception as e:
        return st.error(f"Could not load your schedule: {e}")
    st.markdown(
        "**One venue per day** — a suggested plan built from your favorites and "
        "reservations. It does not change your schedule."
    )
    venue_plan(sched_, sched)


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
tab_ask, tab_search, tab_sched, tab_plans, tab_cat = st.tabs(
    ["Ask", "Search", "My schedule", "Plans", "Catalog"]
)
with tab_ask:
    ask_tab()
with tab_search:
    search_tab(signed_in)
with tab_sched:
    schedule_tab(signed_in)
with tab_plans:
    plans_tab(signed_in)
with tab_cat:
    catalog_tab(signed_in)
