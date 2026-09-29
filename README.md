# re:Invent planner agent

An agent that plans your AWS re:Invent week on top of the [AWS Events API](https://docs.aws.amazon.com/events/latest/devguide/what-is-events-api.html):

- **Schedule optimizer:** goals in plain language → a conflict-free, walkable schedule → favorites and reservations.
- **Catalog Q&A:** RAG over the session catalog.
- **Spatial layer:** venue clustering, walking buffers and a map.

See [`docs/DESIGN.md`](docs/DESIGN.md) for the architecture and [`docs/M0-auth-spike.md`](docs/M0-auth-spike.md) for how sign-in and unattended reservations work.

## Quick start

Everything runs in **us-east-1** with your **default AWS profile**. You need Python 3.11+, [uv](https://docs.astral.sh/uv/), Node (for the CDK CLI), and Bedrock model access in us-east-1 for **Titan Text Embeddings v2** and **Claude Opus 4.8** (check with `uv run reinvent-agent check-models`).

```bash
uv sync --all-groups --extra ui

# 1. Deploy storage + search (first time: npx aws-cdk@2 bootstrap)
cd infra && npx aws-cdk@2 deploy --all && cd ..

# 2. Sign in (AWS Builder ID; opens a browser, callback on localhost:8484-8489).
#    Or use "Sign in with AWS Builder ID" in the app's sidebar.
uv run reinvent-agent auth login

# 3. Download the full catalog from the Events API (ListSessions needs the sign-in).
#    Writes fixtures/reinvent2026/catalog.jsonl (git-ignored) and copies it to the
#    catalog bucket at catalog/reinvent2026/catalog.jsonl. The app's Catalog tab does the same.
uv run reinvent-agent catalog dump
uv run reinvent-agent catalog upload path/to/catalog.jsonl   # ...or upload a file you already have
uv run reinvent-agent catalog report          # field coverage + venue inference

# 3b. Optional: use a Claude API key (platform.claude.com) instead of Bedrock for Claude.
#     Stored in Secrets Manager via a hidden prompt; embeddings still use Bedrock Titan.
uv run reinvent-agent config set-api-key
uv run reinvent-agent check-models

# 4. Embed and index into S3 Vectors + DynamoDB (local file if present, else the bucket copy)
uv run reinvent-agent catalog index

# 5. Use it
uv run reinvent-agent catalog search "zero-ETL Aurora Redshift" --min-level 300
uv run reinvent-agent ask "Which sessions touch on zero-ETL between Aurora and Redshift?"
uv run streamlit run ui/app.py                # Ask / Search / My schedule / Catalog, with in-app sign-in
```

### Reserving seats on October 6

Reserved seats release in two phases on **Oct 6, 2026: 9:00 AM and 5:00 PM PDT**. In the app's **Plans** tab (or the `reserve` CLI):

1. **Build and approve a plan** from your favorites: sessions to reserve (never overlapping), a priority each, and backups tried when an overlapping session is full.
2. **Enable the unattended run** and subscribe an email. A Lambda then runs at 8:58 AM and 4:58 PM PDT (plus sign-in checks at 8:30 AM / 4:30 PM and a reminder on Oct 5). It polls until seats release, reserves in priority order at 30 sessions/min, re-reads your schedule after every batch, falls back to backups, and emails the result.
3. **Sign in again on the evening of Oct 5 (PDT)** and press *Refresh cloud sign-in*: Builder ID sessions expire on their own schedule.

```bash
uv run reinvent-agent reserve build && uv run reinvent-agent reserve approve
uv run reinvent-agent auth push-secret            # enable the unattended run
uv run reinvent-agent reserve notify you@example.com
uv run reinvent-agent reserve preflight --cloud   # test sign-in + email now
uv run reinvent-agent reserve status
uv run reinvent-agent reserve run --wait-minutes 10   # manual fallback from your machine
```

The app and CLI read the bucket, table and secret names from the deployed stacks' outputs, so there is nothing to configure. Environment variables (`REINVENT_VECTOR_BUCKET`, `REINVENT_SESSIONS_TABLE`, `REINVENT_MODEL`, `REINVENT_LLM_PROVIDER`, `ANTHROPIC_API_KEY`, …; see `src/reinvent_agent/config.py`) override them. `uv run reinvent-agent config show` prints what is in effect (never the key).

**Which Claude endpoint is used:** endpoints are defined in `src/reinvent_agent/llm.py` and all speak the same Messages API. The choice is, in order: `REINVENT_LLM_PROVIDER`; the provider saved by `config set-provider`; otherwise `auto`, which means the Claude API when a key is available (`ANTHROPIC_API_KEY`, or the `AnthropicApiKey` secret set by `config set-api-key`), else Amazon Bedrock. The default model is Claude Opus 4.8 on either endpoint. When Bedrock access arrives:

```bash
uv run reinvent-agent check-models --provider bedrock    # probe without switching
uv run reinvent-agent config set-provider bedrock        # optionally --model claude-opus-4-7
```

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest
# infrastructure (needs Node for the CDK CLI)
cd infra && npx aws-cdk@2 synth
```

## Layout

| Path | What |
|---|---|
| `src/reinvent_agent/events_api/` | Builder ID auth (PKCE, refresh rotation, token stores) and REST client |
| `src/reinvent_agent/catalog/` | Catalog download/S3 storage (`source.py`), venue inference, search documents, embeddings, S3 Vectors store, search, ingest |
| `src/reinvent_agent/llm.py` | Claude endpoints (Bedrock, Claude API): model IDs and client factory |
| `src/reinvent_agent/qa.py` | Q&A agent: Claude with `catalog_search`, `get_my_schedule` and `plan_one_venue_per_day` tools |
| `src/reinvent_agent/schedule.py` | Your GetSchedule, cached locally (synced on sign-in) and joined with catalog details |
| `src/reinvent_agent/reservation_plan.py` | Reservation plans (primaries, priorities, backups), approval and storage in DynamoDB |
| `src/reinvent_agent/reservation_runner.py`, `lambda_handler.py` | The Oct 6 run: wait for release, paced reserve, read-back, backups, SNS report |
| `src/reinvent_agent/accounts.py` | Token storage (local or shared with the cloud run), notifications, cloud invoke |
| `src/reinvent_agent/planner.py` | Deterministic planning, e.g. one venue per day from your favorites |
| `src/reinvent_agent/cli.py` | `reinvent-agent` CLI |
| `ui/app.py` | Streamlit app (run locally) |
| `infra/` | AWS CDK app: data (S3, DynamoDB, secrets), search (S3 Vectors), reservations (Lambda, Scheduler, SNS) |
| `fixtures/` | OpenAPI spec, public API samples, synthetic re:Invent fixtures |
| `docs/` | Design and spike notes |
