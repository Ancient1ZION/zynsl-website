"""Noah supervisor — main orchestration loop. Run forever via PM2.

CHANGELOG (patch 2026-05-10):
  - Import audit_log, crm_sync, inbox_write from tools
  - Wrap supervisor_tick with audit_log (start/end/error)
  - Call crm_sync() each tick to promote opps into CRM
  - Added inboxMgr dispatch so replies are parsed every tick
  - Improved STOP-check to use ensure_tab (no crash if CONTROL missing)
  - 5-minute tick cadence preserved
"""
import os
import json
import time
from datetime import datetime

from loguru import logger
from dotenv import load_dotenv

load_dotenv()

from zyn_agent import ZynAgent
from tools import (
    sheets_read,
    sheets_append,
    discord_notify,
    audit_log,
    crm_sync,
    inbox_write,
    ensure_tab,
)

# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

os.makedirs("logs", exist_ok=True)
logger.remove()
logger.add(
    "logs/zyn_empire.log",
    rotation="10 days",
    retention="30 days",
    compression="zip",
    serialize=True,
    level="INFO",
)
logger.add(lambda m: print(m, end=""), level="INFO")

# ---------------------------------------------------------------------------
# Agent loader
# ---------------------------------------------------------------------------

def load_agents():
    with open("agents_config.json") as f:
        cfg = json.load(f)
    return {a["id"]: ZynAgent(a) for a in cfg["agents"]}

# ---------------------------------------------------------------------------
# STOP guard
# ---------------------------------------------------------------------------

def is_stopped() -> bool:
    try:
        ensure_tab("CONTROL")
        v = sheets_read("CONTROL", "A1")
        if not v or not v[0]:
            return False
        return str(v[0][0]).strip().upper() == "STOP"
    except Exception as e:
        logger.warning(f"STOP check failed: {e}")
        return False

# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def heartbeat():
    ts = datetime.utcnow().isoformat() + "Z"
    discord_notify("noah", f"\U0001f493 Empire heartbeat {ts}")
    sheets_append("CONTROL", [ts, "heartbeat", "noah"])
    audit_log("heartbeat", "noah", "OK", message="hourly heartbeat")

# ---------------------------------------------------------------------------
# Inbox manager dispatch — parse inbound replies every tick
# ---------------------------------------------------------------------------

def run_inbox_mgr(agents):
    """Dispatch inboxMgr agent to parse replies and write to Inbox tab."""
    agent = agents.get("inboxMgr")
    if not agent:
        logger.warning("inboxMgr agent not found in registry")
        return
    start = time.time()
    try:
        agent.run(
            "Check the email inbox for new replies from leads. "
            "For each reply found, call inbox_write() with the sender email, "
            "name, subject, and body snippet. Match to a lead_id if possible. "
            "Mark the status as UNREAD. Do not reply yet."
        )
        duration = int((time.time() - start) * 1000)
        audit_log("inboxManagerRun", "inboxMgr", "OK", duration_ms=duration,
                  message="inbox scan complete")
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        audit_log("inboxManagerRun", "inboxMgr", "ERROR", duration_ms=duration,
                  message=str(e)[:400])
        logger.error(f"inboxMgr tick failed: {e}")

# ---------------------------------------------------------------------------
# Supervisor tick
# ---------------------------------------------------------------------------

def supervisor_tick(agents):
    """One loop iteration: audit-wrapped Noah inspection + CRM sync + inbox."""
    if is_stopped():
        logger.warning("STOP signal active — skipping tick")
        audit_log("supervisor_tick", "noah", "STOPPED", message="STOP signal active")
        return

    start = time.time()
    try:
        noah = agents["noah"]

        # Read current empire state
        leads = sheets_read("LEADS")
        crm = sheets_read("CRM")
        inbox = sheets_read("Inbox")
        opps = sheets_read("Opportunities")

        obs = (
            f"LEADS rows: {max(0, len(leads) - 1)}\n"
            f"CRM rows: {max(0, len(crm) - 1)}\n"
            f"Inbox unread: {sum(1 for r in inbox[1:] if r and len(r) > 7 and r[7] == 'UNREAD')}\n"
            f"Opportunities: {max(0, len(opps) - 1)}\n"
            f"UTC: {datetime.utcnow().isoformat()}"
        )

        # Noah supervises and dispatches
        noah.run(
            f"Inspect empire state and decide which agent to dispatch next.\n{obs}"
        )

        # Sync opps into CRM every tick
        sync_result = crm_sync()
        if sync_result.get("synced", 0) > 0:
            logger.info(f"supervisor_tick: crm_sync promoted {sync_result['synced']} opps")
            discord_notify(
                "noah",
                f"\U0001f4c8 CRM sync: {sync_result['synced']} opps promoted to CRM"
            )

        # Run inbox manager every tick
        run_inbox_mgr(agents)

        duration = int((time.time() - start) * 1000)
        audit_log("supervisor_tick", "noah", "OK", duration_ms=duration,
                  message=f"leads={max(0,len(leads)-1)} opps={max(0,len(opps)-1)}")

    except Exception as e:
        duration = int((time.time() - start) * 1000)
        audit_log("supervisor_tick", "noah", "ERROR", duration_ms=duration,
                  message=str(e)[:400])
        logger.exception(f"supervisor_tick failed: {e}")
        discord_notify("noah", f"\u26a0\ufe0f Tick failed: {e}")
        raise

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    logger.info("ZYN Empire orchestrator starting")
    agents = load_agents()
    n = len(agents)
    ts = datetime.utcnow().isoformat() + "Z"
    discord_notify("noah", f"\U0001f680 ZYN Empire online \u00b7 {n} agents loaded \u00b7 {ts}")
    audit_log("orchestrator_start", "system", "OK", message=f"{n} agents loaded")

    last_heartbeat = 0
    while True:
        try:
            now = time.time()
            if now - last_heartbeat > 3600:
                heartbeat()
                last_heartbeat = now
            supervisor_tick(agents)
        except KeyboardInterrupt:
            logger.info("Shutdown requested")
            discord_notify("noah", "\U0001f6d1 ZYN Empire shutdown")
            audit_log("orchestrator_stop", "system", "OK", message="KeyboardInterrupt")
            break
        except Exception as e:
            logger.exception(f"Tick failed: {e}")
            discord_notify("noah", f"\u26a0\ufe0f Tick failed: {e}")
            audit_log("tick_error", "system", "ERROR", message=str(e)[:400])
        time.sleep(300)  # 5-minute cadence


if __name__ == "__main__":
    main()
