"""`reinvent-agent` command line."""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from reinvent_agent.events_api import EventsApiClient, FileTokenStore, Session, TokenProvider
from reinvent_agent.events_api.auth import (
    AuthError,
    SecretsManagerTokenStore,
    interactive_login,
    revoke,
)

DEFAULT_EVENT = os.environ.get("REINVENT_EVENT_ID", "reinvent2026")

app = typer.Typer(no_args_is_help=True, help="re:Invent planner agent")
auth_app = typer.Typer(no_args_is_help=True, help="AWS Builder ID sign-in")
catalog_app = typer.Typer(no_args_is_help=True, help="Read the session catalog")
app.add_typer(auth_app, name="auth")
app.add_typer(catalog_app, name="catalog")


def _client() -> EventsApiClient:
    return EventsApiClient(TokenProvider(FileTokenStore()))


@auth_app.command("login")
def auth_login(no_browser: bool = typer.Option(False, help="Print the URL instead of opening it")):
    """Sign in with AWS Builder ID (runs a callback server on localhost:8484)."""
    tokens = interactive_login(FileTokenStore(), open_browser=not no_browser)
    typer.echo(f"Signed in. Access token valid until epoch {int(tokens.expires_at)}.")


@auth_app.command("status")
def auth_status():
    tokens = FileTokenStore().load()
    if tokens is None:
        typer.echo("Not signed in.")
        raise typer.Exit(1)
    state = "expired (will refresh)" if tokens.is_expired() else "valid"
    typer.echo(f"Access token {state}; refresh token stored at {FileTokenStore().path}")


@auth_app.command("push-secret")
def auth_push_secret(secret_id: str = typer.Option(..., envvar="REINVENT_TOKEN_SECRET_ID")):
    """Copy local tokens to Secrets Manager so the cloud side can act unattended.

    After this, the cloud side owns refresh-token rotation; signing in locally
    again later simply replaces the stored tokens.
    """
    tokens = FileTokenStore().load()
    if tokens is None:
        typer.echo("Not signed in; run `reinvent-agent auth login` first.")
        raise typer.Exit(1)
    SecretsManagerTokenStore(secret_id).save(tokens)
    typer.echo(f"Tokens stored in {secret_id}.")


@auth_app.command("logout")
def auth_logout():
    """Revoke the refresh token and delete local tokens.

    Tokens already pushed with `push-secret` are revoked too, since they share the
    refresh token. To also end the Builder ID browser session, sign out at
    https://profile.aws.amazon.com.
    """
    store = FileTokenStore()
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
    out: Path = typer.Option(Path("catalog.jsonl"), "--out"),
    no_abstracts: bool = False,
):
    """Walk ListSessions and write one JSON session per line (API field names)."""
    n, total = 0, None
    with out.open("w") as f:
        for page in _client().iter_session_pages(event_id, include_abstracts=not no_abstracts):
            total = page.get("totalCount", total)
            for raw in page["items"]:
                s = Session.model_validate(raw)
                f.write(json.dumps(s.model_dump(mode="json", by_alias=True, exclude_none=True)))
                f.write("\n")
                n += 1
    typer.echo(f"Wrote {n} sessions to {out} (API totalCount: {total})")
    if total is not None and n != total:
        typer.echo("WARNING: session count differs from totalCount; run `catalog probe`.")


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


@catalog_app.command("schedule")
def catalog_schedule(event_id: str = typer.Option(DEFAULT_EVENT, "--event")):
    """Show your reservations, favorites and personal time."""
    sched = _client().get_schedule(event_id)
    typer.echo(json.dumps(sched.model_dump(mode="json", by_alias=True), indent=2))


if __name__ == "__main__":
    app()
