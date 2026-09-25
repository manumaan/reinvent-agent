import json
import time
from pathlib import Path

import httpx
import pytest

from reinvent_agent.events_api import EventsApiClient, FileTokenStore, TokenProvider, Tokens

FIXTURES = Path(__file__).parent.parent / "fixtures"


def load(name: str) -> dict:
    """Load a fixture by path relative to fixtures/; bare names come from reinvent2026/."""
    path = FIXTURES / name if "/" in name else FIXTURES / "reinvent2026" / name
    return json.loads(path.read_text())


@pytest.fixture
def token_store(tmp_path) -> FileTokenStore:
    store = FileTokenStore(tmp_path / "tokens.json")
    store.save(Tokens("access-1", "refresh-1", time.time() + 3600))
    return store


@pytest.fixture
def make_client(token_store):
    def _make(handler) -> EventsApiClient:
        http = httpx.Client(transport=httpx.MockTransport(handler))
        return EventsApiClient(
            TokenProvider(token_store, http=http), http=http, sleep=lambda s: None
        )

    return _make
