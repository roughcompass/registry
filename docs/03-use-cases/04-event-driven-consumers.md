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

## Sections to fill

- Preconditions (consumer tenant provisioned, webhook endpoint reachable, bearer token minted)
- Step 1 — Declare an adoption (prerequisite for subscriptions)
- Step 2 — Register a webhook endpoint
- Step 3 — Create a subscription for specific event types
- Step 4 — Verify webhook signature on receipt
- Step 5 — Acknowledge a notification
- Step 6 — Replay missed deliveries from notification log
- Polling alternative: cursor-based notification polling
- Event type reference (lifecycle transitions, breaking-change previews, annotation responses)
- Related guides and reference docs
