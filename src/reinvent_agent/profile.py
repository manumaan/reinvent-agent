"""The attendee's profile: what the short list is built from. Every field is optional.

Choice fields hold values taken from the catalog itself (roles, industries, topics,
session types, AWS services, days, areas of interest), so they match sessions
exactly; free-text fields are matched semantically (see ``shortlist``).
Stored locally (``~/.config/reinvent-agent/profile-<event>.json``) and, when signed
in, in the PlansTable so it follows you to other machines.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

EXPERIENCE = {
    "new": "New to this area (100–200)",
    "some": "Some hands-on experience (200–300)",
    "experienced": "Experienced practitioner (300–400)",
    "expert": "Expert (400–500)",
}
EXPERIENCE_LEVELS = {
    "new": {100, 200},
    "some": {200, 300},
    "experienced": {300, 400},
    "expert": {400, 500},
}


@dataclass
class Profile:
    roles: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    tech_stack: str = ""  # technologies, platforms, languages I use
    session_types: list[str] = field(default_factory=list)
    aws_using: list[str] = field(default_factory=list)  # -> 300-400 sessions
    aws_learning: list[str] = field(default_factory=list)  # -> 100-200 sessions
    days: list[str] = field(default_factory=list)  # YYYY-MM-DD attending
    architecture: str = ""
    experience: str | None = None  # key of EXPERIENCE
    projects: str = ""
    learn_other: str = ""  # technologies I want to learn (free text)
    interests: list[str] = field(default_factory=list)  # strategic: areas of interest
    irrelevant_topics: list[str] = field(default_factory=list)
    constraints: str = ""

    def answered(self) -> int:
        return sum(bool(getattr(self, f.name)) for f in fields(self))

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> Profile:
        data = json.loads(text)
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def describe(self) -> str:
        """Plain-language summary (for Claude and for display)."""
        parts = {
            "Role": ", ".join(self.roles),
            "Industry": ", ".join(self.industries),
            "Topics of interest": ", ".join(self.topics),
            "Technologies, platforms, languages used": self.tech_stack,
            "Preferred session types": ", ".join(self.session_types),
            "AWS services used (wants 300-400 depth)": ", ".join(self.aws_using),
            "AWS services to learn (wants 100-200 intros)": ", ".join(self.aws_learning),
            "Days attending": ", ".join(self.days),
            "Architecture areas": self.architecture,
            "Experience": EXPERIENCE.get(self.experience or "", ""),
            "Projects / problems": self.projects,
            "Other technologies to learn": self.learn_other,
            "Strategic interests": ", ".join(self.interests),
            "Probably irrelevant topics": ", ".join(self.irrelevant_topics),
            "Preferences and constraints": self.constraints,
        }
        return "\n".join(f"- {k}: {v}" for k, v in parts.items() if v)


def profile_path(event_id: str) -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "reinvent-agent" / f"profile-{event_id}.json"


def load_local(event_id: str) -> Profile | None:
    try:
        return Profile.from_json(profile_path(event_id).read_text())
    except (OSError, ValueError, TypeError):
        return None


def save_local(profile: Profile, event_id: str) -> None:
    path = profile_path(event_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(profile.to_json())
