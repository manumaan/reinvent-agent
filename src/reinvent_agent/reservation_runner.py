"""The reservation run: reserve an approved plan the moment seats release.

Rules from the Events API guide (DESIGN.md §1):
* ReserveSessions answers **409** until seats release -> poll, one session at a time.
* The quota counts **sessions**: 30 per minute per attendee -> pace batches of <=10.
* Writes are **not idempotent** and results can be lost in transit -> after every
  batch (and after any error) re-read GetSchedule, the source of truth, and only
  send what is still missing.
* Per-session failures: ``sessionFull`` / ``sessionNotReservable`` /
  ``insufficientAccess`` -> try that primary's backups; ``scheduleConflict`` -> you
  already hold something at that time; ``alreadyScheduled`` -> fine.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from reinvent_agent.events_api.client import OperationClosedError
from reinvent_agent.events_api.models import BulkResult
from reinvent_agent.reservation_plan import PlanItem, ReservationPlan

MAX_BATCH = 10
SESSIONS_PER_MINUTE = 30
POLL_SECONDS = 3.0  # 20 probe sessions/min while closed: inside the 30/min quota
TRY_BACKUP = {"sessionFull", "sessionNotReservable", "insufficientAccess"}
FAILURE_TEXT = {
    "sessionFull": "full",
    "sessionNotReservable": "no reserved seating",
    "insufficientAccess": "not included in your pass",
    "scheduleConflict": "conflicts with a session you already hold",
    "timePassed": "already happened",
    "unknown": "outcome unknown after retries",
}


class Notifier:
    def notify(self, subject: str, message: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class ListNotifier(Notifier):
    """Collects messages (tests, local runs)."""

    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def notify(self, subject: str, message: str) -> None:
        self.messages.append((subject, message))


class SnsNotifier(Notifier):
    def __init__(self, topic_arn: str, client=None):
        if client is None:
            import boto3

            client = boto3.client("sns")
        self.topic_arn, self.client = topic_arn, client

    def notify(self, subject: str, message: str) -> None:
        self.client.publish(TopicArn=self.topic_arn, Subject=subject[:100], Message=message)


@dataclass
class RunReport:
    label: str
    started_at: float
    status: str = "running"  # done | closed | auth_failed | no_plan | error
    finished_at: float | None = None
    reserved: list[dict] = field(default_factory=list)  # {code, title, how}
    failed: list[dict] = field(default_factory=list)  # {code, title, reason}
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def subject(self) -> str:
        if self.status == "done":
            counts = f"{len(self.reserved)} reserved"
            if self.failed:
                counts += f", {len(self.failed)} not"
            return f"re:Invent reservations ({self.label}): {counts}"
        return f"re:Invent reservations ({self.label}): {self.status.replace('_', ' ')}"

    def text(self) -> str:
        lines = [self.subject(), ""]
        lines += self.notes
        if self.reserved:
            lines += ["", "Reserved:"] + [
                f"  {r['code']}  {r['title']}  ({r['how']})" for r in self.reserved
            ]
        if self.failed:
            lines += ["", "Not reserved:"] + [
                f"  {f['code']}  {f['title']}  - {f['reason']}" for f in self.failed
            ]
        return "\n".join(lines)


class RateLimiter:
    """At most ``limit`` sessions sent in any rolling 60 seconds."""

    def __init__(self, clock, sleep, limit: int = SESSIONS_PER_MINUTE):
        self.clock, self.sleep, self.limit = clock, sleep, limit
        self.sent: deque[tuple[float, int]] = deque()

    def acquire(self, n: int) -> None:
        while True:
            now = self.clock()
            while self.sent and now - self.sent[0][0] >= 60:
                self.sent.popleft()
            used = sum(k for _, k in self.sent)
            if used + n <= self.limit:
                self.sent.append((now, n))
                return
            self.sleep(max(0.5, 60 - (now - self.sent[0][0])))


class ReservationRunner:
    def __init__(
        self,
        client,
        event_id: str,
        notifier: Notifier | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: float = POLL_SECONDS,
    ):
        self.client, self.event_id = client, event_id
        self.notifier = notifier or ListNotifier()
        self.clock, self.sleep, self.poll_seconds = clock, sleep, poll_seconds
        self.limiter = RateLimiter(clock, sleep)

    # --- preflight -------------------------------------------------------------

    def preflight(self, label: str = "check", notify: bool = True) -> tuple[bool, str]:
        """Token refresh + GetSchedule: proves the unattended run can act for you."""
        try:
            sched = self.client.get_schedule(self.event_id)
        except Exception as e:
            msg = (
                f"The reservation run cannot sign in as you: {e}\n\n"
                "Sign in again on your Mac: open the planner app, Sign in with AWS Builder "
                "ID, then Plans -> Enable unattended reservations. Or run:\n"
                "  uv run reinvent-agent auth login && uv run reinvent-agent auth push-secret"
            )
            if notify:
                self.notifier.notify(f"ACTION NEEDED: re:Invent reservations ({label})", msg)
            return False, msg
        msg = (
            f"Sign-in OK. You hold {len(sched.reserved)} reservations and "
            f"{len(sched.favorites)} favorites."
        )
        if notify:
            self.notifier.notify(f"re:Invent reservations ({label}): ready", msg)
        return True, msg

    # --- run -------------------------------------------------------------------

    def run(self, plan: ReservationPlan | None, label: str, open_deadline: float) -> RunReport:
        """Reserve ``plan``; wait for seats to release until ``open_deadline`` (epoch)."""
        report = RunReport(label=label, started_at=self.clock())
        try:
            self._run(plan, report, open_deadline)
        except Exception as e:  # never die silently: tell the attendee
            report.status = "error"
            report.notes.append(f"The run stopped with an error: {e}")
        report.finished_at = self.clock()
        self.notifier.notify(report.subject(), report.text())
        return report

    def _schedule(self) -> set[str]:
        return set(self.client.get_schedule(self.event_id).reserved)

    def _run(self, plan: ReservationPlan | None, report: RunReport, open_deadline: float):
        if plan is None or not plan.primaries:
            report.status = "no_plan"
            report.notes.append("There is no approved reservation plan. Approve one in the app.")
            return
        try:
            held = self._schedule()
        except Exception as e:
            report.status = "auth_failed"
            report.notes.append(f"Could not read your schedule (sign-in?): {e}")
            return

        self.plan, self.report, self.held = plan, report, held
        self.tried: set[str] = set()
        queue = [p for p in plan.primaries if p.session_id not in held]
        for p in plan.primaries:
            if p.session_id in held:
                self._ok(p, "already reserved")
        # Keep a backup reserved in an earlier phase rather than risk losing both.
        queue = [p for p in queue if not self._covered_by_backup(p)]
        if not queue:
            report.status = "done"
            report.notes.append("Everything in the plan is already reserved.")
            return

        # 1) Wait for release: probe with the top-priority session alone.
        first, rest = queue[0], queue[1:]
        result = self._probe_until_open(first, open_deadline)
        if result is None:
            report.status = "closed"
            report.notes.append(
                "Reservations were still closed at the deadline. Nothing was reserved; "
                "the next scheduled run will try again, or use Reserve in the app."
            )
            return
        self._settle([first], result)

        # 2) Everything else, in priority order, paced and read back.
        for i in range(0, len(rest), MAX_BATCH):
            batch = [p for p in rest[i : i + MAX_BATCH] if p.session_id not in self.held]
            if batch:
                self._settle(batch, self._send(batch))
        report.status = "done"

    def _covered_by_backup(self, primary: PlanItem) -> bool:
        for b in self.plan.backups_for(primary.session_id):
            if b.session_id in self.held:
                self._ok(b, f"backup for {primary.code}, reserved earlier")
                return True
        return False

    def _probe_until_open(self, item: PlanItem, deadline: float):
        while True:
            self.limiter.acquire(1)
            try:
                return self.client.reserve_sessions(self.event_id, [item.session_id])
            except OperationClosedError:
                if self.clock() >= deadline:
                    return None
                self.sleep(self.poll_seconds)
            except Exception as e:  # open, but this response was lost: reconcile
                self.report.notes.append(f"Reserve request failed ({e}); re-checking.")
                return self._after_unknown([item], attempt=1) or BulkResult()

    def _send(self, items: list[PlanItem], attempt: int = 1):
        """ReserveSessions; on a lost/failed request, reconcile and resend once."""
        self.limiter.acquire(len(items))
        ids = [i.session_id for i in items]
        try:
            return self.client.reserve_sessions(self.event_id, ids)
        except OperationClosedError:
            self.sleep(self.poll_seconds)
        except Exception as e:  # lost response, 5xx, network: outcome unknown
            self.report.notes.append(f"Reserve request failed ({e}); re-checking your schedule.")
        return self._after_unknown(items, attempt)

    def _after_unknown(self, items: list[PlanItem], attempt: int):
        """Read back GetSchedule; resend only what is still missing (once)."""
        self.held = self._schedule()
        missing = [i for i in items if i.session_id not in self.held]
        if missing and attempt < 2:
            return self._send(missing, attempt + 1)
        return None

    def _settle(self, items: list[PlanItem], result) -> None:
        """Read back GetSchedule, record outcomes, fall back to backups."""
        codes = {}
        if result is not None:
            for f in result.failed:
                codes[f.session_id] = f.code
        self.held = self._schedule()
        for item in items:
            self.tried.add(item.session_id)
            if item.session_id in self.held:
                self._ok(item, "reserved")
                continue
            code = codes.get(item.session_id, "unknown")
            if code == "alreadyScheduled":
                self._ok(item, "already reserved")
            elif code in TRY_BACKUP and self._try_backups(item, code):
                continue
            else:
                self._fail(item, code)

    def _try_backups(self, primary: PlanItem, why: str) -> bool:
        for b in self.plan.backups_for(primary.session_id):
            if b.session_id in self.tried or b.session_id in self.held:
                continue
            if self._clashes_with_held(b, primary):
                continue
            self.tried.add(b.session_id)
            self._send([b])
            self.held = self._schedule()
            if b.session_id in self.held:
                self._ok(b, f"backup for {primary.code} ({FAILURE_TEXT.get(why, why)})")
                return True
        return False

    def _clashes_with_held(self, backup: PlanItem, replacing: PlanItem) -> bool:
        for sid in self.held:
            other = self.plan.item(sid)
            if other and other.session_id != replacing.session_id and backup.overlaps(other):
                return True
        return False

    def _ok(self, item: PlanItem, how: str) -> None:
        if any(r["session_id"] == item.session_id for r in self.report.reserved):
            return
        self.report.reserved.append(
            {"session_id": item.session_id, "code": item.code, "title": item.title, "how": how}
        )

    def _fail(self, item: PlanItem, code: str) -> None:
        reason = FAILURE_TEXT.get(code, code)
        if self.plan.backups_for(item.session_id):
            reason += "; no backup could be reserved"
        self.report.failed.append(
            {
                "session_id": item.session_id,
                "code": item.code,
                "title": item.title,
                "reason": reason,
            }
        )
