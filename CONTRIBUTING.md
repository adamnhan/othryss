# Contributing

Othryss is an early local pilot. Small fixes, reproduction cases, and clear usability feedback are especially useful. Open an issue before a substantial adapter, storage, or deployment change so its scope can be discussed.

## Development

Use Python 3.11+ and a virtual environment. Install `requirements-reference.txt`, then run `python -m unittest discover -s tests -v`. Tests use temporary databases and synthetic responses. A deployment-specific acceptance test is skipped when its separately prepared bot source is absent.

Optional browser checks use Node and Playwright:

```powershell
npm ci --ignore-scripts
npx playwright install chromium
```

Set `BROWSER_CHANNEL=chromium` in the shell, or use the default installed Microsoft Edge. `npm run test:browser` exercises the synthetic example. Other `test:*` scripts exercise isolated account-risk, incident, order-investigation, execution, markout, and notification scenarios.

## Pull requests

Explain the problem, resulting behavior, and relevant validation. Keep exchange adapters, event semantics, analysis, storage, and presentation separate. Preserve exact decimal arithmetic, account/subaccount isolation, explicit timing uncertainty, and unknown/missing evidence states.

Trading requests must never be introduced into Othryss collection or analysis. Telemetry must preserve bot behavior and report loss instead of blocking trading on collection. Tests should use synthetic records; do not submit real account history, credentials, webhook destinations, or bot logs.

The Windows task supports one installation per user. Use the isolated onboarding harness to test startup without changing an existing task. Follow [release validation](docs/pilot-release.md) for package changes.
