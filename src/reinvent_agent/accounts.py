"""Where your Builder ID tokens live, and the unattended-reservation switches.

Normally tokens stay in ``~/.config/reinvent-agent/tokens.json``. Enabling unattended
reservations copies them to Secrets Manager and from then on the laptop reads and
writes that shared copy too (``SyncedTokenStore``), with refreshes serialized by a
DynamoDB lease. That way a refresh-token rotation on either side never strands the
other: the Lambda and the app always hold the same, current refresh token.
"""

from __future__ import annotations

import json
from functools import cache

from reinvent_agent.config import Settings, load_preferences, save_preferences, settings
from reinvent_agent.events_api.auth import (
    FileTokenStore,
    SecretsManagerTokenStore,
    SyncedTokenStore,
    TokenProvider,
    TokenStore,
    token_subject,
)
from reinvent_agent.events_api.client import EventsApiClient


def unattended_enabled() -> bool:
    return bool(load_preferences().get("unattended"))


@cache
def _client(service: str, region: str):
    """One client per service: a new one means a new TLS connection on every page run."""
    import boto3

    return boto3.client(service, region_name=region)


@cache
def _dynamodb(region: str):
    import boto3

    return boto3.resource("dynamodb", region_name=region)


def _table(cfg: Settings):
    return _dynamodb(cfg.region).Table(cfg.plans_table)


def token_store(cfg: Settings | None = None) -> TokenStore:
    cfg = cfg or settings()
    local = FileTokenStore()
    if unattended_enabled() and cfg.token_secret_arn:
        client = _client("secretsmanager", cfg.region)
        return SyncedTokenStore(SecretsManagerTokenStore(cfg.token_secret_arn, client), local)
    return local


def token_provider(cfg: Settings | None = None) -> TokenProvider:
    cfg = cfg or settings()
    lock = None
    if unattended_enabled() and cfg.plans_table:
        from reinvent_agent.reservation_plan import dynamo_lease

        lock = dynamo_lease(_table(cfg))
    return TokenProvider(token_store(cfg), shared_lock=lock)


def events_client(cfg: Settings | None = None) -> EventsApiClient | None:
    """None when not signed in."""
    cfg = cfg or settings()
    if token_store(cfg).load() is None:
        return None
    return EventsApiClient(token_provider(cfg))


def user_id(cfg: Settings | None = None) -> str | None:
    tokens = token_store(cfg).load()
    return (token_subject(tokens.access_token) or "me") if tokens else None


def plan_store(cfg: Settings | None = None):
    from reinvent_agent.reservation_plan import PlanStore

    cfg = cfg or settings()
    uid = user_id(cfg)
    if not (uid and cfg.plans_table):
        return None
    return PlanStore(_table(cfg), uid, cfg.event_id)


def enable_unattended(cfg: Settings | None = None) -> None:
    """Copy current tokens to Secrets Manager and switch to the shared store."""
    cfg = cfg or settings()
    if not cfg.token_secret_arn:
        raise RuntimeError("Deploy ReinventAgentData first (no token secret).")
    tokens = FileTokenStore().load()
    if tokens is None:
        raise RuntimeError("Sign in first.")
    import boto3

    client = boto3.client("secretsmanager", region_name=cfg.region)
    SecretsManagerTokenStore(cfg.token_secret_arn, client).save(tokens)
    save_preferences(load_preferences() | {"unattended": True})


def disable_unattended(cfg: Settings | None = None) -> None:
    """Remove the cloud copy (keeps the local sign-in) and switch back to local tokens."""
    cfg = cfg or settings()
    shared = token_store(cfg)
    current = shared.load()
    if isinstance(shared, SyncedTokenStore):
        shared.shared.clear()
    if current is not None:
        FileTokenStore().save(current)  # the newest (possibly rotated) tokens
    save_preferences({k: v for k, v in load_preferences().items() if k != "unattended"})


# --- notifications and the cloud run -----------------------------------------


def _sns(cfg: Settings):
    return _client("sns", cfg.region)


def subscriptions(cfg: Settings | None = None) -> list[dict]:
    cfg = cfg or settings()
    if not cfg.reservation_topic_arn:
        return []
    resp = _sns(cfg).list_subscriptions_by_topic(TopicArn=cfg.reservation_topic_arn)
    return [
        {
            "endpoint": s["Endpoint"],
            "protocol": s["Protocol"],
            "confirmed": s["SubscriptionArn"].startswith("arn:"),  # not Pending/Deleted
        }
        for s in resp.get("Subscriptions", [])
    ]


def subscribe(email: str, cfg: Settings | None = None) -> None:
    """Email notifications; AWS sends a confirmation link that must be clicked."""
    cfg = cfg or settings()
    if not cfg.reservation_topic_arn:
        raise RuntimeError("Deploy ReinventAgentReservations first.")
    _sns(cfg).subscribe(TopicArn=cfg.reservation_topic_arn, Protocol="email", Endpoint=email)


def has_confirmed_email(cfg: Settings | None = None) -> bool:
    return any(s["confirmed"] for s in subscriptions(cfg))


def notifier(cfg: Settings | None = None):
    """Sends to the subscribed email(s); None when nobody would get it."""
    from reinvent_agent.reservation_runner import SnsNotifier

    cfg = cfg or settings()
    if not has_confirmed_email(cfg):
        return None
    return SnsNotifier(cfg.reservation_topic_arn, _sns(cfg))


def scheduler_client(cfg: Settings | None = None):
    cfg = cfg or settings()
    return _client("scheduler", cfg.region)


def invoke_cloud(
    action: str, label: str, cfg: Settings | None = None, wait_seconds: int = 0, **extra
) -> dict:
    """Run the deployed Lambda now (e.g. a preflight to test sign-in + email)."""
    cfg = cfg or settings()
    if not cfg.reservation_function:
        raise RuntimeError("Deploy ReinventAgentReservations first.")
    resp = _client("lambda", cfg.region).invoke(
        FunctionName=cfg.reservation_function,
        Payload=json.dumps(
            {"action": action, "label": label, "wait_seconds": wait_seconds, **extra}
        ).encode(),
    )
    body = json.loads(resp["Payload"].read() or b"null")
    if resp.get("FunctionError"):
        raise RuntimeError(f"Lambda error: {body}")
    return body
