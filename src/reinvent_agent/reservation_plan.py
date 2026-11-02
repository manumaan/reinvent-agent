"""Reservation plans: what to reserve when seats release, in what order, with backups.

A plan lists **primary** sessions (never overlapping each other: the API would refuse
the second with ``scheduleConflict``) and **backup** sessions. A backup covers every
primary it overlaps in time: if that primary is full or can't be reserved, the run
tries its backups in order. ``priority`` (1 must-have, 2 want, 3 nice) decides the
reservation order, because seats go to whoever asks first.

Nothing is reserved from a draft: only an **approved** plan is used by the scheduled
run. Plans and run reports live in the DynamoDB PlansTable (``userId`` + ``sk``).
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterable
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field

from reinvent_agent.catalog.venues import sub_venue
from reinvent_agent.events_api.models import Session
from reinvent_agent.travel import travel_minutes

PRIMARY, BACKUP = "primary", "backup"


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


PRIORITIES = {1: "must-have", 2: "want", 3: "nice to have"}
STRATEGIES = {
    "max_sessions": "Most sessions (any venue)",
    "one_venue": "One venue per day",
}


@dataclass
class PlanItem:
    session_id: str
    code: str
    title: str
    day: str | None
    start: str | None  # HH:MM local
    end: str | None
    venue: str | None
    role: str = BACKUP
    priority: int = 2
    backup_for: list[str] = field(default_factory=list)  # primary session IDs covered
    sub_venue: str | None = None  # e.g. "Red Theater"

    def overlaps(self, other: PlanItem) -> bool:
        return bool(
            self.day
            and self.day == other.day
            and self.start
            and self.end
            and other.start
            and other.end
            and self.start < other.end
            and other.start < self.end
        )

    def too_close(self, other: PlanItem) -> tuple[int, int] | None:
        """``(gap, travel)`` minutes when the two don't overlap but the gap between them
        is shorter than the estimated travel time between their venues; else None."""
        if not (
            self.day
            and self.day == other.day
            and self.start
            and self.end
            and other.start
            and other.end
        ) or self.overlaps(other):
            return None  # fmt: skip
        first, second = sorted((self, other), key=lambda i: i.start)
        gap = _minutes(second.start) - _minutes(first.end)
        travel = travel_minutes(first.venue, second.venue)
        return (gap, travel) if gap < travel else None

    def overlap_minutes(self, other: PlanItem) -> int:
        if not self.overlaps(other):
            return 0

        def minutes(hhmm: str) -> int:
            h, m = hhmm.split(":")
            return int(h) * 60 + int(m)

        lo = max(minutes(self.start), minutes(other.start))
        hi = min(minutes(self.end), minutes(other.end))
        return hi - lo


@dataclass
class ReservationPlan:
    event_id: str
    items: list[PlanItem]
    strategy: str = "custom"
    source: str = "favorites"  # favorites | shortlist | both: what the draft was built from
    version: int = 0
    created_at: float = field(default_factory=time.time)
    approved_at: float | None = None
    # Reservations you hold that aren't in this list: not shown or booked, but the plan
    # and the run's backups must not clash with them.
    held: list[PlanItem] = field(default_factory=list)

    @property
    def primaries(self) -> list[PlanItem]:
        """Reservation order: priority first, then chronological."""
        return sorted(
            (i for i in self.items if i.role == PRIMARY),
            key=lambda i: (i.priority, i.day or "9", i.start or ""),
        )

    def item(self, session_id: str) -> PlanItem | None:
        return next((i for i in self.items if i.session_id == session_id), None)

    def known(self, session_id: str) -> PlanItem | None:
        """A plan item, or a held reservation outside the list."""
        return self.item(session_id) or next(
            (h for h in self.held if h.session_id == session_id), None
        )

    def backups_for(self, primary_id: str) -> list[PlanItem]:
        """Backups covering a primary: same venue first (keeps a one-venue day intact
        and saves walking), then most overlap, then priority, then time."""
        primary = self.item(primary_id)
        cands = [i for i in self.items if i.role == BACKUP and primary_id in i.backup_for]
        return sorted(
            cands,
            key=lambda b: (
                bool(primary) and b.venue != primary.venue,
                -b.overlap_minutes(primary) if primary else 0,
                b.priority,
                b.start or "",
            ),
        )

    def link_backups(self) -> None:
        primaries = [i for i in self.items if i.role == PRIMARY]
        for b in self.items:
            b.backup_for = (
                [p.session_id for p in primaries if b.overlaps(p)] if b.role == BACKUP else []
            )

    def problems(self) -> list[str]:
        """Reasons the plan can't be approved as is."""
        out = []
        primaries = [i for i in self.items if i.role == PRIMARY]
        if not primaries:
            out.append("Nothing to reserve: mark at least one session as 🎟️ reserve.")
        for i, a in enumerate(primaries):
            for b in primaries[i + 1 :]:
                if a.overlaps(b):
                    out.append(
                        f"{a.code} and {b.code} overlap on {a.day}: make one of them a backup."
                    )
            for h in self.held:
                if a.overlaps(h):
                    out.append(
                        f"{a.code} overlaps {h.code} on {a.day}, which you've reserved: "
                        "make it a backup, or unreserve the other session first."
                    )
        return out

    def tight_transfers(self) -> list[tuple[PlanItem, PlanItem, int, int]]:
        """Consecutive reserve sessions you can't reach in time: (from, to, gap, travel)."""
        out = []
        by_day: dict[str, list[PlanItem]] = {}
        held = {h.session_id for h in self.held}
        for p in [*self.items, *self.held]:
            if p.role == PRIMARY and p.day and p.start:
                by_day.setdefault(p.day, []).append(p)
        for items in by_day.values():
            items.sort(key=lambda p: p.start)
            for a, b in zip(items, items[1:], strict=False):
                both_held = a.session_id in held and b.session_id in held
                if not both_held and (hit := a.too_close(b)):
                    out.append((a, b, *hit))
        return out

    def orphan_backups(self) -> list[PlanItem]:
        return [i for i in self.items if i.role == BACKUP and not i.backup_for]

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> ReservationPlan:
        data = json.loads(text)
        data["items"] = [PlanItem(**i) for i in data["items"]]
        data["held"] = [PlanItem(**i) for i in data.get("held", [])]
        return cls(**data)


def plan_item(s: Session, venue: str | None, role: str = BACKUP, priority: int = 2) -> PlanItem:
    return PlanItem(
        session_id=s.session_id,
        code=s.code,
        title=s.title,
        day=s.day.isoformat() if s.day else None,
        start=s.start.strftime("%H:%M") if s.start else None,
        end=s.end.strftime("%H:%M") if s.end else None,
        venue=venue,
        role=role,
        priority=priority,
        sub_venue=sub_venue(s.room),
    )


def build_plan(
    event_id: str,
    sessions: Iterable[Session],
    venue_of: Callable[[Session], str | None],
    reserved: Iterable[str] = (),
    strategy: str = "max_sessions",
    held: Iterable[Session] = (),
) -> ReservationPlan:
    """Draft plan listing exactly ``sessions`` (those already reserved stay primaries).

    ``max_sessions``: per day the largest non-overlapping set, any venue.
    ``one_venue``: per day the best single venue (see planner.plan_one_venue_per_day).
    Everything else becomes a backup for the primaries it overlaps.

    ``held``: reservations outside the list. They aren't listed, but no primary may
    clash with them (time or travel), and a one-venue day stays at their venue.
    """
    from reinvent_agent.planner import best_non_overlapping, plan_one_venue_per_day

    sessions = list({s.session_id: s for s in sessions}.values())
    reserved = set(reserved)
    timed = [s for s in sessions if s.start and s.end and not s.is_all_day_session]
    held = [h for h in held if h.session_id not in {s.session_id for s in sessions}]
    held_timed = [h for h in held if h.start and h.end]

    def clashes_with_held(s: Session) -> bool:
        for h in held_timed:
            if h.day != s.day:
                continue
            first, second = sorted((s, h), key=lambda x: x.start)
            gap = (second.start - first.end).total_seconds() / 60
            if gap < travel_minutes(venue_of(s), venue_of(h)):  # negative gap: overlap
                return True
        return False

    timed = [s for s in timed if s.session_id in reserved or not clashes_with_held(s)]
    if strategy == "one_venue":
        venues_held: dict = {}
        for h in held_timed:
            venues_held.setdefault(h.day, set()).add(venue_of(h))
        timed = [
            s
            for s in timed
            if s.session_id in reserved
            or s.day not in venues_held
            or venue_of(s) in venues_held[s.day]
        ]
    if strategy == "one_venue":
        plan = plan_one_venue_per_day(timed, venue_of, reserved)
        chosen = {x.session_id for d in plan.days for x in d.sessions}
    elif strategy == "max_sessions":
        by_day: dict[str, list[Session]] = {}
        for s in timed:
            by_day.setdefault(s.day.isoformat(), []).append(s)
        chosen = {
            x.session_id
            for ss in by_day.values()
            # any venue: leave time to get from one venue to the next
            for x in best_non_overlapping(ss, reserved, venue_of=venue_of)
        }
    else:
        raise ValueError(f"strategy must be one of {sorted(STRATEGIES)}")
    chosen |= reserved & {s.session_id for s in sessions}
    items = [
        plan_item(
            s,
            venue_of(s),
            PRIMARY if s.session_id in chosen else BACKUP,
            1 if s.session_id in reserved else 2,
        )
        for s in sorted(sessions, key=lambda s: (s.start is None, s.start or 0, s.code))
    ]
    out = ReservationPlan(
        event_id=event_id,
        items=items,
        strategy=strategy,
        held=[plan_item(h, venue_of(h), PRIMARY, 1) for h in held],
    )
    out.link_backups()
    return out


# --- storage (DynamoDB PlansTable) -------------------------------------------


class PlanStore:
    """Plans and run reports for one attendee (``user_id`` = Builder ID token ``sub``)."""

    def __init__(self, table, user_id: str, event_id: str):
        self.table, self.user_id, self.event_id = table, user_id, event_id

    def _key(self, sk: str) -> dict:
        return {"userId": self.user_id, "sk": sk}

    def _get(self, sk: str) -> str | None:
        item = self.table.get_item(Key=self._key(sk)).get("Item")
        return item["body"] if item else None

    def _put(self, sk: str, body: str, **extra) -> None:
        self.table.put_item(Item={**self._key(sk), "body": body, **extra})

    def draft(self) -> ReservationPlan | None:
        body = self._get(f"plan#{self.event_id}#draft")
        return ReservationPlan.from_json(body) if body else None

    def save_draft(self, plan: ReservationPlan) -> None:
        self._put(f"plan#{self.event_id}#draft", plan.to_json())

    def approved(self) -> ReservationPlan | None:
        body = self._get(f"plan#{self.event_id}#approved")
        return ReservationPlan.from_json(body) if body else None

    def approve(self, plan: ReservationPlan, now: float | None = None) -> ReservationPlan:
        problems = plan.problems()
        if problems:
            raise ValueError("; ".join(problems))
        current = self.approved()
        plan.version = (current.version if current else 0) + 1
        plan.approved_at = now or time.time()
        body = plan.to_json()
        self._put(f"plan#{self.event_id}#v{plan.version:04d}", body)  # history
        self._put(f"plan#{self.event_id}#approved", body)
        self.clear_flag("api-run-done")  # a newly approved plan gets reserved again
        return plan

    def profile_json(self) -> str | None:
        """The attendee profile (see ``reinvent_agent.profile``), as JSON."""
        return self._get(f"profile#{self.event_id}")

    def save_profile_json(self, body: str) -> None:
        self._put(f"profile#{self.event_id}", body)

    def travel_json(self) -> str | None:
        """Travel-time estimates edited on the Setup tab (see ``reinvent_agent.travel``)."""
        return self._get(f"travel#{self.event_id}")

    def save_travel_json(self, body: str) -> None:
        self._put(f"travel#{self.event_id}", body)

    def clear_travel(self) -> None:
        self.table.delete_item(Key=self._key(f"travel#{self.event_id}"))

    def flag(self, name: str) -> float | None:
        """Epoch seconds a flag was set (e.g. "api-run-done", "alert"), or None."""
        body = self._get(f"flag#{self.event_id}#{name}")
        return float(body) if body else None

    def set_flag(self, name: str, at: float | None = None) -> None:
        self._put(f"flag#{self.event_id}#{name}", str(at or time.time()))

    def clear_flag(self, name: str) -> None:
        self.table.delete_item(Key=self._key(f"flag#{self.event_id}#{name}"))

    def withdraw(self) -> None:
        self.table.delete_item(Key=self._key(f"plan#{self.event_id}#approved"))

    def save_run(self, report: dict) -> None:
        sk = f"run#{self.event_id}#{report['started_at']:.0f}#{report.get('label', '')}"
        self._put(sk, json.dumps(report))

    def runs(self, limit: int = 10) -> list[dict]:
        from boto3.dynamodb.conditions import Key

        resp = self.table.query(
            KeyConditionExpression=Key("userId").eq(self.user_id)
            & Key("sk").begins_with(f"run#{self.event_id}#"),
            ScanIndexForward=False,
            Limit=limit,
        )
        return [json.loads(i["body"]) for i in resp.get("Items", [])]


class LeaseBusy(RuntimeError):
    pass


def dynamo_lease(table, name: str = "tokens", ttl: float = 30, wait: float = 20, sleep=time.sleep):
    """Context-manager factory: a short cross-process lease on a PlansTable item.

    Used around Builder ID token refreshes so the laptop and the Lambda never refresh
    the same rotating refresh token at once.
    """

    @contextmanager
    def lease():
        owner = uuid.uuid4().hex
        key = {"userId": "_lock", "sk": name}
        deadline = time.time() + wait
        while True:
            now = time.time()
            try:
                table.put_item(
                    Item={**key, "owner": owner, "expires": int(now + ttl)},
                    ConditionExpression="attribute_not_exists(userId) OR expires < :now",
                    ExpressionAttributeValues={":now": int(now)},
                )
                break
            except Exception as e:  # ConditionalCheckFailedException: someone holds it
                if "ConditionalCheckFailed" not in type(e).__name__ + str(e):
                    raise
                if time.time() > deadline:
                    raise LeaseBusy(f"lease {name!r} busy") from None
                sleep(0.5)
        try:
            yield
        finally:
            with suppress(Exception):  # expired and taken over: nothing to release
                table.delete_item(
                    Key=key,
                    ConditionExpression="#o = :me",
                    ExpressionAttributeNames={"#o": "owner"},
                    ExpressionAttributeValues={":me": owner},
                )

    return lease
