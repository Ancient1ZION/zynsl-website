# ZYN Empire — Autonomous Multi-Agent System

**Free-tier autonomous agent fleet for ZYN Empire (19 agents).**
Stack: Python 3.11 · LangGraph · Groq (primary LLM) · Gemini 2.0 Flash (fallback) · gspread · loguru · PM2 · GitHub Actions auto-deploy.

## Quick start (GCP VM)

```bash
# 1. Clone the repo
git clone https://github.com/ancient1zion/zynsl-website.git ~/zyn
cd ~/zyn/zyn-empire-agents

# 2. Run bootstrap — generates all 11 production files
python3 bootstrap.py

# 3. Install Python deps
pip3 install -r requirements.txt

# 4. Configure secrets (free, no credit card)
cp .env.example .env
# edit .env and add:
#   GROQ_API_KEY        (free at https://console.groq.com)
#   GEMINI_API_KEY      (free at https://aistudio.google.com/apikey)
#   GOOGLE_SA_JSON_PATH (path to your Google service account JSON)
#   GAS_PROXY_URL       (your Apps Script web app URL for Discord notifications)

# 5. Verify all connections
python3 test_connection.py

# 6. Run forever via PM2
npm install -g pm2
pm2 start ecosystem.config.js
pm2 save
pm2 startup    # follow the printed command to enable on reboot
```

## What runs on the VM

`orchestrator.py` is the Noah supervisor loop:
1. Reads CRM state from Google Sheet `1WHN438mjORT4HnGiXapWv78uomVy6KMhHsl0llxaeQk`.
2. Checks `CONTROL!A1` — if value is `STOP`, pauses all agents and notifies Noah's Discord.
3. Dispatches the right agent based on lead stage / opportunity / event.
4. Each agent runs the LangGraph reasoning loop (Observe → Reason → Act → Reflect).
5. Logs every thought + action to `logs/zyn_empire.log` via loguru (10-day rotation, 30-day retention).

## Auto-update from GitHub

Every push to `main` triggers `.github/workflows/deploy.yml`, which SSHes into the VM, pulls, reinstalls deps, and runs `pm2 reload all` — zero downtime.

GitHub repo secrets required:
- `VM_HOST` — your GCP VM external IP (e.g. `35.185.40.28`)
- `VM_USER` — your VM username
- `SSH_PRIVATE_KEY` — full private key contents (`ssh-keygen -t ed25519`, paste the private half)
- `SSH_PORT` — `22` unless you customized

## Agents

19 agents defined in `agents_config.json`. Edit that file to change goals, tools, personas, or Discord channels. No code changes needed.

## Cost

`$0/month` operating cost. The only money you spend is whatever the GCP VM itself costs (e.g. e2-micro is in the GCP Always Free tier).

## Troubleshooting

- **Agents not running** — `pm2 logs zyn-orchestrator`
- **Sheets auth fails** — make sure the service account email has Editor access on the master Sheet
- **Groq rate-limit** — automatic Gemini fallback in `llm_router.py`
- **Stop everything** — set `CONTROL!A1` in the Sheet to `STOP`
