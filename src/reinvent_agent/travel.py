"""Minutes needed to get from one re:Invent venue to another.

ESTIMATES, not measurements: door-to-door times including the walk from a session room
out of the casino and into the next venue's conference area (often 10+ minutes on each
side), the walk or shuttle along the Strip, and some crowd margin. The venues run north
to south: Wynn/Encore, Venetian, Caesars Forum, Caesars Palace, MGM Grand. AWS describes
Wynn, Encore and the Venetian as "within 3-10 minutes walking" of each other; MGM Grand
is at the far south end, where the shuttle is the realistic option.

To correct a value, edit the table on the app's Setup tab (saved locally and in the
PlansTable, and used by the cloud reservation run too), or the defaults in ``_PAIRS``.
To replace them with measured times, fill this table from Amazon Location's
CalculateRouteMatrix (pedestrian) plus an indoor allowance per venue.
"""

from __future__ import annotations

import json

SAME_VENUE = 0  # back-to-back in one venue is how re:Invent schedules are built
UNKNOWN = 30  # a venue we don't know (or a missing venue): assume a cross-Strip move

_PAIRS: dict[frozenset[str], int] = {
    frozenset({"Wynn", "Venetian"}): 15,
    frozenset({"Venetian", "Caesars Forum"}): 15,
    frozenset({"Caesars Forum", "Caesars Palace"}): 15,
    frozenset({"Venetian", "Caesars Palace"}): 20,
    frozenset({"Wynn", "Caesars Forum"}): 25,
    frozenset({"Wynn", "Caesars Palace"}): 30,
    frozenset({"Caesars Palace", "MGM Grand"}): 30,
    frozenset({"Caesars Forum", "MGM Grand"}): 30,
    frozenset({"Venetian", "MGM Grand"}): 35,
    frozenset({"Wynn", "MGM Grand"}): 40,
}


DEFAULTS = {"pairs": dict(_PAIRS), "same_venue": SAME_VENUE, "unknown": UNKNOWN}
# The values in use: the defaults above unless the attendee saved their own (Setup tab).
_active: dict = {"pairs": dict(_PAIRS), "same_venue": SAME_VENUE, "unknown": UNKNOWN}


def travel_minutes(a: str | None, b: str | None) -> int:
    """Estimated minutes from venue ``a`` to venue ``b`` (same venue: usually 0)."""
    if a and b and a == b:
        return _active["same_venue"]
    if not (a and b):
        return _active["unknown"]
    return _active["pairs"].get(frozenset({a, b}), _active["unknown"])


def rows() -> list[dict]:
    """The table shown on the Setup tab: one row per venue pair, plus the two defaults."""
    out = [
        {"from": a, "to": b, "minutes": m, "default": DEFAULTS["pairs"].get(frozenset({a, b}))}
        # From A-Z, then To A-Z; each pair once, alphabetical within the pair.
        for (a, b), m in sorted(
            ((tuple(sorted(k)), v) for k, v in _active["pairs"].items()),
            key=lambda kv: (kv[0][0].lower(), kv[0][1].lower()),
        )
    ]
    out.append(
        {
            "from": "(same venue)",
            "to": "(same venue)",
            "minutes": _active["same_venue"],
            "default": SAME_VENUE,
        }
    )
    out.append({"from": "(unknown venue)", "to": "(any)",
                "minutes": _active["unknown"], "default": UNKNOWN})  # fmt: skip
    return out


def from_rows(table: list[dict]) -> dict:
    """Edited Setup-tab rows -> a settings dict (minutes clamped to 0-180)."""
    settings = {"pairs": {}, "same_venue": SAME_VENUE, "unknown": UNKNOWN}
    for r in table:
        minutes = max(0, min(180, int(r["minutes"] or 0)))
        if r["from"] == "(same venue)":
            settings["same_venue"] = minutes
        elif r["from"] == "(unknown venue)":
            settings["unknown"] = minutes
        else:
            settings["pairs"][frozenset({r["from"], r["to"]})] = minutes
    return settings


def apply(settings: dict | None) -> None:
    """Use these values from now on (None: back to the defaults)."""
    src = settings or DEFAULTS
    _active.update(pairs=dict(src["pairs"]), same_venue=src["same_venue"], unknown=src["unknown"])


def to_json(settings: dict) -> str:
    return json.dumps({
        "pairs": [[*sorted(k), v] for k, v in settings["pairs"].items()],
        "same_venue": settings["same_venue"],
        "unknown": settings["unknown"],
    })  # fmt: skip


def from_json(text: str) -> dict:
    data = json.loads(text)
    return {
        "pairs": {frozenset({a, b}): int(m) for a, b, m in data["pairs"]},
        "same_venue": int(data["same_venue"]),
        "unknown": int(data["unknown"]),
    }


def current() -> dict:
    return {"pairs": dict(_active["pairs"]), "same_venue": _active["same_venue"],
            "unknown": _active["unknown"]}  # fmt: skip
