<!--
  title: Use case — Event-driven consumers
  audience: integrator (consumer)
  archetype: explanation (use-case scenario)
  summary: How a consuming team subscribes to capability lifecycle events and processes webhook deliveries.
-->

# Use case: Event-driven consumers

A product team that depends on shared capabilities needs to know immediately when a capability is deprecated, when a breaking change is previewed, or when a lifecycle transition happens. The registry's subscription and webhook delivery system lets consumer tenants declare exactly which events they care about and receive reliable, signed webhook payloads at an endpoint they control — no polling required.

Subscriptions are per-entity and per-event-type. Webhook delivery is handled by a background worker with retry logic. Each delivery carries a signature the consumer can verify, and missed deliveries can be replayed from the notification log. Consumers that prefer pull-based access can poll the notifications endpoint with a cursor instead.

---

## Preconditions

- The consumer tenant must exist and the calling actor must hold at least the `consumer` role.
- Every request requires an `Authorization: Bearer <JWT>` header. See [Authentication](../01-overview/04-authentication.md).
- The capability you want to subscribe to must be visible to your tenant. Discovery is covered in [AI agent capability discovery](01-ai-agent-capability-discovery.md).
- Your webhook endpoint must be reachable from the registry host over HTTPS. Self-signed certificates are accepted in development; production deployments should use a publicly trusted certificate.
- An adoption must exist before you create a subscription. Subscriptions live on capabilities, and the adoption is what establishes the consumer-provider relationship the registry tracks.

---

## Step 1 — Declare an adoption

An adoption records that your tenant depends on a provider's capability. It is the prerequisite for creating a subscription.

```bash
export TOKEN=$(make dev-jwt)
export PROVIDER_CAP_ID=<provider-capability-uuid>

curl -s -X POST \
  http://localhost:8000/v1/capabilities/$PROVIDER_CAP_ID/adoptions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "intent": "Use the payments SDK in our checkout flow",
    "version_pin": "2.4.0"
  }'
```

**Request body fields:**

| Field | Type | Required | Description |
|---|---|---|---|
| `intent` | string or null | no | Free-text description of why your team is adopting this capability. |
| `version_pin` | string or null | no | Semver string. Records the version your team currently targets. |

A `201` response returns an `AdoptionResponse` with `adoption_id`, `consumer_tenant_id`, `provider_capability_id`, and the timestamps. Save the `adoption_id` if you need to cancel the adoption later via `DELETE /v1/capabilities/{provider_cap_id}/adoptions/{adoption_id}`.

---

## Step 2 — Create a subscription

With the adoption in place, create a subscription for the event kinds you want to receive.

```bash
export SIGNING_SECRET=your-locally-generated-hmac-key

curl -s -X POST \
  http://localhost:8000/v1/capabilities/$PROVIDER_CAP_ID/subscriptions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "event_kinds": ["version_published", "deprecation", "breaking_change"],
    "webhook_url": "https://your-service.example.com/hooks/registry",
    "webhook_hmac_secret_ref": "REGISTRY_WEBHOOK_SECRET"
  }'
```

**Request body fields:**

| Field | Type | Required | Description |
|---|---|---|---|
| `event_kinds` | array of string | yes | One or more event kinds from the vocabulary below. Minimum one item. |
| `webhook_url` | string or null | no | HTTPS endpoint the registry will POST events to. Omit to receive events in the notification inbox only. |
| `webhook_hmac_secret_ref` | string or null | no | Name of the env var that holds your HMAC signing secret. Required when `webhook_url` is set. |

The `webhook_hmac_secret_ref` follows the same pattern as sync-source credentials: the value is the name of an environment variable set in the registry's process, not the secret itself. Set it before creating the subscription:

```bash
# In the registry API process environment:
REGISTRY_WEBHOOK_SECRET=<your-secret>
```

A `201` response body is `{"subscription_id": "<uuid>"}`. The full subscription record (including `is_enabled`, `event_kinds`, and `digest_window`) is available from `GET /v1/capabilities/{capability_id}/subscriptions`.

---

## Event kind reference

| Event kind | When it fires |
|---|---|
| `version_published` | A new version of the capability is promoted and available. |
| `deprecation` | The capability has entered deprecated lifecycle state. |
| `breaking_change` | A breaking change has been flagged on a version the consumer may be pinned to. |
| `conflict_added` | A new conflict edge has been added to the capability (e.g. incompatible dependency). |
| `integration_added` | A new integration edge has been added (e.g. the capability now composes another). |

Pass a subset of these to `event_kinds` if you only care about specific transitions. The subscription is tenant-scoped: you receive events only for the capability you subscribed to, only in your tenant.

---

## Step 3 — Verify the webhook signature

Every delivery includes an `X-Registry-Signature-256` header. The value is `sha256=<hex>` where `<hex>` is the HMAC-SHA256 digest of the raw request body, keyed by the secret stored in the env var you named in `webhook_hmac_secret_ref`.

Verify this in constant time before processing the payload:

```python
import hashlib
import hmac
import os

def verify_registry_webhook(raw_body: bytes, header_value: str) -> bool:
    secret = os.environ["REGISTRY_WEBHOOK_SECRET"].encode("utf-8")
    expected = "sha256=" + hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value)
```

Reject any request where verification returns `False`. Do not use string equality (`==`) — it is vulnerable to timing attacks.

**Delivery payload shape** — the fields present on every delivery:

| Field | Type | Description |
|---|---|---|
| `notification_id` | string (UUID) | Stable identifier for this notification. Use it to deduplicate retries. |
| `tenant_id` | string (UUID) | Your tenant. |
| `subscription_id` | string (UUID) | The subscription that triggered this delivery. |
| `capability_id` | string (UUID) | The capability the event is about. |
| `capability_slug` | string | Human-readable slug for the capability. |
| `event_kind` | string | One of the event kinds above. |
| `change_classification` | string or null | Further classification of the change (e.g. `breaking`), where applicable. |
| `version_before` | string or null | Version before the transition, if applicable. |
| `version_after` | string or null | Version after the transition, if applicable. |
| `occurred_at` | string (ISO-8601) | When the event occurred. |
| `fetch_url` | string | URL to fetch the full capability record for additional context. |

---

## Step 4 — Handle delivery reliability

The delivery worker drains pending deliveries every `WEBHOOK_DRAIN_INTERVAL_S` seconds (default `5`). Each attempt times out after `WEBHOOK_REQUEST_TIMEOUT_S` seconds (default `10`).

Retry behavior on failure:

- **2xx** — marked success; no further attempts.
- **5xx, 408, 425, 429** — retried with exponential backoff starting at 60 seconds, capped at 24 hours. Up to 12 attempts total.
- **Other 4xx** — marked permanently failed; no retry. These indicate a caller-side error (bad URL, auth rejected by your service, etc.).

Deliveries that exhaust all retry attempts land in a dead-letter table. Failed deliveries remain readable from the notification log (see below) so they can be replayed.

To avoid processing a delivery twice if your endpoint returns 2xx slowly and the worker retries, use `notification_id` as a deduplication key in your handler.

---

## Step 5 — Acknowledge a notification

Reading from the notification inbox (polling alternative, below) marks items as read explicitly. If you receive events by webhook only and want to keep the inbox clean, acknowledge the notification after processing:

```bash
curl -s -X POST \
  http://localhost:8000/v1/notifications/<notification_id>:mark-read \
  -H "Authorization: Bearer $TOKEN"
```

A `200` response confirms the notification is marked read. Marking an already-read notification is idempotent.

---

## Polling alternative: cursor-based notification inbox

If you prefer pull-based delivery — or need to recover missed events — poll the notification inbox. It holds every event for your tenant regardless of whether webhook delivery succeeded.

```bash
# First page — newest unread notifications
curl -s "http://localhost:8000/v1/notifications?page_size=50" \
  -H "Authorization: Bearer $TOKEN"
```

**Query parameters:**

| Parameter | Default | Description |
|---|---|---|
| `cursor` | (none) | Opaque cursor from a previous response's `next_cursor`. Omit on the first call. |
| `page_size` | (server default) | Number of items per page. |
| `status` | `unread` | Filter by `unread`, `read`, or `all`. |

The response includes a `next_cursor` field. When `next_cursor` is non-null, pass it as `cursor` on the next call to advance the page. Pagination is cursor-based, not offset-based: re-using an old offset would miss events that arrived between calls.

```bash
# Subsequent pages
curl -s "http://localhost:8000/v1/notifications?cursor=<next_cursor>&page_size=50" \
  -H "Authorization: Bearer $TOKEN"
```

After processing a batch, acknowledge each item with `POST /v1/notifications/{id}:mark-read`.

---

## Managing subscriptions

**Update a subscription** — change event kinds, webhook URL, or toggle it on or off:

```bash
curl -s -X PATCH \
  http://localhost:8000/v1/subscriptions/<subscription_id> \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"event_kinds": ["deprecation", "breaking_change"], "is_enabled": true}'
```

`PATCH` honours the `If-Match` header for optimistic concurrency. Get the current ETag from the subscription create response, then pass it:

```bash
-H "If-Match: \"<etag-value>\""
```

A `412` response means another writer updated the record between your read and write; re-fetch and retry.

**Cancel a subscription:**

```bash
curl -s -X DELETE \
  http://localhost:8000/v1/subscriptions/<subscription_id> \
  -H "Authorization: Bearer $TOKEN"
```

The subscription is cancelled and no further deliveries are attempted. Existing notifications already in the inbox remain readable.

---

## See also

- [API reference](../05-reference/01-api.md) — full endpoint contracts for adoptions, subscriptions, and notifications
- [Authentication](../01-overview/04-authentication.md) — how to obtain a JWT
- [Authorization](../01-overview/05-authorization.md) — role grants required for subscription endpoints
- [Consumer feedback and feature requests](05-consumer-feedback-and-requests.md) — the inverse channel: consumers signaling producers via annotations
- [Mirroring an external source of truth](03-mirroring-external-sources.md) — populating capabilities from external sources that consumers then subscribe to
