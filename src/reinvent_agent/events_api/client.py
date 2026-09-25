"""REST client for the AWS Events API."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any

import httpx

from reinvent_agent.events_api.auth import TokenProvider
from reinvent_agent.events_api.models import Event, PersonalTime, ReserveResult, Schedule, Session

BASE_URL = "https://api.awsevents.com/v1"
MAX_BATCH = 10  # AssociateFavorites and ReserveSessions take 1..10 distinct IDs


class EventsApiError(RuntimeError):
    def __init__(self, status: int, body: Any, method: str, path: str):
        self.status = status
        self.body = body
        super().__init__(f"{method} {path} -> {status}: {body}")


def _batches(ids: Iterable[str], size: int = MAX_BATCH) -> Iterator[list[str]]:
    unique = list(dict.fromkeys(ids))
    for i in range(0, len(unique), size):
        yield unique[i : i + size]


def _items(body: dict, *keys: str) -> list[dict]:
    for key in keys:
        if isinstance(body.get(key), list):
            return body[key]
    raise EventsApiError(200, body, "?", f"no list under any of {keys}")


class EventsApiClient:
    def __init__(
        self,
        tokens: TokenProvider | None = None,
        *,
        base_url: str = BASE_URL,
        http: httpx.Client | None = None,
        max_throttle_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.tokens = tokens
        self.http = http or httpx.Client(timeout=30)
        self.base_url = base_url.rstrip("/")
        self.max_throttle_retries = max_throttle_retries
        self._sleep = sleep

    # --- transport -----------------------------------------------------------

    def _request(self, method: str, path: str, *, auth: bool = True, **kwargs) -> Any:
        url = f"{self.base_url}{path}"
        refreshed = False
        throttles = 0
        while True:
            headers = kwargs.pop("headers", {}) or {}
            if auth and self.tokens is not None:
                headers["Authorization"] = f"Bearer {self.tokens.access_token()}"
            resp = self.http.request(method, url, headers=headers, **kwargs)

            if resp.status_code == 401 and auth and self.tokens is not None and not refreshed:
                self.tokens.force_refresh()
                refreshed = True
                continue
            if resp.status_code == 429 and throttles < self.max_throttle_retries:
                throttles += 1
                self._sleep(float(resp.headers.get("Retry-After", "1")))
                continue
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                except ValueError:
                    body = resp.text
                raise EventsApiError(resp.status_code, body, method, path)
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()

    # --- catalog -------------------------------------------------------------

    def list_events(self) -> list[Event]:
        body = self._request("GET", "/events", auth=False)
        return [Event.model_validate(e) for e in _items(body, "events", "items")]

    def get_event(self, event_id: str) -> Event:
        body = self._request("GET", f"/events/{event_id}", auth=False)
        return Event.model_validate(body.get("event", body))

    def iter_sessions(
        self, event_id: str, *, include_abstracts: bool = True, locale: str | None = None
    ) -> Iterator[Session]:
        params: dict[str, str] = {"includeAbstracts": str(include_abstracts).lower()}
        if locale:
            params["locale"] = locale
        next_token: str | None = None
        while True:
            if next_token:
                params["nextToken"] = next_token
            body = self._request("GET", f"/events/{event_id}/sessions", params=params)
            for raw in _items(body, "sessions", "items"):
                yield Session.model_validate(raw)
            next_token = body.get("nextToken")
            if not next_token:
                return

    def get_session(self, event_id: str, session_id: str) -> Session:
        body = self._request("GET", f"/events/{event_id}/sessions/{session_id}")
        return Session.model_validate(body.get("session", body))

    # --- my schedule ---------------------------------------------------------

    def get_schedule(self, event_id: str) -> Schedule:
        return Schedule.model_validate(self._request("GET", f"/events/{event_id}/schedule"))

    def associate_favorites(self, event_id: str, session_ids: Iterable[str]) -> list[Any]:
        return [
            self._request("POST", f"/events/{event_id}/favorites", json={"sessionIds": batch})
            for batch in _batches(session_ids)
        ]

    def disassociate_favorite(self, event_id: str, session_id: str) -> None:
        self._request("DELETE", f"/events/{event_id}/favorites/{session_id}")

    def reserve_sessions(self, event_id: str, session_ids: Iterable[str]) -> ReserveResult:
        """Reserve in batches of 10, merging results.

        Not idempotent: an already-reserved session comes back as a failure, so
        callers should diff against ``get_schedule`` rather than blindly retry.
        """
        merged = ReserveResult()
        for batch in _batches(session_ids):
            body = self._request(
                "POST", f"/events/{event_id}/reservations", json={"sessionIds": batch}
            )
            part = ReserveResult.model_validate(body or {})
            merged.succeeded.extend(part.succeeded)
            merged.failed.extend(part.failed)
        return merged

    def cancel_reservation(self, event_id: str, session_id: str) -> None:
        self._request("DELETE", f"/events/{event_id}/reservations/{session_id}")

    def create_personal_time(self, event_id: str, title: str, start: str, end: str) -> PersonalTime:
        body = self._request(
            "POST",
            f"/events/{event_id}/personal-time",
            json={"title": title, "start": start, "end": end},
        )
        return PersonalTime.model_validate(body.get("personalTime", body))
