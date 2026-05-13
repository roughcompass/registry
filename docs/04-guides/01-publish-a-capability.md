# How to publish and lifecycle a capability

<!--
  title: Publish a capability
  audience: integrator (producer role)
  status: stub
-->

This guide walks a producer team through the steps to register a new capability, attach metadata, define a progression state machine, and manage the capability through its lifecycle — from `alpha` to `ga` to `deprecated`.

**Preconditions:**

- A bearer token with the `producer` role for your tenant. See [`overview/auth.md`](../01-overview/04-auth.md) if you do not have one.
- At least one entity type in your tenant's closed vocabulary. Use `GET /v1/admin/tenants/<tenant_id>/vocabulary` to list what is seeded; add missing values via `POST /v1/admin/tenants/<tenant_id>/vocabulary` with `vocab_type: entity_type`.

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

<!-- stub: POST /v1/capabilities — required fields, name format (slug rules), entity_type vocab constraint, returned entity_id -->

## Attach attributes

<!-- stub: POST /v1/capabilities/{id}/attributes — typed key-value, valid_from/valid_to, ?as_of time travel -->

## Register an interface surface

<!-- stub: PUT /v1/capabilities/{id}/interface — JSON Schema or OpenAPI 3.x body, normalization, soft-supersede of prior version -->

## Attach external IDs

<!-- stub: POST /v1/external-ids — external_system slug, external_id value, resolve back via GET /v1/entities?external_system=&external_id= -->

## Define a progression state machine

<!-- stub: POST /v1/admin/tenants/{tenant_id}/progression-definitions — states array, gates, is_advisory mode, dry_run check; pointer to runbook-progression.md for the advisory→enforcing procedure -->

## Advance the lifecycle

<!-- stub: PATCH /v1/capabilities/{id} with lifecycle field, gate checks (422 on gate failure), override path (see runbook-progression.md) -->

## Preview breaking changes before publishing

<!-- stub: POST /v1/capabilities/{id}/preview-version — read-only advisor, diff classification, anonymized impact list, no write occurs -->

## Deprecate and retire

<!-- stub: lifecycle progression to deprecated, then retired; retired is terminal; successor capability registration pattern -->

---

**See also:**

- [`overview/vocabulary.md`](../01-overview/03-vocabulary.md) — lifecycle states, attributes, edges, progression definition vocabulary
- [`reference/api.md`](../05-reference/01-api.md) — full endpoint reference with request/response schemas
- [`operations/progression.md`](../06-operations/02-progression.md) — operator procedures for managing progression definitions
