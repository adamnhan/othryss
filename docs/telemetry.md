# Bot integration during the pilot

Account collection needs only a read-only Kalshi key. Request latency, comparisons with bot state, and reference capture for bot markets additionally require telemetry. The bundled SDK supports the LIP incentives probe; arbitrary bots need an adapter and should be treated as an assisted integration during this pilot.

For a compatible LIP source, prepare a patch from the Othryss installation directory:

```powershell
python integrations/lip/prepare_patch.py --source C:/path/to/bot/scripts/lip_requote_probe.py
```

This produces a reviewable patch, staged source, SDK, and hashes under `artifacts/integrations/lip`. It does not modify or restart the bot. Review compatibility with the actual source and deploy through that bot's normal controlled stop/restart process, preserving its trading settings and rollback copy. The SDK belongs beside the bot's importable modules, not in the Othryss service process.

Place `othryss_telemetry.json` beside the installed SDK:

```json
{
  "directory": "C:/OthryssPilot/artifacts/telemetry/lip",
  "account": "my-account",
  "environment": "production",
  "all_lip_probes": true
}
```

Use the installation's exact path, account label, and environment. The SDK checks the REST host and assumes primary subaccount 0. If the bot has a compatible supervisor retirement log, configure its actual absolute path as `lip_supervisor_log` in `ops.local.json`, then restart Othryss. Otherwise leave it null.

Run `python -m othryss.onboarding check --live --require-telemetry` using the installation's Python environment. Check every intended probe in **Data sources**, then inspect a naturally occurring request and fill in **Orders** or **Execution**. A single healthy session does not certify all bots. Fills from before quote capture can lack markouts.

For a different bot, provide its language, runtime location, request lifecycle, order/client IDs, and how it reports current order and position state. Use [the event contract](event-contract-draft.md) and [bot-state semantics](bot-state-incidents.md) as references. Agree on adapter scope before modifying a running trading system.
