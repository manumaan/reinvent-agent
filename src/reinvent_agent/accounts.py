"""Where your Builder ID tokens live, and the unattended-reservation switches.

Normally tokens stay in ``~/.config/reinvent-agent/tokens.json``. Enabling unattended
reservations copies them to Secrets Manager and from then on the laptop reads and
writes that shared copy too (``SyncedTokenStore``), with refreshes serialized by a
DynamoDB lease. That way a refresh-token rotation on either side never strands the
other: the Lambda and the app always hold the same, current refresh token.
"""

from __future__ import annotations

import json

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


def _table(cfg: Settings):
    import boto3

    return boto3.resource("dynamodb", region_name=cfg.region).Table(cfg.plans_table)


def token_store(cfg: Settings | None = None) -> TokenStore:
    cfg = cfg or settings()
    local = FileTokenStore()
    if unattended_enabled() and cfg.token_secret_arn:
        import boto3

        client = boto3.client("secretsmanager", region_name=cfg.region)
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
    import boto3

    return boto3.client("sns", region_name=cfg.region)


def subscriptions(cfg: Settings | None = None) -> list[dict]:
    cfg = cfg or settings()
    if not cfg.reservation_topic_arn:
        return []
    resp = _sns(cfg).list_subscriptions_by_topic(TopicArn=cfg.reservation_topic_arn)
    return [
        {
            "endpoint": s["Endpoint"],
            "protocol": s["Protocol"],
            "confirmed": s["SubscriptionArn"] != "PendingConfirmation",
        }
        for s in resp.get("Subscriptions", [])
    ]


def subscribe(email: str, cfg: Settings | None = None) -> None:
    """Email notifications; AWS sends a confirmation link that must be clicked."""
    cfg = cfg or settings()
    if not cfg.reservation_topic_arn:
        raise RuntimeError("Deploy ReinventAgentReservations first.")
    _sns(cfg).subscribe(TopicArn=cfg.reservation_topic_arn, Protocol="email", Endpoint=email)


def invoke_cloud(action: str, label: str, cfg: Settings | None = None) -> dict:
    """Run the deployed Lambda now (e.g. a preflight to test sign-in + email)."""
    import boto3

    cfg = cfg or settings()
    if not cfg.reservation_function:
        raise RuntimeError("Deploy ReinventAgentReservations first.")
    resp = boto3.client("lambda", region_name=cfg.region).invoke(
        FunctionName=cfg.reservation_function,
        Payload=json.dumps({"action": action, "label": label}).encode(),
    )
    body = json.loads(resp["Payload"].read() or b"null")
    if resp.get("FunctionError"):
        raise RuntimeError(f"Lambda error: {body}")
    return body
