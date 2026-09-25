"""Fill in missing session venues from room names.

In the re:Invent 2026 catalog about 40% of sessions have no ``venue`` but do have
a ``room`` such as ``"Level 3 | Chairman's 363 | Content Hub | Red Theater"``.
Room names identify buildings, so we learn room-name -> venue from the sessions
that do carry a venue, then apply it to the rest. A room that names a venue
outright (``"Caesars Palace | Promenade South | ..."``) wins over learned votes.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from reinvent_agent.events_api.models import Session

# Known re:Invent campus venues and spellings that may appear inside room strings.
VENUE_ALIASES: dict[str, str] = {
    "mgm grand": "MGM Grand",
    "caesars forum": "Caesars Forum",
    "caesars palace": "Caesars Palace",
    "venetian": "Venetian",
    "palazzo": "Venetian",
    "wynn": "Wynn",
    "encore": "Wynn",
    "mandalay bay": "Mandalay Bay",
    "aria": "ARIA",
}

# Room-name tokens that say nothing about the building.
_GENERIC = re.compile(r"^(level|floor|room|content hub|expo|hall|theater|theatre|lobby)\b")
_LEVEL = re.compile(r"^(level|floor)\s*-?\d+$", re.I)


def room_tokens(room: str) -> list[str]:
    """``"Level 3 | Chairman's 363 | Content Hub"`` -> ``["chairman's"]``.

    Each ``|``- or comma-separated part minus trailing room numbers, lower-cased, skipping
    floor markers and generic words.
    """
    tokens = []
    for part in re.split(r"[|,]", room):
        part = part.strip()
        if not part or _LEVEL.match(part):
            continue
        token = re.sub(r"[\s\d\-–]+$", "", part).strip().lower()
        if token and not _GENERIC.match(token):
            tokens.append(token)
    return tokens


def venue_named_in(room: str) -> str | None:
    low = room.lower()
    for alias, venue in sorted(VENUE_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(alias)}\b", low):
            return venue
    return None


@dataclass
class VenueInferrer:
    token_venue: dict[str, str]

    @classmethod
    def learn(
        cls, sessions: Iterable[Session], min_count: int = 2, min_purity: float = 0.9
    ) -> VenueInferrer:
        votes: dict[str, Counter] = defaultdict(Counter)
        for s in sessions:
            if s.venue and s.room:
                for tok in room_tokens(s.room):
                    votes[tok][s.venue] += 1
        mapping = {}
        for tok, counter in votes.items():
            venue, n = counter.most_common(1)[0]
            total = sum(counter.values())
            if total >= min_count and n / total >= min_purity:
                mapping[tok] = venue
        return cls(mapping)

    def infer(self, session: Session) -> tuple[str | None, str]:
        """Returns ``(venue, source)``; source is api, room-name, room-learned or none."""
        if session.venue:
            return session.venue, "api"
        if not session.room:
            return None, "none"
        named = venue_named_in(session.room)
        if named:
            return named, "room-name"
        votes = Counter(
            self.token_venue[t] for t in room_tokens(session.room) if t in self.token_venue
        )
        if not votes:
            return None, "none"
        (venue, n), *rest = votes.most_common()
        if rest and rest[0][1] == n:  # tie between venues: don't guess
            return None, "none"
        return venue, "room-learned"
