# re:Invent planner agent

An agent that plans your AWS re:Invent week on top of the [AWS Events API](https://docs.aws.amazon.com/events/latest/devguide/what-is-events-api.html):

- **Schedule optimizer:** goals in plain language → a conflict-free, walkable schedule → favorites and reservations.
- **Catalog Q&A:** RAG over the session catalog.
- **Spatial layer:** venue clustering, walking buffers and a map.

See [`docs/DESIGN.md`](docs/DESIGN.md) for the architecture and [`docs/M0-auth-spike.md`](docs/M0-auth-spike.md) for how sign-in and unattended reservations work.

## Quick start

```bash
uv sync
uv run reinvent-agent auth login          # AWS Builder ID, opens browser, callback on localhost:8484
uv run reinvent-agent catalog events
uv run reinvent-agent catalog dump --event reinvent2026 --out catalog.jsonl
uv run reinvent-agent catalog schedule
```

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest
# infrastructure (needs Node for the CDK CLI)
uv sync --group infra && cd infra && npx aws-cdk@2 synth
```

## Layout

| Path | What |
|---|---|
| `src/reinvent_agent/events_api/` | Builder ID auth (PKCE, refresh rotation, token stores) and REST client |
| `src/reinvent_agent/cli.py` | `reinvent-agent` CLI |
| `infra/` | AWS CDK app (data stack in M0) |
| `fixtures/` | Synthetic API responses for tests |
| `docs/` | Design and spike notes |
