"""`reinvent-agent` command line."""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from reinvent_agent.events_api import EventsApiClient, FileTokenStore
from reinvent_agent.events_api.auth import (
    AuthError,
    interactive_login,
    revoke,
)

DEFAULT_EVENT = os.environ.get("REINVENT_EVENT_ID", "reinvent2026")

app = typer.Typer(no_args_is_help=True, help="re:Invent planner agent")
auth_app = typer.Typer(no_args_is_help=True, help="AWS Builder ID sign-in")
catalog_app = typer.Typer(no_args_is_help=True, help="Read the session catalog")
app.add_typer(auth_app, name="auth")
app.add_typer(catalog_app, name="catalog")
config_app = typer.Typer(no_args_is_help=True, help="Deployment settings and secrets")
app.add_typer(config_app, name="config")


@config_app.command("set-api-key")
def config_set_api_key():
    """Store a Claude API key (platform.claude.com) in the AnthropicApiKey secret.

    Prompts without echo, so the key stays out of shell history and logs.
    """
    import boto3

    from reinvent_agent.config import clear_caches, settings

    cfg = settings()
    if not cfg.anthropic_key_secret_arn:
        typer.echo("No AnthropicApiKey secret found; deploy ReinventAgentData first.")
        raise typer.Exit(1)
    key = typer.prompt("Claude API key", hide_input=True).strip()
    if not key.startswith("sk-ant-"):
        typer.echo("That doesn't look like a Claude API key (expected sk-ant-...).")
        raise typer.Exit(1)
    boto3.client("secretsmanager", region_name=cfg.region).put_secret_value(
        SecretId=cfg.anthropic_key_secret_arn, SecretString=key
    )
    clear_caches()
    typer.echo("Stored. Claude calls now use the Claude API (unset with `config clear-api-key`).")


@config_app.command("clear-api-key")
def config_clear_api_key():
    """Reset the secret to its placeholder, switching Claude calls back to Bedrock."""
    import boto3

    from reinvent_agent.config import API_KEY_PLACEHOLDER, settings

    cfg = settings()
    if not cfg.anthropic_key_secret_arn:
        typer.echo("No AnthropicApiKey secret found.")
        raise typer.Exit(1)
    boto3.client("secretsmanager", region_name=cfg.region).put_secret_value(
        SecretId=cfg.anthropic_key_secret_arn, SecretString=API_KEY_PLACEHOLDER
    )
    typer.echo("Cleared. Claude calls use Bedrock again.")


@config_app.command("set-provider")
def config_set_provider(
    provider: str = typer.Argument(..., help="bedrock, anthropic, or auto"),
    model: str | None = typer.Option(None, help="Bare model ID, e.g. claude-opus-4-7"),
):
    """Choose the Claude endpoint (saved locally; REINVENT_LLM_PROVIDER still wins).

    `auto` uses the Claude API when an API key is stored, else Bedrock.
    """
    from reinvent_agent.config import load_preferences, save_preferences
    from reinvent_agent.llm import get_provider

    if provider != "auto":
        get_provider(provider)
    prefs = load_preferences() | {"provider": provider, "model": model}
    path = save_preferences(prefs)
    typer.echo(f"Saved to {path}.")
    config_show()


@config_app.command("show")
def config_show():
    """Print resolved settings (never the API key itself)."""
    from reinvent_agent.config import settings

    for k, v in vars(settings()).items():
        typer.echo(f"{k}: {v}")


def _client() -> EventsApiClient:
    from reinvent_agent.accounts import token_provider

    return EventsApiClient(token_provider())


@auth_app.command("login")
def auth_login(no_browser: bool = typer.Option(False, help="Print the URL instead of opening it")):
    """Sign in with AWS Builder ID (runs a callback server on localhost:8484)."""
    from reinvent_agent.accounts import token_store

    # With unattended reservations on, this also refreshes the cloud copy.
    tokens = interactive_login(token_store(), open_browser=not no_browser)
    typer.echo(f"Signed in. Access token valid until epoch {int(tokens.expires_at)}.")


@auth_app.command("status")
def auth_status():
    from reinvent_agent.accounts import token_store, unattended_enabled

    tokens = token_store().load()
    if tokens is None:
        typer.echo("Not signed in.")
        raise typer.Exit(1)
    state = "expired (will refresh)" if tokens.is_expired() else "valid"
    where = "Secrets Manager + local file" if unattended_enabled() else str(FileTokenStore().path)
    typer.echo(f"Access token {state}; tokens stored in {where}")


@auth_app.command("push-secret")
def auth_push_secret():
    """Enable unattended reservations: copy tokens to Secrets Manager and share them.

    From then on this machine and the cloud run use the same stored tokens, so a
    refresh-token rotation on one side never strands the other.
    """
    from reinvent_agent.accounts import enable_unattended

    try:
        enable_unattended()
    except RuntimeError as e:
        typer.echo(str(e))
        raise typer.Exit(1) from None
    typer.echo("Tokens stored in Secrets Manager; unattended reservations enabled.")


@auth_app.command("logout")
def auth_logout():
    """Revoke the refresh token and delete local tokens.

    Tokens already pushed with `push-secret` are revoked too, since they share the
    refresh token. To also end the Builder ID browser session, sign out at
    https://profile.aws.amazon.com.
    """
    from reinvent_agent.accounts import token_store

    store = token_store()
    tokens = store.load()
    if tokens is not None:
        try:
            revoke(tokens.refresh_token)
        except AuthError as e:
            typer.echo(f"Warning: {e}")
    store.clear()
    typer.echo(
        "Signed out of reinvent-agent. End the Builder ID session at "
        "https://profile.aws.amazon.com if you want that too."
    )


@catalog_app.command("events")
def catalog_events(include_past: bool = typer.Option(False, "--include-past")):
    for e in EventsApiClient().list_events(include_past=include_past):
        typer.echo(f"{e.event_id}\t{e.name}")


@catalog_app.command("dump")
def catalog_dump(
    event_id: str = typer.Option(DEFAULT_EVENT, "--event"),
    out: Path | None = typer.Option(None, "--out", help="Default: fixtures/<event>/catalog.jsonl"),
    upload: bool = typer.Option(True, help="Also copy to the deployed catalog bucket"),
):
    """Walk ListSessions (needs `auth login`) and write one JSON session per line."""
    from reinvent_agent.catalog import source
    from reinvent_agent.config import settings

    items, total = source.download_catalog(_client(), event_id)
    body = source.to_jsonl(items)
    out = out or source.local_path(event_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():  # sessions withdrawn since keep their name in the archive
        source.update_archive(event_id, source.load_sessions(out))
    out.write_text(body)
    source.update_archive(event_id, source.load_sessions(out))
    typer.echo(f"Wrote {len(items)} sessions to {out} (API totalCount: {total})")
    if total is not None and len(items) != total:
        typer.echo("WARNING: session count differs from totalCount; run `catalog probe`.")
    bucket = settings().catalog_bucket
    if upload and bucket:
        typer.echo(f"Uploaded to {source.upload(body, bucket, event_id)}")


@catalog_app.command("upload")
def catalog_upload(
    file: Path | None = typer.Argument(None, help="Default: fixtures/<event>/catalog.jsonl"),
    event_id: str = typer.Option(DEFAULT_EVENT, "--event"),
):
    """Copy a local catalog JSONL to the deployed catalog bucket (validates it first)."""
    from reinvent_agent.catalog import source
    from reinvent_agent.config import settings

    bucket = settings().catalog_bucket
    if not bucket:
        typer.echo("No catalog bucket found; deploy ReinventAgentData first.")
        raise typer.Exit(1)
    file = file or source.local_path(event_id)
    sessions = source.load_sessions(file)
    typer.echo(f"{len(sessions)} valid sessions in {file}")
    typer.echo(f"Uploaded to {source.upload(file.read_text(), bucket, event_id)}")


@catalog_app.command("probe")
def catalog_probe(
    event_id: str = typer.Option(DEFAULT_EVENT, "--event"),
    max_pages: int = typer.Option(200, help="Stop after this many pages"),
):
    """Diagnose a catalog walk: page sizes, totalCount, nextToken, overlap with favorites.

    Prints no tokens and no abstracts, so the output is safe to paste.
    """
    client = _client()
    ids: list[str] = []
    for i, page in enumerate(client.iter_session_pages(event_id, include_abstracts=False)):
        items = page["items"]
        first = items[0].get("sessionId") if items else None
        last = items[-1].get("sessionId") if items else None
        typer.echo(
            f"page {i + 1}: items={len(items)} totalCount={page.get('totalCount')} "
            f"nextToken={'yes' if page.get('nextToken') else 'no'} first={first} last={last}"
        )
        ids.extend(x["sessionId"] for x in items)
        if i + 1 >= max_pages:
            typer.echo(f"stopped after {max_pages} pages")
            break
    unique = set(ids)
    typer.echo(f"sessions: {len(ids)} (unique {len(unique)})")
    try:
        sched = client.get_schedule(event_id)
    except Exception as e:  # diagnostics only
        typer.echo(f"GetSchedule failed: {e}")
        return
    fav = set(sched.favorites)
    typer.echo(
        f"favorites: {len(fav)}; favorites in catalog: {len(fav & unique)}; "
        f"catalog sessions that are NOT favorites: {len(unique - fav)}; "
        f"reserved: {len(sched.reserved)}; personal time: {len(sched.personal_time)}"
    )


@catalog_app.command("report")
def catalog_report(
    file: str | None = typer.Option(None, "--file", help="Local path or s3:// URI"),
    event_id: str = typer.Option(DEFAULT_EVENT, "--event"),
    top: int = 15,
):
    """Field coverage and venue inference for a dumped catalog (no abstracts printed)."""
    from collections import Counter

    from reinvent_agent.catalog import source
    from reinvent_agent.catalog.venues import VenueInferrer, room_tokens
    from reinvent_agent.config import settings

    sessions = source.load_sessions(source.resolve(file, event_id, settings().catalog_bucket))
    inferrer = VenueInferrer.learn(sessions)
    results = [(s, *inferrer.infer(s)) for s in sessions]
    typer.echo(f"{len(sessions)} sessions; learned {len(inferrer.token_venue)} room->venue keys")
    typer.echo(f"venue source: {dict(Counter(src for _, _, src in results))}")
    typer.echo(f"venues after inference: {dict(Counter(v for _, v, _ in results))}")
    typer.echo(f"levels: {dict(Counter(s.level_number for s in sessions))}")
    typer.echo(f"unscheduled (no sessionTime): {sum(1 for s in sessions if s.start is None)}")
    unresolved = Counter(
        " | ".join(room_tokens(s.room)) or s.room for s, v, _ in results if v is None and s.room
    )
    typer.echo(f"unresolved room keys (top {top}):")
    for key, n in unresolved.most_common(top):
        typer.echo(f"  {n:4d}  {key}")
    learned = Counter(v for _, v, src in results if src == "room-learned")
    typer.echo(f"inferred from learned room names, by venue: {dict(learned)}")


def _search_backend():
    from reinvent_agent.catalog.embeddings import BedrockTitanEmbedder
    from reinvent_agent.catalog.search import CatalogSearch
    from reinvent_agent.catalog.vector_store import S3VectorsStore
    from reinvent_agent.config import settings

    cfg = _require_deployed(settings())
    store = S3VectorsStore(cfg.vector_bucket, cfg.vector_index, region=cfg.region)
    return cfg, CatalogSearch(store, BedrockTitanEmbedder(region=cfg.region))


def _require_deployed(cfg):
    if not cfg.vector_bucket:
        typer.echo(
            "No vector bucket found. Deploy first (`cd infra && npx aws-cdk@2 deploy --all`) "
            "or set REINVENT_VECTOR_BUCKET."
        )
        raise typer.Exit(1)
    return cfg


@catalog_app.command("index")
def catalog_index(
    file: str | None = typer.Option(
        None,
        "--file",
        help="Local path or s3:// URI. Default: fixtures/<event>/catalog.jsonl if present, "
        "else the catalog bucket's catalog/<event>/catalog.jsonl",
    ),
    event_id: str = typer.Option(DEFAULT_EVENT, "--event"),
    with_table: bool = typer.Option(True, help="Also upsert the DynamoDB sessions table"),
):
    """Embed a dumped catalog (Bedrock Titan v2) into S3 Vectors, and DynamoDB."""
    import boto3

    from reinvent_agent.catalog import source
    from reinvent_agent.catalog.embeddings import BedrockTitanEmbedder
    from reinvent_agent.catalog.ingest import ingest
    from reinvent_agent.catalog.vector_store import S3VectorsStore
    from reinvent_agent.config import settings

    cfg = _require_deployed(settings())
    location = source.resolve(file, event_id, cfg.catalog_bucket)
    typer.echo(f"Reading {location}")
    sessions = source.load_sessions(location)
    table = None
    if with_table:
        if not cfg.sessions_table:
            typer.echo(
                "Set REINVENT_SESSIONS_TABLE (see `cdk deploy` outputs) or pass --no-with-table"
            )
            raise typer.Exit(1)
        table = boto3.resource("dynamodb", region_name=cfg.region).Table(cfg.sessions_table)
    report = ingest(
        sessions,
        event_id,
        S3VectorsStore(cfg.vector_bucket, cfg.vector_index, region=cfg.region),
        BedrockTitanEmbedder(region=cfg.region),
        table=table,
        progress=lambda done, total: typer.echo(f"  embedded {done}/{total}"),
    )
    typer.echo(f"Indexed {report.indexed} sessions; venue sources: {report.venue_sources}")


@catalog_app.command("search")
def catalog_search(
    query: str,
    min_level: int | None = typer.Option(None),
    day: list[str] = typer.Option([], help="YYYY-MM-DD; repeatable"),
    venue: list[str] = typer.Option([], help="repeatable"),
    session_type: list[str] = typer.Option([], "--type", help="repeatable"),
    k: int = 10,
):
    """Semantic search with filters, e.g. `catalog search "zero-ETL" --min-level 300`."""
    from reinvent_agent.catalog.search import SearchFilters

    cfg, search = _search_backend()
    filters = SearchFilters(
        event_id=cfg.event_id, min_level=min_level, days=day, venues=venue, types=session_type
    )
    for r in search.search(query, filters, k):
        m = r.summary()
        typer.echo(
            f"[{m['code']}] {m['title']}  ({m['type']}, {m['level']}, {m['day']} {m['start']}, "
            f"{m['venue']})  score={m['score']}"
        )


@app.command("check-models")
def check_models(
    provider: str | None = typer.Option(
        None, help="Probe this endpoint instead of the configured one (bedrock, anthropic)"
    ),
):
    """Probe the embedding model and which Claude models an endpoint can call."""
    import json

    import anthropic
    import boto3

    from reinvent_agent.catalog.embeddings import TITAN_V2
    from reinvent_agent.config import settings
    from reinvent_agent.llm import get_provider, make_client

    cfg = settings()
    try:
        body = json.dumps({"inputText": "ok", "dimensions": 1024, "normalize": True})
        boto3.client("bedrock-runtime", region_name=cfg.region).invoke_model(
            modelId=TITAN_V2, body=body
        )
        typer.echo(f"OK    {TITAN_V2} (embeddings)")
    except Exception as e:
        typer.echo(f"FAIL  {TITAN_V2}: {type(e).__name__}: {e}")

    llm = get_provider(provider or cfg.llm_provider)
    typer.echo(f"Claude provider: {llm.name} ({llm.label})")
    try:
        client = make_client(cfg.region, llm.name, cfg.anthropic_key_secret_arn)
    except RuntimeError as e:
        typer.echo(f"FAIL  {e}")
        raise typer.Exit(1) from None
    working = []
    for model in map(llm.model_id, llm.candidates):
        try:
            client.messages.create(
                model=model, max_tokens=64, messages=[{"role": "user", "content": "Say OK."}]
            )
            working.append(model)
            typer.echo(f"OK    {model}")
        except anthropic.APIStatusError as e:
            detail = e.body.get("error", {}).get("message") if isinstance(e.body, dict) else None
            typer.echo(f"FAIL  {model}: {e.status_code} {detail or e.message}")
        except anthropic.APIConnectionError as e:
            typer.echo(f"FAIL  {model}: connection error {e}")
    typer.echo(f"\nin use: {cfg.llm_provider} / {cfg.model}")
    if working and llm.name != cfg.llm_provider:
        bare = working[0].removeprefix(llm.model_prefix)
        typer.echo(f"Switch with:  reinvent-agent config set-provider {llm.name} --model {bare}")
    elif working and cfg.model not in working:
        bare = working[0].removeprefix(llm.model_prefix)
        typer.echo(f"Use:  reinvent-agent config set-provider {llm.name} --model {bare}")
    elif not working:
        hint = (
            f"Request Claude access for this account in Bedrock ({cfg.region})"
            if llm.name == "bedrock"
            else "Check the API key (`config set-api-key`) and your Claude Console billing"
        )
        typer.echo(f"No Claude model answered. {hint}, then re-run.")


def _my_schedule(cfg):
    from reinvent_agent.catalog import source
    from reinvent_agent.schedule import MySchedule

    try:
        sessions = source.load_sessions(source.resolve(None, cfg.event_id, cfg.catalog_bucket))
    except Exception:  # no catalog yet: schedule tools fall back to GetSession
        sessions = []
    from reinvent_agent.accounts import token_store

    signed_in = token_store().load() is not None
    return MySchedule.with_inferred_venues(
        cfg.event_id, lambda: _client() if signed_in else None, sessions
    )


reserve_app = typer.Typer(
    no_args_is_help=True, help="Reservation plan, Oct 6 portal checklist, Oct 8 run"
)
app.add_typer(reserve_app, name="reserve")


def _plan_store():
    from reinvent_agent.accounts import plan_store

    store = plan_store()
    if store is None:
        typer.echo("Sign in and deploy ReinventAgentData first.")
        raise typer.Exit(1)
    return store


def _print_plan(plan) -> None:
    for p in plan.primaries:
        backups = ", ".join(b.code for b in plan.backups_for(p.session_id)) or "-"
        typer.echo(
            f"  P{p.priority} {p.day} {p.start}-{p.end} [{p.code}] {p.title[:60]}"
            f"  (backups: {backups})"
        )


@reserve_app.command("status")
def reserve_status():
    """Approved plan, unattended switch, schedule and recent runs."""
    from reinvent_agent.accounts import subscriptions, unattended_enabled
    from reinvent_agent.reservations import SCHEDULE

    store = _plan_store()
    plan = store.approved()
    if plan:
        typer.echo(f"Approved plan v{plan.version}: {len(plan.primaries)} sessions to reserve")
        _print_plan(plan)
    else:
        typer.echo("No approved plan. Build one: `reinvent-agent reserve build`, then approve.")
    typer.echo(f"\nUnattended reservations: {'ON' if unattended_enabled() else 'off'}")
    subs = subscriptions()
    typer.echo(
        "Notifications: "
        + (", ".join(f"{s['endpoint']}{'' if s['confirmed'] else ' (unconfirmed)'}" for s in subs)
           or "none (reinvent-agent reserve notify EMAIL)")
    )  # fmt: skip
    typer.echo("Schedule:")
    for job in SCHEDULE:
        typer.echo(f"  {job.describe()}  {job.label} ({job.action})")
    for r in store.runs(5):
        typer.echo(f"Run {r['label']}: {r['status']}, {len(r['reserved'])} reserved, "
                   f"{len(r['failed'])} not")  # fmt: skip


@reserve_app.command("build")
def reserve_build(
    strategy: str = typer.Option("max_sessions", help="max_sessions or one_venue"),
):
    """Draft a plan from your favorites (saved as the draft; approve it to use it)."""
    store = _plan_store()
    ms = _my_schedule(_require_deployed_any())
    sched = ms.load(refresh=True)
    from reinvent_agent.reservation_plan import build_plan

    ids = list(dict.fromkeys(sched.reserved + sched.favorites))
    plan = build_plan(ms.event_id, ms.sessions(ids), ms.venue_of, sched.reserved, strategy=strategy)
    store.save_draft(plan)
    typer.echo(f"Draft: {len(plan.primaries)} to reserve, "
               f"{len(plan.items) - len(plan.primaries)} backups")  # fmt: skip
    _print_plan(plan)
    typer.echo("Approve with `reinvent-agent reserve approve` (or edit it in the app).")


@reserve_app.command("approve")
def reserve_approve():
    """Approve the current draft; the scheduled runs use the approved plan."""
    store = _plan_store()
    draft = store.draft()
    if draft is None:
        typer.echo("No draft. Run `reinvent-agent reserve build` first.")
        raise typer.Exit(1)
    try:
        plan = store.approve(draft)
    except ValueError as e:
        typer.echo(f"Can't approve: {e}")
        raise typer.Exit(1) from None
    typer.echo(f"Approved plan v{plan.version} ({len(plan.primaries)} sessions).")


@reserve_app.command("preflight")
def reserve_preflight(cloud: bool = typer.Option(False, help="Run it in the deployed Lambda")):
    """Check that the run can sign in as you (cloud: also sends the notification)."""
    if cloud:
        from reinvent_agent.accounts import invoke_cloud

        typer.echo(invoke_cloud("preflight", "manual check"))
        return
    from reinvent_agent.reservation_runner import ReservationRunner

    ok, msg = ReservationRunner(_client(), DEFAULT_EVENT).preflight(notify=False)
    typer.echo(msg)
    if not ok:
        raise typer.Exit(1)


@reserve_app.command("run")
def reserve_run(
    wait_minutes: float = typer.Option(0, help="Keep polling this long if still closed"),
    cloud: bool = typer.Option(False, help="Run it in the deployed Lambda (tests that path)"),
):
    """Reserve the approved plan now (manual fallback; the API opens Oct 8)."""
    import time

    if cloud:
        from reinvent_agent.accounts import invoke_cloud

        payload_wait = int(wait_minutes * 60)
        typer.echo(invoke_cloud("run", "cloud test", wait_seconds=payload_wait))
        return

    from reinvent_agent.reservation_runner import ReservationRunner

    store = _plan_store()
    runner = ReservationRunner(_client(), DEFAULT_EVENT)
    report = runner.run(store.approved(), "manual", time.time() + wait_minutes * 60)
    store.save_run(report.to_dict())
    typer.echo(report.text())


@reserve_app.command("notify")
def reserve_notify(email: str):
    """Email notifications for the runs (confirm the link AWS sends you)."""
    from reinvent_agent.accounts import subscribe

    subscribe(email)
    typer.echo(f"Subscribed {email}: click the confirmation link in the email from AWS.")


def _require_deployed_any():
    from reinvent_agent.config import settings

    return settings()


@app.command("ask")
def ask(question: str):
    """Ask a question about the catalog; answers cite session codes."""
    from reinvent_agent.llm import make_client
    from reinvent_agent.qa import CatalogQA

    cfg, search = _search_backend()
    client = make_client(cfg.region, cfg.llm_provider, cfg.anthropic_key_secret_arn)
    qa = CatalogQA(search, client, cfg.model, cfg.event_id, schedule=_my_schedule(cfg))
    answer = qa.ask(question)
    typer.echo(answer.text)


@catalog_app.command("schedule")
def catalog_schedule(event_id: str = typer.Option(DEFAULT_EVENT, "--event")):
    """Show your reservations, favorites and personal time."""
    sched = _client().get_schedule(event_id)
    typer.echo(json.dumps(sched.model_dump(mode="json", by_alias=True), indent=2))


if __name__ == "__main__":
    app()
