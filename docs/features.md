# Features and limitations

Othryss is a local Kalshi monitoring and incident-investigation pilot.

| Area | Implemented | Boundary |
| --- | --- | --- |
| Exchange collection | Orders/fills, deduplication, checkpoints, repair, freshness | Polling can miss transient states; default is 60 seconds after a completed cycle |
| Account overview | Balance, position value, inventory, fills/fees, maker/taker counts | Primary subaccount 0; no complete P&L, incentive, or settlement accounting |
| Orders | Active/inactive/unknown sections, search, filters, incremental loading | Latest recorded status |
| Investigation | Exchange observations, fills, requests, cancellation timing, markouts, incidents, exports | Explicit limits; ambiguous linkage remains unknown |
| Bot telemetry | Requests, attempts, retries, outcomes, state, multiple sessions | LIP bot family only; other bots need adapters |
| Reconciliation | Positions, missing/unattributed orders, quantities, missing fills | Requires aligned evidence; shared-market attribution can be ambiguous |
| Incidents | Persistence grace, review states, notes, preserved evidence | Acknowledgement does not establish recovery |
| Source health | Staleness, gaps, dropped records, supported retirement recognition | A stopped collector cannot create new incidents |
| Inventory alerts | Market/total limits, overrides, persistence, JSON/CLI settings | No trading enforcement; disabled by default |
| Execution | Request/attempt latency, retries, cancel/fill classifications | Clock uncertainty; classifications do not automatically create incidents |
| Markouts | 1/5/30-second receipt-time estimates with coverage checks | Requires retained quotes; page summaries, not full accounting |
| Notifications | Discord, signed webhooks, filtering, reminders, recovery, history | No SMS/email or independent host-down alerting; acceptance is not human confirmation |
| Operations | Worker recovery, rotating spools, backups, retries, isolated restores | Windows managed deployment; local backups only; history can grow |
| Onboarding | Synthetic example, repeatable init/check, package regression | Clean-OS installation and independent customer workloads still need validation |

No hosted service, remote authentication, team permissions, other venues, or demonstrated large-customer capacity. Database snapshots are individually consistent, not one atomic cross-database snapshot. Hosting needs additional security and operational work.
