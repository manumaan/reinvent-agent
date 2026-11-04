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
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from reinvent_agent.events_api.client import EventsApiError, OperationClosedError
from reinvent_agent.events_api.models import BulkResult, Schedule
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
RESERVED, REFUSED, SKIPPED, CLOSED = "reserved", "refused", "skipped", "closed"  # log results


def _local_time(when: datetime | float, tz: str | None) -> datetime:
    """In ``tz`` when given and valid, else this machine's zone (a Lambda's is UTC)."""
    if not isinstance(when, datetime):
        when = datetime.fromtimestamp(when).astimezone()
    try:
        return when.astimezone(ZoneInfo(tz)) if tz else when.astimezone()
    except (KeyError, ValueError):
        return when.astimezone()


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
    # Every step, in order: {seconds, action, session_id, code, title, result, detail}
    log: list[dict] = field(default_factory=list)
    dry_run: bool = False
    tz: str | None = None  # the attendee's timezone, for the times in the email
    scheduled_for: str | None = None  # ISO time the run was scheduled for

    def to_dict(self) -> dict:
        return asdict(self)

    def _time(self, when: datetime | float, fmt: str = "%a %b %-d %Y, %-I:%M:%S %p %Z") -> str:
        return _local_time(when, self.tz).strftime(fmt)

    def subject(self) -> str:
        """Says which run this is: SNS keeps only the first 100 characters."""
        at = self._time(self.started_at, "%b %-d %-I:%M %p %Z")
        if self.dry_run:
            if self.status != "done":
                return f"DRY RUN, nothing reserved ({at}): {self.status.replace('_', ' ')}"
            counts = f"{len(self.reserved)} would be reserved or kept, {len(self.failed)} not"
            return f"DRY RUN, nothing reserved ({at}): {counts}"
        if self.status == "done":
            counts = f"{len(self.reserved)} reserved"
            if self.failed:
                counts += f", {len(self.failed)} not"
            return f"re:Invent reservations ({self.label}, {at}): {counts}"
        return f"re:Invent reservations ({self.label}, {at}): {self.status.replace('_', ' ')}"

    def text(self) -> str:
        lines = [self.subject(), "", f"Run: {self.label}"]
        if self.scheduled_for:
            lines.append(f"Scheduled for: {self._time(datetime.fromisoformat(self.scheduled_for))}")
        lines.append(f"Started: {self._time(self.started_at)}")
        if self.finished_at:
            lines.append(f"Finished: {self._time(self.finished_at)}")
        lines.append("")
        lines.append(
            "This was a DRY RUN: nothing was reserved."
            if self.dry_run
            else "This was a REAL run, not a dry run."
        )
        lines += self.notes
        if self.reserved:
            heading = "Would reserve, or already held:" if self.dry_run else "Reserved:"
            lines += ["", heading] + [
                f"  {r['code']}  {r['title']}  ({r['how']})" for r in self.reserved
            ]
        if self.failed:
            lines += ["", "Would not be reserved:" if self.dry_run else "Not reserved:"] + [
                f"  {f['code']}  {f['title']}  - {f['reason']}" for f in self.failed
            ]
        return "\n".join(lines)

    def log_text(self) -> str:
        """Every step, one per line: time, step, session, result, then the details."""
        results = {RESERVED: "would reserve", REFUSED: "would be refused"} if self.dry_run else {}

        def line(r: dict) -> str:
            extra = [r["title"], r["detail"]]
            if r.get("seats"):
                extra.append(f"seats now: {r['seats']}")
            result = results.get(r["result"], r["result"])
            return (
                f"+{r['seconds']:>3}s  {r['action']:<8} {r['code']:<12} {result:<17} "
                + " | ".join(e for e in extra if e)
            )

        return "\n".join(line(r) for r in self.log)


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
        tz: str | None = None,
    ):
        self.client, self.event_id, self.tz = client, event_id, tz
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

    def run(
        self,
        plan: ReservationPlan | None,
        label: str,
        open_deadline: float,
        quiet: Iterable[str] = (),
        on_start: Callable[[dict], None] | None = None,
        scheduled_for: str | None = None,
    ) -> RunReport:
        """Reserve ``plan``; wait for seats to release until ``open_deadline`` (epoch).

        ``quiet``: statuses not to notify about (the all-day poll stays silent while
        the API is closed and rate-limits its own alerts). ``on_start``: gets the report
        as soon as the run begins, so the run history shows it right away.
        """
        report = RunReport(
            label=label, started_at=self.clock(), tz=self.tz, scheduled_for=scheduled_for
        )
        if on_start:
            try:
                on_start(report.to_dict())
            except Exception as e:  # only the history entry: the run must still go ahead
                report.notes.append(f"Couldn't mark the run as started: {e}")
        try:
            self._run(plan, report, open_deadline)
        except Exception as e:  # never die silently: tell the attendee
            report.status = "error"
            report.notes.append(f"The run stopped with an error: {e}")
        report.finished_at = self.clock()
        if report.status not in set(quiet):
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
                self._log("skip", p, SKIPPED, "already reserved")
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
                "The Events API answered that reservations are closed. Nothing was reserved."
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
                self._log("skip", primary, SKIPPED, f"you already hold its backup {b.code}")
                return True
        return False

    def _probe_until_open(self, item: PlanItem, deadline: float):
        tries = 0
        while True:
            self.limiter.acquire(1)
            try:
                result = self.client.reserve_sessions(self.event_id, [item.session_id])
                if tries:
                    self._log("wait", item, CLOSED, f"closed for {tries} tries, now open")
                return result
            except OperationClosedError:
                tries += 1
                if self.clock() >= deadline:
                    self._log("wait", item, CLOSED, f"still closed after {tries} tries")
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
                self._log("reserve", item, RESERVED)
                continue
            code = codes.get(item.session_id, "unknown")
            if code == "alreadyScheduled":
                self._ok(item, "already reserved")
                self._log("skip", item, SKIPPED, "already reserved")
                continue
            self._log("reserve", item, REFUSED, FAILURE_TEXT.get(code, code))
            if not (code in TRY_BACKUP and self._try_backups(item, code)):
                self._fail(item, code)

    def _try_backups(self, primary: PlanItem, why: str) -> bool:
        for b in self.plan.backups_for(primary.session_id):
            if b.session_id in self.tried or b.session_id in self.held:
                continue
            if self._clashes_with_held(b, primary):
                self._log("backup", b, SKIPPED, f"for {primary.code}: clashes with what you hold")
                continue
            self.tried.add(b.session_id)
            result = self._send([b])
            self.held = self._schedule()
            if b.session_id in self.held:
                self._ok(b, f"backup for {primary.code} ({FAILURE_TEXT.get(why, why)})")
                self._log("backup", b, RESERVED, f"for {primary.code}")
                return True
            failed = result.failed if result else []
            code = next((f.code for f in failed if f.session_id == b.session_id), "unknown")
            self._log("backup", b, REFUSED, f"for {primary.code}: {FAILURE_TEXT.get(code, code)}")
        return False

    def _clashes_with_held(self, backup: PlanItem, replacing: PlanItem) -> bool:
        """Overlaps something you hold, or can't be reached from/to it in time."""
        for sid in self.held:
            other = self.plan.known(sid)
            if (
                other
                and other.session_id != replacing.session_id
                and (backup.overlaps(other) or backup.too_close(other))
            ):
                return True
        return False

    def _log(self, action: str, item: PlanItem, result: str, detail: str = "") -> None:
        self.report.log.append(
            {
                "seconds": round(self.clock() - self.report.started_at),
                "action": action,
                "session_id": item.session_id,
                "code": item.code,
                "title": item.title,
                "result": result,
                "detail": detail,
            }
        )

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


# --- dry run -------------------------------------------------------------------


class DryRunClient:
    """Stands in for the Events API in a dry run: reads are real, reserving is worked out.

    ``reserve_sessions`` answers as the API would from your real schedule and live seats.
    Only the calls the runner needs exist here, so a dry run has no way to change anything.
    """

    def __init__(
        self, client, event_id: str, plan: ReservationPlan | None, pretend_full: Iterable[str] = ()
    ):
        self.pretend_full = set(pretend_full)
        self.real = list(client.get_schedule(event_id).reserved)
        self.added: list[str] = []
        self.client, self.event_id = client, event_id
        # Only what the plan and your schedule need, not the whole catalog. The read above
        # has already refreshed the sign-in, so these can go side by side.
        ids = {*(i.session_id for i in plan.items), *self.real} if plan else set()
        with ThreadPoolExecutor(max_workers=6) as pool:
            found = pool.map(self._session, ids)
        self.sessions = {s.session_id: s for s in found if s}

    def _session(self, session_id: str):
        try:
            return self.client.get_session(self.event_id, session_id)
        except EventsApiError as e:
            if e.status != 404:  # 404: withdrawn from the catalog
                raise
            return None

    def get_schedule(self, event_id: str) -> Schedule:
        return Schedule(reserved=[*self.real, *self.added])

    def reserve_sessions(self, event_id: str, session_ids: Iterable[str]) -> BulkResult:
        ok, failed = [], []
        for sid in session_ids:
            code = self._refusal(sid)
            if code:
                failed.append({"sessionId": sid, "code": code})
            else:
                self.added.append(sid)
                ok.append(sid)
        return BulkResult.model_validate({"successful": ok, "failed": failed})

    def _refusal(self, sid: str) -> str | None:
        if sid in [*self.real, *self.added]:
            return "alreadyScheduled"
        if sid in self.pretend_full:
            return "sessionFull"
        s = self.sessions.get(sid)
        if s is None:
            return "sessionNotReservable"  # withdrawn since the plan was built
        if self._overlaps_held(s):
            return "scheduleConflict"
        if not s.is_reservable or s.seat_availability == "walkUp":
            return "sessionNotReservable"
        return "sessionFull" if s.seat_availability == "unavailable" else None

    def _overlaps_held(self, s) -> bool:
        for sid in [*self.real, *self.added]:
            h = self.sessions.get(sid)
            if not (h and s.start and s.end and h.start and h.end):
                continue
            if s.start < h.end and h.start < s.end:
                return True
        return False

    def seats(self, sid: str) -> str:
        """What the seat data said for this session, for the log."""
        if sid in self.pretend_full:
            return "pretending full"
        s = self.sessions.get(sid)
        if s is None:
            return "not in the catalog"
        return s.seat_availability or ("reservable" if s.is_reservable else "no reserved seating")


def _email_log(report: RunReport, notifier: Notifier | None) -> None:
    if notifier is None:
        report.notes.append("The log wasn't emailed: there's no confirmed notification email yet.")
        return
    try:
        body = f"{report.text()}\n\nFull log:\n{report.log_text() or '(no steps)'}"
        notifier.notify(report.subject(), body)
        report.notes.append("The full log was sent to your notification email.")
    except Exception as e:
        report.notes.append(f"Couldn't email the log: {e}")


def dry_run(
    client,
    event_id: str,
    plan: ReservationPlan | None,
    pretend_full: Iterable[str] = (),
    notifier: Notifier | None = None,
    label: str = "dry run",
    tz: str | None = None,
) -> RunReport:
    """The real run's steps with no reservations, then emails the full log to ``notifier``.
    Pacing is simulated, so it finishes at once and the log's times show how a real run
    would be spaced out."""

    class Clock:
        now = time.time()

        def __call__(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    clock = Clock()
    try:
        sim = DryRunClient(client, event_id, plan, pretend_full)
    except Exception as e:
        report = RunReport(label, clock(), "auth_failed", clock(), dry_run=True, tz=tz)
        report.notes.append(f"Could not read your schedule and seats (sign-in?): {e}")
        _email_log(report, notifier)
        return report
    runner = ReservationRunner(sim, event_id, ListNotifier(), clock=clock, sleep=clock.sleep, tz=tz)
    report = runner.run(plan, label, clock())
    report.dry_run = True
    for row in report.log:
        row["seats"] = sim.seats(row["session_id"])
    if plan and not plan.approved_at:
        report.notes.append("This plan isn't approved: a real run would stop until it is.")
    report.notes += [
        "Seats are as of now: a real run gets what's left when it runs, and waits if "
        "reservations are closed.",
    ]
    _email_log(report, notifier)
    return report
