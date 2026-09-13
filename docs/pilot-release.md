# Build the pilot

The release is a Windows source ZIP with a synthetic example and a separate read-only account setup path. The entry point is `README.md` inside the archive. It includes the feedback form, tested dependency pins, operational guides, and the optional LIP SDK. Python itself and dependency wheels are not bundled.

```powershell
python scripts/build_pilot.py
python scripts/build_pilot.py --verify artifacts/releases/othryss-0.1.0-pilot.1.zip
python scripts/check_onboarding_fresh.py --package artifacts/releases/othryss-0.1.0-pilot.1.zip
```

The builder uses an explicit file allowlist, substitutes the synthetic fixture for the internal discovery fixture, and rejects symlink inputs, private-key material, and developer home paths. Local configuration, databases, backups, bot logs, test artifacts, and installed dependencies are excluded. `RELEASE.json` records every member's size and SHA-256; the adjacent `.zip.sha256` covers the archive. These checks detect accidental alteration, not publisher authenticity. An existing output is never overwritten; use `--output` in a new release directory when rebuilding a candidate.

The onboarding check extracts the actual ZIP before running setup, real service processes, synthetic authenticated GETs, collection, backup/restore, and restart checks. It reuses installed dependency distributions in a new virtual environment and simulates Task Scheduler absence. It does not test dependency downloads, a clean OS, or actual provider delivery.

For the example UI, extract the verified archive to a new directory and run:

```powershell
node scripts/check_pilot_browser.mjs C:/path/to/extracted/othryss-0.1.0-pilot.1
```

This starts the extracted app with Python site packages disabled and checks the synthetic timeline, export, replay, and mobile width. Browser development dependencies and Edge are required on the release-check machine only.

Before a new release, update the version in the builder, starter guide, and feedback form together. Run the Python suite, package onboarding check, and package browser check. Retain their reports beside the release record. Share the ZIP and checksum with 2–3 operators; ask for a first-session walkthrough and 2–3 days of ordinary monitoring. Use `FEEDBACK.md` to collect findings. Sending invitations and collecting customer data are manual steps.

Known acceptance limits: the internal live deployment covers one LIP bot family with multiple probes. Independent installations, other bot adapters, clean-OS setup, and longer customer workloads remain pilot validation work. Keep each participant's installation and data separate.
