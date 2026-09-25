"""Client for the AWS Events API (https://api.awsevents.com/v1)."""

from reinvent_agent.events_api.auth import (
    FileTokenStore,
    TokenProvider,
    Tokens,
    TokenStore,
)
from reinvent_agent.events_api.client import EventsApiClient, EventsApiError
from reinvent_agent.events_api.models import (
    Event,
    ReservationFailure,
    ReserveResult,
    Schedule,
    Session,
)

__all__ = [
    "Event",
    "EventsApiClient",
    "EventsApiError",
    "FileTokenStore",
    "ReservationFailure",
    "ReserveResult",
    "Schedule",
    "Session",
    "TokenProvider",
    "TokenStore",
    "Tokens",
]
