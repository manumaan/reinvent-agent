"""Live seat availability: one read-only walk of ListSessions (no abstracts).

The downloaded catalog is a snapshot; seats change by the minute once reservations are
open. A check stores ``{sessionId: {"reservable", "seats"}}`` plus when it ran, in
``~/.config/reinvent-agent/seats-<event>.json``, and notes sessions that aren't in the
local catalog yet (added since the last download).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path


def seats_path(event_id: str) -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "reinvent-agent" / f"seats-{event_id}.json"


@dataclass
class SeatCheck:
    checked_at: float
    sessions: dict[str, dict] = field(default_factory=dict)  # id -> {reservable, seats}

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self.sessions.values():
            key = row.get("seats") or ("reservable" if row["reservable"] else "none")
            out[key] = out.get(key, 0) + 1
        return out


def check_seats(client, event_id: str) -> SeatCheck:
    """Walk ListSessions (paged, ~10 requests; quota is 120/min) and save the result."""
    rows: dict[str, dict] = {}
    for page in client.iter_session_pages(event_id, include_abstracts=False):
        for item in page["items"]:
            rows[item["sessionId"]] = {
                "reservable": bool(item.get("isReservable")),
                "seats": item.get("seatAvailability"),
            }
    result = SeatCheck(time.time(), rows)
    path = seats_path(event_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"checkedAt": result.checked_at, "sessions": rows}))
    return result


def load_seats(event_id: str) -> SeatCheck | None:
    try:
        body = json.loads(seats_path(event_id).read_text())
        return SeatCheck(body["checkedAt"], body["sessions"])
    except (OSError, ValueError, KeyError):
        return None
