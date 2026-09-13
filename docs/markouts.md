# Fill markouts

The explorer computes 1-, 5- and 30-second markouts for imported fills using retained reference observations. Results are **receipt-time estimates**: fill execution time comes from the exchange, while reference selection uses local receipt time. This does not establish synchronized clocks or exact exchange-time pricing.

## Definition

For horizon `h`, target time is `fill.occurred_at + h`.

`markout USD/contract = exposure_sign × (future YES midpoint − fill YES price)`

The sign is +1 for increased YES exposure and −1 for decreased YES exposure. The normalized importer already handles YES/NO buy/sell semantics; the calculation uses that explicit exposure direction. The quantity-weighted value multiplies this per-contract result by the fill's fractional contract quantity. All arithmetic uses Decimal. Positive means the future midpoint is favorable relative to the execution price. Fees, incentives, settlements and portfolio cost basis are excluded; these diagnostics are not strategy P&L.

For example, an increase-YES fill at $0.40 with a selected future midpoint of $0.50 has a +$0.10/contract markout. A decrease-YES fill at the same price has −$0.10/contract. For 2.5 contracts, the respective quantity-weighted values are +$0.25 and −$0.25, before fees.

## Timing and coverage policy

Version `receipt-markout-1` selects the **latest observation at or before the target**, at most one second old. It never selects a later price, interpolates across missing observations, or searches backward past an invalid latest quote. When the selected quote supplies an exchange timestamp, that timestamp must also be at or before the target and at most one second old. Missing exchange timestamps remain explicit receipt-only observations, eligible only as estimates.

Additional checks require:

- A reference at or before the fill, no more than 20 seconds old.
- A subsequent reference strictly after the target, within 20 seconds, to confirm continuing capture. This quote is used for coverage, never as the target price.
- The same account, market, connection and subscription throughout the interval from the pre-fill reference through confirmation. An interval cannot cross a recorded open or closed gap.
- Valid two-sided books with positive spreads, consistent midpoint arithmetic, supported units and increasing subscription sequences. Any invalid book in the interval makes that horizon unavailable. Per-market sequences can skip because the subscription also serves other markets; the capture worker enforces subscription-wide contiguity.
- At most 20 seconds between consecutive observations. Local wall-clock and monotonic elapsed times must agree within 250 milliseconds, with no monotonic reversal. Supplied source timestamps must pass the capture policy's disagreement check as well.

These checks deliberately reject some usable-looking prices. A quiet market refreshed every 15 seconds may lack a quote within one second of a particular target. No estimate is substituted merely because prices on either side look unchanged. Wide spreads and the bots' own resting orders can influence a valid midpoint.

A horizon that has not elapsed is pending. A price with no confirming observation can remain pending until target + 20 seconds, then becomes unavailable. Missing pre-fill capture, old quotes, gaps, invalid books, timing uncertainty and query limits have distinct reasons. A genuine zero is displayed as zero; unavailable is `null` and is never counted as zero.

## Explorer and export

The **Fill markouts** panel follows the selected history account and refreshes every 15 seconds. It shows 25 fills at a time, with exact market/order filters, pagination, per-horizon results, fill/reference inspection and a link to the order's evidence. Fill subaccount identity is retained; public market references are shared across subaccounts within the selected account scope. The panel makes no claim that every account fill belongs to a particular bot or strategy.

Summary cards cover only the displayed page. For each horizon they show estimated, pending and unavailable fill counts. The quantity-weighted mean is `sum(markout × eligible quantity) / sum(eligible quantity)`; unavailable fills and their quantities are excluded. Means are rounded to six decimal places using Decimal's half-even rounding. Exports also retain the exact weighted numerator and eligible denominator. Samples can differ across horizons, so inspect their counts before comparing means.

**Export displayed markouts** includes the calculation policy/version, timestamp, displayed fills, selected target references, pre-fill and confirmation references, and interval counts/digests. It preserves the displayed calculations and boundary evidence, not a complete archive of intervening quotes. Full interval revalidation requires the matching retained reference store; a digest alone cannot reconstruct its contents.

## Operation and scaling

No new exchange connection or worker is introduced. `othryss/markouts.py` separates pure evaluation from scoped database queries. The explorer opens both existing stores read-only and queries indexed, bounded windows. Each response has its own committed snapshots; the two databases are not an atomic shared transaction. The next refresh can see newly imported fills or newly committed reference evidence.

`GET /api/history/markouts?scope=...&instrument=...&order=...&limit=25&offset=0` accepts an existing account scope, optional exact filters, a maximum 50 fills and offset up to 100000. Retrieval policy `bounded-horizons-2` finds the latest observation at or before the fill (within 20 seconds) and the first confirmation after each target (within 20 seconds). Each horizon reads every observation from that anchor through its confirmation, with a separate bound of 2,000 quotes and 100 gaps. Busy periods outside that interval do not consume the budget, and a 30-second horizon hitting its cap does not invalidate shorter horizons. Exceeding a bound still produces `evidence_limit`; no sampling, interpolation or truncated-data estimates are used. The numerical receipt-time policy is unchanged.

Results are recomputed, not durably materialized. Reference retention remains three days or one million records per scope; older estimates may become unavailable after pruning. Export a displayed result to preserve it. A future analytics worker can use the same evaluator with durable results and pinned evidence if long-lived aggregates become necessary. No account-history or reference schema migration is required here.

## Validation

```powershell
python -m unittest discover -s tests -v
npm.cmd run test:markouts-browser
```

Synthetic tests cover signed calculations, fractional weighting, exact zero, missing capture, source-clock and monotonic checks, stale targets, gaps, reconnects, invalid intermediate books, pending deadlines, scope separation, bounded queries, filters, exports and responsive display. Browser fixtures stay in separate artifact databases. Existing live fills predate reference capture and therefore correctly remain unavailable; qualifying future fills will be evaluated automatically after import.
