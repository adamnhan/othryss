# Historical orders and fills

This milestone adds a persistent local event store and an authenticated, GET-only Kalshi importer. The existing explorer still uses its original fixture; imported history is inspectable and exportable through the CLI. No trading bot changes or live watcher are involved.

## Configure the key

Edit the ignored `local.env` file in the project root:

```dotenv
OTHRYSS_KALSHI_KEY_ID=your-read-only-key-id
OTHRYSS_KALSHI_PRIVATE_KEY_PATH=C:/path/to/private-key.pem
```

Use a dedicated key with only `read` scope. The importer checks the current key's metadata through `GET /api_keys` before reading trading history. It neither creates keys nor changes permissions. Kalshi documents the scope field in [Get API Keys](https://docs.kalshi.com/api-reference/api-keys/get-api-keys).

Paths can contain spaces and may be quoted. Relative paths in `local.env` resolve relative to that file. Existing process environment values take precedence; `--key-file` overrides the path. `--env-file` selects a different configuration file, and `--key-id-env` selects a different key-ID variable name. Values are read as text without shell execution or variable expansion. The private key, signatures, key ID and authentication headers are not written to the event store or logs.

The only additional Python dependency is used for RSA request signing:

```powershell
python -m pip install -r requirements-import.txt
```

## First authenticated import

Start with the known incentives market:

```powershell
python -m othryss.history_cli import --account my-account --ticker KXTEMPMIAH-26AUG1811-T90.99 --max-pages 30
```

The database defaults to `artifacts/history/othryss.sqlite`. Change it with the global argument, placed before the subcommand:

```powershell
python -m othryss.history_cli --db artifacts/history/another.sqlite import --account my-account --ticker KXTEMPMIAH-26AUG1811-T90.99
```

Omit `--ticker` to traverse all available orders and fills exposed to the key. The importer does not apply a hidden date limit. `--page-size` defaults to 100; requests are paced at approximately two per second, with bounded retries for rate limits, selected server errors and connection failures.

Each successful page prints a small progress record, including the run ID, record count, inserted events and duplicates. No credential values or raw response bodies are printed. An import can read saved account history but cannot place, cancel or amend orders: the client exposes only GET requests and an explicit endpoint allowlist. Redirects are not followed.

## Resume and inspect

```powershell
python -m othryss.history_cli status
python -m othryss.history_cli status --run-id RUN_ID
python -m othryss.history_cli import --account my-account --resume RUN_ID
```

Resume uses the saved ticker, page size, account scope and pagination cursors. Page budgets apply to the current invocation. An API-expired cursor requires a new run without `--resume`; rescanning safely deduplicates already imported events. Only one importer may write the same database at a time; its OS-held lock releases automatically if the process exits or crashes.

| Status | Meaning |
| --- | --- |
| `running` | Work began; a crash may leave this status until resumed. |
| `paused` | The configured page budget was reached; committed data and the next cursor are retained. |
| `failed` | Request, normalization or persistence failed; the last committed page is retained. |
| `traversed` | All four endpoint cursors were exhausted, and before/after archive cutoff timestamps agreed. This is not an atomic account snapshot. |
| `needs_rescan` | The archive cutoff moved during the import; start another run to traverse both tiers again. |
| `saved_only` | Fills were imported from a saved bot report; no independent exchange fetch occurred. |

Exit codes: 0 for a successful traversal/saved import or inspection, 2 for a paused/needs-rescan run or failed known-fill comparison, 1 for a command error, and 130 for interruption.

An empty or short page with a nonempty cursor is not completion. A missing cursor, cursor cycle, malformed record, missing fill fee or conflicting execution identity fails the import visibly; the problematic page does not partially commit. Failed source pages are fetched again on resume; their contents are not currently quarantined in the database.

## Verify the known fills

```powershell
python -m othryss.history_cli verify-known 'C:\path\to\saved-probe.json' --run-id RUN_ID
```

This compares the original fill IDs, order links, timestamps and normalized economics against the imported scope. It reports matched, missing and conflicting records. Saved-only comparisons are explicitly distinguished from independent API imports.

To exercise persistence without credentials:

```powershell
python -m othryss.history_cli import-saved 'C:\path\to\saved-probe.json'
```

Running this twice retains two fill events and records two duplicates on the second run. Saved reports occupy a separate `saved-report` scope so they cannot masquerade as authenticated history.

## Export

```powershell
python -m othryss.history_cli export --run-id RUN_ID --output artifacts/history/export.ndjson
```

The export streams all normalized events in the selected run's account scope, including events from other imports in that scope. It writes one JSON record per line and refuses to overwrite an existing destination. Source page evidence and checkpoints remain in SQLite. The account scope is explicit and is not interchangeable with the original fixture's anonymized aliases.

## Storage and scope

SQLite uses WAL, full synchronous commits, foreign keys and a versioned schema. A page's normalized facts, allowlisted source rows, evidence links and next cursor commit in one transaction. On duplicate observations, existing facts remain unchanged and manual imports link new source-page evidence. The polling collector retains new-event evidence only, avoiding copies of unchanged payloads on every poll; see [collector operation](collector.md).

- Scope includes workspace, venue, production/demo/saved environment and a stable local account label. The label binds to a hash of the credential ID to prevent accidental cross-account mixing. Credential rotation under an existing label is deliberately not automated; use a new label until an explicit account-verification/migration flow exists.
- Fill identities are scoped to the account and subaccount. Economic values and timestamps normalize before comparison, so string formatting alone does not produce duplicate fills. Conflicting immutable fill records fail visibly rather than overwrite history.
- Orders are versioned state observations, not reconstructed submissions or ACKs. Distinct observed states are retained; repeatedly observing the same state is deduplicated. Missing last-update timestamps stay null and do not inherit the order's creation time.
- Strategy attribution stays unknown in exchange-only imports. Source `client_order_id` is retained on order observations for later linking, without parsing proprietary bot conventions in the analytics core.
- Decimal quantities, prices and fees remain strings in storage and exports. Replay calculations use Decimal. Missing fees do not become zero.
- Account, raw credential and proprietary model details are not necessary for the importer. Source evidence uses a field allowlist. Files remain local; the browser server cannot serve the database or configuration files.

The storage and venue adapter are separate modules. SQLite is the local implementation for this milestone; moving to PostgreSQL will require a new storage implementation and migration, not changing Kalshi field mappings or normalized event semantics. No hosted scalability claim has been established by these local tests.

## API behavior verified during implementation

The importer traverses `/portfolio/orders`, `/portfolio/fills`, `/historical/orders` and `/historical/fills`. It records `/historical/cutoff` before and after traversal. Kalshi's historical split means a current endpoint alone cannot establish full available history. [Historical data](https://docs.kalshi.com/getting_started/historical_data)

Current endpoints support richer filters than the archived endpoints. This importer uses only ticker, limit and cursor shared by the relevant endpoints, avoiding an unsupported archived `min_ts` or subaccount query. A key restricted to a subaccount defines the authorized scope; returned records must match that bound scope. [Historical orders](https://docs.kalshi.com/api-reference/historical/get-historical-orders), [historical fills](https://docs.kalshi.com/api-reference/historical/get-historical-fills)

Signatures use RSA-PSS/SHA-256 over timestamp + GET + path, excluding query parameters. [Kalshi authentication](https://docs.kalshi.com/getting_started/api_keys)

## Validation so far

The test suite covers multi-page and cross-tier deduplication, SQLite reopen/resume, transaction rollback, failed requests, incomplete/cyclic pagination, moving cutoffs, order-state history, scope isolation, exact decimals, authentication signatures, strict read-only checks and local.env parsing. Persisted order observations can be consumed by the replay engine without turning snapshot fill counters into extra executions or inventing bot state.

Authenticated production validation completed on September 9, 2026, after verifying that the configured key has only the `read` scope. The account import traversed all four current and historical order/fill streams across 66 pages, storing 6,414 unique events: 5,481 order observations (5,481 distinct exchange orders) and 933 fills. Every pagination cursor completed, the historical cutoffs remained unchanged, and SQLite's integrity check returned `ok`. Traversal establishes the history available through those endpoints, not a complete stream of every past order-state transition.

All 42 known incentives fills from 14 saved reports independently matched the authenticated API import by identity, timestamp and economics, with zero missing or conflicting records. A repeat import of the original example paused after one page and resumed in a separate CLI process; it completed with zero inserted events and four duplicates, leaving the account event count unchanged. The local validation summary is saved at `artifacts/history/authenticated-validation.json`; the full account traversal report is at `artifacts/history/account-import-report.json`.

## Browse imported evidence

Run `python -m othryss.server` and open `http://127.0.0.1:8765`. Imported history is the default view. Select an account, search by ticker or order ID, and select an order to inspect its recorded fills and exchange order snapshots. `--db PATH` selects an alternate history database. The browser's refresh button rereads local committed data; it never triggers exchange requests.

The reader opens SQLite with `mode=ro` and `query_only`, and each API response uses a consistent read transaction. Account metadata excludes credential fingerprints. Order queries always bind account scope; event detail additionally binds instrument and order ID. Lists return 25 orders and detail returns 50 events by default, with an API maximum of 100 per page. Totals stream all fills for the selected order using Decimal, so pagination does not change economics and snapshot fill counters never add executions. Source references include import run, endpoint, page, row and receipt time (at most 20 references per event, with the total reference count shown).

Evidence exports retrieve every event page for the selected order and fail if event counts or fill totals change between pages. They exclude raw API pages and credentials. Import receipt time is explicitly distinguished from execution time and bot latency; missing source update times remain unavailable. Snapshot status is last observed status, not a live freshness claim. Independent positions, settlements, incentives and strategy attribution remain outside this view.

Validation: 53 Python tests pass, including collection recovery, scope/instrument isolation, bounded pagination, whole-order decimal totals, missing timestamps, read-only enforcement and HTTP file restrictions. Browser acceptance checks passed against the authenticated history: account totals, order pagination/search, source-account switching, original fill evidence, complete order export, empty states, fixture switching, collection health and mobile/tablet layouts. The original fixture browser checks also pass.
