# Fixtures

| Path | Source |
|---|---|
| `openapi.json` | Live OpenAPI description, `https://api.awsevents.com/v1/openapi.json` (v1.0.0, fetched 2026-09-25) |
| `public/events.json` | Live `ListEvents?includePast=true`, trimmed to three events |
| `public/cloudturkiye2026_sessions.json` | Live `ListSessions` for a public event (no sign-in needed), trimmed to 5 sessions |
| `reinvent2026/*.json` | **Synthetic**, in the exact shapes from `openapi.json`. The re:Invent catalog requires a registered attendee's token. |

To replace the synthetic catalog with a real one once signed in:

```bash
reinvent-agent auth login
reinvent-agent catalog dump --event reinvent2026 --out fixtures/reinvent2026/catalog.jsonl
```
