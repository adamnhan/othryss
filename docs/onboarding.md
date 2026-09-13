# Repeatable local onboarding

This is the v0 path for one local Kalshi installation: configure, check, start, verify, then add bot telemetry and alerts. Run commands from the Othryss checkout. Use the same Python environment for dependency installation, service startup and scheduled startup. Windows is the supported managed-service deployment; Python 3.11 or later is required. Node is only needed for browser development tests.

## 1. Initialize the installation

Choose a stable account label and the environment belonging to the key. This label also identifies bot telemetry. Do not reuse another customer's database or credentials.

```powershell
python -m othryss.onboarding init --account my-account --environment demo
python -m pip install -r requirements-reference.txt
```

For a real account, replace `demo` with `production`. Optionally pass `--port 8770`; the default is 8766. A Python virtual environment can be used, but activate it before every command, including the scheduled-task installer.

Initialization creates only missing `ops.local.json`, `local.env` and `alerts.local.json` files. It preserves existing file contents on rerun, including credentials, routes and custom backup settings. An existing account/environment or explicitly requested port mismatch fails before any files are created. Interrupted initialization can be rerun. Fresh configuration has no founder market, no reconciliation pilot, no enabled alert routes and the usual six-hour backup schedule.

Use a separate checkout for another installation; v0 is not a hosted multi-tenant onboarding service. The Windows recovery task currently supports one installation per user and rejects a conflicting checkout. Changing a key on an already bound account requires reviewing the stored binding; setup does not rebind history.

## 2. Configure credentials and check prerequisites

Edit `local.env` with the ID of a dedicated key that has only read scope and the path to its separate unencrypted RSA private-key file:

```dotenv
OTHRYSS_KALSHI_KEY_ID=your-key-id
OTHRYSS_KALSHI_PRIVATE_KEY_PATH=C:/private/kalshi-read-only.pem
```

Relative key paths in `local.env` resolve relative to that file. Process environment variables override file settings; check them if a different key seems to be selected. Keep the private key out of the repository. These local configuration files and common key extensions are ignored by version control.

```powershell
python -m othryss.onboarding check
```

Preflight checks dependency versions, operational settings, private-key loading, enabled alert destinations, an optional retirement-log path and port availability. It makes no network requests. `ready: true` with `phase: preflight` means configuration is ready to try; read scope and collection are still unverified. A busy port is reported separately because an existing Othryss installation may already own it.

Checks emit sanitized JSON with a status, whether each check is required, and a next step. Exit code 0 means the requested phase passed; 1 means a required check failed or is pending. Arbitrary exception text, key IDs, key paths, destinations and response bodies are excluded from reports.

## 3. Start and verify current collection

```powershell
python -m othryss.ops start
python -m othryss.ops status
python -m othryss.onboarding check --live --report artifacts/onboarding/first-live.json
```

Initial account-wide traversal may take several cycles. A pending result is expected until it completes; rerun the check, using a new report filename. Report creation refuses to overwrite an existing file. Open the loopback address for the configured port, select the account and inspect an order if any exist. An empty account can pass collection readiness without manufacturing test fills.

Live checks use the existing GET-only client to verify read scope, environment access and compatibility with primary-subaccount integrations. They verify the stored credential/account binding, a fresh supervisor, all four processes, recent completed collection, and that the local explorer serves that account. They also report available bot sessions, reference evidence, alert activation/provider acceptance and recent backup status. A running process alone cannot pass collection readiness. Checks do not start services, import records, change account bindings, send messages or place orders.

Initialization writes explicit account/environment values. Readiness checks flag configurations that still rely on inherited defaults.

## 4. Add bot evidence when needed

History collection works without a bot hook. Request latency, bot-state reconciliation, exact request linkage and future reference capture need the supported LIP integration. Other bot implementations need a compatible adapter; the LIP hook is not a universal bot installer.

Follow [pilot bot integration](pilot-bot-integration.md) to prepare and review the source patch for the actual bot checkout. Install it using that bot's normal controlled deployment process. Configure the adjacent `othryss_telemetry.json` for the intended probes:

```json
{
  "directory": "C:/path/to/othryss/artifacts/telemetry/lip",
  "account": "my-account",
  "environment": "demo",
  "all_lip_probes": true
}
```

Replace the path, account and environment with this installation's values. Use `all_lip_probes: false` and a `ticker` instead to select one market. The SDK must recognize the bot's REST host; use a supported LIP deployment. Othryss onboarding does not modify or restart a trading bot. Optional `lip_supervisor_log` in `ops.local.json` must point to that deployment's actual log if expected market retirements should be recognized; otherwise leave it null.

```powershell
python -m othryss.onboarding check --live --require-telemetry --report artifacts/onboarding/bot-ready.json
```

This requires at least one healthy bot session and eligible reference evidence. Verify every intended probe appears in the UI; a single healthy session does not certify all bots. Observe a naturally occurring request, then a naturally occurring fill. Export its order investigation and confirm exact linkage, request timing and markout coverage. Historical fills from before reference capture can correctly lack markouts. These live-event acceptance steps remain pending until evidence exists.

## 5. Configure optional alerts and limits

Follow [Discord/webhook setup](alert-delivery.md). Scope routes to this account, put destination secrets in `local.env`, preview the payload, then explicitly enable the desired route. Onboarding never enables a route or sends a test. If a test is wanted, request it explicitly with the alert CLI and confirm it arrives, or wait for a genuine incident.

```powershell
python -m othryss.onboarding check --live --require-alerts --report artifacts/onboarding/alerts-ready.json
```

This requires current activation of every enabled route for this account and a provider-accepted delivery since each activation. Acceptance is evidence from the provider, not proof the recipient read the message. Routes for other accounts do not satisfy this check. The flags can be combined with `--require-telemetry`. Without them, optional integrations remain visible but do not block basic collection readiness.

Configure inventory limits through the [JSON/CLI flow](account-risk-overview.md); the UI displays the configured limits. Limits start disabled. No onboarding default assumes an appropriate inventory size.

## 6. Complete operational acceptance

For login recovery, install the existing Windows task using the same Python environment:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/install_ops_task.ps1
```

Check the first automatic backup completes. On a fresh installation the supervisor reports `waiting_for_databases` until all three database schemas are initialized, then attempts the backup without waiting for the six-hour interval. A manual backup does not replace the supervisor's last scheduled-backup status. Follow [backup and isolated restore](operations.md) to verify a restored copy; restored alert queues are disarmed. The onboarding report reads the supervisor's recorded backup result, and does not perform a new integrity check or restore. Credentials, alert destinations and bot code are excluded from backups and need separate setup on a new machine.

## Run the isolated onboarding regression

```powershell
python scripts/check_onboarding_fresh.py
```

This creates a clean source copy and a new virtual environment under `artifacts/onboarding/fresh-*`, reusing installed dependency distributions. It runs the actual setup, check, start, stop, backup and restore commands with synthetic signed Kalshi GET responses and blocked non-loopback connections. The real local explorer and service processes run on a separate port. Task Scheduler absence is simulated so the test cannot touch an existing installation's task. Acceptance reports remain in that directory; the test's synthetic private key is removed and its services are stopped afterward.

This tests fresh application state on the current machine. It does not validate installing Python/dependencies on a clean operating system, actual venue responses, or external notification delivery.

Keep an acceptance record with:

- Preflight and live reports for the intended account/environment.
- Every intended bot observed, plus an exported naturally occurring request/fill when telemetry is in scope.
- Recipient-confirmed delivery when alerts are in scope.
- Chosen inventory limits or an explicit decision to leave them disabled.
- A completed backup and an isolated restore verification.
- An overnight run with fresh collection, no unexplained recurring alerts and no unexplained source gaps.

Rerun `check --live` after restarting or changing configuration. A successful snapshot check is not an overnight reliability certification. Repeatable configuration and readiness checks are automated; bot deployment, recipient confirmation and the longer observation period still need their own evidence.
