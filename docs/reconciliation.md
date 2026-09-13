# Position reconciliation pilot

The first pilot watches `KXTRUMPPHOTO-26SEP13-6` in the founder account's primary subaccount (0). Discovery found no nonzero primary positions, but two active binary markets had explicit zero-position rows. An explicit zero is a valid starting point; an absent position row is not.

The collector reads `/portfolio/positions` for the selected ticker with an explicit subaccount and `position,total_traded` count filter, preserving zero positions with trading history. Position request-start and receipt times are recorded separately from source last-update time. Kalshi's position API defaults to primary subaccount, while some other portfolio endpoints default to all subaccounts; explicit scope prevents mixing them. [Get Positions](https://docs.kalshi.com/api-reference/portfolio/get-positions)

`position_fp` uses signed contracts: positive is YES exposure and negative is NO exposure. This was checked against the official `MarketPosition` schema. Fill directions use the existing normalized YES-exposure convention; all arithmetic uses Decimal. [OpenAPI specification](https://docs.kalshi.com/openapi.yaml), [Order direction](https://docs.kalshi.com/getting_started/order_direction)

## Comparison rules

1. Complete a ticker-specific audit of both current and archived orders/fills. Incomplete pages or changing archive cutoffs prevent a new check.
2. Capture an explicit position row, market status/type and fully paginated settlements. Only a complete capture is stored; an endpoint failure records an error while retaining older results.
3. Select an initial quiet, active, binary-market snapshot after its visibility grace period. Save that baseline identity permanently; discrepancies do not cause automatic rebasing. Before a baseline exists, inspect up to the latest 50 mature candidates for a quiet observation window.
4. Compare the baseline with the latest mature snapshot. The fill audit must have started at least 120 seconds after that snapshot was received. This introduces intentional latency so recently executed fills have time to appear.
5. Sum signed fills between snapshot request windows. Any relevant fill inside either request window or its two-second clock margin makes the comparison timing-uncertain. Missing fill subaccount identity also blocks the comparison. Other subaccounts are excluded.
6. Compute `expected = baseline + net fills`, then `difference = observed - expected`. Exact zero is consistent. A nonzero difference is unexplained and needs review; it does not establish its cause or prove an incident.

The ticker audit revisits all available fills on every check, including archived fills, so a fill arriving after the normal collector overlap can repair a later comparison. Checks preserve their original evidence and are not rewritten. The next check uses the newly available facts.

Snapshot receipt time is an observation bound, not execution time or bot latency. The two-second margin and two-minute visibility delay are conservative local assumptions, not exchange guarantees. REST endpoints are not an atomic portfolio snapshot. Clock skew outside the margin, unusually delayed data, position transfers and other non-fill adjustments can still produce differences requiring investigation.

## Settlement and missing evidence

Each capture also records selected-market settlement evidence and market lifecycle status. A settlement overlapping the baseline, current settlement evidence, or a non-active market stops fill-only comparisons. The panel displays **Settlement / closure**, rather than interpreting a resolved or disappeared position as a missing fill. Settlement counts and timestamps remain inspectable. [Get Settlements](https://docs.kalshi.com/api-reference/portfolio/get-settlements), [Market settlement](https://docs.kalshi.com/getting_started/market_settlement)

This milestone gates settlement transitions; it does not yet reconstruct settlement cash flows, reset positions from payouts, reconcile archived positions, or calculate settlement P&L. It also does not automatically switch to another market after this pilot closes. Target changes require an explicit migration so an existing baseline cannot silently move to a different market or subaccount.

## Operation

```powershell
python -m othryss.collector_cli --account my-account --reconcile-ticker KXTRUMPPHOTO-26SEP13-6 --reconcile-subaccount 0 --stop-file artifacts/history/collector.stop
```

`--reconcile-grace` changes the visibility allowance at initial configuration (30–3600 seconds, default 120). Target, subaccount and grace are durable and must match on subsequent configuration. Ordinary restarts need no reconciliation flags once the target exists. Stop the running collector before launching another instance; use the existing stop-file control or Ctrl+C. A configured position check runs after each successful account collection cycle. Its failures are recorded separately and do not stop order/fill collection.

The pilot's ticker audit has a 100-page budget; positions and settlements each have a 20-page budget. Incomplete audits restart on the next cycle using deduplicated records. Position evidence commits only after all endpoints succeed. Unchanged position quantities are deliberately captured again: those independent observations are needed to establish a later comparison.

SQLite schema 3 adds targets, position observations and immutable reconciliation checks, plus indexes. Existing schema 2 data migrates transactionally. A pre-migration SQLite backup is at `artifacts/history/before-reconciliation-v3.sqlite`. The reader supports schemas 1–3.

## Inspecting results

Refresh the explorer at `http://127.0.0.1:8766`. The **Position reconciliation** panel updates every 15 seconds and is scoped to the selected account. It shows the fixed baseline, signed fill change, expected quantity, observed quantity, difference and comparison timestamps. Pending, unavailable, stale and failed-capture states remain explicit.

Select one of the 20 most recent checks to preserve a historical result while browsing its evidence. Expand the evidence panel to see baseline/target snapshots, current capture, market lifecycle, settlements, audit run identity and contributing normalized fills. Fill order links filter the order browser. Check exports contain the displayed result and metadata; when more than 100 fills contributed, both display and export explicitly contain only the first 100. Exact totals still cover all contributing fills. Older checks remain in SQLite and are available through the account-scoped check-ID API.

The storage and comparison code are separated from venue requests. This pilot still scans the configured market's fills and retains observation/check history; retention policy, larger-market audit budgets and incremental projections require measured work before broader deployment.

## Validation

71 Python tests passed. New coverage includes fractional and short positions, starting inventory, partial exits, seeded differences, late-fill repair, duplicate/conflicting fills, boundary timing, missing rows, subaccount isolation, settlement/closure gating, incomplete pagination, capture rollback, persisted baselines and restart recovery. The discrepancy tests are synthetic and do not modify production account records.

Browser checks passed for the original fixture, imported history and reconciliation. The reconciliation suite verifies the real pilot capture, then serves a browser-only synthetic discrepancy to test review wording, evidence, recent-check selection, export, order navigation and mobile/tablet layout. Synthetic screenshots and exports are labeled separately from real capture artifacts in `artifacts/browser/`.

Authenticated validation captured five snapshots and established a durable zero-position baseline. Mature comparisons reported expected `0`, observed `0`, difference `0`, with no settlements recorded and no position-capture errors. No new pilot fills occurred during this validation; nonzero arithmetic and discrepancies were validated synthetically. The saved result and database integrity check are in `artifacts/history/reconciliation-validation.json`.
