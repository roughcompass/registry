# How to publish and lifecycle a capability

<!--
  title: Publish a capability
  audience: integrator (producer role)
  status: current
-->

This guide walks a producer team through the steps to register a new capability, attach metadata, define a progression state machine, and manage the capability through its lifecycle — from `alpha` to `ga` to `deprecated`.

**Preconditions:**

- A bearer token with the `producer` role for your tenant. See [`overview/authentication.md`](../01-overview/04-authentication.md) for how to obtain one and [`overview/authorization.md`](../01-overview/05-authorization.md) for how roles are resolved. In local dev, run `make dev-jwt` to mint a short-lived token.
- At least one `entity_type` value in your tenant's vocabulary. Use `GET /v1/admin/vocabularies/entity_type` to list what is seeded; add missing values via `POST /v1/admin/vocabularies/entity_type` with `{"value": "service"}`. Both endpoints require the `admin` role.

**What this guide covers:**

- [Register a new capability](#register-a-new-capability)
- [Attach attributes](#attach-attributes)
- [Register an interface surface](#register-an-interface-surface)
- [Attach external IDs](#attach-external-ids)
- [Define a progression state machine](#define-a-progression-state-machine)
- [Advance the lifecycle](#advance-the-lifecycle)
- [Preview breaking changes before publishing](#preview-breaking-changes-before-publishing)
- [Deprecate and retire](#deprecate-and-retire)

---

## Register a new capability

`POST /v1/capabilities` creates the entity record. The `name` field is a slug: lowercase letters, digits, and hyphens; 1–200 characters; must start and end with an alphanumeric character; no consecutive hyphens. The `entity_type` value must exist in the `entity_type` vocabulary for your tenant.

```bash
curl -s -X POST https://api.example.com/v1/capabilities \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "payment-gateway",
    "entity_type": "service",
    "display_name": "Payment Gateway",
    "description": "Handles card-present and card-not-present payment flows.",
    "lifecycle": "alpha",
    "visibility": "internal"
  }' | jq .
```

The response includes `entity_id` — save it; every subsequent call uses this UUID or the slug (`payment-gateway`).

```json
{
  "entity_id": "a1b2c3d4-0000-0000-0000-000000000001",
  "name": "payment-gateway",
  "entity_type": "service",
  "lifecycle": "alpha",
  "created_at": "2026-05-14T10:00:00Z"
}
```

**Idempotency:** Send `Idempotency-Key: <uuid>` to make the create safe to retry. Same key + same body replays the original 201; same key + different body returns 409.

**Errors:**
- `422` — `name` violates slug rules, or `entity_type` is not in the vocabulary.
- `409` — name is already taken within your tenant.

---

## Attach attributes

Attributes are typed key-value pairs that extend a capability's metadata beyond the core fields. They are stored as bi-temporal facts and support `valid_from` / `valid_to` windowing for time-travel queries.

```bash
curl -s -X POST \
  "https://api.example.com/v1/capabilities/payment-gateway/attributes" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "key": "sla_tier",
    "value": "gold",
    "valid_from": "2026-05-14T00:00:00Z"
  }' | jq .
```

Use `GET /v1/capabilities/payment-gateway?as_of=<ISO-8601>` to retrieve the attribute state at any point in time.

---

## Register an interface surface

`PUT /v1/capabilities/{capability_id}/interface` stores a JSON Schema or OpenAPI 3.x document as the capability's declared interface. The service normalises the document and soft-supersedes any prior version — the previous interface is retained in bi-temporal history.

```bash
curl -s -X PUT \
  "https://api.example.com/v1/capabilities/payment-gateway/interface" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "interface_format": "openapi_3",
    "interface_source": {
      "openapi": "3.0.3",
      "info": {"title": "Payment Gateway API", "version": "1.0.0"},
      "paths": {
        "/charge": {
          "post": {
            "operationId": "charge",
            "summary": "Initiate a charge",
            "requestBody": {
              "required": true,
              "content": {
                "application/json": {
                  "schema": {
                    "type": "object",
                    "required": ["amount_cents", "currency"],
                    "properties": {
                      "amount_cents": {"type": "integer"},
                      "currency": {"type": "string"}
                    }
                  }
                }
              }
            },
            "responses": {"200": {"description": "Charge accepted"}}
          }
        }
      }
    }
  }' | jq .
```

The response contains the normalized `operations`, `events`, and `fields` surfaces extracted from the document.

Retrieve the current interface — or its state at a historical point — with `GET /v1/capabilities/payment-gateway/interface?as_of=<ISO-8601>`.

---

## Attach external IDs

External IDs let you cross-reference a capability to its identity in upstream systems (GitHub repos, Backstage, PagerDuty, etc.).

First, register the external system if it is not already registered (admin role required):

```bash
curl -s -X POST https://api.example.com/v1/admin/external-systems \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "slug": "github",
    "display_name": "GitHub",
    "url_template": "https://github.com/{external_id}"
  }' | jq .
```

Then attach the mapping to the capability (producer role sufficient):

```bash
curl -s -X POST \
  "https://api.example.com/v1/entities/a1b2c3d4-0000-0000-0000-000000000001/external-ids" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "external_system_slug": "github",
    "external_id": "acme-corp/payment-gateway"
  }' | jq .
```

To resolve back from an external reference to a registry entity:

```bash
curl -s \
  "https://api.example.com/v1/entities?external_system=github&external_id=acme-corp%2Fpayment-gateway" \
  -H "Authorization: Bearer <token>" | jq .entity_id
```

Returns `404` when no mapping exists for the `(external_system, external_id)` pair.

---

## Define a progression state machine

Progression definitions encode the gates that must pass before a capability can move between lifecycle stages. They are tenant-scoped in the URL and require the `admin` role.

```bash
curl -s -X POST \
  "https://api.example.com/v1/admin/tenants/<tenant_id>/progression-definitions" \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "entity_type": "service",
    "is_advisory": true,
    "definition": {
      "states": ["alpha", "beta", "ga", "deprecated", "retired"],
      "transitions": [
        {
          "from": "alpha",
          "to": "beta",
          "gates": [{"type": "interface_registered"}, {"type": "has_adoption"}]
        },
        {
          "from": "beta",
          "to": "ga",
          "gates": [{"type": "min_adoption_count", "count": 3}]
        },
        {
          "from": "ga",
          "to": "deprecated",
          "gates": []
        },
        {
          "from": "deprecated",
          "to": "retired",
          "gates": [{"type": "zero_adoptions"}]
        }
      ]
    }
  }' | jq .
```

When `is_advisory` is `true` the gate failures are recorded but the transition is not blocked — useful for rollout. When `is_advisory` is `false`, a gate failure returns `422` with `code: "progression_rejected"` and the gate that failed. See [`operations/progression.md`](../06-operations/02-progression.md) for the procedure to flip from advisory to enforcing safely.

---

## Advance the lifecycle

Use `PATCH /v1/capabilities/{entity_id}` to move the lifecycle field. If a progression definition is active for the capability's `entity_type`, the gate checks run before the write.

```bash
curl -s -X PATCH \
  "https://api.example.com/v1/capabilities/payment-gateway" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"lifecycle": "beta"}' | jq .lifecycle
```

**Gate failure:** if the transition is blocked (enforcing mode, gate not met), the response is:

```json
HTTP 422
{
  "code": "progression_rejected",
  "reason": "gate 'interface_registered' not satisfied for transition alpha→beta"
}
```

**Override:** when an exception is needed, an admin can create a progression override before the transition attempt:

```bash
curl -s -X POST \
  "https://api.example.com/v1/admin/tenants/<tenant_id>/entities/a1b2c3d4-0000-0000-0000-000000000001/progression-overrides" \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "transition_from": "alpha",
    "transition_to": "beta",
    "reason": "Bootstrapping — interface will be uploaded within 24 h."
  }' | jq .
```

---

## Preview breaking changes before publishing

Before uploading a new interface version, run the breaking-change advisor to understand the blast radius. The endpoint is read-only — it writes nothing.

```bash
curl -s -X POST \
  "https://api.example.com/v1/capabilities/payment-gateway/preview-version" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "proposed_version": "2.0.0",
    "interface_format": "openapi_3",
    "proposed_interface": {
      "openapi": "3.0.3",
      "info": {"title": "Payment Gateway API", "version": "2.0.0"},
      "paths": {
        "/charge": {
          "post": {
            "operationId": "charge",
            "requestBody": {
              "required": true,
              "content": {
                "application/json": {
                  "schema": {
                    "type": "object",
                    "required": ["amount_cents", "currency", "idempotency_key"],
                    "properties": {
                      "amount_cents": {"type": "integer"},
                      "currency": {"type": "string"},
                      "idempotency_key": {"type": "string"}
                    }
                  }
                }
              }
            },
            "responses": {"200": {"description": "Charge accepted"}}
          }
        }
      }
    }
  }' | jq '{classification: .diff_classification, affected: (.affected_consumers | length)}'
```

The response fields:

| Field | Type | Meaning |
|---|---|---|
| `diff_classification` | string | `breaking`, `non_breaking`, or `compatible` |
| `changes` | array | Per-element diff entries (field added/removed/type-changed) |
| `affected_consumers` | array | Consumers of this capability; cross-tenant entries are anonymized |
| `release_notes_scaffold` | string | Plain-text scaffold for a CHANGELOG entry |

If `diff_classification` is `breaking`, notify consumers before uploading the new interface with `PUT /v1/capabilities/{id}/interface`.

---

## Deprecate and retire

Deprecation signals to consumers that the capability is on its way out. Retirement is terminal — no further transitions are allowed.

**Deprecate:**

```bash
curl -s -X PATCH \
  "https://api.example.com/v1/capabilities/payment-gateway" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"lifecycle": "deprecated"}' | jq .lifecycle
```

**Register a successor** (optional but recommended — consumers can discover the replacement via the registry graph):

```bash
# Create the successor capability first, then link it as a dependency edge
# from the deprecated capability to the new one via POST /v1/capabilities/{id}/edges.
```

**Retire** (only when all adoptions have been removed — a gate check enforces zero active adoptions when the progression definition is in enforcing mode):

```bash
curl -s -X PATCH \
  "https://api.example.com/v1/capabilities/payment-gateway" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"lifecycle": "retired"}' | jq .lifecycle
```

`retired` is terminal. Once set, the capability can no longer be adopted and is hidden from the default listing unless `?include_retired=true` is passed.

---

**See also:**

- [`overview/vocabulary.md`](../01-overview/03-vocabulary.md) — lifecycle states, attributes, edges, progression definition vocabulary
- [`reference/api.md`](../05-reference/01-api.md) — full endpoint reference with request/response schemas
- [`operations/progression.md`](../06-operations/02-progression.md) — operator procedures for managing progression definitions
