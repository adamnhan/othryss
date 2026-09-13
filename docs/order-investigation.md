# Order investigation

Select an order in **Imported history** to inspect exchange observations, fills, bot intent and HTTP attempts, cancellation analysis, reference prices, 1/5/30-second markouts, and related incidents together. The timeline filters by exchange, bot, market reference or incident, with 25 entries per page. Filtering and paging use the same retained response. **Refresh investigation** explicitly obtains a new response; health polling does not change an open investigation.

**Export order evidence** downloads `othryss-order-evidence.json` from that exact response, including underlying records, policies, source references and limits. It does not refetch pages during export. The exchange totals still cover every stored fill for the selected account/market/order ID. An export exceeding the evidence limits is explicitly partial; it is not represented as a complete order history.

## Identity and interpretation

- Exchange events require an exact account scope, instrument and order ID. One known, consistent subaccount across these events is required for bot and incident links. Unknown or conflicting subaccounts disable those links and remain visible in coverage.
- Bot requests match an exchange order ID or previous order ID, in the same account, instrument and subaccount. A client-order ID can link a request only if it maps to one exchange order in that scope and the request has no conflicting explicit order ID. Reused IDs and conflicting request identities are excluded. A timeout linked by client ID still has unknown outcome.
- Amendments can identify an old and replacement order. Their relationship is shown, while exchange fill totals and markouts remain specific to the selected order.
- Direct incidents require a matching order or fill ID and a monitor with the same subaccount. Position/source incidents and primary-subaccount per-market inventory incidents are labeled **Same-market context** only when their detection interval overlaps the retained order/trace window, allowing 120 seconds of grace. Unrelated order incidents and account-total inventory incidents are excluded. Review history and first/latest check evidence are included; viewing/exporting does not acknowledge or resolve an incident.
- Request latency, retry timings and cancel/fill classifications reuse [execution analysis](execution-analysis.md). HTTP success alone is not confirmation that a cancellation completed. Earlier execution imported late is separate from suspected execution after a confirmed cancellation response.
- Markouts reuse the [receipt-time policy](markouts.md). Missing reference capture stays unavailable. Estimates exclude fees, incentives and settlement and are not strategy P&L. Boundary quotes and interval digests are exported, not the full intervening quote stream.
- Exchange source times, import receipts, bot wall clocks and reference receipts retain their labels. Sorting these timestamps supports navigation; it does not synchronize clocks or prove causality. Bot session sequence and monotonic durations preserve local request order.

## Evidence bounds

| Source | Included |
| --- | --- |
| Exchange records | First 500 in source-or-receipt order; latest observation and all-fill totals are separate. Each event includes up to 20 import references and its full reference count. |
| Bot requests | Latest 25 matching candidate requests, at most 200 records each; execution policy verifies session continuity over at most 5,000 sequence positions. Up to 100 fills per cancellation. |
| Client-order discovery | First 50 distinct client IDs, each checked for unique order mapping. |
| Markouts | Latest 25 fills, with existing per-fill quote/gap limits and unavailable reasons. |
| Context reference observations | Latest 100 quotes and 100 gaps, from 20 seconds before the retained order/trace window through 30 seconds after it; the lookback is capped at 300 seconds before the window's end. This contextual sample does not limit the independent per-fill markout queries. |
| Incidents | Up to 20, direct links first then most recently checked; latest 20 review actions per incident. First/latest check and exchange snapshot JSON are each capped at 128 KiB. Oversize evidence is omitted and flagged. |
| Timeline | Latest 1,000 assembled entries, with omitted-entry count when capped. Other source sections retain their own bounded records. |

The API is `GET /api/history/order-investigation?scope=...&instrument=...&order=...`. It opens local databases read-only, requires an order already present in exchange history, and performs no exchange requests. Bot-only submissions without an exchange order remain accessible in the telemetry and execution panels. History uses one read transaction; references use a separate read transaction shared by contextual prices and markouts. This is not an atomic snapshot across both databases. A missing, corrupt or unsupported reference database leaves exchange/trace/incident evidence usable and marks reference evidence unavailable.

Implementation boundaries: `order_detail.py` joins evidence; `execution.py` and `markouts.py` own calculation policies; `web/order-detail.js` renders the investigation. No storage migration or new worker is required. The isolated browser check (`npm.cmd run test:order-detail-browser`) exercises overlapping synthetic quotes/fills and incidents without contacting a venue or delivering alerts. Real historical fills that predate reference capture cannot acquire retrospective markouts.
