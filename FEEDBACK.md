# Pilot feedback

Release: **0.1.0-pilot.1**

Start with a 15-minute walkthrough, then try normal monitoring for 2–3 days if you connect an account. Skip features you do not use.

## First session

- [ ] Launch the synthetic example or connect your read-only account.
- [ ] From Overview, explain whether collection is current and anything needs attention.
- [ ] Find an order, distinguish active from inactive, and inspect a fill if available.
- [ ] Open an incident if one exists and identify the evidence behind it.
- [ ] Check that dates, font sizes, and labels are easy to read.

## Connected-account checks

- [ ] Live onboarding check passes after the first import.
- [ ] Othryss remains current after an overnight run and a normal restart.
- [ ] An automatic backup completes; an isolated restore succeeds.
- [ ] If telemetry is configured, every intended bot appears and a natural request/fill links correctly.
- [ ] If alerts are configured, an intended test or real incident reaches the recipient.

No incident or fill during the pilot is a valid observation. Mark unavailable checks **not observed**, rather than treating them as passed or creating trading activity to test them.

## Send back

1. **Setup:** Windows/Python version, example or connected account, bot family if applicable, and time to first useful screen.
2. **Most useful:** What did Othryss help you understand or do?
3. **Most confusing:** Which screen, wording, or behavior made you stop?
4. **Missing:** What would make this worth keeping open while you trade?
5. **Reliability:** Any slow screens, missing activity, or noisy/missing alerts? Include local date/time, steps, expected result, and actual result.
6. **Would you keep using it?** Yes / maybe / no, and why.

Attach a sanitized onboarding report if setup failed. Screenshots and order evidence are optional; review account details, market activity, identifiers, and notes before sharing. Never send `local.env`, private keys, alert configuration, or the entire installation folder.
