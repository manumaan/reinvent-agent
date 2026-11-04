"""Automated reservation runs at a date and time the attendee chooses.

Each run is a one-time EventBridge Scheduler job that invokes the reservation Lambda
with the approved plan as it stands at that moment, then deletes itself. Runs more than
35 minutes away also get a sign-in + plan check 30 minutes before, which emails the
result, so a problem can still be fixed in time.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

PREFIX = "reinvent-reservations-custom-"
CHECK_BEFORE = timedelta(minutes=30)
MIN_LEAD = timedelta(minutes=2)


@dataclass
class ScheduledRun:
    stamp: str  # identifies the run: its UTC time, e.g. 20261110T1700
    when: datetime  # timezone-aware
    has_check: bool = False


def _create(client, name, when, tz, target_arn, role_arn, payload, description) -> None:
    client.create_schedule(
        Name=name,
        Description=description,
        ScheduleExpression=f"at({when.astimezone(ZoneInfo(tz)):%Y-%m-%dT%H:%M:%S})",
        ScheduleExpressionTimezone=tz,
        FlexibleTimeWindow={"Mode": "OFF"},
        ActionAfterCompletion="DELETE",
        Target={
            "Arn": target_arn,
            "RoleArn": role_arn,
            "Input": json.dumps(payload),
            # A late retry could reserve at a time nobody chose.
            "RetryPolicy": {"MaximumRetryAttempts": 0},
        },
    )


def schedule_run(
    when: datetime, tz: str, function_arn: str, role_arn: str, client, now: datetime | None = None
) -> ScheduledRun:
    """Create the run (and its check). ``when`` is timezone-aware; ``tz`` an IANA name."""
    now = now or datetime.now(UTC)
    if when < now + MIN_LEAD:
        raise ValueError("Choose a time at least 2 minutes from now.")
    stamp = when.astimezone(UTC).strftime("%Y%m%dT%H%M")
    label = "scheduled run"
    local = when.astimezone(ZoneInfo(tz))
    check_label = f"check before the run at {local:%b %-d %-I:%M %p %Z}"
    _create(
        client, f"{PREFIX}{stamp}-run", when, tz, function_arn, role_arn,
        {"action": "run", "label": label, "tz": tz, "scheduled_for": when.isoformat()},
        "re:Invent: reserve the approved plan",
    )  # fmt: skip
    check_at = when - CHECK_BEFORE
    has_check = check_at >= now + MIN_LEAD
    if has_check:
        try:
            _create(
                client, f"{PREFIX}{stamp}-check", check_at, tz, function_arn, role_arn,
                {"action": "preflight", "label": check_label},
                "re:Invent: sign-in and plan check before the run",
            )  # fmt: skip
        except Exception:
            cancel_run(stamp, client)  # all or nothing
            raise
    return ScheduledRun(stamp, when, has_check)


def list_runs(client) -> list[ScheduledRun]:
    """Upcoming runs, soonest first (completed ones have deleted themselves)."""
    names: list[str] = []
    kwargs = {"NamePrefix": PREFIX}
    while True:
        page = client.list_schedules(**kwargs)
        names += [s["Name"] for s in page.get("Schedules", [])]
        if not page.get("NextToken"):
            break
        kwargs["NextToken"] = page["NextToken"]
    out = []
    for name in names:
        if not name.endswith("-run"):
            continue
        detail = client.get_schedule(Name=name)
        at = re.fullmatch(r"at\((.+)\)", detail["ScheduleExpression"]).group(1)
        when = datetime.fromisoformat(at).replace(
            tzinfo=ZoneInfo(detail.get("ScheduleExpressionTimezone") or "UTC")
        )
        stamp = name.removeprefix(PREFIX).removesuffix("-run")
        out.append(ScheduledRun(stamp, when, f"{PREFIX}{stamp}-check" in names))
    return sorted(out, key=lambda r: r.when)


def cancel_run(stamp: str, client) -> None:
    """Delete a run and its check. Already gone (fired or cancelled) is fine."""
    for suffix in ("run", "check"):
        try:
            client.delete_schedule(Name=f"{PREFIX}{stamp}-{suffix}")
        except Exception as e:
            if "ResourceNotFound" not in type(e).__name__:
                raise
