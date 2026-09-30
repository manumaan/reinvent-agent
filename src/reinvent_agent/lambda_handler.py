"""AWS Lambda entry point for the unattended reservation run.

EventBridge Scheduler invokes it with ``{"action": "preflight" | "run", "label": ...}``
(see ``reservations.SCHEDULE``). Environment: TOKEN_SECRET_ARN, PLANS_TABLE, TOPIC_ARN,
EVENT_ID. Only the Events API client and the reservation modules are imported here, so
the deployment package stays small (httpx, pydantic, tzdata; boto3 is in the runtime).
"""

from __future__ import annotations

import os
from datetime import timedelta


def _deps():
    import boto3

    from reinvent_agent.events_api.auth import (
        SecretsManagerTokenStore,
        TokenProvider,
        token_subject,
    )
    from reinvent_agent.events_api.client import EventsApiClient
    from reinvent_agent.reservation_plan import PlanStore, dynamo_lease
    from reinvent_agent.reservation_runner import ReservationRunner, SnsNotifier

    table = boto3.resource("dynamodb").Table(os.environ["PLANS_TABLE"])
    tokens = SecretsManagerTokenStore(os.environ["TOKEN_SECRET_ARN"])
    provider = TokenProvider(tokens, shared_lock=dynamo_lease(table))
    event_id = os.environ.get("EVENT_ID", "reinvent2026")
    notifier = SnsNotifier(os.environ["TOPIC_ARN"])
    runner = ReservationRunner(EventsApiClient(provider), event_id, notifier)
    stored = tokens.load()
    user = token_subject(stored.access_token) if stored else None
    store = PlanStore(table, user or "me", event_id)
    return runner, store, stored


def handler(event, context):
    from reinvent_agent.reservations import OPEN_GRACE_MINUTES, release_for

    action = event.get("action", "preflight")
    label = event.get("label", action)
    runner, store, stored = _deps()
    if stored is None:
        msg = "No sign-in stored for the unattended run. In the app: Plans -> Enable."
        runner.notifier.notify(f"ACTION NEEDED: re:Invent reservations ({label})", msg)
        return {"ok": False, "message": msg}

    if action == "preflight":
        ok, msg = runner.preflight(label)
        plan = store.approved()
        if ok and plan is None:
            runner.notifier.notify(
                f"ACTION NEEDED: re:Invent reservations ({label})",
                "Sign-in is fine, but no reservation plan is approved. Approve one in the "
                "app (Plans tab) before the release.",
            )
        return {"ok": ok, "message": msg, "plan_version": plan.version if plan else None}

    if action == "run":
        from reinvent_agent.reservation_plan import LeaseBusy, dynamo_lease

        release = release_for(label)
        deadline = release + timedelta(minutes=OPEN_GRACE_MINUTES)
        # Leave 2 minutes of Lambda time for reading back, saving and notifying.
        remaining = context.get_remaining_time_in_millis() / 1000 if context else 900
        deadline_ts = min(deadline.timestamp(), runner.clock() + remaining - 120)
        # One run at a time (a Lambda lives at most 15 minutes, so the lease does too).
        run_lease = dynamo_lease(store.table, name="reservation-run", ttl=900, wait=0)
        try:
            with run_lease():
                report = runner.run(store.approved(), label, deadline_ts)
                store.save_run(report.to_dict())
        except LeaseBusy:
            msg = "Another reservation run is already in progress; this one stopped."
            runner.notifier.notify(f"re:Invent reservations ({label}): skipped", msg)
            return {"ok": False, "status": "busy"}
        return {"ok": report.status == "done", "status": report.status}

    raise ValueError(f"unknown action {action!r}")
