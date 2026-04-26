# ZYN Empire Architecture

This document describes how the three layers of ZYN Empire fit together.

## High-Level Diagram

```
                    ┌─────────────────────────────┐
                    │   Operator (Browser)        │
                    │   dashboard.html (Pages)    │
                    └────────────┬────────────────┘
                                 │ reads / writes
                                 ▼
                    ┌─────────────────────────────┐
                    │   Google Sheet              │
                    │   - CRM tab                 │
                    │   - CONTROL tab (A1=STOP)   │
                    │   - Heartbeat tab           │
                    └────────────┬────────────────┘
                                 │ gspread (read/write)
                                 ▼
   ┌─────────────────────────────────────────────────────────┐
   │              GCP VM  (35.185.40.28)                      │
   │                                                           │
   │   ┌───────────────────┐    ┌───────────────────────┐     │
   │   │  zyn-ops daemons  │───▶│  zyn-empire-agents     │     │
   │   │                   │    │  (PM2-managed)         │     │
   │   │  mission_control  │    │   ┌──────────────┐     │     │
   │   │  health_audit     │    │   │ orchestrator │     │     │
   │   │  drift_detector   │    │   │  (Noah)      │     │     │
   │   └─────────┬─────────┘    │   └──────┬───────┘     │     │
   │             │              │          │              │     │
   │             │ reads logs   │   ┌──────▼───────┐     │     │
   │             │ git fetch    │   │  19 agents   │     │     │
   │             │              │   │  (LangGraph) │     │     │
   │             │              │   └──────┬───────┘     │     │
   │             │              │          │              │     │
   │             │              │   ┌──────▼───────┐     │     │
   │             │              │   │  llm_router  │     │     │
   │             │              │   │  + tools.py  │     │     │
   │             │              │   └──────┬───────┘     │     │
   │             │              └──────────┼─────────────┘     │
   │             │                         │                     │
   │             ▼                         ▼                     │
   │     logs/zyn_empire.log       Groq / Gemini APIs           │
   │                                Discord (via GAS proxy)      │
   └─────────────────────────────────────────────────────────────┘
```

## Layer 1: Dashboard (Browser)

- **Hosting:** GitHub Pages (`dashboard.html` → `https://ancient1zion.github.io/zynsl-website/dashboard.html`)
- **Stack:** Static HTML/CSS/JS, no framework. Vanilla CodeMirror-free.
- **Notable subsystems:**
  - `ZYN_CONFIG` block — runtime configuration (proxy URL, sheet ID, version, STOP cell)
  - `zynNotify()` — proxies all webhook traffic through GAS, never embeds raw Discord URLs
  - `zynCheckStop()` — polls `CONTROL!A1`, freezes UI on STOP
  - Sidebar a11y — full keyboard navigation across 17 nav items
- **Persistence:** the dashboard is stateless; all data lives in the Google Sheet.

## Layer 2: Agent Stack (`zyn-empire-agents/`)

- **Process supervisor:** PM2 (`ecosystem.config.js`)
- **Reasoning loop:** LangGraph state machine — `observe → reason → act → reflect`, capped at 5 iterations per task to prevent runaway loops.
- **Agent definition:** declarative JSON (`agents_config.json`). Each agent has `id`, `role`, `goal`, `tools`, `persona`, `memory_key`, `discord_channel`.
- **LLM router:** Groq primary, Gemini fallback. Failover triggers on 429 / 5xx / timeout.
- **Tools:**
  - `sheets_read/write/append` — gspread to the master sheet
  - `discord_notify` — through GAS proxy
  - `web_search` — DuckDuckGo (no API key)
  - `sam_gov_search` — government contract search
  - `send_email` — Gmail SMTP via app-password
  - `file_read/write` — sandboxed to repo subfolder
- **Orchestrator (Noah):** 5-minute task tick, hourly heartbeat to Sheet, every-tick STOP-cell check.

## Layer 3: Mission Control (`zyn-ops/`)

The meta-agent layer. Watches the watchers.

### `mission_control.py`

A daemon that runs on the VM alongside the agent stack. Every 60 seconds it:

1. `git fetch && git diff HEAD origin/main --name-only`
2. If any path under `zyn-empire-agents/**` changed:
   - `git pull --ff-only`
   - If `agents_config.json` changed → `pm2 restart all`
   - Else → `pm2 reload all`
   - Post deploy summary to Discord via GAS proxy
3. Heartbeat row written to Sheet `HEARTBEAT` tab.

### `health_audit.py`

Hourly executive summary. Reads `logs/zyn_empire.log`, parses for:

- Retry / Error / RateLimit signals
- Agent activity counts (last hour)
- Tool failure rates
- Heartbeat freshness from Sheet

Posts a clean status report to Discord and writes a snapshot to `zyn-ops/audits/YYYY-MM-DD-HH.json`.

### `drift_detector.py`

The killer feature. A meta-agent that watches the agents.

**Detection signals:**

- Sentiment swings (using Groq for cheap classification)
- Off-topic outputs (compares output to agent's declared `goal`)
- Tool failure clusters (>3 failures of the same tool by the same agent in 10 min)
- Persona deviation (output style markedly different from `persona` field)

**Severity ladder:**

| Severity | Trigger | Action |
|----------|---------|--------|
| LOW | 1-2 anomalies in last hour | Log warning, open GitHub issue tagged `drift:low` |
| HIGH | 3+ anomalies OR critical tool failure | Auto `pm2 restart <agent>` + GitHub issue `drift:high` |
| CRITICAL | Off-rails persona OR data exfil pattern | Set Sheet `CONTROL!A1 = STOP` + page operator |

**Issue body** includes:

- Offending log lines (last 20 entries for the agent)
- Current `agents_config.json` entry for the agent
- Proposed `persona`/`goal` patch as a unified diff
- Severity score and recommended action

The operator reviews and merges. `mission_control.py` then auto-deploys.

## Failure Modes & Recovery

| Failure | Detection | Recovery |
|---------|-----------|----------|
| Agent process crash | PM2 `autorestart: true` | Automatic restart within 1s |
| Groq rate limit | `llm_router.py` 429 handler | Failover to Gemini |
| Sheet API quota | gspread retry-with-backoff | Up to 3 retries, then queue |
| Drift / off-rails | `drift_detector.py` | Severity-based response |
| VM down | Operator notices via dashboard heartbeat staleness | Manual SSH + `pm2 resurrect` |
| Repo conflict | `mission_control.py` skips pull | Discord alert; operator resolves |
| Operator E-stop | Sheet `CONTROL!A1 = STOP` | All agents halt within 60s |

## Configuration & Secrets

| Where | What |
|-------|------|
| GitHub Actions secrets | `VM_HOST`, `VM_USER`, `SSH_PRIVATE_KEY`, `SSH_PORT` |
| VM `/opt/zyn-empire/.env` | `GROQ_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_SA_JSON_PATH`, `SHEET_ID`, `GAS_PROXY_URL`, `GITHUB_TOKEN` (for issue creation) |
| GAS Script Properties | `WEBHOOK_NOAH`, `WEBHOOK_MALIK`, ... (one per agent) |
| Dashboard `ZYN_CONFIG` | `GAS_PROXY_URL` (the only public-side secret-adjacent value) |

## Roadmap

- pytest suite for the agent reasoning loop
- Public demo mode (read-only sheet, sandbox agents)
- Ollama fallback for the privacy-sensitive deployment profile
- Multi-tenant support (one sheet per tenant)
