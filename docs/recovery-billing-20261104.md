# Restore the miniapp backend contract

## Problem

The GitHub snapshot predates several production features. Deploying that snapshot
removed charge history, parking bills, weather, charging cost cards, and telemetry
normalization. Before this change, the Hong Kong service returns 404 for
`GET /api/charge_sessions`, `/api/parking_sessions`, and `/api/weather`.

## Sources and boundaries

- Start from GitHub main `d722550`; merge the verified November 2 source using
  `542ec29` as the common base. The November 3 pre-deployment backup matches that
  verified backend source.
- Retain monitor user operations, protected-account checks, and confirmation gates.
  Their Python syntax trees are identical to main after the merge.
- Align helper modules and the watcher with the files already running in production.
  Include the corresponding offline regression tests; no user records or credentials.
- The resulting web page matches the current production page, including its unbind
  confirmation. Existing UI updates are preserved.
- No history is invented, no user directory is deleted, and no vehicle command is
  needed for verification. Missing historical bill data is a separate recovery issue.

## Validation

Python 3.11 syntax checks and 50 offline tests pass:

```text
python -m unittest test_restored_contracts test_charging_ledger test_parking_ledger
  test_state_observability test_weather_bridge test_vehicle_recording
  test_charge_chat_card test_trip_addresses test_telemetry_ingest
```

The HTTP dispatch tests exercise authentication on the restored GET/POST routes,
charge history privacy, cost persistence, and protected/unconfirmed unbind rejection.
Existing scenario scripts hard-code `/opt/tesla-fleet-multi`; they cannot run as-is
on Windows and are not counted as passing.

## Release procedure

Follow the review requirement in AGENTS.md before deployment. Re-read and hash the
actual systemd service source, back it up, and apply only the reviewed difference.
Do not overwrite newer production work with a stale repository snapshot. Most
helper modules already match production; do not restart the watcher needlessly.

After deployment, verify health, unauthenticated 401 responses instead of 404 on
the restored routes, authenticated charge/parking responses, and the existing trip
list. Verify runtime error counts and synchronize the Beijing standby and local
runtime mirror. Use isolated tests for writes and unbind protections; do not send
real vehicle commands or edit live billing records as a smoke test.

If verification fails, restore the timestamped backup and restart only tesla-web.
