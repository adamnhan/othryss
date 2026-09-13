# Othryss pilot 0.1.0-pilot.1

Othryss helps you investigate trading activity: current inventory, orders and fills, incidents, and the evidence behind them. This pilot runs locally on Windows with a read-only Kalshi key. Python 3.11+ is required; this release was checked with Python 3.12.10. Node is not required.

Extract the ZIP into a new permanent folder, such as `C:\OthryssPilot`. Open PowerShell in that folder. Start with the example, or connect your account below.

## Try the example first

```powershell
python -m othryss.server --port 8765
```

Open **http://127.0.0.1:8765/#example**. The saved example uses entirely synthetic orders and fills; no credentials or extra packages are needed. Try opening a fill, filtering its timeline, and exporting the evidence. The example illustrates a partial fill and exit, not a live monitored account. Stop it with **Ctrl+C**.

## Connect your account

Create an isolated Python environment and install the tested dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-pilot.txt
.\.venv\Scripts\python.exe -m othryss.onboarding init --account my-account --environment production
```

Use a stable label instead of `my-account`. Use `demo` instead of `production` for a Kalshi demo key. Keep that environment choice consistent with the key.

Edit the generated `local.env` with your **dedicated read-only key ID** and the path to its separate private-key file:

```dotenv
OTHRYSS_KALSHI_KEY_ID=your-key-id
OTHRYSS_KALSHI_PRIVATE_KEY_PATH=C:/private/kalshi-read-only.pem
```

Then check configuration and start collection:

```powershell
.\.venv\Scripts\python.exe -m othryss.onboarding check
.\.venv\Scripts\python.exe -m othryss.ops start
.\.venv\Scripts\python.exe -m othryss.onboarding check --live
```

Open **http://127.0.0.1:8766**. The first history import can take several collection cycles. If the live check is pending, rerun it after collection completes. Empty accounts can pass readiness; there is no need to place a test trade.

In the app, start with **Overview**, then open an order in **Orders**. Active orders show their latest recorded resting status. Inactive orders start with five rows; **Show more** loads another 25. **Incidents** holds items needing review. Times display in your local timezone.

## Optional setup

- **Bot telemetry:** request timing, bot-state reconciliation, and reference/markout evidence need a supported integration. See [bot integration](docs/pilot-bot-integration.md). Basic account collection works without one.
- **Discord or a webhook:** follow [alert setup](docs/alert-delivery.md). Routes start disabled. No SMS adapter is included.
- **Inventory alerts:** choose limits through [JSON and the CLI](docs/account-risk-overview.md); limits start disabled.
- **Recovery after login:** follow [service recovery](docs/operations.md). The Windows task supports one installation per user. Install it only from the permanent installation folder, with this installation's Python environment selected.

## Check it over a few days

Use [the pilot checklist and feedback form](FEEDBACK.md). Missing bot evidence and markouts for old fills can be expected; unexplained stale collection or repeated alerts are useful findings.

Save a setup report if something is unclear:

```powershell
.\.venv\Scripts\python.exe -m othryss.onboarding check --live --report artifacts/onboarding/pilot-check.json
```

Use a new report filename each time. This report excludes credential values and destination secrets. Order exports and screenshots can contain trading details; review those before sharing. Send feedback to the person who gave you this package. Othryss does not upload feedback automatically.

## Stop and recover

```powershell
.\.venv\Scripts\python.exe -m othryss.ops stop
.\.venv\Scripts\python.exe -m othryss.ops status
```

Wait for `status: stopped`. Start again with `ops start`. Scheduled backups run every six hours, keep seven completed copies, and retry failures after five minutes. Confirm a completed automatic backup in `ops status`; [restore instructions](docs/operations.md) explain how to check it in a separate directory.

To remove an installed login task, stop Othryss first, then run `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/install_ops_task.ps1 -Remove`. Keep the installation folder until you have preserved any history you want. Upgrades should be coordinated during this pilot; do not overwrite a running installation or copy another user's database into it.

## Pilot boundaries

This is a local Kalshi pilot for primary subaccount 0. The UI has no remote authentication and should stay on loopback. Backups stay on this computer. Historical evidence can grow; producer spool limits do not bound the history database. There is no hosted service or complete strategy P&L. Other bot families need an adapter, and Windows clean-OS installation and broader customer workloads still need field validation.
