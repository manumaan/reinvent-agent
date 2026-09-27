"""Deterministic schedule planning over a set of chosen sessions (e.g. favorites).

The LLM should not solve scheduling puzzles itself (DESIGN.md §3), so the agent calls
these functions as tools and explains the result.

``plan_one_venue_per_day``: for each day, pick the single venue where the most
non-overlapping chosen sessions fit (reserved sessions first, then count, then minutes
in sessions), and report what could not fit plus the free slots in between.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, time

from reinvent_agent.events_api.models import Session

MIN_GAP_MINUTES = 30  # shorter free stretches are not worth listing


@dataclass
class Slot:
    code: str
    session_id: str
    title: str
    start: str  # HH:MM
    end: str
    venue: str | None
    reserved: bool = False


@dataclass
class Gap:
    start: str
    end: str
    minutes: int
    # Chosen sessions that fall in this gap but are at another venue (or overlap).
    elsewhere: list[str] = field(default_factory=list)


@dataclass
class DayPlan:
    day: str
    weekday: str
    venue: str | None
    sessions: list[Slot]
    not_scheduled: list[dict]  # {code, title, start, end, venue, reason}
    free_slots: list[Gap]
    all_fit: bool  # every chosen session that day fits at this one venue


@dataclass
class Plan:
    days: list[DayPlan]
    feasible: bool  # every day is all_fit
    unplaceable: list[dict]  # no time or venue: cannot be planned
    summary: str

    def to_dict(self) -> dict:
        return asdict(self)


def _hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _slot(s: Session, venue: str | None, reserved: set[str]) -> Slot:
    return Slot(
        code=s.code,
        session_id=s.session_id,
        title=s.title,
        start=_hhmm(s.start),
        end=_hhmm(s.end),
        venue=venue,
        reserved=s.session_id in reserved,
    )


def best_non_overlapping(
    sessions: list[Session], reserved: set[str], buffer_minutes: int = 0
) -> list[Session]:
    """Weighted interval scheduling. Score, compared lexicographically:
    (reserved sessions kept, sessions kept, minutes in sessions)."""
    items = sorted(sessions, key=lambda s: (s.end, s.start))
    n = len(items)

    def weight(s: Session) -> tuple[int, int, int]:
        return (int(s.session_id in reserved), 1, int(s.duration.total_seconds() // 60))

    def add(a, b):
        return tuple(x + y for x, y in zip(a, b, strict=True))

    # prev[i]: number of items (prefix length) that end before items[i] starts.
    prev = []
    for i, s in enumerate(items):
        j = i
        while j > 0 and (items[j - 1].end.timestamp() + buffer_minutes * 60 > s.start.timestamp()):
            j -= 1
        prev.append(j)
    best: list[tuple[int, int, int]] = [(0, 0, 0)] * (n + 1)
    take: list[bool] = [False] * (n + 1)
    for i in range(1, n + 1):
        with_i = add(best[prev[i - 1]], weight(items[i - 1]))
        if with_i > best[i - 1]:
            best[i], take[i] = with_i, True
        else:
            best[i] = best[i - 1]
    chosen, i = [], n
    while i > 0:
        if take[i]:
            chosen.append(items[i - 1])
            i = prev[i - 1]
        else:
            i -= 1
    return sorted(chosen, key=lambda s: s.start)


def _gaps(
    chosen: list[Session], day_sessions: list[Session], day_start: time, day_end: time
) -> list[Gap]:
    if not day_sessions:
        return []
    day = day_sessions[0].start.date()
    lo = min([datetime.combine(day, day_start), *(s.start for s in chosen)])
    hi = max([datetime.combine(day, day_end), *(s.end for s in chosen)])
    edges = [lo, *[t for s in chosen for t in (s.start, s.end)], hi]
    chosen_ids = {s.session_id for s in chosen}
    gaps = []
    for a, b in zip(edges[::2], edges[1::2], strict=True):
        minutes = int((b - a).total_seconds() // 60)
        if minutes < MIN_GAP_MINUTES:
            continue
        elsewhere = [
            s.code
            for s in day_sessions
            if s.session_id not in chosen_ids and s.start < b and s.end > a
        ]
        gaps.append(Gap(_hhmm(a), _hhmm(b), minutes, elsewhere))
    return gaps


def plan_one_venue_per_day(
    sessions: Iterable[Session],
    venue_of: Callable[[Session], str | None],
    reserved: Iterable[str] = (),
    day_start: time = time(8, 0),
    day_end: time = time(18, 0),
) -> Plan:
    reserved = set(reserved)
    by_day: dict[str, list[Session]] = defaultdict(list)
    unplaceable = []
    for s in sessions:
        venue = venue_of(s)
        if s.start is None or s.end is None or s.is_all_day_session:
            unplaceable.append({"code": s.code, "title": s.title, "reason": "no fixed time"})
        elif not venue:
            unplaceable.append({"code": s.code, "title": s.title, "reason": "venue unknown"})
        else:
            by_day[s.day.isoformat()].append(s)

    days = []
    for day in sorted(by_day):
        day_sessions = sorted(by_day[day], key=lambda s: s.start)
        by_venue: dict[str, list[Session]] = defaultdict(list)
        for s in day_sessions:
            by_venue[venue_of(s)].append(s)

        def score(chosen):
            return (
                sum(s.session_id in reserved for s in chosen),
                len(chosen),
                sum(s.duration.total_seconds() for s in chosen),
            )

        options = {v: best_non_overlapping(ss, reserved) for v, ss in by_venue.items()}
        venue = max(sorted(options), key=lambda v: score(options[v]))
        chosen = options[venue]
        chosen_ids = {s.session_id for s in chosen}
        not_scheduled = [
            {
                "code": s.code,
                "title": s.title,
                "start": _hhmm(s.start),
                "end": _hhmm(s.end),
                "venue": venue_of(s),
                "reason": "other venue" if venue_of(s) != venue else "time overlap",
            }
            for s in day_sessions
            if s.session_id not in chosen_ids
        ]
        days.append(
            DayPlan(
                day=day,
                weekday=day_sessions[0].start.strftime("%A"),
                venue=venue,
                sessions=[_slot(s, venue, reserved) for s in chosen],
                not_scheduled=not_scheduled,
                free_slots=_gaps(chosen, day_sessions, day_start, day_end),
                all_fit=not not_scheduled,
            )
        )

    feasible = all(d.all_fit for d in days) and not unplaceable
    kept = sum(len(d.sessions) for d in days)
    total = kept + sum(len(d.not_scheduled) for d in days) + len(unplaceable)
    summary = (
        f"{'All' if feasible else kept} of {total} chosen sessions fit with one venue per day."
        if total
        else "No sessions to plan."
    )
    return Plan(days=days, feasible=feasible, unplaceable=unplaceable, summary=summary)
