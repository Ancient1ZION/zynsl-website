# ZYN Empire

> An open-source, multi-agent autonomous CRM platform powered by free-tier LLM APIs.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Agents](https://img.shields.io/badge/agents-19-blueviolet)](./zyn-empire-agents/agents_config.json)
[![Stack](https://img.shields.io/badge/llm-Groq%20%2B%20Gemini-00d8ff)](#llm-stack)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](./CONTRIBUTING.md)

ZYN Empire is a fully-autonomous, multi-agent CRM and operations platform. Nineteen specialized agents (Noah the supervisor, Sara the sourcer, Malik the closer, Lea the tracker, Ruth the analyst, and 14 others) reason, act, and reflect on tasks pulled from a Google Sheet, coordinated by a LangGraph state machine, with a self-healing mission-control layer watching the watchers.

## Architecture

The system is split into three independently-evolvable layers:

1. **Dashboard** (`/dashboard.html`) — the human-facing control surface. Live at [ancient1zion.github.io/zynsl-website/dashboard.html](https://ancient1zion.github.io/zynsl-website/dashboard.html).
2. **Agents** (`/zyn-empire-agents/`) — the 19-agent reasoning stack running on a GCP VM via PM2.
3. **Mission Control** (`/zyn-ops/`) — the meta-agent layer that monitors, audits, and self-heals the agent stack.

For a full system diagram see [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md).

## Quickstart

```bash
curl -fsSL https://raw.githubusercontent.com/ancient1zion/zynsl-website/main/zyn-ops/install.sh | bash
```

The installer clones the repo, installs Python deps, registers PM2 services for the agent stack and the mission-control daemons, and posts a startup heartbeat to your Discord proxy.

## LLM Stack

ZYN Empire uses an API-based reasoning stack (no local GPU required):

- **Primary:** Groq (`llama-3.3-70b-versatile`) — free tier, sub-second latency
- **Fallback:** Google Gemini 2.0 Flash — free tier, automatic failover on 429/5xx

Both providers offer permanent free tiers with no credit card required. The router (`zyn-empire-agents/llm_router.py`) handles failover transparently.

## Mission Control

Three daemons supervise the empire:

- `mission_control.py` — git-watch deploy daemon. Auto-pulls and `pm2 reload`s on commits to `zyn-empire-agents/**`.
- `health_audit.py` — hourly executive summary posted to Discord.
- `drift_detector.py` — meta-agent that watches agent behavior and opens GitHub issues (with severity scoring) when an agent goes off-rails.

## Repository Structure

```
zynsl-website/
├── dashboard.html          # Live operator dashboard (GitHub Pages)
├── zyn-empire-agents/      # 19-agent reasoning stack
│   ├── agents_config.json
│   ├── llm_router.py
│   ├── tools.py
│   ├── zyn_agent.py
│   ├── orchestrator.py
│   └── ecosystem.config.js
├── zyn-ops/                # Mission-control / meta-agent layer
│   ├── mission_control.py
│   ├── health_audit.py
│   ├── drift_detector.py
│   └── install.sh
├── docs/
│   └── ARCHITECTURE.md
└── .github/
    ├── workflows/          # CI + deploy automation
    └── ISSUE_TEMPLATE/
```

## Contributing

PRs welcome. See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the development loop, and [`SECURITY.md`](./SECURITY.md) for vulnerability disclosure.

## License

MIT — see [`LICENSE`](./LICENSE). Fork freely.
