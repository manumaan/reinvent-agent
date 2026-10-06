"""Conversational Q&A over the session catalog.

Claude (see ``llm`` for the endpoint) drives a small tool loop (SDK tool runner) with one tool,
``catalog_search``, and must cite session codes from what the tool returned.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from reinvent_agent.catalog.search import CatalogSearch, SearchFilters
from reinvent_agent.llm import make_client  # noqa: F401  (re-exported for callers)
from reinvent_agent.schedule import MySchedule, NotSignedIn

SYSTEM_PROMPT = """\
You help an attendee explore the AWS re:Invent {year} session catalog ({event_id}, \
Las Vegas, times are local Pacific time).

Answer only from sessions returned by the catalog_search tool. Search as many times as \
you need: rephrase, split multi-part questions, and use filters (level, day, venue, \
session type, AWS service names) when the question implies them. Level 100/200 are \
introductory; 300 advanced; 400/500 expert.

In your answer, cite each session by its code in square brackets, e.g. [SVS401], with \
its title, type, level, day, time and venue when known; add the sub-venue in \
parentheses when the tools give one, e.g. "Caesars Palace (Red Theater)". If nothing \
relevant turns up, say so plainly instead of stretching. Keep answers compact: a short \
lead sentence, then the sessions grouped sensibly.

The attendee's own favorites, reservations and personal time come from get_my_schedule \
(live from the AWS Events API). For any plan or itinerary built from their sessions, \
call plan_one_venue_per_day (or get_my_schedule for other layouts) rather than working \
out overlaps yourself, and present its result day by day: the venue, the sessions in \
time order, the free slots, and what could not fit and why. For questions about what \
runs in parallel at a venue (how many sessions or tracks at once, the busiest times), \
call venue_concurrency; a "track" is the session-code prefix such as AIM or SEC. Use \
the weekday and times exactly as the tools give them. If a tool says the attendee is \
not signed in, tell them to sign in with AWS Builder ID in the app sidebar (or \
`reinvent-agent auth login`)."""


@dataclass
class Answer:
    text: str
    cited: list[dict] = field(default_factory=list)  # search results the model saw


def thinking_config(model: str) -> dict | None:
    """Opus 4.7/4.8 run without thinking unless adaptive thinking is set explicitly;
    Haiku 4.5 predates adaptive thinking, so leave it off there."""
    return None if "haiku" in model else {"type": "adaptive"}


class CatalogQA:
    def __init__(
        self,
        search: CatalogSearch,
        client,
        model: str,
        event_id: str = "reinvent2026",
        schedule: MySchedule | None = None,
    ):
        self.search, self.client, self.model, self.event_id = search, client, model, event_id
        self.schedule = schedule

    def _tools(self, seen: dict[str, dict]):
        from anthropic import beta_tool

        search, event_id = self.search, self.event_id

        @beta_tool
        def catalog_search(
            query: str,
            min_level: int | None = None,
            max_level: int | None = None,
            days: list[str] | None = None,
            venues: list[str] | None = None,
            session_types: list[str] | None = None,
            services: list[str] | None = None,
            limit: int = 10,
        ) -> str:
            """Semantic search over re:Invent sessions, with optional filters.

            Args:
                query: What the sessions should be about, in natural language.
                min_level: Minimum level, e.g. 300 to skip introductory sessions.
                max_level: Maximum level, e.g. 200 for introductory sessions only.
                days: Dates to include, formatted YYYY-MM-DD (event runs 2026-11-30..12-04).
                venues: Venue names, e.g. "MGM Grand", "Caesars Forum", "Venetian".
                session_types: e.g. "Breakout session", "Chalk talk", "Workshop",
                    "Builders' session", "Code talk", "Lightning talk".
                services: Exact AWS service names, e.g. "Amazon Aurora", "AWS Lambda".
                limit: Number of sessions to return, 1-25.
            """
            filters = SearchFilters(
                event_id=event_id,
                min_level=min_level,
                max_level=max_level,
                days=days or [],
                venues=venues or [],
                types=session_types or [],
                services=services or [],
            )
            results = [r.summary() for r in search.search(query, filters, max(1, min(limit, 25)))]
            for r in results:
                seen[r["sessionId"]] = r
            return json.dumps(results) if results else "No matching sessions."

        if self.schedule is None:
            return [catalog_search]
        schedule = self.schedule

        def cite(rows: list[dict]) -> None:
            for r in rows:
                if r.get("code"):
                    seen[r["sessionId"]] = r

        @beta_tool
        def get_my_schedule(refresh: bool = False) -> str:
            """The attendee's favorited and reserved sessions (with day, time, venue) and
            personal time, from the AWS Events API.

            Args:
                refresh: Re-read from the API instead of the recent snapshot, e.g. after
                    the attendee says they just changed their favorites.
            """
            try:
                sched = schedule.load(refresh=refresh)
            except NotSignedIn:
                return "Not signed in: the attendee must sign in with AWS Builder ID first."
            favorites = [schedule.describe(i) for i in sched.favorites]
            reserved = [schedule.describe(i) for i in sched.reserved]
            cite(favorites + reserved)
            key = lambda r: (r.get("day") or "9", r.get("start") or "")  # noqa: E731
            return json.dumps(
                {
                    "favorites": sorted(favorites, key=key),
                    "reserved": sorted(reserved, key=key),
                    "personalTime": [
                        json.loads(p.model_dump_json(by_alias=True)) for p in sched.personal_time
                    ],
                }
            )

        @beta_tool
        def plan_one_venue_per_day(
            include_favorites: bool = True,
            include_reserved: bool = True,
            day_start: str = "08:00",
            day_end: str = "18:00",
        ) -> str:
            """Build a plan from the attendee's own sessions that stays at ONE venue each
            day. Per day it picks the venue where the most non-overlapping sessions fit
            (reserved sessions take priority), lists what could not fit (other venue or
            time overlap), and the free slots between sessions. `feasible` is true only
            if every session fits.

            Args:
                include_favorites: Plan over favorited sessions.
                include_reserved: Also plan over reserved sessions (kept first).
                day_start: Start of the day window for free slots, HH:MM.
                day_end: End of the day window for free slots, HH:MM.
            """
            from datetime import time as dtime

            from reinvent_agent.planner import plan_one_venue_per_day as plan

            try:
                sched = schedule.load()
            except NotSignedIn:
                return "Not signed in: the attendee must sign in with AWS Builder ID first."
            ids = (sched.favorites if include_favorites else []) + (
                sched.reserved if include_reserved else []
            )
            result = plan(
                schedule.sessions(list(dict.fromkeys(ids))),
                schedule.venue_of,
                reserved=sched.reserved if include_reserved else (),
                day_start=dtime.fromisoformat(day_start),
                day_end=dtime.fromisoformat(day_end),
            )
            for d in result.days:
                cite([schedule.describe(x.session_id) for x in d.sessions])
            return json.dumps(result.to_dict())

        @beta_tool
        def venue_concurrency(
            day: str | None = None, venue: str | None = None, at: str | None = None
        ) -> str:
            """How many sessions and tracks run at the same time, per venue.

            A track is the session-code prefix (AIM, SEC, DAT, ...); the catalog's own
            tracks field is empty. Without ``at``: per day and venue, the session/room/
            track counts and the busiest moment (peak parallel sessions and tracks); with
            ``day`` and ``venue`` also each 30-minute slot. With ``at``: what is running at
            that moment, per venue, grouped by track.

            Args:
                day: YYYY-MM-DD (event runs 2026-11-30..12-04); omit for all days.
                venue: e.g. "MGM Grand", "Wynn", "Caesars Forum"; omit for all venues.
                at: HH:MM local time, e.g. "10:30" (needs ``day``).
            """
            from collections import Counter

            from reinvent_agent.catalog import concurrency as cc

            sessions = list(schedule.catalog.values())
            labels = cc.track_labels(sessions)
            if at:
                if not day:
                    return "Give a day (YYYY-MM-DD) with a time."
                by_venue: dict[str, list] = {}
                for sess, v in cc.running_at(sessions, schedule.venue_of, day, at, venue):
                    by_venue.setdefault(v, []).append(sess)
                return json.dumps(
                    {
                        v: {
                            "sessions": len(ss),
                            "tracks": {
                                labels.get(t, t): n
                                for t, n in Counter(cc.track_of(x) for x in ss).most_common()
                            },
                            "codes": [x.code for x in ss],
                        }
                        for v, ss in sorted(by_venue.items())
                    }
                )
            rows = [
                r.__dict__
                for r in cc.overview(sessions, schedule.venue_of)
                if (not day or r.day == day) and (not venue or r.venue == venue)
            ]
            out: dict = {"venue_days": rows}
            if day and venue:
                out["slots"] = [
                    {
                        "start": sl.start,
                        "end": sl.end,
                        "sessions": len(sl.sessions),
                        "tracks": {labels.get(t, t): n for t, n in sl.tracks.most_common()},
                    }
                    for sl in cc.venue_day_slots(sessions, schedule.venue_of, day, venue)
                ]
            return json.dumps(out)

        return [catalog_search, get_my_schedule, plan_one_venue_per_day, venue_concurrency]

    def ask(self, question: str, history: list[dict] | None = None) -> Answer:
        seen: dict[str, dict] = {}
        year = self.event_id[-4:] if self.event_id[-4:].isdigit() else ""
        extra = {}
        if (thinking := thinking_config(self.model)) is not None:
            extra["thinking"] = thinking
        runner = self.client.beta.messages.tool_runner(
            model=self.model,
            max_tokens=16000,
            **extra,
            system=SYSTEM_PROMPT.format(year=year, event_id=self.event_id),
            tools=self._tools(seen),
            messages=[*(history or []), {"role": "user", "content": question}],
        )
        final = None
        for message in runner:
            final = message
        text = "".join(b.text for b in final.content if b.type == "text") if final else ""
        if final is not None and final.stop_reason == "refusal":
            text = text or "The model declined to answer this request."
        cited = [r for sid, r in seen.items() if f"[{r['code']}]" in text]
        return Answer(text=text, cited=cited)
