"""Catalog facets for the Search filter panel (like the re:Invent portal's).

Within one facet a session matches if it has **any** selected value; across facets it
must match **all** of them. Text filters (id, title, abstract) are case-insensitive
substrings. Works on full ``Session`` objects, so it covers fields the vector index
doesn't carry (speakers, roles, industries, features, abstract).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import time

from reinvent_agent.catalog.concurrency import track_of
from reinvent_agent.events_api.models import Session

# facet name -> how to read its values from a session (venue is supplied separately).
LIST_FACETS: dict[str, Callable[[Session], list[str]]] = {
    "Speakers": lambda s: s.speaker_names,
    "Topics": lambda s: s.topics,
    "Services": lambda s: s.services,
    "Interests": lambda s: s.areas_of_interest,
    "Role": lambda s: s.roles,
    "Industry": lambda s: s.industries,
    "Features": lambda s: s.features,
}


def facet_values(s: Session, name: str, venue_of: Callable[[Session], str | None]) -> list:
    if name == "Level":
        return [s.level_number] if s.level_number else []
    if name == "Type":
        return [s.type] if s.type else []
    if name == "Day":
        return [s.day.isoformat()] if s.day else []
    if name == "Venue":
        v = venue_of(s)
        return [v] if v else []
    if name == "Track":
        return [track_of(s)]
    return LIST_FACETS[name](s)


def options(
    sessions: Iterable[Session], name: str, venue_of: Callable[[Session], str | None]
) -> Counter:
    """Each value of a facet with how many sessions have it."""
    return Counter(v for s in sessions for v in set(facet_values(s, name, venue_of)))


@dataclass
class Filters:
    code: str = ""
    title: str = ""
    abstract: str = ""
    selected: dict[str, list] = field(default_factory=dict)  # facet -> any-of values
    start_from: time | None = None
    start_to: time | None = None
    only_ids: set[str] | None = None  # e.g. your favorites

    @property
    def active(self) -> int:
        n = sum(bool(x) for x in (self.code, self.title, self.abstract))
        n += sum(bool(v) for v in self.selected.values())
        n += self.start_from is not None or self.start_to is not None
        return n + (self.only_ids is not None)

    def key(self) -> tuple:
        return (
            self.code,
            self.title,
            self.abstract,
            tuple(sorted((k, tuple(v)) for k, v in self.selected.items() if v)),
            self.start_from,
            self.start_to,
            None if self.only_ids is None else len(self.only_ids),
        )

    def matches(self, s: Session, venue_of: Callable[[Session], str | None]) -> bool:
        if self.only_ids is not None and s.session_id not in self.only_ids:
            return False
        if self.code and self.code.lower() not in (s.code or "").lower():
            return False
        if self.title and self.title.lower() not in s.title.lower():
            return False
        if self.abstract and self.abstract.lower() not in (s.abstract or "").lower():
            return False
        if self.start_from or self.start_to:
            if s.start is None:
                return False
            t = s.start.time()
            if self.start_from and t < self.start_from:
                return False
            if self.start_to and t > self.start_to:
                return False
        for name, wanted in self.selected.items():
            if wanted and not set(wanted) & set(facet_values(s, name, venue_of)):
                return False
        return True


def apply(
    sessions: Iterable[Session], filters: Filters, venue_of: Callable[[Session], str | None]
) -> list[Session]:
    return [s for s in sessions if filters.matches(s, venue_of)]


GROUPS = {
    "Day": lambda s, v: s.day.strftime("%a %b %-d") if s.day else "No fixed time",
    "Venue": lambda s, v: v(s) or "unknown",
    "Type": lambda s, v: s.type or "unknown",
    "Level": lambda s, v: str(s.level_number or "—"),
    "Track": lambda s, v: track_of(s),
    "Topic": lambda s, v: s.topics[0] if s.topics else "—",
}
