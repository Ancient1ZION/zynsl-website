#!/usr/bin/env python3
"""ZYN Empire — mission_control.py

Git-watch deployment daemon. Runs on the GCP VM under PM2.

Every MISSION_TICK_SECONDS:
  1. git fetch
  2. compare HEAD vs origin/main
  3. if zyn-empire-agents/** changed:
       - git pull --ff-only
       - pm2 restart all (if agents_config.json changed) OR pm2 reload all
       - notify Discord via GAS proxy
  4. write heartbeat row to Sheet HEARTBEAT tab

Also exposes a /healthz HTTP endpoint on MISSION_HEALTH_PORT for external probes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import List

import requests
from dotenv import load_dotenv
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

MISSION_TICK_SECONDS = int(os.getenv("MISSION_TICK_SECONDS", "60"))
MISSION_HEALTH_PORT = int(os.getenv("MISSION_HEALTH_PORT", "9090"))
GAS_PROXY_URL = os.getenv("GAS_PROXY_URL", "").strip()
SHEET_ID = os.getenv("SHEET_ID", "").strip()
AGENTS_DIR = ROOT / "zyn-empire-agents"

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
logger.add(
    LOG_DIR / "mission_control.log",
    rotation="10 MB",
    retention="30 days",
    level="INFO",
    enqueue=True,
)


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _run(cmd: List[str], cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess:
    logger.debug(f"$ {' '.join(cmd)}")
    return subprocess.run(
        cmd, cwd=cwd, check=check, capture_output=True, text=True
    )


def git_fetch() -> None:
    _run(["git", "fetch", "--prune", "origin", "main"])


def git_local_sha() -> str:
    return _run(["git", "rev-parse", "HEAD"]).stdout.strip()


def git_remote_sha() -> str:
    return _run(["git", "rev-parse", "origin/main"]).stdout.strip()


def changed_paths(local: str, remote: str) -> List[str]:
    if local == remote:
        return []
    res = _run(["git", "diff", "--name-only", local, remote])
    return [p for p in res.stdout.splitlines() if p.strip()]


def git_pull() -> None:
    _run(["git", "pull", "--ff-only", "origin", "main"])


def pm2_reload_all() -> None:
    _run(["pm2", "reload", "all", "--update-env"])


def pm2_restart_all() -> None:
    _run(["pm2", "restart", "all", "--update-env"])


# ---------------------------------------------------------------------------
# Discord notification (via GAS proxy — never raw webhook)
# ---------------------------------------------------------------------------

def notify(channel: str, content: str) -> None:
    if not GAS_PROXY_URL:
        logger.warning("GAS_PROXY_URL not set; skipping Discord notification")
        return
    try:
        requests.post(
            GAS_PROXY_URL,
            json={"channel": channel, "content": content},
            timeout=10,
        )
    except Exception as exc:
        logger.error(f"Discord notify failed: {exc}")


# ---------------------------------------------------------------------------
# Heartbeat to Google Sheet
# ---------------------------------------------------------------------------

def _sheet_client():
    import gspread
    from google.oauth2.service_account import Credentials

    sa_path = os.getenv("GOOGLE_SA_JSON_PATH", "")
    if not sa_path or not Path(sa_path).exists():
        return None
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(sa_path, scopes=scopes)
    return gspread.authorize(creds)


def write_heartbeat(local_sha: str, action: str) -> None:
    if not SHEET_ID:
        return
    try:
        gc = _sheet_client()
        if gc is None:
            return
        sh = gc.open_by_key(SHEET_ID)
        try:
            ws = sh.worksheet("HEARTBEAT")
        except Exception:
            ws = sh.add_worksheet(title="HEARTBEAT", rows=2000, cols=4)
            ws.update("A1:D1", [["timestamp_utc", "component", "sha", "action"]])
        ws.append_row(
            [
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "mission_control",
                local_sha[:7],
                action,
            ],
            value_input_option="USER_ENTERED",
        )
    except Exception as exc:
        logger.warning(f"Sheet heartbeat failed: {exc}")


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/healthz":
            payload = {
                "ok": True,
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "sha": git_local_sha(),
            }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):  # noqa: A002
        return  # silence default stderr logging


def _start_health_server() -> None:
    try:
        srv = HTTPServer(("0.0.0.0", MISSION_HEALTH_PORT), _HealthHandler)
        Thread(target=srv.serve_forever, daemon=True).start()
        logger.info(f"Health endpoint listening on :{MISSION_HEALTH_PORT}/healthz")
    except Exception as exc:
        logger.warning(f"Could not start health endpoint: {exc}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def tick() -> None:
    git_fetch()
    local = git_local_sha()
    remote = git_remote_sha()
    if local == remote:
        write_heartbeat(local, "noop")
        return

    paths = changed_paths(local, remote)
    agent_paths = [p for p in paths if p.startswith("zyn-empire-agents/")]
    ops_paths = [p for p in paths if p.startswith("zyn-ops/")]

    if not agent_paths and not ops_paths:
        logger.info(f"Remote ahead but only docs/dashboard changed: {paths}")
        git_pull()
        write_heartbeat(git_local_sha(), "pull-docs")
        return

    logger.info(f"Pulling {len(paths)} changed paths: {paths}")
    git_pull()
    new_sha = git_local_sha()

    if any("agents_config.json" in p for p in agent_paths):
        logger.info("agents_config.json changed → full restart")
        pm2_restart_all()
        action = "restart"
    elif agent_paths:
        logger.info("agent code changed → reload")
        pm2_reload_all()
        action = "reload"
    else:
        action = "ops-only"

    write_heartbeat(new_sha, action)
    notify(
        "ops",
        f"🚀 mission_control: {action} @ {new_sha[:7]}\n"
        + "\n".join(f"  • {p}" for p in paths[:10]),
    )


def main() -> int:
    logger.info("mission_control starting")
    if not (ROOT / ".git").exists():
        logger.error(f"{ROOT} is not a git checkout; aborting")
        return 2

    _start_health_server()
    notify("ops", "🛰  mission_control online")

    while True:
        try:
            tick()
        except subprocess.CalledProcessError as exc:
            logger.error(
                f"command failed: {exc.cmd} (exit {exc.returncode}) "
                f"stdout={exc.stdout!r} stderr={exc.stderr!r}"
            )
            notify("ops", f"⚠️ mission_control error: {exc.cmd}: {exc.stderr[:200]}")
        except Exception as exc:
            logger.exception(f"tick failed: {exc}")
            notify("ops", f"⚠️ mission_control unexpected error: {exc}")
        time.sleep(MISSION_TICK_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
