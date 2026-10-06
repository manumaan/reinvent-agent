"""What runs at the same time, per venue: parallel sessions and parallel tracks.

The Events API has a ``tracks`` field, but the re:Invent 2026 catalog leaves it empty
for every session. The session code carries the track instead: its letter prefix
(``AIM314-R`` -> ``AIM``) is the re:Invent track. ``track_labels`` names each prefix by
the topic most of its sessions share (``AIM`` -> "Artificial Intelligence"), computed
from the catalog rather than hard-coded.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from reinvent_agent.events_api.models import Session

SLOT_MINUTES = 30


def track_of(s: Session) -> str:
    """Track = the letter prefix of the session code (``SEC341`` -> ``SEC``)."""
    m = re.match(r"[A-Z]+", s.code or "")
    return m.group(0) if m else "Other"


def track_labels(sessions: Iterable[Session]) -> dict[str, str]:
    """``{"SEC": "SEC · Security & Identity", ...}``: each track's most distinctive
    topic -- the one over-represented most versus the whole catalog, among topics on at
    least a quarter of the track's sessions (plain "most common" would label half the
    tracks "Artificial Intelligence")."""
    sessions = list(sessions)
    overall = Counter(t for s in sessions for t in set(s.topics))
    sizes: Counter = Counter()
    by_track: dict[str, Counter] = defaultdict(Counter)
    for s in sessions:
        sizes[track_of(s)] += 1
        by_track[track_of(s)].update(set(s.topics))
    n = len(sessions) or 1
    labels = {}
    for track, counts in by_track.items():
        common = [(t, c) for t, c in counts.items() if c >= 0.25 * sizes[track]]
        if not common:
            labels[track] = track
            continue
        best = max(common, key=lambda tc: (tc[1] / sizes[track]) / (overall[tc[0]] / n))
        labels[track] = f"{track} · {best[0]}"
    return labels


def _timed(sessions: Iterable[Session], venue_of) -> list[tuple[Session, str]]:
    return [
        (s, v)
        for s in sessions
        if s.start and s.end and not s.is_all_day_session and (v := venue_of(s))
    ]


@dataclass
class Slot:
    start: str  # HH:MM
    end: str
    sessions: list[Session] = field(default_factory=list)

    @property
    def tracks(self) -> Counter:
        return Counter(track_of(s) for s in self.sessions)


def venue_day_slots(
    sessions: Iterable[Session],
    venue_of: Callable[[Session], str | None],
    day: str,
    venue: str,
    slot_minutes: int = SLOT_MINUTES,
) -> list[Slot]:
    """Sessions running in each slot (a session counts in every slot it overlaps)."""
    here = [s for s, v in _timed(sessions, venue_of) if v == venue and s.day.isoformat() == day]
    if not here:
        return []
    d = here[0].start.date()
    lo = min(s.start for s in here)
    lo = datetime.combine(d, time(lo.hour, 0 if lo.minute < 30 else 30))
    hi = max(s.end for s in here)
    slots, t, step = [], lo, timedelta(minutes=slot_minutes)
    while t < hi:
        slots.append(
            Slot(
                f"{t:%H:%M}",
                f"{t + step:%H:%M}",
                sorted((s for s in here if s.start < t + step and s.end > t), key=track_of),
            )
        )
        t += step
    return slots


@dataclass
class VenueDay:
    day: str
    venue: str
    sessions: int
    rooms: int
    tracks: int
    peak_sessions: int
    peak_tracks: int
    peak_at: str  # HH:MM of the busiest moment


def peak_at_instants(here: list[Session]) -> tuple[int, int, str]:
    """Busiest instant: (sessions running, distinct tracks running, HH:MM)."""
    best = (0, 0, "")
    for t in sorted({s.start for s in here}):
        running = [s for s in here if s.start <= t < s.end]
        cand = (len(running), len({track_of(s) for s in running}), f"{t:%H:%M}")
        best = max(best, cand, key=lambda x: (x[0], x[1]))
    return best


def overview(
    sessions: Iterable[Session], venue_of: Callable[[Session], str | None]
) -> list[VenueDay]:
    """Per day and venue: size, and how many sessions / tracks run at the peak."""
    groups: dict[tuple[str, str], list[Session]] = defaultdict(list)
    for s, v in _timed(sessions, venue_of):
        groups[(s.day.isoformat(), v)].append(s)
    out = []
    for (day, venue), here in sorted(groups.items()):
        n, tr, at = peak_at_instants(here)
        out.append(
            VenueDay(
                day=day,
                venue=venue,
                sessions=len(here),
                rooms=len({s.room for s in here if s.room}),
                tracks=len({track_of(s) for s in here}),
                peak_sessions=n,
                peak_tracks=tr,
                peak_at=at,
            )
        )
    return out


def running_at(
    sessions: Iterable[Session],
    venue_of: Callable[[Session], str | None],
    day: str,
    at: str,
    venue: str | None = None,
) -> list[tuple[Session, str]]:
    """Sessions in progress at ``at`` (HH:MM) on ``day``, optionally at one venue."""
    hh, mm = (int(x) for x in at.split(":"))
    return [
        (s, v)
        for s, v in _timed(sessions, venue_of)
        if s.day.isoformat() == day
        and (venue is None or v == venue)
        and s.start.time() <= time(hh, mm) < s.end.time()
    ]


def overlapping(
    target: Session,
    sessions: Iterable[Session],
    venue_of: Callable[[Session], str | None],
) -> list[tuple[Session, str]]:
    """Other sessions whose time overlaps ``target`` on the same day (any venue)."""
    if not (target.start and target.end):
        return []
    return sorted(
        (
            (s, v)
            for s, v in _timed(sessions, venue_of)
            if s.session_id != target.session_id
            and s.day == target.day
            and s.start < target.end
            and target.start < s.end
        ),
        key=lambda sv: (sv[1] != venue_of(target), sv[0].start, sv[0].code),
    )
