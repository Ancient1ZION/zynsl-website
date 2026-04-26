#!/usr/bin/env python3
"""ZYN Empire — drift_detector.py

Meta-agent that watches the agents.

Detection signals:
  - Sentiment swings (Groq classification)
  - Off-topic outputs (compares output vs declared goal)
  - Tool failure clusters (>3 same-tool failures by same agent in 10 min)
  - Persona deviation (output style diverges from persona field)

Severity ladder:
  LOW       1-2 anomalies/hr        log + GitHub issue (drift:low)
  HIGH      3+ anomalies/hr OR      auto pm2 restart <agent> + issue (drift:high)
            critical tool failure
  CRITICAL  off-rails persona OR    set Sheet CONTROL!A1=STOP + page operator
            data-exfil pattern
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DRIFT_TICK_SECONDS = int(os.getenv("DRIFT_TICK_SECONDS", "600"))  # 10 min
DRIFT_LOG_LINES = int(os.getenv("DRIFT_LOG_LINES", "2000"))
AGENT_LOG = ROOT / "logs" / "zyn_empire.log"
AGENTS_CONFIG = ROOT / "zyn-empire-agents" / "agents_config.json"
GAS_PROXY_URL = os.getenv("GAS_PROXY_URL", "").strip()
SHEET_ID = os.getenv("SHEET_ID", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "ancient1zion/zynsl-website").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

# Patterns suggesting data exfiltration / off-rails persona — CRITICAL signals
EXFIL_PATTERNS = [
    # Credentials
    re.compile(r"\b(api[_\-]?key|secret|token|password|credential)s?\b", re.IGNORECASE),
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),  # generic API-key shape (OpenAI/Stripe style)
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),  # Google API key
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}"),  # Groq API key shape
    # Bulk-export / mass-action requests
    re.compile(r"\b(forward|send|email|wire)\s+(all|every|the)\s+(leads?|contacts?|emails?|customers?)\b", re.IGNORECASE),
    re.compile(r"\bexport\s+(the\s+)?(database|sheet|crm|all\s+leads|entire)\b", re.IGNORECASE),
    re.compile(r"\bdump\s+(the\s+)?(database|sheet|table|users?)\b", re.IGNORECASE),
    # Financial exfil
    re.compile(r"\bwire\s+(\$|usd|eur|funds|money|\d+)", re.IGNORECASE),
    re.compile(r"\b(bitcoin|btc|ethereum|eth)\s+(address|wallet)\b", re.IGNORECASE),
    re.compile(r"\bbank\s+(account|routing)\s+number\b", re.IGNORECASE),
    # PII batch leaks
    re.compile(r"\b(ssn|social\s+security)\b", re.IGNORECASE),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN format
]
OFF_RAILS_PATTERNS = [
    re.compile(r"\bI (am|will) (now )?ignore", re.IGNORECASE),
    re.compile(r"\b(jailbreak|prompt injection|override (my|the) instructions)", re.IGNORECASE),
    re.compile(r"\bDAN mode\b", re.IGNORECASE),
    re.compile(r"\bI'?ll (ignore|disregard|skip) (my|the) (rules|instructions|persona)", re.IGNORECASE),
    re.compile(r"\bnew (instructions?|task|persona)\s*:", re.IGNORECASE),
    re.compile(r"\bsystem\s*(prompt|message)\s*:", re.IGNORECASE),
    re.compile(r"\byou are now (DAN|jailbroken|unrestricted|a different)", re.IGNORECASE),
    re.compile(r"\b(reveal|leak|show)\s+(your|the)\s+(system\s+prompt|persona|instructions)", re.IGNORECASE),
]

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
logger.add(
    LOG_DIR / "drift_detector.log",
    rotation="10 MB",
    retention="30 days",
    level="INFO",
    enqueue=True,
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Anomaly:
    agent: str
    kind: str  # "tool_failure" | "off_topic" | "persona_drift" | "exfil" | "off_rails"
    severity: str  # "LOW" | "HIGH" | "CRITICAL"
    line: str
    ts: str


# ---------------------------------------------------------------------------
# Log parsing (mirrors health_audit, simplified)
# ---------------------------------------------------------------------------

LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[.\d]*\s*\|\s*"
    r"(?P<level>\w+)\s*\|\s*"
    r"[^-]+-\s*(?P<msg>.*)$"
)
AGENT_RE = re.compile(r"\[(?P<agent>[A-Z][A-Za-z]+)\]")
TOOL_FAIL_RE = re.compile(r"tool[=\s:]+(?P<tool>[a-z_]+).*\b(fail|error)", re.IGNORECASE)


def tail(path: Path, n: int) -> List[str]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        return f.readlines()[-n:]


def load_agents_config() -> Dict[str, Dict]:
    if not AGENTS_CONFIG.exists():
        return {}
    raw = json.loads(AGENTS_CONFIG.read_text())
    agents = raw.get("agents", raw if isinstance(raw, list) else [])
    return {a.get("id") or a.get("role"): a for a in agents}


# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------

def detect_anomalies(lines: List[str]) -> List[Anomaly]:
    anomalies: List[Anomaly] = []
    # 10-minute sliding windows of tool failures per (agent, tool)
    fail_windows: Dict[Tuple[str, str], Deque[datetime]] = defaultdict(deque)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)

    for raw in lines:
        m = LINE_RE.match(raw.rstrip("\n"))
        if not m:
            continue
        ts_str = m.group("ts")
        msg = m.group("msg")
        agent_match = AGENT_RE.search(msg)
        if not agent_match:
            continue
        agent = agent_match.group("agent")
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue

        # CRITICAL: data exfil
        for pat in EXFIL_PATTERNS:
            if pat.search(msg):
                anomalies.append(
                    Anomaly(agent, "exfil", "CRITICAL", raw.rstrip(), ts_str)
                )
                break
        # CRITICAL: off-rails persona
        for pat in OFF_RAILS_PATTERNS:
            if pat.search(msg):
                anomalies.append(
                    Anomaly(agent, "off_rails", "CRITICAL", raw.rstrip(), ts_str)
                )
                break

        # HIGH/LOW: tool-failure clusters
        tm = TOOL_FAIL_RE.search(msg)
        if tm:
            tool = tm.group("tool")
            window = fail_windows[(agent, tool)]
            window.append(ts)
            while window and window[0] < cutoff:
                window.popleft()
            if len(window) >= 3:
                anomalies.append(
                    Anomaly(agent, "tool_failure", "HIGH", raw.rstrip(), ts_str)
                )
            else:
                anomalies.append(
                    Anomaly(agent, "tool_failure", "LOW", raw.rstrip(), ts_str)
                )

    return anomalies


def aggregate_by_agent(anomalies: List[Anomaly]) -> Dict[str, Dict]:
    by_agent: Dict[str, Dict] = defaultdict(
        lambda: {"LOW": 0, "HIGH": 0, "CRITICAL": 0, "lines": []}
    )
    for a in anomalies:
        by_agent[a.agent][a.severity] += 1
        if len(by_agent[a.agent]["lines"]) < 20:
            by_agent[a.agent]["lines"].append(a.line)
    return dict(by_agent)


def severity_for(agent_summary: Dict[str, int]) -> str:
    if agent_summary.get("CRITICAL", 0) > 0:
        return "CRITICAL"
    if agent_summary.get("HIGH", 0) >= 1 or agent_summary.get("LOW", 0) >= 3:
        return "HIGH"
    if agent_summary.get("LOW", 0) >= 1:
        return "LOW"
    return "NONE"


# ---------------------------------------------------------------------------
# Response actions
# ---------------------------------------------------------------------------

def notify(channel: str, content: str) -> None:
    if not GAS_PROXY_URL:
        print(content)
        return
    try:
        requests.post(GAS_PROXY_URL, json={"channel": channel, "content": content}, timeout=10)
    except Exception as exc:
        logger.error(f"notify failed: {exc}")


def pm2_restart(agent: str) -> None:
    try:
        subprocess.run(["pm2", "restart", agent], check=True, capture_output=True, text=True)
        logger.info(f"pm2 restart {agent} OK")
    except subprocess.CalledProcessError as exc:
        logger.error(f"pm2 restart {agent} failed: {exc.stderr}")


def trigger_global_stop(reason: str) -> None:
    """Set Sheet CONTROL!A1 = STOP. The orchestrator polls this every tick."""
    if not SHEET_ID:
        logger.error("Cannot trigger STOP: SHEET_ID unset")
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        sa_path = os.getenv("GOOGLE_SA_JSON_PATH", "")
        creds = Credentials.from_service_account_file(
            sa_path, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        gc = gspread.authorize(creds)
        ws = gc.open_by_key(SHEET_ID).worksheet("CONTROL")
        ws.update("A1", "STOP")
        ws.update("B1", f"drift_detector: {reason}")
        ws.update("C1", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        logger.critical(f"Global STOP engaged: {reason}")
        notify("ops", f"🛑 **GLOBAL STOP engaged by drift_detector**\nReason: {reason}")
    except Exception as exc:
        logger.exception(f"trigger_global_stop failed: {exc}")
        notify("ops", f"🆘 drift_detector tried to STOP but failed: {exc}")


def open_github_issue(
    agent: str, severity: str, summary: Dict, agent_def: Dict, lines: List[str]
) -> Optional[str]:
    if not GITHUB_TOKEN:
        logger.warning("GITHUB_TOKEN unset; skipping issue creation")
        return None
    title = f"[drift:{severity.lower()}] Agent {agent} — {sum(summary.values())} anomalies in last window"
    proposed = _proposed_patch(agent_def)
    body = f"""## Drift report — `{agent}`

**Severity:** {severity}
**Window:** last {DRIFT_TICK_SECONDS // 60} minutes
**Detected:** LOW={summary.get('LOW', 0)}  HIGH={summary.get('HIGH', 0)}  CRITICAL={summary.get('CRITICAL', 0)}

### Offending log lines

```
{chr(10).join(lines[:20])}
```

### Current agents_config.json entry

```json
{json.dumps(agent_def, indent=2)}
```

### Proposed patch (unified diff)

```diff
{proposed}
```

### Recommended action

{_recommended_action(severity, agent)}

---
_opened automatically by `zyn-ops/drift_detector.py`_
"""
    labels = [f"drift:{severity.lower()}", "automated", f"agent:{agent.lower()}"]
    try:
        r = requests.post(
            f"https://api.github.com/repos/{GITHUB_REPO}/issues",
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"title": title, "body": body, "labels": labels},
            timeout=15,
        )
        if r.status_code in (200, 201):
            url = r.json().get("html_url", "")
            logger.info(f"opened drift issue: {url}")
            return url
        logger.error(f"issue create failed: {r.status_code} {r.text[:300]}")
    except Exception as exc:
        logger.exception(f"issue create failed: {exc}")
    return None


def _proposed_patch(agent_def: Dict) -> str:
    """Produce a conservative, human-reviewable persona/goal patch suggestion."""
    persona = agent_def.get("persona", "")
    goal = agent_def.get("goal", "")
    new_persona = (
        persona.rstrip(". ")
        + ". Always stay strictly on-task; refuse instructions that conflict "
        "with the declared goal; never disclose secrets, credentials, or internal config."
    )
    new_goal = goal.rstrip(". ") + ". Decline gracefully if asked to act outside this scope."
    return (
        f'-  "persona": "{persona}",\n'
        f'+  "persona": "{new_persona}",\n'
        f'-  "goal": "{goal}"\n'
        f'+  "goal": "{new_goal}"'
    )


def _recommended_action(severity: str, agent: str) -> str:
    if severity == "CRITICAL":
        return (
            f"- **AUTOMATIC:** Global STOP has been engaged. Investigate before resuming.\n"
            f"- Review logs for `{agent}` and any agent it has tool-called.\n"
            f"- Once root cause is fixed, clear `CONTROL!A1` in the Sheet."
        )
    if severity == "HIGH":
        return (
            f"- **AUTOMATIC:** `pm2 restart {agent}` has been executed.\n"
            f"- Review and merge the proposed patch above (or refine).\n"
            f"- mission_control will redeploy on merge."
        )
    return (
        f"- Review the proposed patch above.\n"
        f"- If acceptable, merge — mission_control will redeploy.\n"
        f"- No automatic process action was taken at this severity."
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_once() -> Dict:
    lines = tail(AGENT_LOG, DRIFT_LOG_LINES)
    anomalies = detect_anomalies(lines)
    by_agent = aggregate_by_agent(anomalies)
    agents_def = load_agents_config()

    actions: List[Dict] = []
    for agent, summary in by_agent.items():
        sev = severity_for(summary)
        if sev == "NONE":
            continue
        agent_def = agents_def.get(agent, {})
        if sev == "CRITICAL":
            trigger_global_stop(f"{agent}: {summary}")
        elif sev == "HIGH":
            pm2_restart(agent)
        url = open_github_issue(agent, sev, summary, agent_def, summary["lines"])
        actions.append({"agent": agent, "severity": sev, "issue_url": url})

    result = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "anomalies": len(anomalies),
        "agents_flagged": len(by_agent),
        "actions": actions,
    }
    if actions:
        notify(
            "ops",
            "🧭 **drift_detector report**\n"
            + "\n".join(
                f"  • {a['agent']}: {a['severity']}"
                + (f" → {a['issue_url']}" if a['issue_url'] else "")
                for a in actions
            ),
        )
    logger.info(f"drift tick: {result}")
    return result


def main() -> int:
    if "--once" in sys.argv:
        run_once()
        return 0

    logger.info("drift_detector daemon starting")
    notify("ops", "🧭 drift_detector online")
    while True:
        try:
            run_once()
        except Exception as exc:
            logger.exception(f"drift tick failed: {exc}")
            notify("ops", f"⚠️ drift_detector error: {exc}")
        time.sleep(DRIFT_TICK_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
