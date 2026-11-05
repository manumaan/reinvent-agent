"""Has the catalog changed on AWS since it was downloaded?

The app compares its copy with a fresh walk of ListSessions every ``check_hours()`` while
it's open, and as soon as it opens if a check is overdue. Nothing is updated: the result
is saved in ``~/.config/reinvent-agent/catalog-changes-<event>.json`` and the app shows a
notice until the catalog is downloaded again. Seat availability changes by the minute and
isn't a catalog change, so it is ignored.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from reinvent_agent.catalog.source import read_text, to_jsonl

CHECK_HOURS = 6
RETRY_SECONDS = 3600  # after a failed check, e.g. an expired sign-in
IGNORED = {"seatAvailability", "isReservable"}
FIELD_NAMES = {"abstract": "description", "sessionTime": "time", "abbreviation": "code"}

_running = threading.Lock()  # one check at a time, however many tabs are open


def check_hours() -> float:
    """Hours between checks; ``REINVENT_CATALOG_CHECK_HOURS=0`` turns them off."""
    return float(os.environ.get("REINVENT_CATALOG_CHECK_HOURS", CHECK_HOURS))


@dataclass
class CatalogChanges:
    checked_at: float
    added: list[str] = field(default_factory=list)  # session codes
    removed: list[str] = field(default_factory=list)
    changed: dict[str, list[str]] = field(default_factory=dict)  # code -> what changed
    error: str | None = None

    @property
    def found(self) -> bool:
        return bool(self.added or self.removed or self.changed)

    def summary(self) -> str:
        parts = [
            f"{len(self.added)} new" if self.added else "",
            f"{len(self.removed)} removed" if self.removed else "",
            f"{len(self.changed)} updated" if self.changed else "",
        ]
        return ", ".join(p for p in parts if p)


def _by_id(jsonl: str) -> dict[str, dict]:
    rows = (json.loads(line) for line in jsonl.splitlines() if line.strip())
    return {r["sessionId"]: r for r in rows}


def _code(row: dict) -> str:
    return row.get("abbreviation") or row.get("title") or row["sessionId"]


def compare(ours: str, theirs: str, checked_at: float | None = None) -> CatalogChanges:
    """Compare two catalogs in the JSONL form of ``source.to_jsonl``."""
    old, new = _by_id(ours), _by_id(theirs)
    changed = {}
    for sid in old.keys() & new.keys():
        keys = (old[sid].keys() | new[sid].keys()) - IGNORED
        if diff := sorted(k for k in keys if old[sid].get(k) != new[sid].get(k)):
            changed[_code(new[sid])] = [FIELD_NAMES.get(k, k) for k in diff]
    return CatalogChanges(
        checked_at if checked_at is not None else time.time(),
        added=sorted(_code(new[s]) for s in new.keys() - old.keys()),
        removed=sorted(_code(old[s]) for s in old.keys() - new.keys()),
        changed=dict(sorted(changed.items())),
    )


def changes_path(event_id: str) -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "reinvent-agent" / f"catalog-changes-{event_id}.json"


def save_changes(event_id: str, changes: CatalogChanges) -> None:
    path = changes_path(event_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(changes)))


def load_changes(event_id: str) -> CatalogChanges | None:
    try:
        return CatalogChanges(**json.loads(changes_path(event_id).read_text()))
    except (OSError, ValueError, TypeError):
        return None


def mark_up_to_date(event_id: str) -> None:
    """The catalog was just downloaded, so there is nothing to report."""
    save_changes(event_id, CatalogChanges(time.time()))


def check_due(last: CatalogChanges | None, now: float | None = None) -> bool:
    hours = check_hours()
    if hours <= 0:
        return False
    if last is None:
        return True
    wait = RETRY_SECONDS if last.error else hours * 3600
    return (now if now is not None else time.time()) - last.checked_at >= wait


def check_catalog(client, event_id: str, location: str | Path) -> CatalogChanges:
    """Walk ListSessions, compare it with the catalog at ``location``, and save the result."""
    items = [
        item
        for page in client.iter_session_pages(event_id, include_abstracts=True)
        for item in page["items"]
    ]
    changes = compare(read_text(location), to_jsonl(items))
    save_changes(event_id, changes)
    return changes


def start_check(get_client: Callable, event_id: str, location: str | Path) -> bool:
    """Check in a background thread; False if a check is already running."""
    if not _running.acquire(blocking=False):
        return False

    def run() -> None:
        try:
            if (client := get_client()) is not None:
                check_catalog(client, event_id, location)
        except Exception as e:  # shown quietly by the app, and retried later
            save_changes(event_id, CatalogChanges(time.time(), error=str(e)))
        finally:
            _running.release()

    threading.Thread(target=run, daemon=True).start()
    return True
