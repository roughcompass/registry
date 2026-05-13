<!--
  title: Use case — Workspaces: private context alongside the catalog
  audience: integrator (consumer), integrator (producer), end-user agent
  archetype: explanation (use-case scenario)
  summary: How individuals and teams use workspaces to keep private notes, decisions, and queries anchored to catalog entities — without adding noise to the shared catalog.
-->

# Use case: Workspaces — private context alongside the catalog

Not every piece of thinking that surrounds a capability belongs in the shared catalog. An architect evaluating three candidates wants to capture her reasoning without publishing half-formed opinions. A platform team's on-call engineer wants to pin a saved query used during incidents. An AI agent wants to record a decision that shaped a build without making it visible to unrelated consumer tenants. The registry's workspace surface gives each actor a private or team-scoped scratchpad that lives alongside the catalog — anchored to the entities it references, queryable, and subject to the same PII policies as every other surface.

A workspace is a container for entries. Each entry is a typed, Markdown-bodied note — a `note`, `decision`, `open_question`, `saved_query`, `saved_view`, or `private_annotation` — with optional references to capability UUIDs. The entries are visible only to the actor or team that owns them, plus anyone explicitly granted a share. Nothing in a workspace flows into the catalog or becomes visible to other tenants through normal registry queries.

**Before calling any workspace endpoint:** the [tenant](../01-overview/03-vocabulary.md#tenant) must be provisioned and a valid bearer token must be available. Any authenticated `consumer`, `producer`, or `admin` role can create and manage workspaces. See [auth.md](../01-overview/04-auth.md) for how to obtain a token.

---

## Scenario 1 — An architect evaluating capability candidates

An architect is deciding whether to adopt one of three shared capabilities for a new product feature. She wants to record her findings without them becoming part of the shared annotation thread on each capability, which would be visible to producer teams and other consumers.

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

Later she adds an `open_question` entry for a point she needs to resolve before the decision is final. When the evaluation is complete and the decision is recorded, she archives the workspace with a `PATCH` — it disappears from her default listing but remains readable with `include_archived=true` if she needs to trace her reasoning later.

The capability's annotation thread, visible to the producer and other consumers, is untouched throughout. Her working notes stayed private.

---

## Scenario 2 — A platform team sharing an incident scratchpad

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

Any member of the owning tenant can read the workspace automatically — no share grant required for teammates. Entries accumulate as the incident progresses. One engineer saves the registry query they are using to check blast radius:

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

## Scenario 3 — An agent recording persistent memory across sessions

An agent or copilot that evaluates capabilities during a task needs a place to record what it decided and why — so the next session can retrieve that reasoning rather than starting from scratch. The registry's workspace surface serves this directly: entries are persisted in the database and are visible to any session that presents the same actor identity.

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

In later sessions the agent retrieves its workspace by listing:

```bash
curl "https://registry.example.com/v1/workspaces" \
  -H "Authorization: Bearer <token>"
```

The response includes workspaces owned by the calling actor. The agent identifies its workspace by `name` or stores the `workspace_id` in its configuration.

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

The search returns every `decision` entry that references the target capability UUID, across all workspaces the agent owns or holds a share on. The agent can reconstruct its prior reasoning without re-evaluating the catalog from scratch.

**Two agents sharing a memory store.** When two agents with separate actor identities need a common memory — a copilot pair, a planner and an executor — one creates a tenant-owned workspace and grants the other `contributor` access:

```bash
curl -X POST https://registry.example.com/v1/workspaces/<workspace_id>/shares \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "grantee_actor_id": "<second-agent-actor-uuid>",
    "grantee_tenant_id": "<tenant-uuid>",
    "role": "contributor"
  }'
```

Both agents can now write and read from the same workspace. Reads from either actor are subject to the same visibility gate — nothing leaks to unrelated actors or tenants.

**Expiry caveat.** Entries without an `expires_at` persist indefinitely. An agent using workspaces for long-lived memory should omit `expires_at` when writing. If `expires_at` is set, the background expiry worker soft-invalidates the entry after that timestamp — it disappears from list and search results and is no longer useful as a memory store. Use `expires_at` only for intentionally short-lived notes, not for decisions the agent will need to recall in future sessions.

---

## Sharing across tenants

A tenant-owned workspace can be shared with actors in other tenants. An actor-owned workspace can only be shared within the same tenant.

To grant another team's engineer read access to a workspace:

```bash
curl -X POST https://registry.example.com/v1/workspaces/<workspace_id>/shares \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "grantee_actor_id": "<actor-uuid>",
    "grantee_tenant_id": "<their-tenant-uuid>",
    "role": "reader"
  }'
```

`role` must be `reader` (read-only access to entries) or `contributor` (can add and edit entries). The workspace owner or an admin can revoke a share at any time with `DELETE /v1/workspaces/<workspace_id>/shares/<share_id>`.

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

The search endpoint returns entries from all workspaces the caller owns or holds an active share on. It accepts a full-text `q` string, a `kind` filter, and `reference_ids` to find all entries that mention a specific capability:

```bash
# Find all decision entries that reference a specific capability
curl "https://registry.example.com/v1/workspaces/search?kind=decision&reference_ids=<capability-uuid>" \
  -H "Authorization: Bearer <token>"
```

Results are cursor-paginated. Entries from workspaces the caller cannot access are never included — the visibility gate runs at the service layer before any row is returned.

---

## What workspaces are not

**Not a shared annotation channel.** Annotations on capabilities (`POST /v1/capabilities/{id}/annotations`) are cross-tenant signals from consumers to producers. They are visible to the capability owner and are part of the auditable record of that capability. Workspace entries are visible only to the workspace owner and explicit share holders — they do not reach the capability owner. Use `private_annotation` entry kind when you want annotation-shaped content that stays in your workspace; use `submit_annotation` when you want to signal the producer.

**Not versioned or immutable.** Entry bodies are mutable with `PATCH`. Workspaces do not version history of edits. If immutable record-keeping is required, the audit log (accessible to operators) captures workspace mutation events, but workspace entries themselves are not a substitute for an immutable audit trail.

**Not a collaboration tool for the full tenant.** Actor-owned workspaces are personal by default. They can be shared with individual actors but are not automatically visible to every member of the owning tenant — that behavior is for tenant-owned workspaces only.

**Not a vector store.** Entry bodies are full-text searchable via `GET /v1/workspaces/search` and filterable by `kind`, `reference_ids`, owner, and date range. There is no embedding-based or semantic similarity search. If an agent needs "find entries with meaning similar to X," that retrieval has to happen outside the registry — workspaces store the structured record, not the embedding index.

---

## Where this connects

- [Consumer feedback and feature requests](05-consumer-feedback-and-requests.md) — when a gap you noted in a private workspace needs to be escalated to a capability producer, `submit_annotation` is the cross-tenant channel.
- [AI agent capability discovery](01-ai-agent-capability-discovery.md) — an agent evaluating capabilities during discovery can record its reasoning in a workspace before committing to an adoption.
- [Compliance and audit](08-compliance-and-audit.md) — workspace mutation events (create, update, delete, share grant/revoke, expiry) are emitted to the audit log.

---

## Read next

- [API reference](../05-reference/01-api.md) — endpoint contracts for `POST`, `GET`, `PATCH`, `DELETE` on workspaces, entries, and shares
- [Auth](../01-overview/04-auth.md) — how to obtain and scope a bearer token
- [PII policies guide](../04-guides/04-pii-policies.md) — workspace entry bodies are PII-scanned on write; this guide explains how policies are configured
