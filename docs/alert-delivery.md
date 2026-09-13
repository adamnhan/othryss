# Discord and generic webhook delivery

Both integrations use the same incident routing, SQLite queue, attempt history, expiry, rate limiting, and recovery handling. Only message formatting, authentication, and provider-response handling differ. No SMS integration is included. User-configured `inventory_limit` incidents are supported; see [account/risk settings](account-risk-overview.md). Routes with an empty rule filter include them automatically.

The separately supervised worker reads existing incident actions. It never loads Kalshi credentials, changes trading state, or runs inside a bot. The history database remains unchanged; delivery state lives in `artifacts/alerts/delivery.sqlite`. The explorer's **Alert delivery** panel shows the latest 50 scoped deliveries and their attempts, without destination URLs or secrets.

## Configure a destination

Onboarding creates `alerts.local.json` with no routes. Add the desired route from `alerts.local.example.json`, using this account's exact `scope_id` from `/api/history/accounts`. Preserve any existing routes when editing the file. The examples start disabled.

Append the relevant values to the existing `local.env` (do not replace its Kalshi settings):

```dotenv
OTHRYSS_ALERT_DISCORD_URL=https://discord.com/api/webhooks/WEBHOOK_ID/WEBHOOK_TOKEN
OTHRYSS_ALERT_WEBHOOK_URL=https://your-receiver.example/othryss
OTHRYSS_ALERT_WEBHOOK_SECRET=YOUR_RANDOM_SECRET_AT_LEAST_32_CHARACTERS
```

Create an incoming webhook in the Discord channel you want Othryss to post to and copy that URL. For generic webhooks, configure your own HTTPS receiver and share the independent signing secret with it. Only populate the destination you intend to use. These values are secrets; they stay in local configuration and are excluded from notifications, UI responses, logs and backups. Environment variables override the corresponding file entries. No shell expansion is performed.

Inspect a local preview before enabling a route:

```powershell
python -m othryss.alerts_cli preview --route discord
```

Set that route's `enabled` to `true` in `alerts.local.json`. The worker reloads settings every cycle (normally five seconds, longer while delivery calls are in progress). Then explicitly queue a test message:

```powershell
python -m othryss.alerts_cli test --route discord
python -m othryss.alerts_cli test --route webhook
python -m othryss.alerts_cli status
```

`test` sends a clearly labeled test through the shared queue; run it only for a destination you intend to contact. `preview` never sends. The worker must have activated the exact enabled configuration before a test can be queued. Production provider acceptance has not been tested until a real destination is configured and this test succeeds.

## Routing and lifecycle

- Routes require an exact account scope; optional `rules` select incident rules. An empty list includes all existing rules. Those include sustained position/order/fill discrepancies and source-health incidents. Delivery does not change reconciliation thresholds or create new execution-degradation rules.
- `events` accepts `opened` and `resolved`. Pending candidates and routine fills do not notify. An unsent opening is suppressed if the incident has already been acknowledged or resolved. Recovery notifies only if this route previously accepted the opening or its delivery was uncertain.
- `reminder_seconds` defaults to zero. Set 300–86400 to enable reminders while a previously notified incident remains open with a fresh difference assessment. Acknowledgement stops future reminders; it does not establish recovery. Messages already in flight cannot be recalled.
- Initial activation starts after the current incident-action cursor. It does not page through historical incidents or announce already-open incidents. Disabling, removing, changing a route, changing credentials, or recovering missing credentials cancels its waiting notices and starts a fresh activation. Check the incident dashboard for pre-existing problems when enabling delivery.
- After a worker outage, actions older than `ttl_seconds` are skipped. Accepted/unknown attempts persist across restarts. Checkpoints and insertion of new queue rows commit together; a unique route/event key prevents repeated polling from enqueueing duplicates.

Each message carries its event type, market, rule, assessment, evidence-check timestamp, and full incident ID. The webhook includes the account scope and entity ID. Raw bot/exchange records and human review notes are excluded. Messages direct users to the local explorer: a localhost URL would not open the founder machine's explorer on a phone.

## Retries, uncertainty and limits

Provider acceptance means the service accepted the request, not that a person read it. Discord uses `wait=true` and requires a returned message ID. Mentions are disabled so market or incident text cannot trigger `@everyone`/role notifications. See the [Discord webhook API](https://docs.discord.com/developers/resources/webhook) and [rate-limit behavior](https://docs.discord.com/developers/topics/rate-limits).

Explicit HTTP 429 responses schedule a persisted retry respecting `Retry-After` or Discord's `retry_after`, including route-wide cooldown. Other retries back off exponentially from five seconds, capped at 15 minutes, with at most five attempts by default. Permanent rejection (including redirects) fails the delivery. Requests use a ten-second network timeout and never follow redirects with destination secrets.

Discord transport timeouts, server errors and malformed success responses are **delivery unknown** and do not automatically resend; the original message might already exist. Generic webhooks retry transport/server errors with the same delivery ID, because the receiver can deduplicate. A process interruption after recording an attempt becomes **unknown** on restart for either channel. Exactly-once remote delivery is not claimed.

Default per-route limits are one attempt per ten seconds and one-hour expiry. Configure `max_attempts` (1–10), `ttl_seconds` (60–86400) and `min_interval_seconds` (1–3600). At most eight routes are supported. Discovery reads 500 actions per route/cycle and pauses near 4500 pending/retry rows so backlog cannot grow without bound from source polling; reminders are also bounded per cycle. Delivery calls run separately from collectors but sequentially within the notification worker. A slow route can delay other routes by its timeout.

Terminal audit rows older than 30 days are pruned in bounded batches, preserving notification context for still-active incidents. Normalized trading history has its own retention policy. Alert queue snapshots are included in new operational backups when present. Restores disarm routes and waiting deliveries, retain uncertain attempts, and require deliberate reconfiguration; route secrets/configuration are not restored. The queue is a separately consistent snapshot, not an atomic snapshot with exchange history.

The worker can only deliver incidents already recorded by the collector. If the collector is stopped, it cannot create fresh source-health incidents. If the entire machine or internet connection fails, it cannot send an alert at all. Independent collector/host heartbeat alerting remains a separate feature.

## Generic webhook receiver contract

The POST body is UTF-8 JSON with `schema_version: "othryss-alert-1"`, event (`opened`, `resolved`, `reminder`, or `test`), ISO timestamp, account scope, an incident summary, and stable `delivery_id`.

Headers:

```text
Idempotency-Key: DELIVERY_ID
X-Othryss-Timestamp: UNIX_SECONDS
X-Othryss-Signature: sha256=LOWERCASE_HEX_DIGEST
```

Compute HMAC-SHA256 over `timestamp + "." + exact_request_body_bytes` using the configured signing secret. Compare signatures in constant time, check timestamp freshness (for example, within five minutes), and deduplicate by `delivery_id` before side effects. Retries keep the exact body and delivery ID but generate a fresh signature timestamp. Return 2xx only after accepting/persisting the event. Return 429 with `Retry-After` for rate limiting. Delivery outcomes and HTTP codes are recorded, but arbitrary receiver response bodies are not stored.

## Verification

`python -m unittest discover -s tests -v` covers queue replay, configuration changes, scope/rule filters, acknowledgement, recovery, reminders, retry expiry, uncertain sends, backup disarming, HMAC signing and redaction. `npm.cmd run test:alerts-browser` uses an isolated database and injected transports; it does not send external messages.
