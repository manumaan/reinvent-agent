# Fixtures

**Synthetic.** The live API (`api.awsevents.com`) and its OpenAPI spec were not reachable from the development sandbox when these were written. They follow the shapes the developer guide describes. Treat field names as provisional.

Once you have access, replace them with sanitized recordings:

```bash
reinvent-agent auth login
reinvent-agent catalog dump --event reinvent2026 --out fixtures/reinvent2026/catalog.jsonl
curl -s https://api.awsevents.com/v1/openapi.json -o fixtures/openapi.json
```
