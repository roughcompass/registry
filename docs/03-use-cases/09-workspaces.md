<!--
  title: Use case — Workspaces: private scratchpad and agent memory
  audience: integrator (consumer), integrator (producer), end-user agent
  archetype: explanation (use-case scenario)
  summary: How humans use workspaces as a private scratchpad alongside the catalog, and how agents use the same primitive as persistent cross-session memory.
-->

# Use case: Workspaces — private scratchpad and agent memory

The registry's workspace surface serves two kinds of actors doing the same essential thing: keeping context that belongs to them, not to the shared catalog.

For a human, that means a private scratchpad — evaluation notes, saved incident queries, half-formed decisions — anchored to the catalog entities they concern, invisible to other tenants and to producer teams.

For an agent, it means persistent cross-session memory — decisions written at the end of one session and retrieved at the start of the next, so reasoning does not have to be reconstructed from scratch each time.

It is the same primitive. A workspace is a container of typed, Markdown-bodied entries — `note`, `decision`, `open_question`, `saved_query`, `saved_view`, or `private_annotation` — with optional references to capability UUIDs. Visibility is determined by `owner_kind`: an `actor`-owned workspace is visible only to that actor; a `tenant`-owned workspace is visible to every actor in the owning tenant. Workspaces never cross tenant boundaries — there is no cross-tenant share mechanism.

**Before calling any workspace endpoint:** the [tenant](../01-overview/03-vocabulary.md#tenant) must be provisioned and a valid bearer token must be available. Any authenticated `consumer`, `producer`, or `admin` role can create and manage workspaces. See [authentication.md](../01-overview/04-authentication.md) for how to obtain a token.

---

## Scenario 1 — An agent recording persistent memory across sessions

An agent that evaluates capabilities during a task needs a place to record what it decided and why — so the next session can retrieve that reasoning rather than re-evaluating the catalog from scratch. Workspaces serve this directly: entries are persisted in the database and are visible to any session that presents the same actor identity.

The agent creates a personal workspace once, during its first session:

```bash
curl -X POST https://registry.example.com/v1/workspaces \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "agent-memory-capability-decisions",
    "owner_kind": "actor",
    "description": "Persistent decisions and observations recorded across agent sessions"
  }'
```

After reaching a decision about a capability, the agent writes a `decision` entry anchored to the relevant entity UUID:

```bash
curl -X POST https://registry.example.com/v1/workspaces/<workspace_id>/entries \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "kind": "decision",
    "body_md": "Adopted payments-v3 for checkout flow. Evaluated against payments-v2 and stripe-bridge. Decisive factor: payments-v3 exposes idempotency keys on all mutation endpoints.",
    "reference_ids": ["<capability-uuid>"],
    "references_jsonb": {
      "rejected": ["<payments-v2-uuid>", "<stripe-bridge-uuid>"],
      "session_id": "<agent-session-id>"
    }
  }'
```

In the next session, before evaluating the same area, the agent queries its prior decisions:

```bash
curl "https://registry.example.com/v1/workspaces/search?kind=decision&reference_ids=<capability-uuid>" \
  -H "Authorization: Bearer <token>"
```

The search returns every `decision` entry that references the target capability UUID, across the agent's personal workspaces and any tenant-owned workspaces in its tenant. The agent reconstructs its prior reasoning without re-evaluating the catalog.

**Two agents sharing a memory store.** When two agents with separate actor identities but the same tenant need a common memory — a copilot pair, a planner and an executor — one creates a tenant-owned workspace instead of an actor-owned one:

```bash
curl -X POST https://registry.example.com/v1/workspaces \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "copilot-pair-shared-memory",
    "owner_kind": "tenant",
    "description": "Shared decisions + open questions across paired agents"
  }'
```

Any actor in the owning tenant — both agents in this case — can read and write to the workspace automatically. No grant step is required, because tenant-owned workspaces inherit their visibility from tenant membership.

**Expiry caveat.** Entries without an `expires_at` persist indefinitely. An agent using workspaces for long-lived memory should omit `expires_at` when writing. If `expires_at` is set, the background expiry worker soft-invalidates the entry after that timestamp — it disappears from list and search results and is no longer useful as a memory store. Use `expires_at` only for intentionally short-lived notes, not for decisions the agent will need to recall in future sessions.

---

## Scenario 2 — An architect evaluating capability candidates

An architect is deciding whether to adopt one of three shared capabilities for a new product feature. She wants to record her findings without them becoming part of the shared annotation thread on each capability, which is visible to producer teams and other consumers.

She creates a personal workspace scoped to her actor:

```bash
curl -X POST https://registry.example.com/v1/workspaces \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "auth-service-eval-q2",
    "owner_kind": "actor",
    "description": "Evaluation notes for Q2 auth library decision"
  }'
```

The response includes a `workspace_id`. She then adds entries that reference the capability UUIDs she is evaluating:

```bash
curl -X POST https://registry.example.com/v1/workspaces/<workspace_id>/entries \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "kind": "decision",
    "body_md": "Ruling out token-service-v1: rate-limit behavior is undocumented and the owner has not responded to the open annotation for 6 weeks. See token-service-v1 annotations.",
    "reference_ids": ["<capability-uuid>"]
  }'
```

Later she adds an `open_question` entry for a point she needs to resolve before the decision is final. When the evaluation is complete, she archives the workspace with a `PATCH` — it disappears from her default listing but remains readable with `include_archived=true` if she needs to trace her reasoning later.

The capability's annotation thread, visible to the producer and other consumers, is untouched throughout. Her working notes stayed private.

---

## Scenario 3 — A platform team sharing an incident scratchpad

During a live incident, a platform team's on-call group needs a shared space to record observations, pin the queries they are using, and log decisions — all scoped to their tenant rather than any individual engineer.

They create a team workspace:

```bash
curl -X POST https://registry.example.com/v1/workspaces \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "incident-2026-05-12-db-latency",
    "owner_kind": "tenant",
    "description": "Shared scratchpad for the May 12 DB latency spike"
  }'
```

Any member of the owning tenant can read the workspace automatically — no share grant required for teammates. One engineer saves the registry query they are using to check blast radius:

```bash
curl -X POST https://registry.example.com/v1/workspaces/<workspace_id>/entries \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "kind": "saved_query",
    "body_md": "GET /v1/capabilities/<capability-id>/dependents?depth=3 — shows everything downstream of the slow query service",
    "reference_ids": ["<capability-uuid>"]
  }'
```

When the incident is resolved, the team lead adds a `decision` entry summarising the root cause and chosen fix. The workspace persists as a durable incident record that can be searched later.

---

## Visibility model

Workspaces have exactly two owner kinds. Choose at creation time; it cannot be changed afterwards.

| `owner_kind` | Visible to | Typical use |
|---|---|---|
| `actor` | Only the calling actor (`owner_actor_id` = the creator) | Personal scratchpads, agent memory across sessions, individual evaluation notes. |
| `tenant` | Every actor in the owning tenant | Team incident scratchpads, paired-agent shared memory, shared decision logs. |

A workspace never crosses tenant boundaries. There is no cross-tenant share grant — if a workspace's content needs to be visible to another tenant, the right primitive is a capability annotation (see [consumer-feedback-and-requests.md](05-consumer-feedback-and-requests.md)), an external publication of the relevant entries, or a tenant-shared workspace owned by a parent tenant that both teams belong to.

---

## Expiring entries automatically

Individual entries can carry an `expires_at` timestamp for content that is only meaningful for a bounded period — a scratchpad note during an active investigation, a saved query that will become stale once a migration completes.

```bash
curl -X POST https://registry.example.com/v1/workspaces/<workspace_id>/entries \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "kind": "note",
    "body_md": "Circuit breaker is tripped on payment-service — monitor until 18:00 UTC.",
    "expires_at": "2026-05-12T18:00:00Z"
  }'
```

A background worker runs on a schedule and soft-invalidates entries whose `expires_at` has passed. Invalidated entries are excluded from list and search results but are retained for audit and compliance purposes. Physical deletion happens only through an explicit right-to-be-forgotten admin request — not through the expiry worker.

---

## Searching across workspaces

The search endpoint returns entries from every workspace visible to the caller (their personal `actor`-owned workspaces plus every `tenant`-owned workspace in their tenant). It accepts a full-text `q` string, a `kind` filter, and `reference_ids` to find all entries that mention a specific capability:

```bash
# Find all decision entries that reference a specific capability
curl "https://registry.example.com/v1/workspaces/search?kind=decision&reference_ids=<capability-uuid>" \
  -H "Authorization: Bearer <token>"
```

Results are cursor-paginated. Entries from workspaces the caller cannot access are never included — the visibility gate runs at the service layer before any row is returned.

---

## What workspaces are not

**Not a shared annotation channel.** Annotations on capabilities (`POST /v1/capabilities/{id}/annotations`) are cross-tenant signals from consumers to producers. They are visible to the capability owner and are part of the auditable record of that capability. Workspace entries are visible only within the owning actor or tenant scope — they do not reach the capability owner. Use the `private_annotation` entry kind when you want annotation-shaped content that stays in your workspace; use `submit_annotation` when you want to signal the producer.

**Not versioned or immutable.** Entry bodies are mutable with `PATCH`. Workspaces do not version history of edits. If immutable record-keeping is required, the audit log (accessible to operators) captures workspace mutation events, but workspace entries themselves are not a substitute for an immutable audit trail.

**Not a collaboration tool for the full tenant.** Actor-owned workspaces are personal by default. They can be shared with individual actors but are not automatically visible to every member of the owning tenant — that behavior is for tenant-owned workspaces only.

**Not a vector store.** Entry bodies are full-text searchable via `GET /v1/workspaces/search` and filterable by `kind`, `reference_ids`, owner, and date range. There is no embedding-based or semantic similarity search. If an agent needs "find entries with meaning similar to X," that retrieval has to happen outside the registry — workspaces store the structured record, not the embedding index.

---

## Where this connects

- [Consumer feedback and feature requests](05-consumer-feedback-and-requests.md) — when a gap you noted in a private workspace needs to be escalated to a capability producer, `submit_annotation` is the cross-tenant channel.
- [AI agent capability discovery](01-ai-agent-capability-discovery.md) — an agent evaluating capabilities during discovery can record its reasoning in a workspace before committing to an adoption.
- [Compliance and audit](08-compliance-and-audit.md) — workspace mutation events (create, update, delete, expiry) are emitted to the audit log.

---

## Read next

- [API reference](../05-reference/01-api.md#workspaces) — endpoint contracts for `POST`, `GET`, `PATCH`, `DELETE` on workspaces and entries
- [Authentication](../01-overview/04-authentication.md) — how to obtain a bearer token
- [Authorization](../01-overview/05-authorization.md) — how role grants and tenant selection scope the token
- [PII policies guide](../04-guides/04-pii-policies.md) — workspace entry bodies are PII-scanned on write; this guide explains how policies are configured
