"""`reinvent-agent` command line."""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from reinvent_agent.events_api import EventsApiClient, FileTokenStore, TokenProvider
from reinvent_agent.events_api.auth import SecretsManagerTokenStore, interactive_login

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
    FileTokenStore().clear()
    typer.echo("Local tokens removed.")


@catalog_app.command("events")
def catalog_events():
    for e in EventsApiClient().list_events():
        typer.echo(f"{e.event_id}\t{e.name}")


@catalog_app.command("dump")
def catalog_dump(
    event_id: str = typer.Option(DEFAULT_EVENT, "--event"),
    out: Path = typer.Option(Path("catalog.jsonl"), "--out"),
    no_abstracts: bool = False,
):
    """Walk ListSessions and write one JSON session per line."""
    n = 0
    with out.open("w") as f:
        for s in _client().iter_sessions(event_id, include_abstracts=not no_abstracts):
            f.write(json.dumps(s.model_dump(mode="json")) + "\n")
            n += 1
    typer.echo(f"Wrote {n} sessions to {out}")


@catalog_app.command("schedule")
def catalog_schedule(event_id: str = typer.Option(DEFAULT_EVENT, "--event")):
    """Show your reservations, favorites and personal time."""
    sched = _client().get_schedule(event_id)
    typer.echo(json.dumps(sched.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    app()
