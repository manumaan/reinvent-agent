"""Build a short list of sessions from the attendee's profile.

Deterministic scoring over the catalog (every point comes with a reason shown on the
card), plus semantic matches for the free-text answers, plus an optional Claude pass
that re-ranks the best candidates against the whole profile, including constraints
that rules can't read ("no workshops before 10", "prefer sessions near the Venetian").

Scoring (additive):
* AWS service you **use** in a **300-400** session: +6 (500: +4)
* AWS service you want to **learn** in a **100-200** session: +6 (300: +2)
* topic of interest +3 · strategic interest +2 each (max 2) · role +2 · industry +3
* free-text answers (stack, architecture, projects, technologies to learn): semantic
  search; top 10 hits +4, top 25 +2, top 50 +1, per answer
* level within your experience band +1
* irrelevant topic -6; sessions whose topics are *all* irrelevant are dropped
* hard filters: days attending, session types (when given)
A session needs at least one content match (not just a level match) to be listed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from reinvent_agent.events_api.models import Session
from reinvent_agent.profile import EXPERIENCE_LEVELS, Profile

SEMANTIC_FIELDS = {
    "tech_stack": "your stack",
    "architecture": "your architecture areas",
    "projects": "your projects",
    "learn_other": "what you want to learn",
}


@dataclass
class Pick:
    session: Session
    score: float
    reasons: list[str] = field(default_factory=list)
    content: bool = False  # matched something beyond level/experience

    def add(self, points: float, reason: str, content: bool = True) -> None:
        self.score += points
        self.reasons.append(reason)
        self.content = self.content or content


def semantic_queries(profile: Profile) -> dict[str, str]:
    """Free-text answers worth a semantic search: field -> query text."""
    return {f: getattr(profile, f) for f in SEMANTIC_FIELDS if getattr(profile, f).strip()}


def rank_points(rank: int) -> int:
    return 4 if rank < 10 else 2 if rank < 25 else 1 if rank < 50 else 0


def score_sessions(
    profile: Profile,
    sessions: Iterable[Session],
    semantic: dict[str, list[str]] | None = None,
) -> list[Pick]:
    """Every session that passes the hard filters and matches something, best first.

    ``semantic``: for each free-text field, session IDs ranked by similarity.
    """
    semantic = semantic or {}
    ranks = {f: {sid: i for i, sid in enumerate(ids)} for f, ids in semantic.items()}
    using, learning = set(profile.aws_using), set(profile.aws_learning)
    band = EXPERIENCE_LEVELS.get(profile.experience or "", set())
    irrelevant = set(profile.irrelevant_topics)
    picks = []
    for s in sessions:
        if profile.days and (not s.day or s.day.isoformat() not in profile.days):
            continue
        if profile.session_types and s.type not in profile.session_types:
            continue
        if irrelevant and s.topics and set(s.topics) <= irrelevant:
            continue
        level = s.level_number
        p = Pick(s, 0.0)
        services = set(s.services)
        for svc in sorted(services & using):
            if level in (300, 400):
                p.add(6, f"{svc}, which you use, at {level}")
            elif level == 500:
                p.add(4, f"{svc}, which you use, at {level}")
        for svc in sorted(services & learning):
            if level in (100, 200):
                p.add(6, f"Intro to {svc}, which you want to learn ({level})")
            elif level == 300:
                p.add(2, f"{svc}, which you want to learn ({level})")
        if hit := sorted(set(s.topics) & set(profile.topics)):
            p.add(3, f"Topic: {', '.join(hit)}")
        for interest in sorted(set(s.areas_of_interest) & set(profile.interests))[:2]:
            p.add(2, f"Interest: {interest}")
        if hit := sorted(set(s.roles) & set(profile.roles)):
            p.add(2, f"For {', '.join(hit)}")
        if hit := sorted(set(s.industries) & set(profile.industries)):
            p.add(3, f"Industry: {', '.join(hit)}")
        for f, label in SEMANTIC_FIELDS.items():
            rank = ranks.get(f, {}).get(s.session_id)
            if rank is not None and (pts := rank_points(rank)):
                p.add(pts, f"Matches {label}")
        if band and level in band:
            p.add(1, f"Level {level} fits your experience", content=False)
        if hit := sorted(set(s.topics) & irrelevant):
            p.add(-6, f"Touches {', '.join(hit)} (marked irrelevant)", content=False)
        if p.content and p.score > 0:
            picks.append(p)
    return sorted(picks, key=lambda p: (-p.score, s_key(p.session)))


def s_key(s: Session):
    return (s.day.isoformat() if s.day else "9", s.start or 0, s.code)


# --- optional Claude re-ranking ---------------------------------------------------

REFINE_TOOL = {
    "name": "shortlist",
    "description": "Return the chosen sessions, best first, with a one-line reason each.",
    "input_schema": {
        "type": "object",
        "properties": {
            "picks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "reason": {"type": "string", "description": "<= 20 words"},
                    },
                    "required": ["code", "reason"],
                },
            }
        },
        "required": ["picks"],
    },
}


def candidate_text(p: Pick, venue_of: Callable[[Session], str | None]) -> dict:
    s = p.session
    return {
        "code": s.code,
        "title": s.title,
        "type": s.type,
        "level": s.level_number,
        "day": s.day.isoformat() if s.day else None,
        "time": f"{s.start:%H:%M}-{s.end:%H:%M}" if s.start and s.end else None,
        "venue": venue_of(s),
        "topics": s.topics,
        "services": s.services[:6],
        "abstract": (s.abstract or "")[:400],
        "rule_score": p.score,
    }


def refine_with_claude(
    client, model: str, profile: Profile, picks: list[Pick], venue_of, limit: int = 25
) -> list[tuple[str, str]]:
    """Ask Claude to choose ``limit`` sessions from the candidates. Returns (code, reason)."""
    candidates = [candidate_text(p, venue_of) for p in picks]
    prompt = (
        "You help an attendee pick AWS re:Invent 2026 sessions. Here is their profile:\n"
        f"{profile.describe()}\n\n"
        f"From these {len(candidates)} candidates (already pre-scored by rules), choose "
        f"the {limit} best for this person, best first. Honour their preferences and "
        "constraints and skip anything they'd find irrelevant. Prefer breadth over "
        "several near-identical sessions (e.g. repeats of the same talk). Only use codes "
        "from the list.\n\n" + json.dumps(candidates)
    )
    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        tools=[REFINE_TOOL],
        tool_choice={"type": "tool", "name": "shortlist"},
        messages=[{"role": "user", "content": prompt}],
    )
    block = next(b for b in resp.content if b.type == "tool_use")
    known = {c["code"] for c in candidates}
    return [(x["code"], x["reason"]) for x in block.input["picks"] if x["code"] in known][:limit]
