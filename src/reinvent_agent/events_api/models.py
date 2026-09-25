"""Data models for AWS Events API responses.

Field names follow the developer guide's prose. The OpenAPI spec
(https://api.awsevents.com/v1/openapi.json) was not reachable when this was
written, so each field accepts a few plausible spellings via ``AliasChoices``
and unknown fields are kept (``extra="allow"``). Tighten once the spec is
checked in under ``fixtures/openapi.json``.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


def _alias(*names: str) -> AliasChoices:
    return AliasChoices(*names)


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Event(_Model):
    event_id: str = Field(validation_alias=_alias("eventId", "id"))
    name: str = Field(default="", validation_alias=_alias("name", "title"))
    requires_registration: bool | None = Field(
        default=None, validation_alias=_alias("requiresRegistration", "registrationRequired")
    )
    start: datetime | None = Field(default=None, validation_alias=_alias("start", "startTime"))
    end: datetime | None = Field(default=None, validation_alias=_alias("end", "endTime"))


class Session(_Model):
    session_id: str = Field(validation_alias=_alias("sessionId", "id"))
    code: str = Field(default="", validation_alias=_alias("code", "sessionCode"))
    title: str = ""
    abstract: str | None = None
    type: str | None = Field(default=None, validation_alias=_alias("type", "sessionType"))
    level: str | None = None
    tracks: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    start: datetime | None = Field(default=None, validation_alias=_alias("start", "startTime"))
    end: datetime | None = Field(default=None, validation_alias=_alias("end", "endTime"))
    room: str | None = None
    venue: str | None = None
    all_day: bool = Field(default=False, validation_alias=_alias("allDay", "isAllDay"))
    reservable: bool = Field(default=False, validation_alias=_alias("reservable", "isReservable"))
    fullness: str | None = Field(
        default=None, validation_alias=_alias("fullness", "capacityStatus", "availability")
    )
    speakers: list[str] = Field(default_factory=list)

    @property
    def level_number(self) -> int | None:
        """'300 - Advanced' -> 300."""
        if not self.level:
            return None
        digits = "".join(ch for ch in self.level if ch.isdigit())[:3]
        return int(digits) if digits else None


class PersonalTime(_Model):
    personal_time_id: str | None = Field(
        default=None, validation_alias=_alias("personalTimeId", "id")
    )
    title: str = Field(default="", validation_alias=_alias("title", "name"))
    start: datetime = Field(validation_alias=_alias("start", "startTime"))
    end: datetime = Field(validation_alias=_alias("end", "endTime"))


class Schedule(_Model):
    reservations: list[Session] = Field(
        default_factory=list, validation_alias=_alias("reservations", "reserved")
    )
    favorites: list[Session] = Field(default_factory=list)
    personal_time: list[PersonalTime] = Field(
        default_factory=list, validation_alias=_alias("personalTime", "personal_time")
    )


class ReservationFailure(_Model):
    session_id: str = Field(validation_alias=_alias("sessionId", "id"))
    reason: str = Field(default="", validation_alias=_alias("reason", "code", "errorCode"))
    message: str | None = None

    @property
    def is_full(self) -> bool:
        return "full" in self.reason.lower()

    @property
    def is_conflict(self) -> bool:
        return "conflict" in self.reason.lower()

    @property
    def is_already_reserved(self) -> bool:
        r = self.reason.lower()
        return "already" in r or "duplicate" in r


class ReserveResult(_Model):
    """ReserveSessions returns 200 even on partial failure: always read ``failed``."""

    succeeded: list[str] = Field(
        default_factory=list, validation_alias=_alias("succeeded", "successful", "reserved")
    )
    failed: list[ReservationFailure] = Field(
        default_factory=list, validation_alias=_alias("failed", "failures", "errors")
    )
