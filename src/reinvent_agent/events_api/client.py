"""REST client for the AWS Events API.

Behaviour follows the developer guide's "Handling errors" and "Quotas and
throttling" pages:

* 401 -> refresh once and retry; a second 401 means sign in again.
* 403 with a JSON body -> not registered for the event (never retry).
  403 with no body -> the edge is refusing us; slow down.
* 409 -> the operation is closed (e.g. reservations before seating opens).
* 429 -> wait ``Retry-After`` seconds.
* Writes have no idempotency key: never blindly re-send one whose outcome is
  unknown; reconcile from ``get_schedule`` instead. Single removals are safe,
  because a 404 means "already gone".
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx

from reinvent_agent.events_api.auth import TokenProvider
from reinvent_agent.events_api.models import BatchResult, Event, Schedule, Session

BASE_URL = "https://api.awsevents.com/v1"
MAX_BATCH = 10  # AssociateFavorites and ReserveSessions take 1..10 distinct IDs


class EventsApiError(RuntimeError):
    def __init__(self, status: int, body: Any, method: str, path: str):
        self.status = status
        self.body = body
        message = body.get("message", body) if isinstance(body, dict) else body
        super().__init__(f"{method} {path} -> {status}: {message}")


class NotRegisteredError(EventsApiError):
    """Signed in, but not registered for this event. Signing in again does not help."""


class OperationClosedError(EventsApiError):
    """409: the operation is closed for now (e.g. reservations before seating opens)."""


def _batches(ids: Iterable[str], size: int = MAX_BATCH) -> Iterator[list[str]]:
    unique = list(dict.fromkeys(ids))
    for i in range(0, len(unique), size):
        yield unique[i : i + size]


def _items(body: dict, *keys: str) -> list[dict]:
    for key in keys:
        if isinstance(body.get(key), list):
            return body[key]
    raise EventsApiError(200, body, "?", f"no list under any of {keys}")


def format_personal_time(dt: datetime) -> str:
    """UTC, ``YYYY-MM-DDTHH:mm:ss``, no offset, seconds zero."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    if dt.second or dt.microsecond:
        raise ValueError(f"personal time must be whole minutes: {dt}")
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def personal_time_body(
    title: str, description: str, start: datetime, end: datetime, location: str | None = None
) -> dict[str, str]:
    if not 1 <= len(title) <= 128:
        raise ValueError("title must be 1-128 characters")
    if not 1 <= len(description) <= 250:
        raise ValueError("description must be 1-250 characters")
    if location is not None and len(location) > 255:
        raise ValueError("location must be at most 255 characters")
    start_s, end_s = format_personal_time(start), format_personal_time(end)
    minutes = (datetime.fromisoformat(end_s) - datetime.fromisoformat(start_s)).total_seconds() / 60
    if minutes <= 0 or minutes % 5:
        raise ValueError("personal time must end after it starts, in 5-minute increments")
    body = {
        "title": title,
        "description": description,
        "startDateTime": start_s,
        "endDateTime": end_s,
    }
    if location:
        body["location"] = location
    return body


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
            headers = {}
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
                # The edge can answer with no body or HTML: never assume JSON.
                try:
                    body = resp.json()
                except ValueError:
                    body = resp.text
                if resp.status_code == 403 and isinstance(body, dict):
                    raise NotRegisteredError(resp.status_code, body, method, path)
                if resp.status_code == 409:
                    raise OperationClosedError(resp.status_code, body, method, path)
                raise EventsApiError(resp.status_code, body, method, path)
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()

    def _delete_idempotent(self, path: str) -> bool:
        """Single removals: 404 means already gone. Returns False in that case."""
        try:
            self._request("DELETE", path)
        except EventsApiError as e:
            if e.status == 404:
                return False
            raise
        return True

    # --- catalog -------------------------------------------------------------

    def list_events(self, *, include_past: bool = False) -> list[Event]:
        params = {"includePast": "true"} if include_past else None
        body = self._request("GET", "/events", auth=False, params=params)
        return [Event.model_validate(e) for e in _items(body, "events", "items")]

    def get_event(self, event_id: str) -> Event:
        body = self._request("GET", f"/events/{event_id}", auth=False)
        return Event.model_validate(body.get("event", body))

    def iter_sessions(
        self, event_id: str, *, include_abstracts: bool = True, locale: str | None = None
    ) -> Iterator[Session]:
        """Walk every page. Pages vary in size; only an absent ``nextToken`` ends the walk."""
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
        """Source of truth for your own data: read it back after every write."""
        return Schedule.model_validate(self._request("GET", f"/events/{event_id}/schedule"))

    def _batch_write(self, path: str, session_ids: Iterable[str]) -> BatchResult:
        merged = BatchResult()
        for batch in _batches(session_ids):
            body = self._request("POST", path, json={"sessionIds": batch})
            part = BatchResult.model_validate(body or {})
            merged.succeeded.extend(part.succeeded)
            merged.failed.extend(part.failed)
        return merged

    def reserve_sessions(self, event_id: str, session_ids: Iterable[str]) -> BatchResult:
        """Reserve in batches of 10, merging per-session results.

        Not idempotent: an already-reserved session comes back as a failure, so
        diff against ``get_schedule`` rather than re-sending. Raises
        ``OperationClosedError`` until reserved seating opens. The quota counts
        sessions (30/min), not requests.
        """
        return self._batch_write(f"/events/{event_id}/reservations", session_ids)

    def cancel_reservation(self, event_id: str, session_id: str) -> bool:
        return self._delete_idempotent(f"/events/{event_id}/reservations/{session_id}")

    def associate_favorites(self, event_id: str, session_ids: Iterable[str]) -> BatchResult:
        return self._batch_write(f"/events/{event_id}/favorites", session_ids)

    def disassociate_favorite(self, event_id: str, session_id: str) -> bool:
        return self._delete_idempotent(f"/events/{event_id}/favorites/{session_id}")

    def create_personal_time(
        self,
        event_id: str,
        title: str,
        description: str,
        start: datetime,
        end: datetime,
        location: str | None = None,
    ) -> None:
        """Returns no body: read the new entry's ID back from ``get_schedule``.
        Not idempotent: re-sending creates a second entry."""
        body = personal_time_body(title, description, start, end, location)
        self._request("POST", f"/events/{event_id}/personal-time", json=body)

    def update_personal_time(
        self,
        event_id: str,
        personal_time_id: str,
        title: str,
        description: str,
        start: datetime,
        end: datetime,
        location: str | None = None,
    ) -> None:
        """Full replace: omitted ``location`` is cleared."""
        body = personal_time_body(title, description, start, end, location)
        self._request("PUT", f"/events/{event_id}/personal-time/{personal_time_id}", json=body)

    def delete_personal_time(self, event_id: str, personal_time_id: str) -> bool:
        return self._delete_idempotent(f"/events/{event_id}/personal-time/{personal_time_id}")
