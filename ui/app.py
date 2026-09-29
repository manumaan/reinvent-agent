"""re:Invent planner: local Streamlit app.

Run from the repo root:  uv run --extra ui streamlit run ui/app.py

Runs on your machine on purpose: AWS Builder ID sign-in only redirects to
localhost (ports 8484-8489), so the app signs you in itself.
"""

from __future__ import annotations

import threading
import time

import streamlit as st

from reinvent_agent import accounts
from reinvent_agent.catalog import source
from reinvent_agent.config import settings
from reinvent_agent.events_api import EventsApiClient, Session
from reinvent_agent.events_api.auth import AuthError, interactive_login, revoke
from reinvent_agent.llm import get_provider
from reinvent_agent.reservations import ReservationInfo, release_note
from reinvent_agent.schedule import MySchedule

FAV_ICON, RES_ICON, NO_RES_ICON = "⭐", "🎟️", "🚫"

st.set_page_config(page_title="re:Invent planner", page_icon="🗓️", layout="wide")
cfg = settings()
store = accounts.token_store(cfg)  # shared with the cloud run once unattended is on


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
    st.sidebar.caption(
        "Unattended reservations: "
        + ("**on**" if accounts.unattended_enabled() else "off")
        + " (Plans tab)"
    )
    if st.sidebar.button("Sign out"):
        try:
            revoke(tokens.refresh_token)
        except AuthError as e:
            st.sidebar.warning(str(e))
        store.clear()  # also removes the cloud copy when unattended is on
        if accounts.unattended_enabled():
            accounts.disable_unattended(cfg)
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
    return accounts.events_client(cfg)


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


@st.cache_resource
def reservation_info() -> ReservationInfo:
    return ReservationInfo(local_catalog().values())


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
            "res": reservation_icon(r["sessionId"], reserved),
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
            "res": st.column_config.TextColumn(
                RES_ICON, width=40, help=f"{RES_ICON} reserved · {NO_RES_ICON} no reserved seating"
            ),
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
    info = reservation_info()
    no_seating = [r for r in picked if not info.can_reserve(r["sessionId"])]
    to_reserve = [i for i in ids if i not in reserved and info.can_reserve(i)]
    to_cancel = [i for i in ids if i in reserved]
    sched = my_schedule()

    with st.container(border=True):
        if flash := st.session_state.pop("action_result", None):
            for kind, text in flash:
                getattr(st, kind)(text)
        if not ids:
            st.markdown(
                f"☑️ **Tick sessions in the table below**, then {FAV_ICON} favorite or "
                f"{RES_ICON} reserve them here.  \n{FAV_ICON} favorite · {RES_ICON} reserved · "
                f"{NO_RES_ICON} no reserved seating"
            )
            return
        codes = ", ".join(r["code"] for r in picked[:6]) + ("…" if len(picked) > 6 else "")
        st.markdown(f"**{len(ids)} selected:** {codes}")
        b1, b2, b3, b4 = st.columns(4)
        actions = [
            (b1, FAV_ICON, "Favorite", to_fav, sched.favorite, "primary"),
            (b2, "☆", "Unfavorite", to_unfav, sched.unfavorite, "secondary"),
            (b3, RES_ICON, "Reserve", to_reserve, reserve_in_viewer_tz, "primary"),
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
        if no_seating:
            names = ", ".join(r["code"] for r in no_seating)
            st.warning(
                f"{NO_RES_ICON} Some of the sessions you have selected do not have reserved "
                f"seating: {names}."
                if len(no_seating) < len(picked)
                else f"{NO_RES_ICON} None of the sessions you have selected have reserved "
                f"seating ({names})."
            )
        if to_reserve and (note := release_note(viewer_tz())):
            st.caption(f"🗓️ {note}")


def viewer_tz() -> str | None:
    """The browser's IANA timezone (e.g. Asia/Kolkata), if Streamlit knows it."""
    try:
        return st.context.timezone
    except Exception:
        return None


def reserve_in_viewer_tz(ids: list[str]):
    return my_schedule().reserve(ids, tz=viewer_tz())


def reservation_icon(session_id: str, reserved: set[str]) -> str:
    if session_id in reserved:
        return RES_ICON
    return "" if reservation_info().can_reserve(session_id) else NO_RES_ICON


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

    client = accounts.events_client(cfg)
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
    reservation_info.clear()
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

    if note := release_note(viewer_tz()):
        st.info(f"🗓️ {note}")
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
    pstore = accounts.plan_store(cfg)
    if pstore is None:
        return st.warning("Deploy ReinventAgentData to store reservation plans.")

    st.subheader(f"{RES_ICON} Reservation plan for October 6")
    if note := release_note(viewer_tz()):
        st.info(f"🗓️ {note}")
    flash()
    approved = pstore.approved()
    c1, c2, c3 = st.columns(3)
    c1.metric("Approved plan", f"v{approved.version}" if approved else "none")
    c2.metric("Sessions to reserve", len(approved.primaries) if approved else 0)
    c3.metric("Unattended run", "on" if accounts.unattended_enabled() else "off")

    with st.expander("1 · Build and approve the plan", expanded=approved is None):
        plan_editor(pstore, sched_, sched)
    if approved:
        with st.expander(f"2 · Approved plan v{approved.version}", expanded=True):
            show_plan(approved)
    with st.expander("3 · Unattended run and notifications", expanded=bool(approved)):
        unattended_section(approved)
    runs = pstore.runs(10)
    if runs:
        with st.expander(f"4 · Run history ({len(runs)})"):
            for r in runs:
                when = time.strftime("%b %d %H:%M", time.localtime(r["started_at"]))
                st.markdown(
                    f"**{when} · {r['label']}** — {r['status']}: {len(r['reserved'])} reserved, "
                    f"{len(r['failed'])} not"
                )
                for n in r["notes"]:
                    st.caption(n)
                if r["reserved"] or r["failed"]:
                    st.dataframe(
                        [{"": "✅", "code": x["code"], "title": x["title"], "how": x["how"]}
                         for x in r["reserved"]]
                        + [{"": "❌", "code": x["code"], "title": x["title"], "how": x["reason"]}
                           for x in r["failed"]],
                        hide_index=True,
                        use_container_width=True,
                    )  # fmt: skip
    with st.expander("One venue per day (suggestion only)"):
        venue_plan(sched_, sched)


def flash() -> None:
    for kind, text in st.session_state.pop("plans_flash", []):
        getattr(st, kind)(text)


def set_flash(*msgs) -> None:
    st.session_state["plans_flash"] = list(msgs)
    st.rerun()


ROLE_LABELS = {"primary": f"{RES_ICON} reserve", "backup": "↪ backup", "skip": "✖ skip"}


def plan_editor(pstore, sched_: MySchedule, sched) -> None:
    from reinvent_agent.reservation_plan import (
        PRIORITIES,
        STRATEGIES,
        ReservationPlan,
        build_plan,
    )

    st.markdown(
        "Pick what to reserve. **Reserve** rows must not overlap each other; **backup** rows "
        "are tried, in order, when an overlapping session is full or can't be reserved. "
        "**Priority** sets the order: seats go to whoever asks first."
    )
    c1, c2 = st.columns([3, 1])
    strategy = c1.radio("Start from", list(STRATEGIES), format_func=STRATEGIES.get, horizontal=True)
    draft = pstore.draft()
    if c2.button("New draft from favorites", use_container_width=True) or draft is None:
        ids = list(dict.fromkeys(sched.reserved + sched.favorites))
        draft = build_plan(
            cfg.event_id, sched_.sessions(ids), sched_.venue_of, sched.reserved, strategy
        )
        pstore.save_draft(draft)
        st.session_state.pop("plan_editor", None)

    rows = [
        {
            "action": ROLE_LABELS[i.role],
            "priority": PRIORITIES[i.priority],
            "day": i.day,
            "start": i.start,
            "end": i.end,
            "code": i.code,
            "title": i.title,
            "venue": i.venue,
            "backup for": ", ".join(draft.item(p).code for p in i.backup_for if draft.item(p)),
        }
        for i in draft.items
    ]
    edited = st.data_editor(
        rows,
        key="plan_editor",
        hide_index=True,
        use_container_width=True,
        disabled=["day", "start", "end", "code", "title", "venue", "backup for"],
        column_config={
            "action": st.column_config.SelectboxColumn(
                options=list(ROLE_LABELS.values()), required=True, width="small"
            ),
            "priority": st.column_config.SelectboxColumn(
                options=list(PRIORITIES.values()), required=True, width="small"
            ),
        },
    )
    by_label = {v: k for k, v in ROLE_LABELS.items()}
    by_prio = {v: k for k, v in PRIORITIES.items()}
    items = []
    for item, row in zip(draft.items, edited, strict=True):
        role = by_label[row["action"]]
        if role == "skip":
            continue
        item.role, item.priority = role, by_prio[row["priority"]]
        items.append(item)
    plan = ReservationPlan(event_id=cfg.event_id, items=items, strategy=draft.strategy)
    plan.link_backups()

    problems = plan.problems()
    for p in problems:
        st.error(p)
    orphans = plan.orphan_backups()
    if orphans:
        st.warning(
            "These backups don't overlap any session you reserve, so they'd never be used: "
            + ", ".join(o.code for o in orphans)
        )
    st.caption(
        f"{len(plan.primaries)} to reserve · "
        f"{sum(1 for i in plan.items if i.role == 'backup')} backups"
    )
    b1, b2 = st.columns(2)
    if b1.button("Save draft", use_container_width=True):
        pstore.save_draft(plan)
        set_flash(("success", "Draft saved."))
    if b2.button(
        f"✅ Approve plan ({len(plan.primaries)} sessions)",
        type="primary",
        disabled=bool(problems),
        use_container_width=True,
    ):
        pstore.save_draft(plan)
        v = pstore.approve(plan)
        set_flash(("success", f"Plan v{v.version} approved. The scheduled runs will use it."))


def show_plan(plan) -> None:
    from reinvent_agent.reservation_plan import PRIORITIES

    days: dict[str, list] = {}
    for p in plan.primaries:
        days.setdefault(p.day or "unscheduled", []).append(p)
    st.caption(
        f"Approved {time.strftime('%b %d %H:%M', time.localtime(plan.approved_at))}. "
        "Reserved in priority order when seats release; backups used if a session is full."
    )
    for day in sorted(days):
        st.markdown(f"**{day}**")
        st.dataframe(
            [
                {
                    "priority": PRIORITIES[p.priority],
                    "start": p.start,
                    "end": p.end,
                    "code": p.code,
                    "title": p.title,
                    "venue": p.venue,
                    "backups": ", ".join(b.code for b in plan.backups_for(p.session_id)),
                }
                for p in sorted(days[day], key=lambda p: p.start or "")
            ],
            hide_index=True,
            use_container_width=True,
        )


def unattended_section(approved) -> None:
    from reinvent_agent.reservations import SCHEDULE, format_time

    tz = viewer_tz()
    st.markdown(
        "The cloud job signs in with a copy of your Builder ID tokens (Secrets Manager) and "
        "runs at these times, even with your laptop closed:"
    )
    st.dataframe(
        [
            {"when": f"{when:%a %b %-d}, {format_time(when, tz)}", "what": label,
             "does": "reserve the approved plan" if action == "run" else "check sign-in + plan"}
            for _n, when, action, label in SCHEDULE
        ],
        hide_index=True,
        use_container_width=True,
    )  # fmt: skip
    if not cfg.reservation_function:
        return st.warning(
            "The scheduled job isn't deployed yet: run `cd infra && npx aws-cdk@2 deploy --all`."
        )
    st.caption(
        "Sign in again on the evening of Oct 5 (PDT) and re-enable: the Builder ID session "
        "has its own lifetime, so tokens from weeks earlier may no longer work."
    )
    c1, c2 = st.columns(2)
    if accounts.unattended_enabled():
        if c1.button("Refresh cloud sign-in", use_container_width=True):
            accounts.enable_unattended(cfg)
            set_flash(("success", "Cloud copy of your sign-in refreshed."))
        if c2.button("Turn off unattended run", use_container_width=True):
            accounts.disable_unattended(cfg)
            set_flash(("info", "Unattended run off; the cloud copy of your sign-in was removed."))
    elif c1.button("Enable unattended run", type="primary", use_container_width=True):
        accounts.enable_unattended(cfg)
        set_flash(("success", "Enabled. Your sign-in is now shared with the scheduled job."))

    st.markdown("**Notifications**")
    subs = accounts.subscriptions(cfg)
    for sub in subs:
        st.caption(
            f"📧 {sub['endpoint']}" + ("" if sub["confirmed"] else " — confirm the email from AWS")
        )
    e1, e2 = st.columns([3, 1])
    email = e1.text_input("Email for run results", label_visibility="collapsed",
                          placeholder="you@example.com")  # fmt: skip
    if e2.button("Subscribe", use_container_width=True, disabled="@" not in email):
        accounts.subscribe(email.strip(), cfg)
        set_flash(("success", f"Check {email} for AWS's confirmation link."))

    st.markdown("**Test now**")
    t1, t2 = st.columns(2)
    check_help = "Invokes the Lambda: signs in as you, reads your schedule, emails you."
    if t1.button("Run the cloud check now", use_container_width=True, help=check_help):
        try:
            result = accounts.invoke_cloud("preflight", "manual check", cfg)
        except Exception as e:
            set_flash(("error", f"Cloud check failed: {e}"))
        set_flash(("success" if result.get("ok") else "error", result.get("message", str(result))))
    if t2.button(f"{RES_ICON} Reserve the plan now (this computer)", use_container_width=True,
                 disabled=approved is None,
                 help="Manual fallback: runs the same logic here, once."):  # fmt: skip
        from reinvent_agent.reservation_runner import ReservationRunner

        runner = ReservationRunner(accounts.events_client(cfg), cfg.event_id)
        report = runner.run(accounts.plan_store(cfg).approved(), "manual", time.time())
        accounts.plan_store(cfg).save_run(report.to_dict())
        my_schedule().refresh()
        set_flash(("info", report.text()))


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
