"""Conversational Q&A over the session catalog.

Claude on Amazon Bedrock drives a small tool loop (SDK tool runner) with one tool,
``catalog_search``, and must cite session codes from what the tool returned.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from reinvent_agent.catalog.search import CatalogSearch, SearchFilters

SYSTEM_PROMPT = """\
You help an attendee explore the AWS re:Invent {year} session catalog ({event_id}, \
Las Vegas, times are local Pacific time).

Answer only from sessions returned by the catalog_search tool. Search as many times as \
you need: rephrase, split multi-part questions, and use filters (level, day, venue, \
session type, AWS service names) when the question implies them. Level 100/200 are \
introductory; 300 advanced; 400/500 expert.

In your answer, cite each session by its code in square brackets, e.g. [SVS401], with \
its title, type, level, day, time and venue when known. If nothing relevant turns up, \
say so plainly instead of stretching. Keep answers compact: a short lead sentence, then \
the sessions grouped sensibly."""


@dataclass
class Answer:
    text: str
    cited: list[dict] = field(default_factory=list)  # search results the model saw


def make_client(region: str):
    from anthropic import AnthropicBedrockMantle

    return AnthropicBedrockMantle(aws_region=region)


class CatalogQA:
    def __init__(self, search: CatalogSearch, client, model: str, event_id: str = "reinvent2026"):
        self.search, self.client, self.model, self.event_id = search, client, model, event_id

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

        return [catalog_search]

    def ask(self, question: str, history: list[dict] | None = None) -> Answer:
        seen: dict[str, dict] = {}
        year = self.event_id[-4:] if self.event_id[-4:].isdigit() else ""
        runner = self.client.beta.messages.tool_runner(
            model=self.model,
            max_tokens=16000,
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
