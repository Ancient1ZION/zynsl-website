# ZYN GAS Discord Proxy

Single-purpose Google Apps Script that fronts all 7 Discord webhooks.

## Why
- Webhook URLs never appear in source, dashboard HTML, or VM env files.
- One chokepoint for rate-limiting + STOP-signal enforcement.
- Rotating a webhook = update one Script Property, no redeploy.

## Setup (one-time)

1. Open <https://script.google.com> -> **New project**.
2. Replace `Code.gs` with the contents of [`Code.gs`](./Code.gs) in this folder.
3. **Project Settings -> Script Properties -> Add script property** for each:
   - `WEBHOOK_NOAH`, `WEBHOOK_MALIK`, `WEBHOOK_SARA`, `WEBHOOK_DRIFT`,
     `WEBHOOK_HEALTH`, `WEBHOOK_ALERTS`, `WEBHOOK_OPS`
   - `SHEET_ID` = the spreadsheet ID holding `CONTROL!A1`
   - `SHARED_SECRET` = a long random string (agents will send this as `?key=...`)
4. **Deploy -> New deployment -> Type: Web app**.
   - Execute as: **Me**
   - Who has access: **Anyone** (auth happens via `SHARED_SECRET`)
5. Copy the resulting `/exec` URL.

## Wire-up

- On the VM, set in `zyn-empire-agents/.env`:
  ```
  GAS_PROXY_URL=https://script.google.com/macros/s/AKfy.../exec
  GAS_PROXY_KEY=<same SHARED_SECRET>
  ```
- In `dashboard.html`, replace the empty `GAS_PROXY_URL: ''` line with the
  same URL (no `?key=` — dashboard uses GET-only health checks).

## Calling

```bash
curl -X POST "$GAS_PROXY_URL?key=$GAS_PROXY_KEY" \
     -H 'Content-Type: application/json' \
     -d '{"channel":"noah","username":"Noah","content":"hello"}'
```

## Rotating a webhook

1. Discord -> Server Settings -> Integrations -> Webhooks -> regenerate URL.
2. Apps Script -> Project Settings -> Script Properties -> edit `WEBHOOK_<NAME>`.
3. Done. No redeploy needed; `PropertiesService` reads live.

## STOP signal

If `CONTROL!A1` in the linked sheet equals `STOP` (case-insensitive,
trimmed), every POST returns `{ok:false, dropped:true}`. Agents that see
`dropped:true` should pause their loop.

