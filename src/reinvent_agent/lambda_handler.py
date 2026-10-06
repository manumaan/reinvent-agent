"""AWS Lambda entry point for the unattended reservation run.

EventBridge Scheduler invokes it (see ``reservations.SCHEDULE``) with
``{"action": ..., "label": ...}``:

* ``preflight`` -- sign-in + plan check; always emails the result.
* ``poll`` -- every 2 minutes on Oct 8, when the Events API opens at an unannounced
  time: one probe; silent while closed; once open, reserves the approved plan, emails
  the report and marks itself done. Problems are emailed at most once an hour.
* ``run`` -- reserve now (manual, or to test the cloud path): one probe, no waiting
  unless ``wait_seconds`` is given; always emails the report.
 Environment: TOKEN_SECRET_ARN, PLANS_TABLE, TOPIC_ARN,
EVENT_ID. Only the Events API client and the reservation modules are imported here, so
the deployment package stays small (httpx, pydantic, tzdata; boto3 is in the runtime).
"""

from __future__ import annotations

import os


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


ALERT_EVERY_SECONDS = 3600


def handler(event, context):
    action = event.get("action", "preflight")
    label = event.get("label", action)
    runner, store, stored = _deps()
    try:
        return _handle(action, label, event, context, runner, store, stored)
    except Exception as e:  # anything outside the runner's own handling: still email
        if action != "poll" or _alert_due(store, runner):
            runner.notifier.notify(
                f"ERROR: re:Invent reservations ({label})",
                f"The {action} job failed: {e}. Reserve from the app if the API is open.",
            )
        raise


def _alert_due(store, runner) -> bool:
    last = store.flag("alert")
    if last and runner.clock() - last < ALERT_EVERY_SECONDS:
        return False
    store.set_flag("alert", runner.clock())
    return True


def _handle(action, label, event, context, runner, store, stored):
    from reinvent_agent.reservation_plan import LeaseBusy, dynamo_lease

    if stored is None:
        msg = "No sign-in stored for the unattended run. In the app: Plans -> Enable."
        if action != "poll" or _alert_due(store, runner):
            runner.notifier.notify(f"ACTION NEEDED: re:Invent reservations ({label})", msg)
        return {"ok": False, "message": msg}

    if action == "preflight":
        ok, msg = runner.preflight(label)
        plan = store.approved()
        if ok and plan is None:
            runner.notifier.notify(
                f"ACTION NEEDED: re:Invent reservations ({label})",
                "Sign-in is fine, but no reservation plan is approved. Approve one in the "
                "app (Plans tab) before the API opens.",
            )
        return {"ok": ok, "message": msg, "plan_version": plan.version if plan else None}

    if action not in ("poll", "run"):
        raise ValueError(f"unknown action {action!r}")
    if action == "poll" and store.flag("api-run-done"):
        return {"ok": True, "status": "already done"}

    # Leave 2 minutes of Lambda time for reading back, saving and notifying.
    remaining = context.get_remaining_time_in_millis() / 1000 if context else 900
    wait = min(float(event.get("wait_seconds", 0)), remaining - 120)
    deadline = runner.clock() + max(wait, 0)
    quiet = {"closed", "auth_failed", "error", "no_plan"} if action == "poll" else ()
    # One run at a time (a Lambda lives at most 15 minutes, so the lease does too).
    lease = dynamo_lease(store.table, name="reservation-run", ttl=900, wait=0)
    try:
        with lease():
            report = runner.run(store.approved(), label, deadline, quiet=quiet)
            if report.status != "closed":
                store.save_run(report.to_dict())
    except LeaseBusy:
        if action == "run":
            runner.notifier.notify(
                f"re:Invent reservations ({label}): skipped",
                "Another reservation run is already in progress; this one stopped.",
            )
        return {"ok": False, "status": "busy"}

    if action == "poll":
        if report.status == "done":
            store.set_flag("api-run-done", runner.clock())
        elif report.status != "closed" and _alert_due(store, runner):
            runner.notifier.notify(report.subject(), report.text())
    return {"ok": report.status == "done", "status": report.status}
