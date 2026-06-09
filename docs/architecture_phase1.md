# Architecture Phase 1

This phase keeps the live model, policy thresholds, and AutoJS trade decisions unchanged.
The goal is to make the running system easier to operate and easier to migrate between
local and server environments.

## Runtime Boundary

- `server.js`
  - API, dashboard assets, process supervision, and tablet diagnostics.
  - Starts `py/signal_btc.py` and triggers `py/update_live_data.py`.
- `py/signal_btc.py`
  - Loads model artifacts and writes `data/live_signals.json`.
  - Writes signal snapshots to `data/signal_audit.jsonl`.
- `py/update_live_data.py`
  - Refreshes BTC market CSV files.
- `auto_btc.js`
  - Polls `/api/signal`, executes tablet clicks, and reports events to `/api/trade-audit`.

## Event Store

Trade audit events now pass through `lib/event_store.js`.

Each trade audit event gets:

- `serverId`
- `eventId`
- `receivedAt`
- `eventStoreVersion`

The backing storage is still JSONL for now. This is intentional: it is low risk and keeps
the current service behavior stable. The new event store boundary makes it possible to
move to SQLite or Postgres later without rewriting all routes at once.

## API Token

Set one of these environment variables on the server to enable write/control API auth:

```powershell
$env:API_TOKEN = "change-me"
```

Supported names:

- `API_TOKEN`
- `CODEX_API_TOKEN`
- `TRADE_API_TOKEN`

Protected endpoints include:

- `POST /api/config`
- `POST /api/manual`
- `DELETE /api/manual`
- `POST /api/trade-audit`
- `POST /api/trade-audit/import`
- `POST /api/balance`
- `POST /api/data-update/refresh`
- `POST /api/reports/refresh`

Clients can send the token with:

- header `X-API-Token`
- header `Authorization: Bearer <token>`
- query string `?token=<token>`

`auto_btc.js` has `API_TOKEN = ""` by default. Leave it empty when the server does not
enforce auth. If the server enables auth, fill the same token in the tablet script.

## Runtime Environment

Useful environment variables:

- `SERVER_ID`: stable name for this runtime, shown in `/api/runtime`.
- `DATA_DIR`: data directory used by `server.js`; defaults to `<repo>/data`.
- `PUBLIC_BASE_URL`: public URL served to the tablet.
- `API_TOKEN`: optional write/control API token.
- `DISABLE_MANAGED_PROCESSES=1`: start only the Node API and do not launch Python signal
  or data update jobs. This is useful for local API tests.

## Import Local Trade Audit To Server

Use this when old tablet events were posted to the local machine before the tablet was
switched to the server.

```powershell
Set-Location E:\codex
powershell -ExecutionPolicy Bypass -File .\tools\import_trade_audit.ps1 `
  -ServerUrl "http://115.190.218.128:3000" `
  -AuditPath ".\data\trade_audit.jsonl" `
  -Source "local-before-server-switch"
```

If the server has `API_TOKEN` enabled:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\import_trade_audit.ps1 `
  -ServerUrl "http://115.190.218.128:3000" `
  -Token $env:API_TOKEN
```

The import endpoint deduplicates by `eventId`, so rerunning the same import should skip
already imported rows.

## Current Next Refactor

Recommended next steps:

- Move route groups out of `server.js`.
- Move signal policy configuration into versioned manifest files.
- Move JSONL event storage to SQLite after the event API is stable.
- Add basic API tests for auth, import, signal health, and trade history.
