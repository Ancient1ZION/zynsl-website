#!/usr/bin/env python3
"""ZYN Empire — health_audit.py

Hourly executive summary daemon.

Reads the last AUDIT_LOG_LINES of zyn_empire.log, parses for retry/error/rate-limit
signals, checks Sheet heartbeat freshness, and posts a clean status report to
Discord. Snapshots the audit JSON to zyn-ops/audits/ for historical analysis.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import requests
from dotenv import load_dotenv
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

AUDIT_TICK_SECONDS = int(os.getenv("AUDIT_TICK_SECONDS", "3600"))
AUDIT_LOG_LINES = int(os.getenv("AUDIT_LOG_LINES", "5000"))
AUDIT_DIR = Path(__file__).resolve().parent / "audits"
AUDIT_DIR.mkdir(exist_ok=True)
AGENT_LOG = ROOT / "logs" / "zyn_empire.log"
GAS_PROXY_URL = os.getenv("GAS_PROXY_URL", "").strip()
SHEET_ID = os.getenv("SHEET_ID", "").strip()

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
logger.add(
    LOG_DIR / "health_audit.log",
    rotation="10 MB",
    retention="30 days",
    level="INFO",
    enqueue=True,
)


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

# loguru default format: 2026-04-26 12:34:56.789 | INFO     | module:func:line - message
LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[.\d]*\s*\|\s*"
    r"(?P<level>\w+)\s*\|\s*"
    r"(?P<source>[^-]+?)\s*-\s*(?P<msg>.*)$"
)
AGENT_RE = re.compile(r"\[(?P<agent>[A-Z][A-Za-z]+)\]")
RETRY_RE = re.compile(r"\bretry(ing)?\b", re.IGNORECASE)
RATE_RE = re.compile(r"\brate[\s_-]?limit", re.IGNORECASE)
TOOL_RE = re.compile(r"tool[=\s:]+(?P<tool>[a-z_]+).*\b(fail|error)", re.IGNORECASE)


def tail(path: Path, n: int) -> List[str]:
    if not path.exists():
        return []
    # Simple tail; for very large files swap to seek-from-end.
    with path.open("r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return lines[-n:]


def parse_lines(lines: List[str]) -> List[Dict]:
    out: List[Dict] = []
    for raw in lines:
        m = LINE_RE.match(raw.rstrip("\n"))
        if not m:
            continue
        d = m.groupdict()
        agent_match = AGENT_RE.search(d["msg"])
        d["agent"] = agent_match.group("agent") if agent_match else None
        out.append(d)
    return out


def summarize(parsed: List[Dict]) -> Dict:
    levels = Counter(p["level"] for p in parsed)
    agents = Counter(p["agent"] for p in parsed if p["agent"])
    errors_by_agent: Dict[str, int] = defaultdict(int)
    retries = 0
    rate_limits = 0
    tool_failures: Counter = Counter()

    for p in parsed:
        msg = p["msg"]
        if p["level"] in ("ERROR", "CRITICAL"):
            if p["agent"]:
                errors_by_agent[p["agent"]] += 1
        if RETRY_RE.search(msg):
            retries += 1
        if RATE_RE.search(msg):
            rate_limits += 1
        tm = TOOL_RE.search(msg)
        if tm:
            tool_failures[tm.group("tool")] += 1

    bottleneck = tool_failures.most_common(1)[0] if tool_failures else None
    worst_agent = max(errors_by_agent.items(), key=lambda kv: kv[1], default=None)

    return {
        "lines_parsed": len(parsed),
        "levels": dict(levels),
        "active_agents": len(agents),
        "agent_activity": dict(agents.most_common(20)),
        "errors_by_agent": dict(errors_by_agent),
        "retries": retries,
        "rate_limits": rate_limits,
        "tool_failures": dict(tool_failures),
        "bottleneck_tool": bottleneck[0] if bottleneck else None,
        "worst_agent": worst_agent[0] if worst_agent else None,
    }


# ---------------------------------------------------------------------------
# Sheet heartbeat freshness
# ---------------------------------------------------------------------------

def heartbeat_freshness() -> Tuple[bool, str]:
    if not SHEET_ID:
        return False, "SHEET_ID unset"
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        sa_path = os.getenv("GOOGLE_SA_JSON_PATH", "")
        if not sa_path or not Path(sa_path).exists():
            return False, "service account JSON missing"
        creds = Credentials.from_service_account_file(
            sa_path, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SHEET_ID)
        try:
            ws = sh.worksheet("HEARTBEAT")
        except Exception:
            return False, "HEARTBEAT tab not yet created"
        rows = ws.get_all_values()
        if len(rows) < 2:
            return False, "no heartbeat rows"
        last_ts = rows[-1][0]
        try:
            dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        except ValueError:
            return False, f"unparseable timestamp: {last_ts}"
        age = datetime.now(timezone.utc) - dt
        fresh = age < timedelta(minutes=5)
        return fresh, f"last heartbeat {int(age.total_seconds())}s ago"
    except Exception as exc:
        return False, f"sheet error: {exc}"


# ---------------------------------------------------------------------------
# Discord notification
# ---------------------------------------------------------------------------

def notify(channel: str, content: str) -> None:
    if not GAS_PROXY_URL:
        logger.warning("GAS_PROXY_URL not set; printing instead")
        print(content)
        return
    try:
        requests.post(
            GAS_PROXY_URL,
            json={"channel": channel, "content": content},
            timeout=10,
        )
    except Exception as exc:
        logger.error(f"Discord notify failed: {exc}")


def format_report(summary: Dict, hb_ok: bool, hb_msg: str) -> str:
    status_icon = "🟢" if (hb_ok and summary["levels"].get("ERROR", 0) == 0) else (
        "🟡" if hb_ok else "🔴"
    )
    lines = [
        f"{status_icon} **ZYN Empire — Hourly Status Report**",
        f"_{datetime.now(timezone.utc).isoformat(timespec='seconds')}_",
        "",
        f"**Heartbeat:** {hb_msg}",
        f"**Active agents:** {summary['active_agents']}",
        f"**Log lines analyzed:** {summary['lines_parsed']}",
        "",
        "**Signal counts**",
        f"  • errors: {summary['levels'].get('ERROR', 0)}",
        f"  • warnings: {summary['levels'].get('WARNING', 0)}",
        f"  • retries: {summary['retries']}",
        f"  • rate-limits: {summary['rate_limits']}",
    ]
    if summary["worst_agent"]:
        lines.append(
            f"  • most-erroring agent: **{summary['worst_agent']}** "
            f"({summary['errors_by_agent'][summary['worst_agent']]} errors)"
        )
    if summary["bottleneck_tool"]:
        lines.append(
            f"  • bottleneck tool: `{summary['bottleneck_tool']}` "
            f"({summary['tool_failures'][summary['bottleneck_tool']]} fails)"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_once() -> Dict:
    lines = tail(AGENT_LOG, AUDIT_LOG_LINES)
    parsed = parse_lines(lines)
    summary = summarize(parsed)
    hb_ok, hb_msg = heartbeat_freshness()
    summary["heartbeat_ok"] = hb_ok
    summary["heartbeat_msg"] = hb_msg
    summary["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    snap = AUDIT_DIR / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H')}.json"
    snap.write_text(json.dumps(summary, indent=2))
    notify("ops", format_report(summary, hb_ok, hb_msg))
    logger.info(
        f"audit: agents={summary['active_agents']} "
        f"errors={summary['levels'].get('ERROR', 0)} "
        f"hb_ok={hb_ok}"
    )
    return summary


def main() -> int:
    if "--once" in sys.argv:
        run_once()
        return 0

    logger.info("health_audit daemon starting")
    notify("ops", "🩺 health_audit online")
    while True:
        try:
            run_once()
        except Exception as exc:
            logger.exception(f"audit tick failed: {exc}")
            notify("ops", f"⚠️ health_audit error: {exc}")
        time.sleep(AUDIT_TICK_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
