# Account and risk overview

The imported-history explorer now shows available balance, exchange position value, consolidated inventory, fill/fee summaries and user-configured inventory limits. It covers **primary subaccount 0**, across all returned markets. Other subaccounts are explicitly excluded; this is not a combined view of every subaccount or a strategy P&L report.

## Data and meanings

The existing read-only collector fetches `/portfolio/balance?subaccount=0` and traverses `/portfolio/positions?subaccount=0&count_filter=position` after a successful history cycle. It verifies the dedicated read-only key and its subaccount coverage before these calls. It never submits or cancels orders.

Balance fields are returned in cents and converted using Decimal. **Available balance** and **exchange position value** are shown separately; the app does not infer total equity or realized profit from their sum. The provider's balance-update timestamp is retained separately from local fetch time. A recently fetched unchanged balance can have an older update timestamp. See [Kalshi balance definitions](https://docs.kalshi.com/api-reference/portfolio/get-balance).

Inventory uses signed YES contract positions. Negative positions are retained, and absolute inventory is the sum of each market's absolute position. A long 10.25-contract position and a short 7.5-contract position therefore count as 17.75 absolute contracts. This is a contract-count measure, not maximum loss or dollar exposure. Resting orders, possible future fills, incentives, collateral offsets, and positions in other subaccounts are excluded.

The positions query requests nonzero positions and consumes every page. A complete empty result establishes no nonzero positions in that scope. A failed or incomplete query establishes no such fact and never falls back to an older successful capture. Duplicate tickers, wrong scope, unsupported exchange index, missing fixed-point quantity, cursor loops, or capture-budget exhaustion invalidate the full position set. See [Kalshi positions and pagination](https://docs.kalshi.com/api-reference/portfolio/get-positions).

Balance and position failures are independent. A failed balance call can leave usable inventory, and a failed position call can leave a usable balance. Requests are sequential and do not form an atomic exchange snapshot. Current totals require a capture started within 180 seconds, with valid receipt timing. Old retained positions may remain inspectable but are labeled as historical; stale data does not produce current risk totals.

## Fill and fee summaries

Choose last 24 hours, last 7 days, or all imported fills. The window ends at the API snapshot's current UTC time. Summaries include fill count, contract volume, execution fees and maker/taker/unknown counts, plus per-market totals. Negative reported fees remain negative; they are not silently clamped to zero.

Only retained valid fills with explicit primary-subaccount identity contribute. Unknown subaccounts, other subaccounts and invalid records are separately counted as excluded. The collection freshness indicator and window bounds remain part of the export. These are imported-data summaries, not a guarantee of complete venue history, a fill-rate denominator, or complete P&L. Fees exclude incentives and settlements.

## Configure inventory alerts

The Overview shows the current limits read-only under **Inventory monitoring**. Configure them programmatically with `othryss.risk_cli` from the installation directory. Limits are account-wide for primary subaccount 0, not per-bot trading controls. They start disabled.

```powershell
python -m othryss.risk_cli accounts
python -m othryss.risk_cli show --scope YOUR_SCOPE_ID | Set-Content -Encoding utf8 inventory-limits.local.json
```

Edit the exported JSON. Keep its explicit `scope_id`, `subaccount: 0`, and current `revision`. The `config` object accepts:

```json
{
  "enabled": true,
  "per_market_limit": "50",
  "total_limit": "200",
  "overrides": [{"ticker": "YOUR-MARKET", "limit": "25"}],
  "grace_seconds": 120
}
```

These are example limits, not defaults. Use decimal strings; `null` disables a limit and zero permits no nonzero inventory. An override replaces the per-market default. Total absolute contracts sum positions without netting unrelated markets. The persistence delay is 30 to 3600 seconds.

```powershell
python -m othryss.risk_cli check --file inventory-limits.local.json
python -m othryss.risk_cli apply --file inventory-limits.local.json
```

`check` validates the configuration, account and revision without changing settings. `apply` writes to the local history database; the collector picks it up on subsequent captures without restarting. A stale revision is rejected. Export the current configuration again before making another edit. File changes alone do not change active limits. Use `--db PATH` before the subcommand for another installation's database. The database must already exist and contain that account.

A breach is strictly greater than the limit. At least two distinct fresh, complete captures and the persistence delay are required to open an incident. Incomplete or stale evidence cannot open or resolve one. Two comparable clear captures resolve an open incident; acknowledgement records review and suppresses reminders but does not clear a breach. Applying configuration neither places orders nor controls bots.

Each revision starts an independent risk assessment using subsequent captures. Changing or disabling settings suspends older unresolved inventory incidents as unknown rather than claiming recovery; their evidence remains inspectable. New incidents retain the exact configuration and source snapshot used for evaluation. Inventory monitoring follows account positions, so it does not attribute exposure to a particular bot.

The existing `inventory_limit` incident type routes through the same Discord/webhook queue as other incidents. All-rule routes include it automatically; explicitly filtered routes must include `inventory_limit`. Discord notices identify the affected market or primary-account total, observed absolute contracts and configured maximum. Pending candidates do not notify. No live limits were chosen automatically during installation.

## Storage, API and boundaries

History schema 6 adds `account_snapshots` and `risk_settings`; existing evidence and settings are preserved by migration. The collector retains the latest 100 captures per account/primary subaccount. Insertion order selects the latest capture even when local timestamps tie or move backward. Risk checks preserve their own source evidence in the existing incident check store, independently of rolling snapshot retention. Backups include these tables through the existing history snapshot.

`GET /api/history/account-risk?scope=...&window=24h&offset=0` returns a consistent read-only view. Position pages contain 25 entries; totals use the whole captured set. Captures allow at most 20 pages / 10000 positions. Fill scans cap at 100000 records and explicitly report truncation. Per-market fill display/export lists the 100 highest-volume markets; headline totals include all scanned valid fills.

The existing loopback compatibility endpoint `POST /api/history/risk-settings` accepts `{scope, revision, config}` from the local explorer origin with the existing review header. Config fields are `enabled`, `per_market_limit`, `total_limit`, `overrides`, and `grace_seconds`. Up to 100 unique overrides are supported; values must be fixed-point decimals, nonnegative and at most 1e12, with at most 12 fractional digits. The UI no longer calls this endpoint. Use the CLI for programmatic configuration; this is not a hosted authenticated API.

Verification: `python -m unittest discover -s tests -v` and `npm.cmd run test:account-risk-browser`. Synthetic breach tests use isolated databases and injected alert transports; they do not create live trading incidents or send Discord messages.
