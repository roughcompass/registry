/**
 * CAP-P5-T05: k6 write scenario
 *
 * Simulates ~50 writes/min (≈ 1 write every 1.2 s) using 1 VU in a tight loop.
 * Keeps write throughput low so it does not saturate the outbox during read tests.
 *
 * Env vars (required):
 *   BASE_URL   — e.g. https://catalog.example.com
 *   API_TOKEN  — Bearer token with write scope
 *   TENANT_ID  — UUID of the tenant to scope requests to
 *
 * Run (smoke):
 *   k6 run --vus 1 --duration 1m scripts/load_test/write_scenario.js
 *
 * Run (full SLO gate — run alongside read_scenario.js in separate processes):
 *   k6 run scripts/load_test/write_scenario.js
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

// ---------------------------------------------------------------------------
// k6 options
// ---------------------------------------------------------------------------

export const options = {
  scenarios: {
    write_load: {
      executor: "constant-vus",
      vus: 1,
      duration: "30m",
    },
  },
  thresholds: {
    // Write endpoint
    "http_req_duration{type:write}": ["p(99)<500"],
    // Overall error rate guard
    http_req_failed: ["rate<0.01"],
  },
};

// ---------------------------------------------------------------------------
// Custom counter
// ---------------------------------------------------------------------------

const writeRequests = new Counter("write_requests");

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let _seq = 0;

function nextSeq() {
  _seq += 1;
  return _seq;
}

function authHeaders() {
  return {
    Authorization: `Bearer ${API_TOKEN}`,
    "X-Tenant-Id": TENANT_ID,
    "Content-Type": "application/json",
  };
}

function buildPayload() {
  const seq = nextSeq();
  return JSON.stringify({
    name: `load-test-cap-${seq}`,
    description: `Load-test capability created by write_scenario.js (seq=${seq})`,
    version: "1.0.0",
    tags: ["load-test"],
    metadata: {
      owner: "load-test",
      seq: seq,
    },
  });
}

// ---------------------------------------------------------------------------
// VU default function
// 1 write every 1.2 s → ~50 writes/min from 1 VU
// ---------------------------------------------------------------------------

export default function () {
  const payload = buildPayload();

  const res = http.post(
    `${BASE_URL}/v1/capabilities`,
    payload,
    {
      headers: authHeaders(),
      tags: { type: "write" },
    }
  );

  check(res, {
    "write 201 or 200": (r) => r.status === 201 || r.status === 200,
  });

  writeRequests.add(1);

  // 1.2 s pacing → ~50 writes/min from 1 VU
  sleep(1.2);
}
