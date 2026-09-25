# re:Invent planner agent

An agent that plans your AWS re:Invent week on top of the [AWS Events API](https://docs.aws.amazon.com/events/latest/devguide/what-is-events-api.html):

- **Schedule optimizer:** goals in plain language → a conflict-free, walkable schedule → favorites and reservations.
- **Catalog Q&A:** RAG over the session catalog.
- **Spatial layer:** venue clustering, walking buffers and a map.

See [`docs/DESIGN.md`](docs/DESIGN.md) for the architecture and [`docs/M0-auth-spike.md`](docs/M0-auth-spike.md) for how sign-in and unattended reservations work.

## Quick start

Everything runs in **us-east-1** with your **default AWS profile**. You need Python 3.11+, [uv](https://docs.astral.sh/uv/), Node (for the CDK CLI), and Bedrock model access in us-east-1 for **Titan Text Embeddings v2** and **Claude Opus 5**.

```bash
uv sync --all-groups --extra ui

# 1. Sign in (AWS Builder ID; opens a browser, callback on localhost:8484-8489)
uv run reinvent-agent auth login

# 2. Download the catalog (registration-gated: stays local, git-ignored)
uv run reinvent-agent catalog dump --event reinvent2026 --out fixtures/reinvent2026/catalog.jsonl
uv run reinvent-agent catalog report          # field coverage + venue inference

# 3. Deploy storage + search (first time: npx aws-cdk@2 bootstrap)
cd infra && npx aws-cdk@2 deploy --all && cd ..

# 4. Embed and index the catalog into S3 Vectors + DynamoDB
uv run reinvent-agent catalog index

# 5. Use it
uv run reinvent-agent catalog search "zero-ETL Aurora Redshift" --min-level 300
uv run reinvent-agent ask "Which sessions touch on zero-ETL between Aurora and Redshift?"
uv run streamlit run ui/app.py                # Ask / Search / My schedule, with in-app sign-in
```

The app and CLI read the bucket, table and secret names from the deployed stacks' outputs, so there is nothing to configure. Environment variables (`REINVENT_VECTOR_BUCKET`, `REINVENT_SESSIONS_TABLE`, `REINVENT_MODEL`, …; see `src/reinvent_agent/config.py`) override them.

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
| `src/reinvent_agent/catalog/` | Venue inference, search documents, embeddings, S3 Vectors store, search, ingest |
| `src/reinvent_agent/qa.py` | Catalog Q&A: Claude on Bedrock with a `catalog_search` tool |
| `src/reinvent_agent/cli.py` | `reinvent-agent` CLI |
| `ui/app.py` | Streamlit app (run locally) |
| `infra/` | AWS CDK app: data stack (S3, DynamoDB, token secret) and search stack (S3 Vectors) |
| `fixtures/` | OpenAPI spec, public API samples, synthetic re:Invent fixtures |
| `docs/` | Design and spike notes |
