# Progression Management Runbook

This runbook covers the four admin operations on progression definitions and the
one-time entity override mechanism. Audience: operators with admin access who
manage progression definitions for entity types within a tenant.

---

## 1. When to version vs override

Use the following decision tree when something about progression enforcement needs
to change.

| Situation | Operation | HTTP method | Notes |
|---|---|---|---|
| The rules are wrong for everyone — states or gates need fixing | **Version change** | `PUT` on the definition | Creates a new definition version; previous version is closed bi-temporally |
| The rules are correct but one entity needs a one-time exception | **Override** | `POST` override | Single-use; expires after the window; audited before commit |
| Rules are still being refined; you do not want to reject writes yet | **Advisory mode** | `PUT` with `is_advisory=true` | Violations are logged but not blocked |
| The entity type no longer needs progression rules at all | **Soft-delete** | `DELETE` on the definition | Sets `t_valid_to = now()`; no enforcement until a new definition is created |

When in doubt: use advisory mode while refining. Graduate to enforcing only once
a `dry_run=true` check returns zero offenders (see Section 4).

---

## 2. Creating an override

**When to use:** an entity is legitimately stuck and cannot satisfy the gate
conditions. The override allows a single transition to proceed past one specific
gate, for one entity, within a time window.

**Required role:** `admin`

**Endpoint:**

```
POST /v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides
```

**Required body fields:**

| Field | Type | Description |
|---|---|---|
| `from_state` | string | Current state the entity is in |
| `to_state` | string | Target state the entity should move to |
| `gate_id` | string | The specific gate being bypassed |
| `reason` | string | Human-readable rationale for the override |

**Optional body fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `bypass_skip_rules` | boolean | `false` | Set `true` to also bypass skip-rule enforcement; must be explicit opt-in |
| `t_valid_to` | ISO-8601 datetime | now + 1 hour | Override expiry; after this time the override cannot be consumed |

**Single-use:** once the override has been consumed (`consumed_at` is set), it
cannot be applied again. Create a new override if a second exception is needed.

**Audit trail:** the audit record (`progression.override.created`) is written and
committed before the override row is inserted. An override without an audit record
is structurally impossible.

**Example:**

```bash
curl -X POST \
  "https://api.example.com/v1/admin/tenants/TENANT_ID/entities/ENTITY_ID/progression-overrides" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "from_state": "pending_review",
    "to_state": "approved",
    "gate_id": "compliance_check",
    "reason": "Manual compliance review completed offline; ticket REF-1234"
  }'
```

To list all unconsumed overrides for an entity:

```bash
curl "https://api.example.com/v1/admin/tenants/TENANT_ID/entities/ENTITY_ID/progression-overrides?consumed=false" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

---

## 3. Destructive state-removal migration

**Destructive case:** you are removing a state from an existing definition at a
point when entities currently occupy that state. Entities in a removed state will
fail their next `stage_progression` write once the definition is enforcing.

**Procedure:**

**Step 1 — Identify affected entities with `dry_run=true`.**

Send `PUT` on the definition with the new `definition` body and `dry_run=true`.
The response lists all entities whose current state is not valid under the
proposed definition, or whose gate conditions are not satisfied.

```bash
curl -X PUT \
  "https://api.example.com/v1/admin/tenants/TENANT_ID/progression-definitions/PROGRESSION_ID" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "definition": { "states": [...] },
    "dry_run": true
  }'
```

**Step 2 — Decide on a `migration_target_state`** for each affected entity.
The target must be a valid state in the new definition.

**Step 3 — Resolve the stranded entities, then publish.**

Option A (recommended): move each affected entity to a valid state manually
before sending the `PUT` with `dry_run=false`. A subsequent `dry_run=true` must
return zero offenders before you send the final write.

Option B (force path): if manual migration is not practical, send the `PUT` with
`force=true` and a `migration_plan` string describing what bulk-move will happen
and when. The definition is written immediately. You must execute the bulk move
separately — no automatic migration is performed. The `migration_plan` string is
recorded in the `progression.definition.published` audit event so the bypass is
discoverable during any future compliance review.

```bash
curl -X PUT \
  "https://api.example.com/v1/admin/tenants/TENANT_ID/progression-definitions/PROGRESSION_ID" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "definition": { "states": [...] },
    "force": true,
    "migration_plan": "Bulk move from removed_state to valid_state scheduled for 2026-05-15 maintenance window"
  }'
```

`force=true` without `migration_plan` is rejected with HTTP 400.

---

## 4. Advisory to enforcing graduation

This is the highest-impact routine operation. A definition starts in advisory mode
(`is_advisory=true`) where progression violations are logged but not blocked.
Graduation to enforcing (`is_advisory=false`) means violations are rejected.

**Procedure:**

**Step 1 — Dry run (mandatory before production graduation).**

Send a `PUT` with the updated definition, `is_advisory=false`, and `dry_run=true`.
The response lists every entity that would be rejected under enforcing mode.
No new definition row is written.

```bash
curl -X PUT \
  "https://api.example.com/v1/admin/tenants/TENANT_ID/progression-definitions/PROGRESSION_ID" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "definition": { "states": [...] },
    "is_advisory": false,
    "dry_run": true
  }'
```

**Step 2 — Resolve each offender.**

For each entity in the offender list, either:
- Move the entity to a valid state that satisfies all required gates, or
- Create a per-entity override (Section 2) to allow it to progress past the
  blocking gate during its remediation window.

**Step 3 — Graduate when dry-run returns zero offenders.**

Send the same `PUT` with `dry_run=false` (or simply omit `dry_run`). The new
enforcing definition is written and the previous definition version is closed.

```bash
curl -X PUT \
  "https://api.example.com/v1/admin/tenants/TENANT_ID/progression-definitions/PROGRESSION_ID" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "definition": { "states": [...] },
    "is_advisory": false
  }'
```

**Tuning `force_timeout_seconds`** (default 30 seconds): the pre-flight scan
runs within this deadline. For tenants with more than 10,000 entities of the
type, increase the timeout or schedule graduation during a maintenance window.

```json
{
  "definition": { "states": [...] },
  "is_advisory": false,
  "force_timeout_seconds": 120
}
```

If the scan times out, the response is HTTP 409 with `"code": "preflight_timeout"`
and a partial offender list. No definition is written. Retry with a higher
timeout or use a maintenance window.

**Force bypass (use sparingly):** `force=true` with `migration_plan` skips the
pre-flight scan entirely and writes the enforcing definition immediately. Entities
that are currently offenders will fail their next `stage_progression` write.
The `migration_plan` string is recorded in the `progression.definition.published`
audit event. Reserve this path for situations where the pre-flight scan itself
cannot complete within an acceptable window.

---

## 5. Emergency soft-delete and rollback

**Soft-delete** removes active enforcement for an entity type without deleting
any historical records.

```bash
curl -X DELETE \
  "https://api.example.com/v1/admin/tenants/TENANT_ID/progression-definitions/PROGRESSION_ID" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

- Sets `t_valid_to = now()` on the active definition row. Returns HTTP 204.
- After soft-delete: the entity type has no active definition. All
  `stage_progression` writes for that entity type pass through without
  progression enforcement.
- Historical definition versions and audit records are preserved in full.
  Past writes remain verifiable against the definition that was active at
  the time of each write (bi-temporal history is intact).
- Emits audit event `progression.definition.soft_deleted`.

**Rollback:** create a new definition via `POST /v1/admin/tenants/{tenant_id}/progression-definitions`
with the same (or corrected) definition body. The new row becomes the active
definition immediately. All prior definition rows remain in the audit history.
There is no concept of "restoring" a closed row — creating a new version is the
correct rollback path.

---

## Role review

The `admin` role as defined in the role mapping table is the correct and
sufficient gate for override creation in the current release. Splitting out a
dedicated `override-grantor` role is deferred — deployments with stricter
separation-of-duties requirements can extend the role mapping table in their own
deployment configuration without code changes here.
