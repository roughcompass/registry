<!--
  title: Use case — Platform team running a shared registry
  audience: operator, integrator (producer)
  archetype: explanation (use-case scenario)
  summary: How a platform team provisions tenants, publishes capabilities, and governs lifecycle across many consuming teams.
-->

# Use case: Platform team running a shared registry

A platform team that maintains shared infrastructure — APIs, libraries, design systems, agent frameworks — needs a single durable catalog where consuming product teams can discover what exists, track what they depend on, and receive advance notice of breaking changes. The registry provides the multi-tenant isolation model, progression governance, and event delivery that make this work at organizational scale.

The platform team operates as both administrator (provisioning tenants, managing progression definitions) and producer (registering and lifecycling capabilities). Consumer teams each get their own tenant, declare adoptions of capabilities they depend on, and subscribe to lifecycle events so they are notified before a deprecation or breaking change lands.

---

## Preconditions

Before you start, you need:

- An OIDC IdP configured (or the local `mock-oauth2-server` running via `make dev-jwt`). Every call requires a JWT in `Authorization: Bearer <token>`.
- An entitlement service where you can grant role strings of the form `<tenant_slug>_REGISTRY_<ROLE>`. The registry JIT-creates a tenant the first time a JWT carrying a valid entitlement is used — there is no separate "create tenant" API call.
- The platform team's `admin`-role token for provisioning vocabulary and progression definitions.
- Consumer team slugs agreed in advance so you can grant their entitlements before they log in.

---

## Step 1 — Bring producer and consumer tenants online

There is no `POST /v1/admin/tenants` endpoint. A tenant materializes automatically the first time an entitlement for it resolves. To bring the platform team's producer tenant online, grant an entitlement in your IdP and make any authenticated call:

```bash
# Verify that the platform team's tenant has been created
curl -s https://registry.example.com/v1/whoami \
  -H "Authorization: Bearer <platform-admin-token>" | jq .
```

Repeat the same step for each consumer team. Grant the consumer team's first user an entitlement of the form `payments_REGISTRY_CONSUMER` (where `payments` is the team's agreed slug) in your entitlement service. That team's tenant is live the first time they call any endpoint with that token.

**Roles summary:**

| Role | What it can do |
|---|---|
| `admin` | Manage vocabulary, progression definitions, and overrides for the tenant |
| `producer` | Register, update, and lifecycle capabilities; triage annotations |
| `consumer` | Discover and adopt capabilities; submit annotations; subscribe to events |
| `auditor` | Read-only access to capabilities, annotations, and the audit log |

---

## Step 2 — Define lifecycle vocabulary and a progression definition

Before registering the first capability, seed the vocabulary your progression will reference. Lifecycle states, edge relationship types, and entity types are all per-tenant vocabulary entries.

```bash
# Add lifecycle states to the platform team's tenant vocabulary
for state in alpha beta ga deprecated retired; do
  curl -s -X POST https://registry.example.com/v1/admin/vocabularies/lifecycle_state \
    -H "Authorization: Bearer <platform-admin-token>" \
    -H "Content-Type: application/json" \
    -d "{\"value\": \"$state\"}" | jq .value
done
```

Now create a progression definition that gates lifecycle advances for capabilities:

```bash
curl -s -X POST \
  "https://registry.example.com/v1/admin/tenants/<platform-tenant-id>/progression-definitions" \
  -H "Authorization: Bearer <platform-admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "entity_type": "capability",
    "is_advisory": false,
    "definition": {
      "transitions": [
        {
          "from": "alpha",
          "to": "beta",
          "gates": [{ "id": "has_owner", "type": "attribute_present", "attribute": "owner_team" }]
        },
        {
          "from": "beta",
          "to": "ga",
          "gates": [
            { "id": "has_owner", "type": "attribute_present", "attribute": "owner_team" },
            { "id": "has_sla", "type": "attribute_present", "attribute": "sla_tier" }
          ]
        },
        { "from": "ga", "to": "deprecated", "gates": [] },
        { "from": "deprecated", "to": "retired", "gates": [] }
      ]
    }
  }' | jq '{progression_id, entity_type, is_advisory}'
```

`is_advisory: false` makes the gates enforcing — a lifecycle advance that fails a gate returns HTTP 422 instead of just a warning.

---

## Step 3 — Register a capability with vocabulary terms

With vocabulary in place, the platform team registers a new capability. Use `attributes` to attach metadata at creation time; the registry stores each attribute value bi-temporally so its history is queryable later.

```bash
curl -s -X POST https://registry.example.com/v1/capabilities \
  -H "Authorization: Bearer <platform-producer-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "identity-service",
    "entity_type": "capability",
    "attributes": {
      "owner_team": "platform",
      "description": "Centralized authentication and token issuance for all product teams",
      "interface_version": "1.0.0",
      "sla_tier": "tier-1"
    }
  }' | jq '{entity_id, name, lifecycle}'
```

The response carries an `entity_id` (UUID). Store it — most subsequent calls are capability-scoped.

**Attach lifecycle state explicitly** if your vocabulary requires it:

```bash
curl -s -X POST \
  "https://registry.example.com/v1/capabilities/<entity_id>/lifecycle:update" \
  -H "Authorization: Bearer <platform-producer-token>" \
  -H "Content-Type: application/json" \
  -d '{"state": "alpha"}' | jq .lifecycle
```

---

## Step 4 — Control visibility

Capabilities start `private` to the owning tenant. The progression toward broader visibility is a deliberate act — each step requires the producer to affirm readiness.

**Share with specific consumer tenants** (tenant-shared):

```bash
curl -s -X POST \
  "https://registry.example.com/v1/capabilities/<entity_id>/visibility:set-visibility" \
  -H "Authorization: Bearer <platform-producer-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "visibility": "tenant-shared",
    "shared_with_tenants": ["<payments-tenant-id>", "<orders-tenant-id>"]
  }' | jq '{entity_id, visibility}'
```

**Open to all tenants** once the capability reaches GA:

```bash
curl -s -X POST \
  "https://registry.example.com/v1/capabilities/<entity_id>/visibility:set-visibility" \
  -H "Authorization: Bearer <platform-producer-token>" \
  -H "Content-Type: application/json" \
  -d '{"visibility": "public"}' | jq '{entity_id, visibility}'
```

The `regulated` visibility value signals that the capability contains regulated data and should be treated as non-discoverable outside explicit grants — use it for capabilities that carry PII or compliance obligations.

Every cross-tenant visibility decision funnels through a single chokepoint (`service/visibility.py`). Callers outside the owning tenant never see a private or tenant-shared capability they are not in the `shared_with_tenants` list for; the registry returns a 404 rather than a 403 so that the existence of the capability is not leaked.

---

## Step 5 — Advance through lifecycle states

Once gates are met, advance the lifecycle:

```bash
# beta → ga (requires owner_team + sla_tier attributes, both already set)
curl -s -X POST \
  "https://registry.example.com/v1/capabilities/<entity_id>/lifecycle:update" \
  -H "Authorization: Bearer <platform-producer-token>" \
  -H "Content-Type: application/json" \
  -d '{"state": "ga"}' | jq .lifecycle
```

If a gate is not met, the response is HTTP 422 with a `gate_failures` array naming which gate IDs blocked the transition.

**Override when an emergency skip is required.** Every bypass writes an audit event before the override row is inserted — the audit trail is always complete:

```bash
curl -s -X POST \
  "https://registry.example.com/v1/admin/tenants/<platform-tenant-id>/entities/<entity_id>/progression-overrides" \
  -H "Authorization: Bearer <platform-admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "from_state": "beta",
    "to_state": "ga",
    "gate_id": "has_sla",
    "reason": "SLA documentation in-flight; approved by VP Engineering",
    "bypass_skip_rules": false
  }' | jq '{override_id, audit_event_id}'
```

The `audit_event_id` in the response links directly to the audit log record for this bypass.

---

## Step 6 — Consumer teams declare adoptions

A consumer team with `consumer` role signals a formal dependency by creating an adoption record. The adoption is stored against the provider capability and carries an optional `version_pin` so the registry can later flag drift when the provider's interface advances past that pin.

```bash
# Payments team adopts identity-service
curl -s -X POST \
  "https://registry.example.com/v1/capabilities/<entity_id>/adoptions" \
  -H "Authorization: Bearer <payments-consumer-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "version_pin": "1.0.0",
    "intent": "Token validation for all payment checkout flows"
  }' | jq '{adoption_id, version_pin}'
```

The adoption record carries both the provider's `tenant_id` and the consumer's `tenant_id`, making the dependency visible to the provider. Consumers see only their own adoptions; the provider sees all adoptions across all consumer tenants.

To remove an adoption when a dependency is retired:

```bash
curl -s -X POST \
  "https://registry.example.com/v1/capabilities/<entity_id>/adoptions/<adoption_id>:unadopt" \
  -H "Authorization: Bearer <payments-consumer-token>" | jq .status
```

---

## Step 7 — Fan out a breaking-change preview

When the platform team is planning a breaking change — a new required field, a removed endpoint, a changed auth scheme — use the preview endpoint to assess blast radius before any code lands:

```bash
curl -s -X POST \
  "https://registry.example.com/v1/capabilities/<entity_id>/preview-version" \
  -H "Authorization: Bearer <platform-producer-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "proposed_version": "2.0.0",
    "interface": {
      "breaking_changes": ["auth_scheme changed from API key to OIDC JWT"],
      "deprecates": ["GET /v1/internal/token"]
    }
  }' | jq '{affected_adoptions_count, affected_tenants}'
```

For targeted notification, subscribe consumers to lifecycle events on the capability. Each consumer subscribes once and receives webhook deliveries for subsequent events:

```bash
# Consumer subscribes to deprecation and lifecycle-change events
curl -s -X POST \
  "https://registry.example.com/v1/capabilities/<entity_id>/subscriptions" \
  -H "Authorization: Bearer <payments-consumer-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "event_kinds": ["lifecycle.state_changed", "capability.deprecated"],
    "webhook_url": "https://payments.example.com/hooks/registry",
    "webhook_hmac_secret_ref": "payments/registry-webhook-secret"
  }' | jq '{subscription_id, event_kinds}'
```

Consumers can also poll `GET /v1/notifications` for unread lifecycle events without configuring a webhook:

```bash
curl -s "https://registry.example.com/v1/notifications?status=unread" \
  -H "Authorization: Bearer <payments-consumer-token>" | jq '.items[] | {notification_id, kind, created_at}'
```

---

## Step 8 — Triage consumer annotations and close the loop

Consumers surface feedback through annotations. The platform team triages them on the producer side:

```bash
# List open annotations on the identity-service capability (provider view — all tenants)
curl -s \
  "https://registry.example.com/v1/capabilities/<entity_id>/annotations?status=open" \
  -H "Authorization: Bearer <platform-producer-token>" \
  | jq '.items[] | {annotation_id, category, author_tenant_id, body}'
```

Advance the annotation status with a triage note:

```bash
curl -s -X POST \
  "https://registry.example.com/v1/annotations/<annotation_id>:update" \
  -H "Authorization: Bearer <platform-producer-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "status": "acknowledged",
    "triage_note": "Breaking change scheduled for v2.0.0 — migration guide in progress"
  }' | jq '{annotation_id, status}'
```

---

## Governance patterns

**Blast radius before deprecation.** Before advancing `identity-service` to `deprecated`, check how many adoptions would be affected:

```bash
curl -s \
  "https://registry.example.com/v1/capabilities/<entity_id>/blast-radius?direction=downstream&depth=3" \
  -H "Authorization: Bearer <platform-producer-token>" \
  | jq '{node_count, edge_count}'
```

**Audit trail for policy decisions.** Every lifecycle advance, override, and visibility change emits an audit event. Confirm that the transition to `ga` was recorded:

```bash
curl -s \
  "https://registry.example.com/v1/admin/audit?target_id=<entity_id>&action=LIFECYCLE_STATE_CHANGED" \
  -H "Authorization: Bearer <platform-admin-token>" \
  | jq '.items[] | {ts, actor_id, action, detail}'
```

The audit log is partitioned by month and archived per the `audit_partition_max_age_days` setting. See [Compliance and audit](08-compliance-and-audit.md) for the full archival and PII scanning story.

**Edge graph for dependency tracing.** Declare edges from derived capabilities back to their upstream provider to make the dependency graph explicit:

```bash
# Register that payments-checkout "depends_on" identity-service
curl -s -X POST https://registry.example.com/v1/capabilities \
  -H "Authorization: Bearer <payments-producer-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "payments-checkout",
    "entity_type": "capability",
    "attributes": {
      "depends_on": "<identity-service-entity-id>"
    }
  }' | jq .entity_id
```

---

## See also

- [Authentication](../01-overview/04-authentication.md) — obtaining and validating JWTs
- [Authorization](../01-overview/05-authorization.md) — role grants and entitlement strings
- [Consumer feedback and requests](05-consumer-feedback-and-requests.md) — annotation channel detail
- [Event-driven consumers](04-event-driven-consumers.md) — subscription and webhook delivery
- [Compliance and audit](08-compliance-and-audit.md) — audit log, PII scanning, archival
- [API reference](../05-reference/01-api.md) — endpoint contracts
