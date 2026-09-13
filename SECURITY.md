# Security reports

Do not post credentials, private keys, webhook tokens, account exports, or exploitable security details in public issues.

Use [GitHub's private vulnerability reporting](https://github.com/adamnhan/othryss/security/advisories/new) to report a suspected vulnerability. Include the affected version, reproduction steps using synthetic data, impact, and any suggested fix. Response times are not guaranteed during this early pilot.

The supported deployment is a local Windows installation with a loopback-only UI and dedicated read-only Kalshi credentials. Remote exposure, hosted multi-tenant operation, and other exchange credentials are outside the current supported boundary.

Configuration and key files must remain local. The release builder uses an explicit file allowlist; backups exclude credentials and restore notification routes disarmed. Treat screenshots and order exports as potentially sensitive trading data.
