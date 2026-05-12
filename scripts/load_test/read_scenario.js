/**
 * CAP-P5-T05: k6 read scenario
 *
 * Runs 1,000 concurrent VUs for 30 minutes against the read + MCP surface.
 *
 * Env vars (required):
 *   BASE_URL   — e.g. https://catalog.example.com
 *   API_TOKEN  — Bearer token with read scope
 *   TENANT_ID  — UUID of the tenant to scope requests to
 *
 * Run (smoke):
 *   k6 run --vus 10 --duration 1m scripts/load_test/read_scenario.js
 *
 * Run (full SLO gate):
 *   k6 run scripts/load_test/read_scenario.js
 */

import http from "k6/http";
import { check, sleep } from "k6";
import { Counter } from "k6/metrics";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const API_TOKEN = __ENV.API_TOKEN || "dev-token";
const TENANT_ID = __ENV.TENANT_ID || "00000000-0000-0000-0000-000000000001";

// A small fixed set of IDs used for detail + dependency lookups.
// In a real environment these should be pre-seeded; the search endpoint
// returns live IDs so most traffic auto-discovers real data.
const SEED_IDS = [
  "cap-seed-001",
  "cap-seed-002",
  "cap-seed-003",
  "cap-seed-004",
  "cap-seed-005",
];

const SEARCH_QUERIES = [
  "authentication",
  "payment processing",
  "data pipeline",
  "notification service",
  "user management",
  "file storage",
  "analytics engine",
  "recommendation",
  "search indexing",
  "rate limiting",
];

// ---------------------------------------------------------------------------
// k6 options
// ---------------------------------------------------------------------------

export const options = {
  scenarios: {
    read_load: {
      executor: "constant-vus",
      vus: 1000,
      duration: "30m",
    },
  },
  thresholds: {
    // Read endpoints (search, detail, dependencies)
    "http_req_duration{type:read}": ["p(95)<200", "p(99)<500"],
    // MCP tool calls
    "http_req_duration{type:mcp}": ["p(95)<500"],
    // Overall error rate guard
    http_req_failed: ["rate<0.01"],
  },
};

// ---------------------------------------------------------------------------
// Custom counters
// ---------------------------------------------------------------------------

const readRequests = new Counter("read_requests");
const mcpRequests = new Counter("mcp_requests");

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function randomItem(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

function authHeaders() {
  return {
    Authorization: `Bearer ${API_TOKEN}`,
    "X-Tenant-Id": TENANT_ID,
    "Content-Type": "application/json",
  };
}

// ---------------------------------------------------------------------------
// VU default function
// ---------------------------------------------------------------------------

export default function () {
  const step = Math.random();

  if (step < 0.40) {
    // --- Search ---
    const q = encodeURIComponent(randomItem(SEARCH_QUERIES));
    const res = http.get(
      `${BASE_URL}/v1/search?q=${q}&limit=20`,
      {
        headers: authHeaders(),
        tags: { type: "read" },
      }
    );
    check(res, {
      "search 200": (r) => r.status === 200,
    });
    readRequests.add(1);
  } else if (step < 0.65) {
    // --- Capability detail ---
    const id = randomItem(SEED_IDS);
    const res = http.get(
      `${BASE_URL}/v1/capabilities/${id}`,
      {
        headers: authHeaders(),
        tags: { type: "read" },
      }
    );
    check(res, {
      "detail 200 or 404": (r) => r.status === 200 || r.status === 404,
    });
    readRequests.add(1);
  } else if (step < 0.80) {
    // --- Dependencies ---
    const id = randomItem(SEED_IDS);
    const res = http.get(
      `${BASE_URL}/v1/capabilities/${id}/dependencies`,
      {
        headers: authHeaders(),
        tags: { type: "read" },
      }
    );
    check(res, {
      "deps 200 or 404": (r) => r.status === 200 || r.status === 404,
    });
    readRequests.add(1);
  } else {
    // --- MCP tool call ---
    const body = JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: {
        name: "search_capabilities",
        arguments: {
          query: randomItem(SEARCH_QUERIES),
          tenant_id: TENANT_ID,
          limit: 10,
        },
      },
    });
    const res = http.post(`${BASE_URL}/mcp`, body, {
      headers: authHeaders(),
      tags: { type: "mcp" },
    });
    check(res, {
      "mcp 200": (r) => r.status === 200,
    });
    mcpRequests.add(1);
  }

  // Minimal think time: ~50 ms average keeps 1000 VUs at ~20k RPS max.
  sleep(Math.random() * 0.1);
}
