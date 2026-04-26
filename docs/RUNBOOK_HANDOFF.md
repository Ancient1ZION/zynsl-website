# Operator Handoff Runbook

Three tasks only the human operator can do. Each is ~5 minutes. Order matters.

---

## 1. Rotate the 7 Discord webhooks

For each of the 7 channels (`noah`, `malik`, `sara`, `drift`, `health`, `alerts`, `ops`):

1. Open Discord -> your server -> **Server Settings** -> **Integrations** -> **Webhooks**.
2. Click the webhook for that channel -> **... -> Delete Webhook**. Confirm.
3. **New Webhook** -> name it (e.g. `noah`) -> select the matching channel ->
   **Copy Webhook URL**.
4. Paste the URL somewhere private (you will move it into Apps Script next).

You should end with 7 fresh webhook URLs in a private notepad. Do **not** put
them in Slack, email, GitHub, or the dashboard.

---

## 2. Deploy the GAS proxy

See [`zyn-ops/gas-proxy/README.md`](../zyn-ops/gas-proxy/README.md) for the
full procedure. Short form:

1. <https://script.google.com> -> **New project** -> paste `Code.gs`.
2. Script Properties: add `WEBHOOK_NOAH` ... `WEBHOOK_OPS` (the 7 URLs from step 1),
   plus `SHEET_ID` and `SHARED_SECRET` (a long random string).
3. **Deploy -> New deployment -> Web app**, execute as **Me**, access **Anyone**.
4. Copy the `/exec` URL.
5. SSH to the VM and edit `zyn-empire-agents/.env`:
   ```
   GAS_PROXY_URL=https://script.google.com/macros/s/.../exec
   GAS_PROXY_KEY=<same SHARED_SECRET>
   ```
6. Tell Claude in chat: "GAS proxy URL is `<URL>`" — Claude will commit it
   to `dashboard.html` for you.

---

## 3. GitHub Actions secrets — NOT NEEDED ANYMORE

The previous deploy model required `VM_HOST`, `VM_USER`, `SSH_PRIVATE_KEY`,
`SSH_PORT`. We replaced it with a **pull** model:

- `zyn-ops/mission_control.py` runs on the VM as a pm2 daemon.
- It polls `origin/main` every 30 seconds and fast-forwards.
- When it sees changes under `zyn-empire-agents/`, it runs
  `pm2 reload ecosystem.config.js`.
- GitHub Actions never SSHes to the VM. No private key leaves your machine.

`.github/workflows/deploy.yml` is now just a verifier (lint + sanity check).
There are no secrets to add. **You can skip this task entirely.**

---

## Verification

After steps 1 + 2:

```bash
# From the VM
cd ~/zyn-empire-agents
python3 -c "
import os, requests, json
url = os.environ['GAS_PROXY_URL']
key = os.environ['GAS_PROXY_KEY']
r = requests.post(url + '?key=' + key, json={
    'channel': 'ops',
    'username': 'handoff-test',
    'content': 'Proxy is live.'
}, timeout=10)
print(r.status_code, r.text)
"
```

You should see `200` and a "Proxy is live." message land in `#ops`.

