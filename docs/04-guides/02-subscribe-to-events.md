# How to subscribe to capability lifecycle events

<!--
  title: Subscribe to lifecycle events
  audience: integrator (consumer or producer role)
  status: stub
-->

This guide walks a consumer team through declaring an adoption, subscribing to lifecycle events via webhook, and consuming event notifications — both push (webhook) and pull (polling API).

**Preconditions:**

- A bearer token with `consumer` role for your tenant. See [`overview/auth.md`](../01-overview/04-auth.md).
- The UUID or slug-form name of the capability you want to subscribe to.
- A publicly accessible HTTPS endpoint able to receive POST requests, if using webhooks.

**What this guide covers:**

- [Declare an adoption](#declare-an-adoption)
- [Create a webhook subscription](#create-a-webhook-subscription)
- [Verify webhook signatures](#verify-webhook-signatures)
- [Consume notifications via polling](#consume-notifications-via-polling)
- [Update or cancel a subscription](#update-or-cancel-a-subscription)
- [Replay failed deliveries](#replay-failed-deliveries)

---

## Declare an adoption

<!-- stub: POST /v1/adoptions — records consumer dependency, creates provides_to edge, optional auto-subscribe flag; why adoption matters for producer impact assessment -->

## Create a webhook subscription

<!-- stub: POST /v1/capabilities/{capability_id}/subscriptions — webhook_url, signing_secret (generate with secrets.token_urlsafe(32)), event_kinds filter; Idempotency-Key usage; returned subscription_id -->

## Verify webhook signatures

<!-- stub: X-Registry-Signature-256: sha256=<hex>; HMAC-SHA256 over raw request body; Python verification snippet; timing-safe compare_digest; when to reject (signature mismatch, stale timestamp) -->

## Consume notifications via polling

<!-- stub: GET /v1/notifications — cursor pagination with ?cursor + ?page_size, ?status=unread|read|all; POST /v1/notifications/{id}:mark-read; when to prefer polling over webhooks (agent-loop pattern, no public endpoint) -->

## Update or cancel a subscription

<!-- stub: PATCH /v1/subscriptions/{id} — webhook_url, enabled flag, event_kinds; DELETE /v1/subscriptions/{id} for cancellation; idempotency -->

## Replay failed deliveries

<!-- stub: pointer to runbook-ops.md#replaying-failed-webhook-deliveries for the SQL procedure; when to use (subscriber endpoint fixed after 4xx failures exhausted retries) -->

---

**See also:**

- [`overview/vocabulary.md`](../01-overview/03-vocabulary.md) — adoption, subscription, notification vocabulary
- [`reference/api.md`](../05-reference/01-api.md#subscriptions-and-notifications) — full endpoint reference
- [`operations/ops.md`](../06-operations/01-ops.md) — operator procedure for replaying failed deliveries and rotating webhook secrets
