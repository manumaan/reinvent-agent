# re:Invent planner agent

An agent that plans your AWS re:Invent week on top of the [AWS Events API](https://docs.aws.amazon.com/events/latest/devguide/what-is-events-api.html):

- **Schedule optimizer:** goals in plain language → a conflict-free, walkable schedule → favorites and reservations.
- **Catalog Q&A:** RAG over the session catalog.
- **Spatial layer:** venue clustering, walking buffers and a map.

See [`docs/DESIGN.md`](docs/DESIGN.md) for the architecture and [`docs/M0-auth-spike.md`](docs/M0-auth-spike.md) for how sign-in and unattended reservations work.

## Quick start

Everything runs in **us-east-1** with your **default AWS profile**. You need Python 3.11+, [uv](https://docs.astral.sh/uv/), Node (for the CDK CLI), Bedrock model access in us-east-1 for **Titan Text Embeddings v2**, and an OpenRouter key for Claude (check with `uv run reinvent-agent check-models`).

```bash
uv sync --all-groups --extra ui

# 1. Deploy storage + search (first time: npx aws-cdk@2 bootstrap)
cd infra && npx aws-cdk@2 deploy --all && cd ..

# 2. Sign in (AWS Builder ID; opens a browser, callback on localhost:8484-8489).
#    Or use "Sign in with AWS Builder ID" in the app's sidebar.
uv run reinvent-agent auth login

# 3. Download the full catalog from the Events API (ListSessions needs the sign-in).
#    Writes fixtures/reinvent2026/catalog.jsonl (git-ignored) and copies it to the
#    catalog bucket at catalog/reinvent2026/catalog.jsonl. The app's Setup tab does the same.
uv run reinvent-agent catalog dump
uv run reinvent-agent catalog upload path/to/catalog.jsonl   # ...or upload a file you already have
uv run reinvent-agent catalog report          # field coverage + venue inference

# 3b. Claude calls go through OpenRouter, with the key in the `instarag-openrouter-key-mt`
#     secret. Embeddings still use Bedrock Titan. Check that both work:
uv run reinvent-agent check-models

# 4. Embed and index into S3 Vectors + DynamoDB (local file if present, else the bucket copy)
uv run reinvent-agent catalog index

# 5. Use it
uv run reinvent-agent catalog search "zero-ETL Aurora Redshift" --min-level 300
uv run reinvent-agent ask "Which sessions touch on zero-ETL between Aurora and Redshift?"
uv run streamlit run ui/app.py                # Ask / Search / My schedule / Catalog, with in-app sign-in
```

### Reserving seats

The Events API is open for reservations. Reserve directly from **Search** or the **Short list** (live seat availability shown), or automate it in the **Plans** tab (or the `reserve` CLI):

1. **Build and approve a plan** from your favorites and/or short list: sessions to reserve (never overlapping, with travel time between venues), a priority each, and backups tried when an overlapping session is full.
2. **Enable the unattended run** and subscribe an email.
3. **Schedule a run** for any date and time. A one-time EventBridge schedule invokes the Lambda, which reserves whatever in the approved plan you don't hold yet (priority order, 30 sessions/min, read-back after every batch, backups), emails the result and deletes its schedule. Runs more than 35 minutes away also get a sign-in and plan check 30 minutes before.

Sign in again and press *Refresh cloud sign-in* shortly before a far-off run: Builder ID sessions expire on their own.

```bash
uv run reinvent-agent reserve build && uv run reinvent-agent reserve approve
uv run reinvent-agent auth push-secret            # enable the unattended run
uv run reinvent-agent reserve notify you@example.com
uv run reinvent-agent reserve schedule "2026-11-10 09:00" --tz America/Los_Angeles
uv run reinvent-agent reserve status              # plan, scheduled runs, recent runs
uv run reinvent-agent reserve unschedule 20261110T1700
uv run reinvent-agent reserve preflight --cloud   # test sign-in + email now
uv run reinvent-agent reserve dry-run             # step-by-step log, also emailed; reserves nothing
```

The app and CLI read the bucket, table and secret names from the deployed stacks' outputs, so there is nothing to configure. Environment variables (`REINVENT_VECTOR_BUCKET`, `REINVENT_SESSIONS_TABLE`, `REINVENT_MODEL`, `REINVENT_LLM_PROVIDER`, `ANTHROPIC_API_KEY`, …; see `src/reinvent_agent/config.py`) override them. `uv run reinvent-agent config show` prints what is in effect (never the key).

**Which Claude endpoint is used:** endpoints are defined in `src/reinvent_agent/llm.py` and all speak the same Messages API. The default is **OpenRouter**, using the normal Anthropic SDK pointed at `https://openrouter.ai/api`, with the key from `OPENROUTER_API_KEY` or the `instarag-openrouter-key-mt` secret (`REINVENT_OPENROUTER_SECRET_ID` names a different one). `REINVENT_LLM_PROVIDER` or a provider saved by `config set-provider` overrides it. The default model is Claude Opus 4.8 on every endpoint. To use another endpoint:

```bash
uv run reinvent-agent check-models --provider bedrock    # probe without switching
uv run reinvent-agent config set-provider bedrock        # optionally --model claude-opus-4-7
uv run reinvent-agent config set-provider anthropic      # with a key from `config set-api-key`
```

**Catalog changes:** while the app is open and you're signed in, it compares your catalog with the Events API every 6 hours, and on opening if a check is overdue (`REINVENT_CATALOG_CHECK_HOURS` changes the interval; 0 turns it off). The sidebar says how many sessions changed until you download the catalog again. Seat availability is ignored.

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
| `src/reinvent_agent/llm.py` | Claude endpoints (OpenRouter, Bedrock, Claude API): model IDs and client factory |
| `src/reinvent_agent/qa.py` | Q&A agent: Claude with `catalog_search`, `get_my_schedule` and `plan_one_venue_per_day` tools |
| `src/reinvent_agent/schedule.py` | Your GetSchedule, cached locally (synced on sign-in) and joined with catalog details |
| `src/reinvent_agent/reservation_plan.py` | Reservation plans (primaries, priorities, backups), approval and storage in DynamoDB |
| `src/reinvent_agent/reservation_runner.py`, `lambda_handler.py` | The automated run: paced reserve, read-back, backups, SNS report |
| `src/reinvent_agent/run_schedule.py` | One-time schedules for automated runs at a chosen date and time |
| `src/reinvent_agent/accounts.py` | Token storage (local or shared with the cloud run), notifications, cloud invoke |
| `src/reinvent_agent/planner.py` | Deterministic planning, e.g. one venue per day from your favorites |
| `src/reinvent_agent/cli.py` | `reinvent-agent` CLI |
| `ui/app.py` | Streamlit app (run locally) |
| `infra/` | AWS CDK app: data (S3, DynamoDB, secrets), search (S3 Vectors), reservations (Lambda, SNS, scheduler role) |
| `fixtures/` | OpenAPI spec, public API samples, synthetic re:Invent fixtures |
| `docs/` | Design and spike notes |
