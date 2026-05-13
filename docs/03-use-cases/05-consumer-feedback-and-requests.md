<!--
  title: Use case — Consumer feedback and feature requests
  audience: integrator (consumer), integrator (producer), end-user agent
  archetype: explanation (use-case scenario)
  summary: How a consumer that depends on a producer's capability submits feedback, defects, and feature requests back into the registry, where the signal routes to the producer's human or agentic triage queue.
-->

# Use case: Consumer feedback and feature requests

A team that adopts a shared capability will eventually encounter a gap: an interface that does not behave as documented, a missing feature, or a bug that only surfaces under their workload. Without a return channel, that signal stays scattered across Jira, email, or feature request tools with weak traceability and no seamless agentic intake — the producer ships blind, and the consumer is stuck waiting. The registry closes this loop: any authenticated consumer can post an annotation directly on the capability it depends on. The annotation carries a category drawn from a closed vocabulary, an optional version target, and a cross-tenant attribution so the producer always knows which adopter raised the signal. Agentic systems can poll and triage feedback directly without manual ticket entry.

This is the inverse of the [event-driven consumers](04-event-driven-consumers.md) use case: rather than the producer signaling consumers about lifecycle events, the consumer signals the producer about needs. Together, these two use cases form a bidirectional channel between producers and consumers, with the registry as the durable, auditable hub.

---

## The channel

Annotations are the carrier for consumer feedback. There is no dedicated feedback router: the mechanism is `POST /v1/capabilities/{capability_id}/annotations` (REST) or the `submit_annotation` MCP tool. Both entry points write to the same record.

**Category vocabulary.** Every annotation must specify one of five categories, enforced by a CHECK constraint in the database and validated again at the service layer:

| Category | Meaning |
|---|---|
| `bug` | A defect — observed behavior diverges from the declared interface or documentation |
| `suggestion` | A feature request or enhancement to the capability |
| `feedback` | General feedback that does not fit the other categories |
| `question` | A clarifying question about behavior, usage, or interface design |
| `doc_gap` | Missing or incorrect documentation |

**Cross-tenant attribution.** The registry records two tenant identifiers on every annotation: `tenant_id` is the capability's owner tenant (the producer), and `author_tenant_id` is the submitting tenant (the consumer). These fields are set at write time and are not mutable — the producer cannot rewrite who reported what. This separation is what makes the channel usable across organizational boundaries: competing consumer teams each see only their own annotations on a given capability; the producer sees all of them.

**Lineage fields.** Three fields together give a producer enough context to filter and prioritize without an external issue tracker:

- `capability_id` — the capability the annotation targets
- `author_tenant_id` — the consumer tenant that submitted it
- `version_target` — the capability version the signal relates to (optional, recommended for bug reports)

**Example payload.** A consumer agent reporting a bug on a specific version:

```json
{
  "body": "The /export endpoint returns HTTP 200 with an empty body when the dataset is empty; the interface contract states 204.",
  "category": "bug",
  "version_target": "2.4.1"
}
```

The response includes the assigned `annotation_id`, the initial `status` (`open`), the `author_tenant_id` stamped from the calling token, and `created_at`. If the PII scanner detects sensitive data in the body at warn-level, a `warnings` array appears in the response; a block-level match rejects the annotation with HTTP 422 before any row is written.

**Before calling.** The caller's [tenant](../01-overview/03-vocabulary.md#tenant) must have visibility to the capability. Any actor with `consumer`, `producer`, or `admin` role can submit an annotation. See [auth.md](../01-overview/04-auth.md) for token scoping.

---

## Producer-side routing: human versus agentic

The registry records the annotation and makes it listable. It is neutral about what the producer does with it. Two routing patterns are common; both consume the same surface.

**Human-managed triage.** The producer's team watches their annotation queue by calling `GET /v1/capabilities/{capability_id}/annotations`. Because the caller's tenant owns the capability, the endpoint applies the provider path: it returns every active annotation across all consumer tenants, optionally filtered by status. A team triaging in a dashboard calls `PATCH /v1/annotations/{annotation_id}` (or the `triage_annotation` MCP tool) to advance the status and attach a `triage_note`. Only actors in the capability owner's tenant with `producer` or `admin` role can triage; the annotation's `author_tenant_id` is never overwritten.

**Agentic triage.** A producer can operate an automated grooming agent in front of the human queue. The agent authenticates with a `producer`-scoped token, polls the capability's annotation endpoint for `status=open` annotations, classifies them by category and urgency, and triages or routes them before a human sees them. This automated intake stage is the pattern covered in [AISDLC pipeline](07-aisdlc-pipeline.md) — the agent pipeline consumes this same annotation surface as one of its input channels.

The registry makes no assumption about which path the producer chose. Both modes call identical endpoints; the difference is who holds the polling loop or the webhook listener.

---

## What the registry guarantees

**Immutability of the consumer signal.** The body, category, `author_tenant_id`, and `author_actor_id` fields are set at create time and are not writable by the triage operation. A producer triaging an annotation can update `status`, `triage_note`, and `version_target` — nothing else. The original complaint is preserved exactly as submitted.

**Audit trail.** Every annotation write and status transition emits an audit event: `ANNOTATION_CREATED` on submit and `ANNOTATION_TRIAGED` on each status change. These events flow into the same bi-temporal audit log as every other registry mutation. See [compliance and audit](08-compliance-and-audit.md) for how the audit record is queried and archived.

**Consumer annotation privacy.** On the list endpoint, the provider path (caller is the capability owner) returns all annotations across all consumer tenants. The author path (caller is a consumer) returns only that tenant's own annotations. A consumer cannot enumerate another tenant's annotations on the same capability. The registry returns an empty list for callers with no authored annotations — not a 403.

---

## How resolution flows back

Status transitions are how a producer communicates progress. The valid statuses are `open`, `triaged`, `acknowledged`, and `closed`. Transitions in any direction are permitted — the status vocabulary is a signal, not an enforced state machine.

A typical flow: the consumer submits at `open`; a triage agent or team sets it to `triaged` with a note; a PM confirms it is on the backlog and sets `acknowledged`; resolution lands in a release and the annotation is set to `closed`. The `triage_note` field carries the producer's response text on any transition.

Consumers poll their own submitted annotations with `GET /v1/capabilities/{capability_id}/annotations` (returning only their own annotations on the author path) or the `list_my_annotations` MCP tool (scoped to the calling tenant, filtered by capability UUID). There is no dedicated notification event for annotation status changes today; consumers that want push notification of triage responses should poll.

When a bug report or feature request eventually lands in a capability release, the producer can optionally link that transition through the capability's lifecycle progression — a separate surface covered in the platform-team use case. The annotation's `closed` status and `triage_note` are the registry-side record of resolution.

---

## Where this connects

- [Event-driven consumers](04-event-driven-consumers.md) — the inverse channel: producer-to-consumer lifecycle signals via subscriptions and webhooks.
- [AISDLC pipeline](07-aisdlc-pipeline.md) — a worked example of the agentic triage pattern, where an intake stage processes this annotation channel automatically.
- [Compliance and audit](08-compliance-and-audit.md) — annotation events are part of the bi-temporal audit record for a capability.
- [AI agent capability discovery](01-ai-agent-capability-discovery.md) — the discovery flow where a `doc_gap` annotation is the natural response when an agent finds an undocumented interface.

---

## Read next

- [API reference — annotations](../05-reference/01-api.md) — endpoint contracts for `POST`, `GET`, and `PATCH` on annotations
- [MCP tools reference](../05-reference/02-mcp-tools.md) — parameter tables for `submit_annotation`, `list_my_annotations`, and `triage_annotation`
- [Auth](../01-overview/04-auth.md) — how to obtain a token with the right role for the annotation surface
