# k6 Load Test Scripts

## Scripts

| Script | Purpose | VUs | Duration |
|---|---|---|---|
| `read_scenario.js` | Exercises search, capability detail, dependency, and MCP tool calls | 1,000 | 30 min |
| `write_scenario.js` | Simulates ~50 writes/min via 1 VU | 1 | 30 min |

## SLO Thresholds

| Tag | p(95) | p(99) |
|---|---|---|
| `type:read` | < 200 ms | < 500 ms |
| `type:mcp` | < 500 ms | — |
| `type:write` | — | < 500 ms |

## Environment Variables

All three variables are required at runtime. Defaults fall back to the local dev stack.

| Variable | Description | Default |
|---|---|---|
| `BASE_URL` | Root URL of the catalog API | `http://localhost:8000` |
| `API_TOKEN` | Bearer token with appropriate scope | `dev-token` |
| `TENANT_ID` | UUID of the tenant used to scope requests | `00000000-0000-0000-0000-000000000001` |

## Running

### Smoke test (10 VUs, 1 min)

```bash
BASE_URL=http://localhost:8000 \
API_TOKEN=dev-token \
TENANT_ID=00000000-0000-0000-0000-000000000001 \
k6 run --vus 10 --duration 1m scripts/load_test/read_scenario.js
```

### Full SLO gate (runs as part of the release pipeline)

Run both scenarios in parallel using separate terminals or processes:

```bash
# Terminal 1 — read traffic
k6 run scripts/load_test/read_scenario.js

# Terminal 2 — write traffic (run concurrently)
k6 run scripts/load_test/write_scenario.js
```

The full run is **not** executed on normal PRs — only on the release gate.

## Seed Data

`read_scenario.js` references five hard-coded seed IDs (`cap-seed-001` … `cap-seed-005`).
These must exist in the target environment before running. The search endpoint auto-discovers
live IDs for the search portion of the workload.

Pre-seed with:

```bash
for i in 001 002 003 004 005; do
  curl -s -X POST "${BASE_URL}/v1/capabilities" \
    -H "Authorization: Bearer ${API_TOKEN}" \
    -H "X-Tenant-Id: ${TENANT_ID}" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"cap-seed-${i}\",\"description\":\"Seed for load test\",\"version\":\"1.0.0\"}"
done
```
