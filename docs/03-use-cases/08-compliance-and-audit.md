<!--
  title: Use case — Compliance and audit over a regulated capability inventory
  audience: operator, operator agent
  archetype: explanation (use-case scenario)
  summary: How to use the bi-temporal data model, audit partitioning, and PII scanning to maintain a compliant, auditable capability inventory.
-->

# Use case: Compliance and audit over a regulated capability inventory

Organizations subject to change-management requirements or data-handling regulations need a capability inventory that is not just current but fully auditable: every write must be traceable, historical states must be reconstructible without touching current data, and sensitive field values must be identified and controlled. The registry's bi-temporal model, audit partition archival, and PII scanning policies address all three.

Every mutable row carries both valid-time (when the fact was true in the world) and transaction-time (when it was recorded) axes, so any historical state can be reconstructed with a point-in-time query. Audit partitions are archived on a configurable schedule. The PII scanner runs on fact and attribute writes and applies per-tenant field policies to flag or redact sensitive values before they are stored.

---

## Preconditions

- At least one actor in the tenant has the `auditor` or `admin` role. The `auditor` role is read-only: it can call `GET /v1/admin/audit`, read capabilities, and read annotations, but it cannot write anything.
- The `audit_partition_max_age_days` setting is configured in the deployment environment. Partitions older than this threshold are archived during the nightly maintenance window. Check `GET /healthz` or the operator runbook for the current value.
- Any PII patterns and field policies you need enforced are already registered (see Step 3 below). The scanner applies policies that exist at write time — it does not retroactively rescan stored data.

---

## Step 1 — Reconstruct a past state with bi-temporal queries

Every capability, attribute, and annotation row carries two time axes:

- **Valid time** (`t_valid_from` / `t_valid_to`) — when the fact was true in the world.
- **Transaction time** (`t_ingested_at` / `t_invalidated_at`) — when it was recorded in the registry.

The `?as_of=<iso8601>` parameter on capability reads selects the valid-time slice you want. This is the primary mechanism for reconstructing what the registry believed at any past instant without modifying any current data.

**Example — reconstruct the state of a capability before a lifecycle transition:**

```bash
# What did identity-service look like before the GA promotion on 2026-02-15?
curl -s \
  "https://registry.example.com/v1/capabilities/<entity_id>?as_of=2026-02-14T23:59:59Z" \
  -H "Authorization: Bearer <auditor-token>" \
  | jq '{entity_id, name, lifecycle, attributes}'
```

The response reflects the capability's state as recorded by that timestamp on the valid-time axis. If the capability did not exist at that time, the response is 404.

**Example — reconstruct the full capability list as of a past date:**

```bash
curl -s \
  "https://registry.example.com/v1/capabilities?as_of=2026-01-01T00:00:00Z" \
  -H "Authorization: Bearer <auditor-token>" \
  | jq '{total: .total, items: [.items[] | {entity_id, name, lifecycle}]}'
```

This is useful for change-management reports: run the query twice (at `T-before` and `T-after` a release window) and diff the responses to enumerate every capability that changed state.

---

## Step 2 — Query the audit log

The audit log is queryable via `GET /v1/admin/audit`. Tenant scope is injected from the caller's auth context — you cannot query another tenant's audit log. Results are returned in descending order by `(ts, audit_id)` with keyset pagination.

**Query parameters:**

| Parameter | Type | Meaning |
|---|---|---|
| `actor_id` | UUID | Filter to events emitted by a specific actor |
| `action` | string | Filter by action name (e.g. `LIFECYCLE_STATE_CHANGED`, `PROGRESSION_OVERRIDE_CREATED`) |
| `target_type` | string | Filter by entity type (e.g. `capability`, `annotation`) |
| `target_id` | UUID | Filter to events on a specific entity |
| `from` | ISO 8601 datetime | Earliest event timestamp to include |
| `to` | ISO 8601 datetime | Latest event timestamp to include |
| `cursor` | string | Pagination cursor from the previous page's `next_cursor` field |
| `page_size` | integer | Results per page (1–500, default 50) |

**Example — pull all events for a specific capability over the last 30 days:**

```bash
curl -s \
  "https://registry.example.com/v1/admin/audit?target_id=<entity_id>&from=2026-04-14T00:00:00Z&to=2026-05-14T23:59:59Z" \
  -H "Authorization: Bearer <auditor-token>" \
  | jq '.items[] | {ts, actor_id, action, detail}'
```

**Example — find all progression overrides across the tenant:**

```bash
curl -s \
  "https://registry.example.com/v1/admin/audit?action=PROGRESSION_OVERRIDE_CREATED" \
  -H "Authorization: Bearer <auditor-token>" \
  | jq '.items[] | {ts, actor_id, target_id, detail: .detail.reason}'
```

**Pagination.** When `next_cursor` is present in the response, there are more results. Pass it as `?cursor=<value>` in the next call:

```bash
# Page 2
curl -s \
  "https://registry.example.com/v1/admin/audit?action=ANNOTATION_CREATED&cursor=<next_cursor>" \
  -H "Authorization: Bearer <auditor-token>" \
  | jq '{items_count: (.items | length), next_cursor}'
```

---

## Step 3 — Configure PII scanning policies

The PII scanner runs at write time on annotation `body` and `triage_note` fields. It applies the most restrictive matching policy from two sources: the default on the pattern, and any per-field-type override in the field policy table.

### Register a custom PII pattern

```bash
curl -s -X POST https://registry.example.com/v1/admin/pii-patterns \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "uk-national-insurance-number",
    "category": "government_id",
    "regex": "\\b[A-Z]{2}\\d{6}[A-Z]\\b",
    "policy_override": "block",
    "is_enabled": true
  }' | jq '{pattern_id, name, category, policy_override}'
```

`policy_override` sets the pattern's default enforcement level. Valid values:

| Value | Effect |
|---|---|
| `advisory` | Match is recorded internally; write proceeds; no warning to caller |
| `warn` | Match is recorded; write proceeds; `warnings` array appears in the response |
| `block` | Match causes the write to be rejected with HTTP 422 before any row is stored |

If `policy_override` is omitted, the pattern defaults to `advisory`.

### Override enforcement per field type

A field policy targets a specific field type and optionally a specific pattern. When a field policy matches, it takes precedence over the pattern's own `policy_override`:

```bash
# Block any PII in annotation body fields, regardless of category
curl -s -X POST https://registry.example.com/v1/admin/pii-field-policies \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "field_type": "annotation_body",
    "policy": "block"
  }' | jq '{policy_id, field_type, policy}'
```

To target a specific pattern on a specific field:

```bash
curl -s -X POST https://registry.example.com/v1/admin/pii-field-policies \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "field_type": "triage_note",
    "pattern_id": "<pattern_id>",
    "policy": "warn"
  }' | jq '{policy_id, field_type, pattern_id, policy}'
```

The scanner resolves effective policy by taking the most restrictive value across all applicable field policies and the pattern's own override. A field policy of `block` overrides a pattern-level `warn`.

### What callers see at write time

When a `warn`-level match fires on `POST /v1/capabilities/{capability_id}/annotations`, the annotation is written but the response body includes a `warnings` array:

```json
{
  "annotation_id": "...",
  "status": "open",
  "warnings": [
    {"pattern_id": "...", "category": "government_id", "field": "body"}
  ]
}
```

A `block`-level match returns HTTP 422 and no row is written:

```json
{
  "detail": "PII block policy matched in field 'body': category=government_id"
}
```

---

## Step 4 — Audit trail for progression overrides

Every progression gate bypass writes an audit event **before** the override row is inserted. The `audit_event_id` in the override response is the foreign key into the audit log for that bypass. This ordering guarantee means the audit record exists even if the override row fails to insert — the bypass is never invisible.

**Query all overrides for a capability:**

```bash
curl -s \
  "https://registry.example.com/v1/admin/tenants/<tenant_id>/entities/<entity_id>/progression-overrides" \
  -H "Authorization: Bearer <admin-token>" \
  | jq '.items[] | {override_id, from_state, to_state, gate_id, reason, authorized_by, audit_event_id}'
```

**Verify each override has a corresponding audit event:**

```bash
curl -s \
  "https://registry.example.com/v1/admin/audit?action=PROGRESSION_OVERRIDE_CREATED&target_id=<entity_id>" \
  -H "Authorization: Bearer <auditor-token>" \
  | jq '.items[] | {ts, actor_id, detail}'
```

For a formal change-management report, join the two: the override row carries the human-readable `reason`; the audit event carries the actor, timestamp, and immutable transaction record.

---

## Step 5 — Generate a change history report for a capability

For a complete picture of everything that happened to a capability — attribute changes, lifecycle transitions, annotation activity, and override bypasses — query the audit log filtered by `target_id` with a time window:

```bash
curl -s \
  "https://registry.example.com/v1/admin/audit?target_id=<entity_id>&from=2026-01-01T00:00:00Z" \
  -H "Authorization: Bearer <auditor-token>" \
  | jq '[.items[] | {ts, action, actor_id, detail}]'
```

Paginate until `next_cursor` is absent to get the full log. For a machine-readable report, pipe to `jq -r '... | @csv'` or send to your SIEM directly.

**Key action names to watch for:**

| Action | Meaning |
|---|---|
| `CAPABILITY_CREATED` | New capability registered |
| `CAPABILITY_UPDATED` | Attributes or metadata changed |
| `LIFECYCLE_STATE_CHANGED` | Lifecycle advanced or rolled back |
| `VISIBILITY_CHANGED` | Visibility or shared tenant list updated |
| `PROGRESSION_OVERRIDE_CREATED` | Gate bypassed — always present before any override row |
| `ADOPTION_CREATED` | A consumer declared a dependency |
| `ADOPTION_DELETED` | A consumer removed a dependency |
| `ANNOTATION_CREATED` | Feedback submitted |
| `ANNOTATION_TRIAGED` | Status or triage note updated |

---

## Audit partition archival

The audit table is partitioned by month (`audit_YYYY_MM`). The `audit_partition_max_age_days` setting controls how many days of partitions the registry retains in the live database before archival:

- Partitions older than the threshold are detached and can be exported to cold storage via the operator runbook (`docs/06-runbooks/`).
- Detached partitions are no longer queryable through `GET /v1/admin/audit` — they must be restored to a read-replica or imported into an analytics store to be queried.
- The current partition is always live and queryable. The nightly archival job does not touch the current month's partition.

If your retention policy requires audit data to remain queryable through the API for longer than the live-database window, increase `audit_partition_max_age_days` in the deployment configuration or export partitions to a queryable archive before they age out.

---

## Role separation: auditor vs. admin

| Capability | `auditor` | `admin` |
|---|---|---|
| `GET /v1/admin/audit` | Yes | Yes |
| Read capabilities and annotations | Yes | Yes |
| `POST /v1/admin/pii-patterns` | No | Yes |
| `POST /v1/admin/pii-field-policies` | No | Yes |
| `POST /v1/admin/tenants/{id}/progression-definitions` | No | Yes |
| `POST /v1/admin/tenants/{id}/entities/{id}/progression-overrides` | No | Yes |
| Write to any capability, annotation, or subscription | No | Yes (with producer role) |

The `auditor` role is designed for compliance team members and automated audit agents that should be able to verify state and history but must never be able to alter it. Grant it separately from `producer` and `admin` — a single actor can hold multiple roles, but the auditor role alone is sufficient for all read-only compliance workflows described in this document.

---

## See also

- [Authentication](../01-overview/04-authentication.md) — JWT structure and OIDC setup
- [Authorization](../01-overview/05-authorization.md) — role grants and entitlement strings
- [Platform team shared registry](02-platform-team-shared-registry.md) — progression definitions, lifecycle governance, and override usage
- [Consumer feedback and requests](05-consumer-feedback-and-requests.md) — annotation audit events
- [Disaster recovery runbook](../06-runbooks/runbook-dr.md) — partition archival and restore procedures
- [API reference](../05-reference/01-api.md) — endpoint contracts for audit, PII, and progression endpoints
