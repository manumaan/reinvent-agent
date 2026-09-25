# re:Invent Planner Agent — Design (v0.2, approved 2026-09-25)

One agent, three capabilities:

1. **Schedule Optimizer**: turns plain-language goals into a conflict-free schedule, then favorites and reserves sessions for you.
2. **Catalog RAG Q&A**: answers questions over the full session catalog (and last year's, for "what's new").
3. **Spatial layer**: groups the schedule by venue, adds walking and shuttle buffers, and draws the day on a map of the Las Vegas venues.

All three sit on the **AWS Events API** (REST and MCP server). The agent runs on **Amazon Bedrock AgentCore**.

---

## 1. What the AWS Events API gives us

Sources: the developer guide pages for ListEvents, ListSessions, GetSession, GetSchedule, AssociateFavorites, ReserveSessions and CreatePersonalTime. Our sandbox could not reach the docs or the OpenAPI spec (see §10), so this summary comes from search-indexed excerpts.

| Operation | Auth | Used for |
|---|---|---|
| `ListEvents` / `GetEvent` | none | Find the `eventId` for re:Invent 2026 (and 2025) |
| `ListSessions` (paginated, `nextToken`, `includeAbstracts`, `locale`) | Builder ID token (re:Invent requires registration) | Catalog ingest |
| `GetSession` | token | Fresh detail and fullness check just before reserving |
| `GetSchedule` | token | Your reservations, favorites and personal time, which the optimizer treats as constraints |
| `AssociateFavorites` (1 to 10 per call) | token | Auto-favorite |
| `ReserveSessions` | token | Auto-reserve. Opens through the API on **Oct 8, 2026** (Oct 6 in the web portal) |
| `CreatePersonalTime` | token | Block lunch, travel and meetings |

**Session fields** (exact, from `openapi.json`): `sessionId`, `title`, `abbreviation` (code), `abstract`, `type`, `level`, `venue`, `room`, `isAllDaySession`, `isReservable`, `seatAvailability` (`available|limited|veryLimited|unavailable|walkUp`), `sessionTime {date, time, length, timezone}`, `speakers[{name}]`, and the taxonomy lists `tracks`, `topics`, `industries`, `areasOfInterest`, `roles`, `services`, `segments`, `features`, `customerPersonas`, `experiences`, `additionalActivities`, `focusAreas`. Only `sessionId` and `title` are guaranteed.
→ The schema has `venue`, so feature #3 is feasible. It needs a fallback that parses `room` for events that leave `venue` out.

**Schedule:** `reserved` and `favorites` are session-ID lists, plus `personalTime[]`. **Bulk writes** return `{result: {successful[], failed[{sessionId, code, conflictsWith[]}]}}`. The codes are `sessionNotReservable`, `scheduleConflict`, `alreadyScheduled`, `sessionFull`, `insufficientAccess`, `timePassed`, `alreadyFavorited`, `notFavorited` and `other`.

**MCP server** (`https://api.awsevents.com/mcp`, streamable HTTP): exposes the same 12 operations as tools. It requires sign-in on **every** call, including catalog reads, and the client does its own OAuth on a fixed localhost callback.

**Behaviour that shapes the design** (from the guide's *Quotas* and *Handling errors* pages):
- **Quotas per attendee per minute:** `ReserveSessions` and `AssociateFavorites` 30 *sessions* (batching saves round trips, not quota). `ListSessions` and `GetSession` 120. `GetSchedule` 60. `429` comes with `Retry-After`.
- **`409` = operation closed.** Reservations return 409 until reserved seating opens. The Oct 8 job polls on 409.
- **Writes have no idempotency key.** After an unknown outcome, reconcile from `GetSchedule` (the source of truth) and send only what is missing. Single removals are safe to retry (404 = already gone).
- **`403` with a JSON body** means you are not registered for the event. **`403` with no body** means the edge is refusing you, so slow down.
- **Any session field may be absent.** Pages vary in size: only a missing `nextToken` ends the walk.

Also available: `DisassociateFavorite` (`DELETE …/favorites/{sessionId}`) and `CancelReservation` (`DELETE …/reservations/{sessionId}`). `ReserveSessions` takes 1–10 distinct IDs and returns 200 even on partial failure: the failure reasons are full, time conflict, or already reserved. Throttling returns `429` + `Retry-After`. Auth details (PKCE, localhost-only redirect, 30-day rotating refresh tokens) are in [`M0-auth-spike.md`](M0-auth-spike.md).

---

## 2. Architecture

```mermaid
flowchart LR
  subgraph UI
    W[Web app<br/>chat · schedule · map]
  end
  W -->|Cognito auth| AR

  subgraph AgentCore
    AR[AgentCore Runtime<br/>Strands agent · Claude on Bedrock]
    MEM[AgentCore Memory<br/>preferences, past plans]
    ID[AgentCore Identity<br/>Builder ID token vault]
    GW[AgentCore Gateway<br/>MCP tool endpoint]
  end
  AR <--> MEM
  AR --> GW
  GW -->|Lambda targets| T1[catalog_search]
  GW --> T4[schedule tools<br/>REST client + reconcile]
  T4 -->|REST, stored token| API
  GW --> T2[optimize_schedule]
  GW --> T3[plan_routes]
  ID -.token.-> GW

  subgraph Data
    ING[Ingest Lambda<br/>EventBridge Scheduler, every 6 h] -->|ListSessions| API[(AWS Events REST API)]
    ING --> S3[(S3 raw catalog)]
    S3 --> KB[Bedrock Knowledge Base<br/>S3 Vectors]
    ING --> DDB[(DynamoDB<br/>sessions table)]
    VG[(venue_graph.json<br/>walk/shuttle minutes)]
  end
  T1 --> KB & DDB
  T2 --> DDB
  T3 --> VG & LOC[Amazon Location Service]

  SCH[EventBridge Scheduler<br/>one-time: Oct 8 open] --> RES[Reservation Lambda]
  RES --> AR
```

### Why AgentCore rather than classic Bedrock Agents
- **MCP-native tools.** AgentCore Gateway serves all our tools as one MCP endpoint, which matches the "Bedrock agent + MCP" pitch.
- **Schedule tools call the REST API, not the Events MCP server.** The Events MCP server expects an interactive client doing its own localhost OAuth, which a server-side agent cannot do. Our REST tools also add what an LLM-driven MCP call lacks: reconcile-from-`GetSchedule`, per-session failure handling and quota pacing. For development, the Events MCP server is still handy directly in Claude Code: `claude mcp add --transport http --scope user awsevents https://api.awsevents.com/mcp --callback-port 8484 --client-id 7vmom55m1qstvq8i71ph127bfq`.
- **AgentCore Identity** stores each user's Builder ID OAuth token. That makes an unattended reservation run at 9 AM on Oct 8 possible.
- **Runtime** hosts a Strands agent (Python), which gives full control of the reasoning loop. Classic action groups are more rigid.
- Fallback: if AgentCore is unavailable in the chosen region, the same Strands agent runs on Lambda with an MCP client.

---

## 3. Feature 1: Schedule Optimizer

The LLM should not solve the scheduling puzzle itself, because it is poor at combinatorial constraints. The work is split into four steps:

```
goals text ──► (1) Preference extraction (LLM → JSON constraints)
            ──► (2) Candidate retrieval (RAG + metadata filters, ~150 sessions)
            ──► (3) Relevance scoring (LLM, batched, cached per session+profile)
            ──► (4) CP-SAT solver (OR-Tools) → optimal schedule
            ──► (5) Explain + human approval ──► favorites / reserve / personal time
```

**(1) Constraint schema** (stored in AgentCore Memory, editable in the UI):
```json
{
  "interests": ["serverless", "event-driven", "Step Functions"],
  "levels": {"min": 300},                 // "avoid intro-level" → exclude 100/200
  "session_types": ["chalk talk", "workshop", "breakout"],
  "blocked": [{"daily": "12:00-13:00", "label": "Lunch"}],
  "max_walk_minutes_between": 15,
  "walk_weight": 0.6,                     // how strongly to penalise venue hops
  "days": ["2026-11-30", "..."],
  "max_sessions_per_day": 5,
  "must_include": [], "exclude": []
}
```
**Hard constraints:** no time overlap, blocked windows (lunch and existing `GetSchedule` personal time), and feasible travel. Travel is feasible when `end_i + travel(venue_i, venue_j) + buffer ≤ start_j`.
**Soft objective:** maximize `Σ relevance·x − λ·Σ travel_minutes − μ·venue_switches`, plus a small bonus for sessions that are less full (they are easier to get).

**(4) Solver:** OR-Tools CP-SAT with one boolean per candidate and pairwise conflict constraints. About 150 candidates × 5 days solves in under a second. It returns the **top plan plus ranked alternates** for each slot, which the reservation run uses when a session fills up.

**(5) Write actions, always with an approval gate:**
- The agent shows a diff (+ reserve, + favorite, + personal time). Nothing is written until you click **Approve**. The approved plan is saved in DynamoDB as `plan_version`.
- **Favorites:** `AssociateFavorites` in batches of 10. Allowed right away.
- **Personal time:** `CreatePersonalTime` for lunch and other blocks.
- **Reservations (Oct 8):** a one-time EventBridge Scheduler job fires just before API open. The Lambda loads the approved plan and polls `ReserveSessions` with the first batch while it returns `409` (closed). Once open, it reserves in priority order, paced to the 30 sessions/min quota, so the highest-value sessions go first. After each batch it reads `GetSchedule` back as the source of truth. When a session is full it takes the next alternate that is still feasible, then re-runs the solver for the rest of the plan. It sends a summary email (SNS).

---

## 4. Feature 2: Catalog RAG Q&A

**Ingest (EventBridge, every 6 h, plus on demand):**
- Page through `ListSessions` for re:Invent 2026 and 2025 (if 2025 is still served), then write raw JSON to S3.
- Normalize into one document per session. The **abstract, title and speakers** are the text. **Level, type, tracks, services, day, venue and year** are metadata.
- Write to a **Bedrock Knowledge Base** backed by **S3 Vectors** (cheapest option at about 3–5k docs), using Titan Text Embeddings v2. Also upsert into DynamoDB for exact lookups and the solver.

**Query:**
1. The agent's `catalog_search(query, filters)` tool runs a KB retrieve with metadata filters. Example: "zero-ETL Aurora → Redshift" becomes semantic search filtered to `services ∋ Aurora|Redshift`.
2. A reranker (Bedrock Rerank, Cohere) narrows the results to the top 10.
3. Claude answers **with session codes cited**. Each code becomes a clickable card with "favorite" and "add to plan" buttons, which connects the Q&A back to the optimizer.

**"What's new vs last year":** retrieve from each year separately (`year` filter), then have the LLM compare the two sets and summarize new themes and services. If the API no longer serves 2025, a small one-off 2025 snapshot is loaded from public sources (open question Q4).

**Evaluation:** a gold set of about 30 questions with expected session codes, reporting recall@10. It runs in CI against fixture data.

---

## 5. Feature 3: Spatial layer

The catalog gives each session a venue and room. We add the geography.

- **`venue_graph.json`** (hand-curated, checked in): lat/lon for each re:Invent campus venue, plus a **walk-minute matrix and shuttle-minute matrix** between them. Values are seeded from Amazon Location `CalculateRouteMatrix` (walking) and then corrected by hand for indoor routes (casino walks are longer than the straight line suggests). It also holds room → floor/zone offsets for each venue (e.g., "Venetian Level 5" = +5 min from lobby).
- **`plan_routes(schedule)`** returns for each day an ordered itinerary with *leave-by* times, walk or shuttle mode, and buffer slack. Transitions are flagged **green/amber/red**, where red means slack below 5 min. The solver uses the same matrix, so the plan and the route never disagree.
- **Clustering:** a soft objective term ("stay on one campus per half-day") makes days naturally cluster by venue. The UI shows the resulting "home venue" for each block.
- **Map:** MapLibre GL with an Amazon Location Service basemap. It shows venue pins numbered in session order, a route polyline, and a timeline scrubber. Indoor floor maps are out of scope for v1 (licensing); floor is shown as text.

---

## 6. Agent design

- **Model:** Claude on Bedrock, the latest Sonnet-class model for the conversation loop. A Haiku-class model does bulk relevance scoring, for cost.
- **Framework:** Strands Agents (Python) on AgentCore Runtime.
- **Tools the agent sees:**

| Tool | Source |
|---|---|
| `catalog_search`, `get_session_details` | Lambda (KB + DDB) |
| `get_my_schedule`, `add_favorites`, `reserve_sessions`, `add_personal_time` | Lambda (REST client, tokens from Secrets Manager) |
| `update_preferences`, `optimize_schedule`, `plan_routes`, `propose_plan`, `approve_plan` | Lambda |

- **Guardrails:** write tools reject calls without a current `approved_plan_id`, so the model cannot reserve on its own initiative. A Bedrock Guardrail filters topics and PII.
- **Memory:** short-term memory holds the conversation. Long-term memory holds preferences and past plans, so "re-plan Tuesday, I have a customer meeting 2–3" works.

## 7. UI

For v1, a **Streamlit** app (`ui/`) that **runs locally** on the attendee's machine. It has three tabs: **Chat**, **Schedule** (a day grid showing reserved, favorite and personal time) and **Map** (pydeck/folium). It calls the AgentCore Runtime endpoint in `us-east-1` using the user's AWS credentials.

**Builder ID sign-in happens inside the app.** A "Sign in with AWS Builder ID" button runs the PKCE flow and starts the callback listener in a background thread on the first free reserved port (`8484`–`8489`; Streamlit itself stays on 8501). This works because the app runs locally, and the Events API only accepts a localhost redirect (see the M0 spike). A second button, "Enable unattended reservations", pushes the tokens to Secrets Manager.

A hosted UI (React on Amplify, or Streamlit on App Runner) is deferred. It could not sign users in itself: the API has no hosted redirect, and the guide says apps that sign attendees in must run locally.
A **CLI** (`reinvent-agent chat`) is also provided for fast development and demos.

## 8. Repo layout & tooling

```
infra/            CDK (Python) stacks: data, agent, api, web, scheduler
agent/            Strands agent, prompts, tool schemas
tools/            Lambda tools: catalog_search, optimizer (OR-Tools), routes
ingest/           ListSessions paginator, normalizer, KB sync
spatial/          venue_graph.json + builder script (Location Service)
ui/               Streamlit app (chat, schedule grid, map, Builder ID sign-in)
tests/            unit + fixture-based integration + RAG eval
fixtures/         recorded API responses (sanitized)
```
Python 3.12, uv, ruff and pytest, with GitHub Actions CI running lint, tests and `cdk synth`.

## 9. Delivery plan (reservation opens via API **Oct 8**)

| Milestone | Target | Scope |
|---|---|---|
| M0 | Sep 28 | Repo skeleton, CDK base, Events API client + fixtures, Builder ID auth spike |
| M1 | Oct 2 | Ingest → KB + DDB, `catalog_search`, CLI Q&A, RAG eval |
| M2 | Oct 6 | Preferences, scoring, CP-SAT optimizer, approval flow, favorites + personal time |
| M3 | **Oct 7** | Reservation Lambda + one-time schedule, dry-run against staging/fixtures |
| M4 | Oct 14 | Venue graph, `plan_routes`, map UI |
| M5 | Oct 21 | Web UI polish, "what's new vs 2025", demo script |

## 10. Decisions (approved 2026-09-25)
**M1 implementation notes (2026-09-25):**
- **S3 Vectors is used directly, not through a Bedrock Knowledge Base.** There is one document per session with structured metadata, so the KB's chunking and ingestion jobs add nothing. Direct `PutVectors`/`QueryVectors` gives exact metadata filters (level, day, venue, type, services, time window) at lower cost. Titan Text Embeddings v2 (1024-dim, cosine).
- **Venue inference:** 621 of 1,603 live sessions have no `venue`. It is learned from room names of sessions that do have one (`catalog/venues.py`); `venueSource` records how each venue was set.
- **Q&A model:** Claude Opus 5 on Amazon Bedrock (`anthropic.claude-opus-5`, Anthropic SDK Bedrock Mantle client) drives a `catalog_search` tool through the SDK tool runner. The reranker is deferred until the eval set shows it is needed.
- **Deployment:** us-east-1, default AWS profile. `ReinventAgentData` and `ReinventAgentSearch` stacks.

Recommendations accepted: AgentCore + Strands, S3 Vectors + reranker, **Streamlit UI run locally** (React deferred), region **`us-east-1`**, push-notification fallback for unattended auth. Q1 is resolved by the M0 spike. Q3 (network access) and Q5 (AWS account) are still open.

### Original risks & open questions

| # | Question / risk | My proposal |
|---|---|---|
| Q1 | **Unattended auth:** can a Builder ID token (or its refresh token) be stored and used at 9 AM Oct 8 without you present? | Spike in M0. If it can't, fall back to a push notification plus one-tap "Run reservations now". |
| Q2 | **ToS / fairness** of automated reservation | Only reserve your approved plan, for your own account, at human-like pace (no hammering). Respect rate limits. |
| Q3 | **Network access:** this cloud sandbox is blocked from `docs.aws.amazon.com` and `api.awsevents.com` | Allow both hosts in the environment's network policy, or commit the OpenAPI spec to the repo. Until then I'll build against recorded fixtures. |
| Q4 | Is re:Invent 2025's catalog still served, for "what's new"? | Check in M0. If not, use a static snapshot. |
| Q5 | AWS account and region for deployment | `us-east-1` or `us-west-2` (AgentCore + S3 Vectors availability). Which account? |
| Q6 | UI scope: full React web app, or CLI + Streamlit for a hackathon-speed demo? | React (above). Tell me if you'd rather go faster with Streamlit. |
| Q7 | Vector store: S3 Vectors (cheap, simple) vs OpenSearch Serverless (hybrid keyword + vector, about $350/mo floor) | S3 Vectors + reranker |

**Rough cost** (for one user, during the conference window): under $30/month. Most of it is Bedrock tokens. S3 Vectors, DynamoDB, Lambda and Location Service are pennies at this scale.
