"""Client for the AWS Events API (https://api.awsevents.com/v1)."""

from reinvent_agent.events_api.auth import (
    FileTokenStore,
    TokenProvider,
    Tokens,
    TokenStore,
)
from reinvent_agent.events_api.client import (
    EventsApiClient,
    EventsApiError,
    NotRegisteredError,
    OperationClosedError,
)
from reinvent_agent.events_api.models import (
    BatchResult,
    Event,
    PersonalTime,
    ReservationFailure,
    Schedule,
    Session,
)

__all__ = [
    "BatchResult",
    "Event",
    "EventsApiClient",
    "EventsApiError",
    "FileTokenStore",
    "NotRegisteredError",
    "OperationClosedError",
    "PersonalTime",
    "ReservationFailure",
    "Schedule",
    "Session",
    "TokenProvider",
    "TokenStore",
    "Tokens",
]
