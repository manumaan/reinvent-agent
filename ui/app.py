"""re:Invent planner: local Streamlit app.

Run from the repo root:  uv run --extra ui streamlit run ui/app.py

Runs on your machine on purpose: AWS Builder ID sign-in only redirects to
localhost (ports 8484-8489), so the app signs you in itself.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from datetime import date, timedelta

import streamlit as st

from reinvent_agent import accounts
from reinvent_agent.catalog import source
from reinvent_agent.config import settings
from reinvent_agent.events_api import EventsApiClient, Session
from reinvent_agent.events_api.auth import AuthError, interactive_login, revoke
from reinvent_agent.llm import get_provider
from reinvent_agent.reservations import API_OPENS, ReservationInfo, release_note
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


# --- profile and short list --------------------------------------------------------


def current_profile():
    from reinvent_agent.profile import Profile, load_local

    if "profile" not in st.session_state:
        prof = None
        with suppress(Exception):  # not signed in / not deployed: fall back to local
            pstore = accounts.plan_store(cfg)
            if pstore and (body := pstore.profile_json()):
                prof = Profile.from_json(body)
        st.session_state["profile"] = prof or load_local(cfg.event_id) or Profile()
    return st.session_state["profile"]


def profile_tab(signed_in: bool):
    from reinvent_agent.catalog import facets as fx
    from reinvent_agent.profile import EXPERIENCE, Profile, save_local

    catalog = local_catalog()
    if not catalog:
        return st.info("Download the catalog first (Catalog tab): the choices come from it.")
    sessions = list(catalog.values())
    venue_of = my_schedule().venue_of
    prof = current_profile()

    def choices(name: str) -> tuple[list, Callable]:
        counts = fx.options(sessions, name, venue_of)
        # Alphabetical (case-insensitive); days in calendar order.
        values = sorted(counts) if name == "Day" else sorted(counts, key=lambda v: str(v).lower())
        if name == "Day":
            return values, lambda v: f"{date.fromisoformat(v):%a %b %-d} ({counts[v]})"
        return values, lambda v: f"{v} ({counts[v]})"

    def multi(label: str, facet: str, current: list, help: str | None = None) -> list:
        values, fmt = choices(facet)
        return st.multiselect(
            label,
            values,
            default=[v for v in current if v in values],
            format_func=fmt,
            placeholder="Any",
            help=help,
        )

    st.subheader("👤 Your profile")
    st.caption(
        f"Answer as many as you like ({prof.answered()} of 15 answered). Choices come from "
        "the session catalog, with session counts. The **Short list** tab picks sessions "
        "from this profile."
    )
    with st.form("profile_form"):
        c1, c2 = st.columns(2)
        with c1:
            roles = multi("1 · My current role", "Role", prof.roles)
            industries = multi("2 · My industry", "Industry", prof.industries)
            topics = multi("3 · Topics I'm interested in", "Topics", prof.topics)
            tech_stack = st.text_area(
                "4 · Technologies, platforms and programming languages I use",
                prof.tech_stack,
                placeholder="e.g. Python, TypeScript, Kubernetes, Postgres, Kafka, Terraform",
            )
            types = multi("5 · Session types I want", "Type", prof.session_types,
                          help="Only these types are short-listed.")  # fmt: skip
            using = multi(
                "6 · AWS services I use", "Services", prof.aws_using,
                help="Short-listed at depth: level 300-400 sessions.",
            )  # fmt: skip
            learning = multi(
                "7 · AWS services I want to learn", "Services", prof.aws_learning,
                help="Short-listed as introductions: level 100-200 sessions.",
            )  # fmt: skip
            days = multi("8 · Days I'm attending", "Day", prof.days,
                         help="Only these days are short-listed.")  # fmt: skip
        with c2:
            architecture = st.text_area(
                "9 · Architecture areas relevant to my work",
                prof.architecture,
                placeholder="e.g. event-driven systems, multi-region resilience, data mesh",
            )
            exp_keys = [None, *EXPERIENCE]
            experience = st.radio(
                "10 · My level of experience",
                exp_keys,
                index=exp_keys.index(prof.experience) if prof.experience in exp_keys else 0,
                format_func=lambda k: "Not saying" if k is None else EXPERIENCE[k],
            )
            projects = st.text_area(
                "11 · Projects or problems I'm working on",
                prof.projects,
                placeholder="e.g. migrating a monolith to microservices; cutting LLM costs",
            )
            learn_other = st.text_area(
                "12 · Other technologies I want to learn",
                prof.learn_other,
                placeholder="e.g. agent frameworks, Rust, vector databases",
            )
            interests = multi("13 · Strategic interests", "Interests", prof.interests)
            irrelevant = multi(
                "14 · Topics that are probably irrelevant to me", "Topics",
                prof.irrelevant_topics,
            )  # fmt: skip
            constraints = st.text_area(
                "15 · Preferences or constraints for the selection",
                prof.constraints,
                placeholder="e.g. no sessions before 10am; prefer hands-on; avoid sponsored talks",
                help="Read by the ✨ Refine with Claude step on the Short list tab.",
            )
        saved = st.form_submit_button("Save profile", type="primary")
    if saved:
        new = Profile(
            roles, industries, topics, tech_stack.strip(), types, using, learning, days,
            architecture.strip(), experience, projects.strip(), learn_other.strip(),
            interests, irrelevant, constraints.strip(),
        )  # fmt: skip
        save_local(new, cfg.event_id)
        where = "on this computer"
        with suppress(Exception):
            if pstore := accounts.plan_store(cfg):
                pstore.save_profile_json(new.to_json())
                where = "on this computer and in your AWS account"
        st.session_state["profile"] = new
        st.session_state.pop("shortlist_refined", None)
        st.success(f"Profile saved {where} ({new.answered()} of 15 answered). "
                   "Open the Short list tab.")  # fmt: skip


def shortlist_tab(signed_in: bool):
    import re

    from reinvent_agent.catalog.search import SearchFilters
    from reinvent_agent.shortlist import refine_with_claude, score_sessions, semantic_queries

    catalog = local_catalog()
    prof = current_profile()
    if not catalog:
        return st.info("Download the catalog first (Catalog tab).")
    if not prof.answered():
        return st.info("Fill in your profile on the 👤 Profile tab first.")
    venue_of = my_schedule().venue_of
    favorites: set[str] = set()
    if signed_in:
        with suppress(Exception):
            favorites = set(my_schedule().load().favorites)

    # Free-text answers -> semantic search (cached per profile).
    key = prof.to_json()
    sem = st.session_state.get("shortlist_semantic")
    if not sem or sem[0] != key:
        ranked = {}
        if (search := search_backend()) and (queries := semantic_queries(prof)):
            with st.spinner("Matching your free-text answers against the catalog…"):
                for f, text in queries.items():
                    hits = search.search(text, SearchFilters(event_id=cfg.event_id), k=50)
                    ranked[f] = [h.session_id for h in hits]
        sem = (key, ranked)
        st.session_state["shortlist_semantic"] = sem
    picks = score_sessions(prof, catalog.values(), sem[1])

    # One card per talk: repeats (-R, -R1, ...) fold into the best-scoring instance.
    base = {}
    for p in picks:
        base.setdefault(re.sub(r"-R\d*$", "", p.session.code), p)
    picks = list(base.values())

    c1, c2, c3 = st.columns([2, 2, 2])
    limit = c1.slider("How many", 6, 60, 24, step=6)
    order = c2.radio("Sort", ["Best match", "Day & time"], horizontal=True)
    refined = st.session_state.get("shortlist_refined")
    if refined and refined[0] != (key, limit):
        refined = None
    if c3.button("✨ Refine with Claude", use_container_width=True,
                 help="Claude re-ranks the top candidates against your whole profile, "
                 "including preferences and constraints."):  # fmt: skip
        from reinvent_agent.llm import make_client

        try:
            client = make_client(cfg.region, cfg.llm_provider, cfg.anthropic_key_secret_arn)
            with st.spinner("Claude is reading your profile and the candidates…"):
                chosen = refine_with_claude(
                    client, cfg.model, prof, picks[: max(60, 2 * limit)], venue_of, limit
                )
            refined = ((key, limit), chosen)
            st.session_state["shortlist_refined"] = refined
        except Exception as e:
            st.error(f"Claude refinement failed: {e}")
    if refined:
        by_code = {p.session.code: p for p in picks}
        shown = []
        for code, reason in refined[1]:
            if p := by_code.get(code):
                p.reasons.insert(0, f"✨ {reason}")
                shown.append(p)
        st.caption(f"✨ Chosen by Claude from your top {min(len(picks), max(60, 2 * limit))} "
                   f"candidates. {len(picks)} sessions matched your profile.")  # fmt: skip
    else:
        shown = picks[:limit]
        st.caption(f"Top {len(shown)} of {len(picks)} sessions that match your profile.")
    if not shown:
        return st.warning("Nothing matches yet: add topics, services or interests.")
    if order == "Day & time":
        from reinvent_agent.shortlist import s_key

        shown = sorted(shown, key=lambda p: s_key(p.session))

    cols = st.columns(3)
    for i, p in enumerate(shown):
        with cols[i % 3]:
            session_card(p, venue_of, p.session.session_id in favorites, signed_in)


def session_card(p, venue_of, is_fav: bool, signed_in: bool) -> None:
    s = p.session
    with st.container(border=True):
        st.markdown(f"**{s.code}** {FAV_ICON if is_fav else ''}  \n**{s.title}**")
        when = (
            f"{s.day:%a %b %-d} · {s.start:%H:%M}–{s.end:%H:%M}" if s.start and s.end
            else "No fixed time"
        )  # fmt: skip
        st.caption(
            f"{s.type or 'Session'} · L{s.level_number or '—'}  \n{when}  \n📍 {venue_of(s) or '—'}"
        )
        b1, b2 = st.columns(2)
        if b1.button("Details", key=f"card_{s.session_id}", use_container_width=True):
            session_dialog(s, venue_of, is_fav, signed_in)
        if signed_in:
            fav_button(b2, s, is_fav, key=f"cardfav_{s.session_id}")


def fav_button(where, s, is_fav: bool, key: str) -> None:
    label = "☆ Unfavorite" if is_fav else f"{FAV_ICON} Favorite"
    if where.button(label, key=key, use_container_width=True):
        try:
            (my_schedule().unfavorite if is_fav else my_schedule().favorite)([s.session_id])
        except Exception as e:
            st.error(f"Could not update favorites: {e}")
            return
        st.rerun()


@st.dialog("Session details", width="large")
def session_dialog(s, venue_of, is_fav: bool, signed_in: bool) -> None:
    st.markdown(f"### {s.code} · {s.title}")
    when = (
        f"{s.day:%A, %b %-d} · {s.start:%H:%M}–{s.end:%H:%M}" if s.start and s.end
        else "No fixed time"
    )  # fmt: skip
    st.markdown(
        f"**{s.type or 'Session'}** · Level {s.level or '—'}  \n{when}  \n"
        f"📍 {venue_of(s) or '—'}{f' · {s.room}' if s.room else ''}"
    )
    st.write(s.abstract or "_No abstract._")
    details = {
        "Speakers": ", ".join(s.speaker_names),
        "Topics": ", ".join(s.topics),
        "AWS services": ", ".join(s.services),
        "Interests": ", ".join(s.areas_of_interest),
        "Roles": ", ".join(s.roles),
        "Industries": ", ".join(s.industries),
        "Format": ", ".join(s.features),
    }
    st.markdown("\n".join(f"**{k}:** {v}  " for k, v in details.items() if v))
    if signed_in:
        fav_button(st, s, is_fav, key=f"dlgfav_{s.session_id}")


def search_tab(signed_in: bool):
    from reinvent_agent.catalog import facets as fx
    from reinvent_agent.catalog.search import SearchFilters

    catalog = local_catalog()
    if not catalog:
        return st.info("No catalog yet: download it from the Catalog tab.")
    sched = None
    if signed_in:
        try:
            sched = my_schedule().load()
        except Exception as e:
            st.warning(f"Could not load your schedule: {e}")
    favorites = set(sched.favorites) if sched else set()
    reserved = set(sched.reserved) if sched else set()
    venue_of = my_schedule().venue_of

    left, right = st.columns([1, 3], gap="medium")
    with left:
        filters, group = filter_panel(catalog, venue_of, favorites, reserved, signed_in)
    with right:
        q = st.text_input(
            "Search sessions",
            placeholder="serverless event-driven architecture (leave empty to browse all)",
        )
        if q:
            search = search_backend()
            if search is None:
                return not_deployed()
            # Selecting rows reruns the script; keep results so we don't re-embed.
            cached = st.session_state.get("search_results")
            if not cached or cached[0] != q:
                hits = search.search(q, SearchFilters(event_id=cfg.event_id), k=100)
                cached = (q, [h.session_id for h in hits])
                st.session_state["search_results"] = cached
            ranked = [catalog[i] for i in cached[1] if i in catalog]
            sessions = fx.apply(ranked, filters, venue_of)
            st.caption(
                f"{len(sessions)} of the 100 best matches for “{q}”"
                + (f" pass {filters.active} filter(s)." if filters.active else ".")
            )
        else:
            ordered = sorted(
                catalog.values(), key=lambda s: (s.start is None, s.start or 0, s.code)
            )
            sessions = fx.apply(ordered, filters, venue_of)
            st.caption(f"{len(sessions)} of {len(catalog)} sessions.")
        if group:
            by = fx.GROUPS[group]
            sessions = sorted(sessions, key=lambda s: by(s, venue_of))  # stable: keeps order
            counts = Counter(by(s, venue_of) for s in sessions)
            st.caption(
                f"Grouped by {group.lower()}: "
                + " · ".join(f"{g} ({n})" for g, n in sorted(counts.items()))
            )
        key = (q, filters.key(), group)
        results = [session_row(s, venue_of) for s in sessions]
        cols = ("code", "title", "type", "level", "weekday", "day", "start", "end", "venue")
        rows = [
            {
                **({group.lower(): fx.GROUPS[group](s, venue_of)} if group else {}),
                "fav": FAV_ICON if r["sessionId"] in favorites else "",
                "res": reservation_icon(r["sessionId"], reserved),
                **{c: r.get(c) for c in cols},
            }
            for s, r in zip(sessions, results, strict=True)
        ]
        results_table(rows, results, key, favorites, reserved, signed_in)


def session_row(s: Session, venue_of) -> dict:
    return {
        "sessionId": s.session_id,
        "code": s.code,
        "title": s.title,
        "type": s.type,
        "level": s.level_number,
        "weekday": s.day.strftime("%A") if s.day else None,
        "day": s.day.isoformat() if s.day else None,
        "start": s.start.strftime("%H:%M") if s.start else None,
        "end": s.end.strftime("%H:%M") if s.end else None,
        "venue": venue_of(s),
    }


FACET_ORDER = ["Level", "Type", "Track", "Day", "Time", "Venue", "Speakers", "Topics",
               "Services", "Interests", "Role", "Industry", "Features"]  # fmt: skip


def filter_panel(catalog, venue_of, favorites, reserved, signed_in):
    """Left-hand filter panel (any-of within a facet, all-of across facets)."""
    from datetime import time as dtime

    from reinvent_agent.catalog import facets as fx

    sessions = list(catalog.values())
    state = st.session_state
    full_day = (dtime(7, 0), dtime(20, 0))

    def is_set(k: str) -> bool:
        value = state.get(f"fx_{k}")
        return tuple(value) != full_day if k == "Time" and value else bool(value)

    active = sum(is_set(k) for k in ["code", "title", "abstract", *FACET_ORDER])
    active += state.get("fx_mine", "all") != "all"
    head, clear = st.columns([3, 2])
    head.markdown("#### 🔽 Filter" + (f" · {active}" if active else ""))
    if clear.button("Clear", disabled=not active, use_container_width=True):
        for k in [k for k in state if str(k).startswith("fx_")]:
            del state[k]
        st.rerun()

    code = st.text_input("Id", key="fx_code", placeholder="Id…", label_visibility="collapsed")
    title = st.text_input(
        "Title", key="fx_title", placeholder="Title…", label_visibility="collapsed"
    )
    abstract = st.text_input(
        "Abstract", key="fx_abstract", placeholder="Abstract…", label_visibility="collapsed"
    )
    group = st.selectbox(
        "Group",
        [None, *fx.GROUPS],
        key="fx_group",
        format_func=lambda g: "Group by…" if g is None else f"Group: {g}",
        label_visibility="collapsed",
    )

    from reinvent_agent.catalog.concurrency import track_labels

    labels = track_labels(sessions)
    selected: dict[str, list] = {}
    start_from = start_to = None
    for name in FACET_ORDER:
        with st.expander(name, expanded=is_set(name)):
            if name == "Time":
                lo, hi = st.slider(
                    "Starts between",
                    value=full_day,
                    step=timedelta(minutes=15),
                    format="HH:mm",
                    key="fx_Time",
                )
                if (lo, hi) != full_day:
                    start_from, start_to = lo, hi
                continue
            counts = fx.options(sessions, name, venue_of)
            if name in ("Level", "Day"):
                values = sorted(counts)
            else:
                values = sorted(counts, key=lambda v: (-counts[v], str(v)))

            def fmt(v, name=name, counts=counts):
                text = (
                    date.fromisoformat(v).strftime("%a %b %-d") if name == "Day"
                    else labels.get(v, v) if name == "Track" else str(v)
                )  # fmt: skip
                return f"{text} ({counts[v]})"

            selected[name] = st.multiselect(
                name,
                values,
                key=f"fx_{name}",
                format_func=fmt,
                placeholder="Any",
                label_visibility="collapsed",
            )

    only_ids = None
    if signed_in:
        mine = state.get("fx_mine", "all")
        with st.expander(f"{FAV_ICON} Favorites", expanded=mine != "all"):
            choice = st.radio(
                "Show",
                ["all", "fav", "res"],
                key="fx_mine",
                format_func={
                    "all": "All sessions",
                    "fav": f"{FAV_ICON} My favorites ({len(favorites)})",
                    "res": f"{RES_ICON} My reservations ({len(reserved)})",
                }.get,
                label_visibility="collapsed",
            )
            only_ids = {"fav": favorites, "res": reserved}.get(choice)
    return (
        fx.Filters(code, title, abstract, selected, start_from, start_to, only_ids),
        group,
    )


def results_table(rows, results, key, favorites, reserved, signed_in) -> None:
    if not signed_in:
        st.dataframe(
            rows,
            hide_index=True,
            use_container_width=True,
            column_config=icon_columns(),
        )
        return st.caption("Sign in to favorite or reserve sessions from here.")

    # Actions sit ABOVE the table so they are visible right after ticking rows. The
    # ✓ column lives in an editable table, so "select all" can tick every box: it sets
    # the column's default and swaps in a fresh table (nonce), which also clears ticks
    # after each action.
    base = f"search_table_{hash(key)}"
    all_flag = st.session_state.get(f"{base}_selectall", False)
    table_key = f"{base}_{st.session_state.get('table_nonce', 0)}"
    selected = {i for i in range(len(rows)) if all_flag}
    for i, change in (st.session_state.get(table_key) or {}).get("edited_rows", {}).items():
        if "✓" in change:
            (selected.add if change["✓"] else selected.discard)(int(i))

    a1, a2, _ = st.columns([1, 1, 1])
    if a1.button(f"☑️ Select all ({len(rows)})", disabled=not rows, use_container_width=True):
        reset_table(base, select_all=True)
    if a2.button("☐ Unselect all", disabled=not selected, use_container_width=True):
        reset_table(base, select_all=False)
    session_actions([results[i] for i in sorted(selected)], favorites, reserved)
    st.data_editor(
        [{"✓": all_flag, **r} for r in rows],
        hide_index=True,
        use_container_width=True,
        disabled=[c for c in rows[0] if c != "✓"] if rows else True,
        column_config={"✓": st.column_config.CheckboxColumn("✓", width=40), **icon_columns()},
        key=table_key,
    )


def icon_columns() -> dict:
    return {
        "fav": st.column_config.TextColumn(FAV_ICON, width=40, help="Favorited"),
        "res": st.column_config.TextColumn(
            RES_ICON, width=40, help=f"{RES_ICON} reserved · {NO_RES_ICON} no reserved seating"
        ),
    }


def reset_table(base: str | None = None, select_all: bool = False) -> None:
    """Fresh search table: every row ticked (select all) or none."""
    for k in [k for k in st.session_state if str(k).endswith("_selectall")]:
        del st.session_state[k]
    if base and select_all:
        st.session_state[f"{base}_selectall"] = True
    st.session_state["table_nonce"] = st.session_state.get("table_nonce", 0) + 1
    st.rerun()


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
    reset_table()


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


BAR_BLUE = "#2a78d6"  # sequential blue, step 450
HEAT_RANGE = ["#cde2fb", "#104281"]  # sequential blue, steps 100 -> 650


def venues_tab(signed_in: bool):
    """What runs in parallel at each venue: sessions, tracks, and overlaps."""
    import altair as alt
    import pandas as pd

    from reinvent_agent.catalog import concurrency as cc

    catalog = local_catalog()
    if not catalog:
        return st.info("No catalog yet: download it from the Catalog tab.")
    sched_ = my_schedule()
    favorites: set[str] = set()
    if signed_in:
        with suppress(Exception):  # schedule unavailable: show the catalog without stars
            favorites = set(sched_.load().favorites)
    st.caption(
        "A **track** is the session-code prefix (AIM, SEC, DAT, …): the catalog's own "
        "tracks field is empty. Labels show each track's most distinctive topic."
    )
    only_favs = signed_in and st.toggle(
        f"Only my {FAV_ICON} favorites", help="Count only sessions you favorited"
    )
    sessions = [s for s in catalog.values() if not only_favs or s.session_id in favorites]
    labels = cc.track_labels(catalog.values())
    venue_of = sched_.venue_of

    rows = cc.overview(sessions, venue_of)
    st.subheader("Busiest moment per venue and day")
    st.dataframe(
        [
            {
                "day": date.fromisoformat(r.day).strftime("%a %b %-d"),
                "venue": r.venue,
                "sessions": r.sessions,
                "rooms": r.rooms,
                "tracks that day": r.tracks,
                "peak: sessions at once": r.peak_sessions,
                "peak: tracks at once": r.peak_tracks,
                "peak at": r.peak_at,
            }
            for r in rows
        ],
        hide_index=True,
        use_container_width=True,
    )

    st.subheader("Through the day")
    days = sorted({r.day for r in rows})
    c1, c2 = st.columns(2)
    day = c1.selectbox(
        "Day", days, format_func=lambda d: date.fromisoformat(d).strftime("%A, %b %-d")
    )
    venues = sorted({r.venue for r in rows if r.day == day})
    venue = c2.selectbox("Venue", venues)
    slots = cc.venue_day_slots(sessions, venue_of, day, venue)
    if not slots:
        return st.info("Nothing scheduled there that day.")

    def breakdown(counter) -> str:
        return ", ".join(f"{t}×{n}" for t, n in counter.most_common())

    bars = pd.DataFrame(
        [
            {
                "slot": sl.start,
                "sessions": len(sl.sessions),
                "tracks": len(sl.tracks),
                "by track": breakdown(sl.tracks),
            }
            for sl in slots
        ]
    )
    st.altair_chart(
        alt.Chart(bars, title="Sessions running in each 30-minute slot")
        .mark_bar(color=BAR_BLUE, cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
        .encode(
            x=alt.X("slot:O", title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("sessions:Q", title="sessions at once"),
            tooltip=[
                alt.Tooltip("slot", title="from"),
                alt.Tooltip("sessions", title="sessions"),
                alt.Tooltip("tracks", title="tracks"),
                alt.Tooltip("by track", title="by track"),
            ],
        )
        .properties(height=220),
        use_container_width=True,
    )

    cells = pd.DataFrame(
        [
            {
                "track": labels.get(t, t),
                "slot": sl.start,
                "sessions": n,
                "codes": ", ".join(x.code for x in sl.sessions if cc.track_of(x) == t),
            }
            for sl in slots
            for t, n in sl.tracks.items()
        ]
    )
    order = cells.groupby("track")["sessions"].sum().sort_values(ascending=False).index.tolist()
    st.altair_chart(
        alt.Chart(cells, title="Which tracks run at the same time")
        .mark_rect(cornerRadius=2, stroke="white", strokeWidth=1)
        .encode(
            x=alt.X("slot:O", title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y(
                "track:N",
                sort=order,
                title=None,
                axis=alt.Axis(labelLimit=260, labelOverlap=False),
            ),
            color=alt.Color(
                "sessions:Q",
                scale=alt.Scale(range=HEAT_RANGE),
                legend=alt.Legend(title="sessions"),
            ),
            tooltip=["track", alt.Tooltip("slot", title="from"), "sessions", "codes"],
        )
        .properties(height=max(160, 28 * len(order))),
        use_container_width=True,
    )

    with st.expander("Slot table"):
        st.dataframe(
            [
                {
                    "from": sl.start,
                    "to": sl.end,
                    "sessions": len(sl.sessions),
                    "tracks": len(sl.tracks),
                    "by track": breakdown(sl.tracks),
                    f"my {FAV_ICON}": ", ".join(
                        x.code for x in sl.sessions if x.session_id in favorites
                    ),
                }
                for sl in slots
            ],
            hide_index=True,
            use_container_width=True,
        )

    st.subheader("What overlaps a session")
    by_code = {s.code: s for s in sorted(catalog.values(), key=lambda s: s.code) if s.start}
    code = st.selectbox(
        "Session",
        list(by_code),
        index=None,
        placeholder="Type a code or pick one, e.g. SEC341",
        format_func=lambda c: f"{c} — {by_code[c].title[:70]}",
    )
    if code:
        target = by_code[code]
        others = cc.overlapping(target, catalog.values(), venue_of)
        tv = venue_of(target)
        same = sum(1 for _s, v in others if v == tv)
        st.markdown(
            f"**{target.code}** · {target.start:%a %b %-d %H:%M}–{target.end:%H:%M} · {tv}: "
            f"**{len(others)}** sessions overlap it, **{same}** at the same venue."
        )
        st.dataframe(
            [
                {
                    "": FAV_ICON if s.session_id in favorites else "",
                    "start": f"{s.start:%H:%M}",
                    "end": f"{s.end:%H:%M}",
                    "code": s.code,
                    "track": labels.get(cc.track_of(s), cc.track_of(s)),
                    "title": s.title,
                    "venue": v,
                    "room": s.room,
                }
                for s, v in others
            ],
            hide_index=True,
            use_container_width=True,
            column_config={"": st.column_config.TextColumn(width=40)},
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

    st.subheader(f"{RES_ICON} Reservation plan")
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
    with st.expander(
        "📝 Oct 6: reserve by hand in the re:Invent portal (checklist)",
        expanded=time.time() < API_OPENS.timestamp(),
    ):
        portal_checklist(approved or pstore.draft())
    with st.expander("3 · Unattended run (Oct 8) and notifications", expanded=bool(approved)):
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
    draft = pstore.draft()
    options = list(STRATEGIES)
    current = options.index(draft.strategy) if draft and draft.strategy in options else 0
    strategy = c1.radio(
        "Start from", options, index=current, format_func=STRATEGIES.get, horizontal=True
    )
    # Switching the strategy rebuilds the draft from favorites (edits are replaced).
    rebuild = c2.button("Rebuild from favorites", use_container_width=True)
    if rebuild or draft is None or draft.strategy != strategy:
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


def portal_checklist(plan) -> None:
    """The plan in reservation order, to work through by hand in the portal on Oct 6."""
    from reinvent_agent.reservation_plan import PRIORITIES
    from reinvent_agent.reservations import RELEASES, format_time

    if plan is None:
        return st.info("Build a plan first (section 1).")
    tz = viewer_tz()
    st.markdown(
        "On **October 6** seats can only be reserved **by hand in the re:Invent portal** "
        f"(first half at {format_time(RELEASES[0][1], tz)}, second half at "
        f"{format_time(RELEASES[1][1], tz)}). The API, and so this app's Reserve and the "
        "unattended run, opens on **October 8**. Work down this list in order: search the "
        "portal by code; if a session is full, try its backups."
    )
    rows = [
        {
            "#": n,
            "priority": PRIORITIES[p.priority],
            "code": p.code,
            "title": p.title,
            "day": p.day,
            "time": f"{p.start}-{p.end}" if p.start else "",
            "venue": p.venue,
            "backups (if full)": ", ".join(b.code for b in plan.backups_for(p.session_id)),
        }
        for n, p in enumerate(plan.primaries, 1)
    ]
    st.dataframe(rows, hide_index=True, use_container_width=True)
    import csv
    import io

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0]) if rows else ["#"])
    writer.writeheader()
    writer.writerows(rows)
    st.download_button(
        "Download checklist (CSV)", buf.getvalue(), "reinvent-portal-checklist.csv", "text/csv"
    )
    st.caption(
        "Whatever you reserve in the portal shows up in your schedule; the Oct 8 run skips "
        "it and only reserves what is still missing."
    )


def unattended_section(approved) -> None:
    from reinvent_agent.reservations import SCHEDULE

    tz = viewer_tz()
    st.markdown(
        "The cloud job signs in with a copy of your Builder ID tokens (Secrets Manager) and "
        "runs at these times, even with your laptop closed. On Oct 8 it checks every 2 "
        "minutes, stays silent while the API is closed, and reserves your approved plan "
        "(minus anything you already hold) as soon as it opens, then emails you."
    )
    st.dataframe(
        [
            {"when": job.describe(tz), "what": job.label,
             "does": "reserve the approved plan once the API opens" if job.action == "poll"
             else "check sign-in + plan, email you"}
            for job in SCHEDULE
        ],
        hide_index=True,
        use_container_width=True,
    )  # fmt: skip
    if not cfg.reservation_function:
        return st.warning(
            "The scheduled job isn't deployed yet: run `cd infra && npx aws-cdk@2 deploy --all`."
        )
    st.caption(
        "Sign in again on the evening of Oct 7 (PDT) and press Refresh cloud sign-in: the "
        "Builder ID session has its own lifetime, so older tokens may no longer work."
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
    t1, t2, t3 = st.columns(3)
    check_help = "Invokes the Lambda: signs in as you, reads your schedule, emails you."
    if t1.button("Run the cloud check now", use_container_width=True, help=check_help):
        try:
            result = accounts.invoke_cloud("preflight", "manual check", cfg)
        except Exception as e:
            set_flash(("error", f"Cloud check failed: {e}"))
        set_flash(("success" if result.get("ok") else "error", result.get("message", str(result))))
    run_help = (
        "Invokes the exact Oct 8 code in the Lambda, once, without waiting. Before the API "
        "opens it gets 'closed' and emails a 'closed' report (nothing reserved). Once the "
        "API is open it reserves your approved plan for real."
    )
    if t2.button("Test the cloud reserve run", use_container_width=True,
                 disabled=approved is None, help=run_help):  # fmt: skip
        try:
            result = accounts.invoke_cloud("run", "cloud test", cfg)
        except Exception as e:
            set_flash(("error", f"Cloud run failed: {e}"))
        set_flash(("info", f"Cloud run finished: {result.get('status')}. See Run history."))
    if t3.button(f"{RES_ICON} Reserve the plan now (this computer)", use_container_width=True,
                 disabled=approved is None,
                 help="Manual fallback from Oct 8: runs the same logic here, once."):  # fmt: skip
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
tab_prof, tab_short, tab_ask, tab_search, tab_sched, tab_plans, tab_venues, tab_cat = st.tabs(
    ["👤 Profile", "⭐ Short list", "Ask", "Search", "My schedule", "Plans", "Venues", "Catalog"]
)
with tab_prof:
    profile_tab(signed_in)
with tab_short:
    shortlist_tab(signed_in)
with tab_ask:
    ask_tab()
with tab_search:
    search_tab(signed_in)
with tab_sched:
    schedule_tab(signed_in)
with tab_plans:
    plans_tab(signed_in)
with tab_venues:
    venues_tab(signed_in)
with tab_cat:
    catalog_tab(signed_in)
