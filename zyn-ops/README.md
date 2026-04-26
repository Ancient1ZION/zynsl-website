# zyn-ops — Mission Control

The meta-agent layer that watches the agent stack.

## Components

| File | Role |
|------|------|
| `mission_control.py` | git-watch deploy daemon: auto-pull + `pm2 reload` on commits to `zyn-empire-agents/**` |
| `health_audit.py` | hourly executive summary; posts to Discord, snapshots to `audits/` |
| `drift_detector.py` | meta-agent: watches agent behavior, opens GitHub issues, can auto-restart or trigger global STOP |
| `install.sh` | one-command bootstrap for a fresh VM |
| `ecosystem.ops.config.js` | PM2 config for the three daemons |

## Severity Ladder (drift_detector)

| Severity | Trigger | Automatic Action |
|----------|---------|------------------|
| **LOW** | 1–2 anomalies in window | Log + open `drift:low` GitHub issue |
| **HIGH** | 3+ anomalies OR critical tool fail | `pm2 restart <agent>` + open `drift:high` issue |
| **CRITICAL** | exfil pattern OR off-rails persona | Set Sheet `CONTROL!A1 = STOP` + page operator |

## Configuration

All daemons read from the project root `.env` (created by `install.sh` from `.env.example`):

```bash
# Required
GROQ_API_KEY=...
GEMINI_API_KEY=...
GOOGLE_SA_JSON_PATH=/opt/zyn-empire/sa.json
SHEET_ID=1WHN438mjORT4HnGiXapWv78uomVy6KMhHsl0llxaeQk
GAS_PROXY_URL=https://script.google.com/macros/s/.../exec

# For drift_detector → GitHub issues (fine-grained PAT, repo:issues scope only)
GITHUB_TOKEN=ghp_...
GITHUB_REPO=ancient1zion/zynsl-website

# Optional tuning
MISSION_TICK_SECONDS=60
AUDIT_TICK_SECONDS=3600
DRIFT_TICK_SECONDS=600
MISSION_HEALTH_PORT=9090
```

## Health Endpoint

`mission_control` exposes `http://<vm>:9090/healthz` returning current SHA + timestamp. Use this for external uptime monitors (UptimeRobot free tier works well).

## Manual Operation

```bash
# One-shot audit (no daemon)
python3 zyn-ops/health_audit.py --once

# One-shot drift scan
python3 zyn-ops/drift_detector.py --once

# Tail mission control logs
pm2 logs mission_control
```

## Failure Modes

- **GitHub API rate-limit:** drift_detector backs off; issue creation will resume next tick.
- **Sheet API quota:** mission_control's heartbeat is best-effort; agent stack continues regardless.
- **GAS proxy down:** notifications are dropped (logged) but no agent state is lost.
- **Repo conflict on `git pull`:** mission_control logs and skips; operator must resolve manually.

## License

MIT — see [`../LICENSE`](../LICENSE).
