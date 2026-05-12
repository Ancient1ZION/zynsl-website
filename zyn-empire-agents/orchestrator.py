"""Noah supervisor — main orchestration loop. Run forever via PM2.

CHANGELOG (patch 2026-05-12):
  - Import update_lead_stage, write_lead, get_leads from tools
  - run_inbox_mgr: after inbox scan, call advance_stages() to write LEADS status column (fixes stage-write bug)
  - advance_stages(): reads Inbox UNREAD rows, calls update_lead_stage() for each matched lead
  - run_consulting_followup(): dispatches consultingFollowup agent every 6h
  - run_autonomous_followup(): dispatches autonomousFollowup agent every 6h
  - supervisor_tick: wires both followup agents into the tick loop
  - run_scraper_write_lead(): dispatches scraper to flush CONSULTING_50 into LEADS via write_lead()

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
    update_lead_stage,
    write_lead,
    ensure_tab,
    get_leads,
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
# Stage advancement — write LEADS status column from Inbox tab
# ---------------------------------------------------------------------------

# Intent-to-stage mapping
INTENT_STAGE_MAP = {
    "INTERESTED": "REPLIED",
    "WARM":        "WARM",
    "HOT":         "HOT",
    "OBJECTION":   "REPLIED",
    "OPT_OUT":     "UNSUB",
    "BOUNCED":     "BAD_EMAIL",
    "UNREAD":      "REPLIED",  # fallback: any reply advances to REPLIED
}

def advance_stages(agents):
    """
    Read UNREAD rows from Inbox tab, advance matched LEADS row status,
    mark Inbox rows as READ. Dispatches inboxMgr's update_lead_stage tool
    by calling it directly via the tools layer.
    """
    try:
        inbox_rows = sheets_read("Inbox")
        if not inbox_rows or len(inbox_rows) < 2:
            return
        headers = [h.lower().strip() for h in inbox_rows[0]]
        # Expected columns: timestamp, from_email, from_name, subject,
        #                   body_snippet, matched_lead_id, division, status
        try:
            email_col  = headers.index("from_email")
            status_col = headers.index("status")
            intent_col = headers.index("intent") if "intent" in headers else -1
        except ValueError:
            logger.warning("advance_stages: Inbox tab missing expected headers")
            return

        inbox_ws = None
        try:
            from tools import _open_tab
            inbox_ws = _open_tab("Inbox")
        except Exception:
            pass

        advanced = 0
        for i, row in enumerate(inbox_rows[1:], start=2):
            padded = row + [""] * max(0, len(headers) - len(row))
            if padded[status_col].upper() != "UNREAD":
                continue

            from_email = padded[email_col].strip()
            if not from_email:
                continue

            # Determine target stage from intent column if available
            intent = padded[intent_col].strip().upper() if intent_col >= 0 and padded[intent_col] else "UNREAD"
            target_stage = INTENT_STAGE_MAP.get(intent, "REPLIED")

            result = update_lead_stage(from_email, target_stage)
            if result.get("ok"):
                advanced += 1
                # Mark Inbox row as READ
                if inbox_ws:
                    try:
                        col_letter = chr(ord("A") + status_col)
                        inbox_ws.update(f"{col_letter}{i}", [["READ"]])
                    except Exception as e:
                        logger.warning(f"advance_stages: could not mark row {i} READ: {e}")
                # Alert Malik on HOT
                if target_stage == "HOT":
                    discord_notify(
                        "malik",
                        f"🔥 HOT lead: {from_email} just replied — ready for close sequence"
                    )
            else:
                logger.warning(f"advance_stages: no LEADS match for {from_email}: {result.get('error')}")

        if advanced:
            logger.info(f"advance_stages: advanced {advanced} lead(s)")
            audit_log("advance_stages", "inboxMgr", "OK", message=f"advanced={advanced}")

    except Exception as e:
        logger.error(f"advance_stages: {e}")
        audit_log("advance_stages", "inboxMgr", "ERROR", message=str(e)[:400])


# ---------------------------------------------------------------------------
# Consulting followup dispatch
# ---------------------------------------------------------------------------

_last_consulting_followup = 0.0
_last_autonomous_followup = 0.0
FOLLOWUP_INTERVAL = 6 * 3600  # 6 hours


def run_consulting_followup(agents):
    """Dispatch consultingFollowup agent every 6 hours."""
    global _last_consulting_followup
    now = time.time()
    if now - _last_consulting_followup < FOLLOWUP_INTERVAL:
        return
    _last_consulting_followup = now

    agent = agents.get("consultingFollowup")
    if not agent:
        logger.warning("consultingFollowup agent not in registry — skipping")
        return

    start = time.time()
    try:
        contacted_leads = get_leads(status_filter="CONTACTED")
        consulting_leads = [l for l in contacted_leads if l.get("division", "").upper() == "CONSULTING"]
        if not consulting_leads:
            logger.info("run_consulting_followup: no CONTACTED consulting leads to follow up")
            return

        agent.run(
            f"You have {len(consulting_leads)} CONTACTED consulting leads awaiting a follow-up. "
            "For each lead that has not replied within 3 days, send a personalized second-touch email "
            "referencing their company and the original pitch angle. Update the lead status to CONTACTED "
            "after sending. Gate any message containing contract/proposal/MSA/SOW language into the "
            "approval queue instead of sending directly."
        )
        duration = int((time.time() - start) * 1000)
        audit_log("consultingFollowupRun", "consultingFollowup", "OK", duration_ms=duration,
                  message=f"leads_eligible={len(consulting_leads)}")
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        audit_log("consultingFollowupRun", "consultingFollowup", "ERROR", duration_ms=duration,
                  message=str(e)[:400])
        logger.error(f"run_consulting_followup: {e}")


# ---------------------------------------------------------------------------
# Autonomous followup dispatch
# ---------------------------------------------------------------------------

def run_autonomous_followup(agents):
    """Dispatch autonomousFollowup agent every 6 hours."""
    global _last_autonomous_followup
    now = time.time()
    if now - _last_autonomous_followup < FOLLOWUP_INTERVAL:
        return
    _last_autonomous_followup = now

    agent = agents.get("autonomousFollowup")
    if not agent:
        logger.warning("autonomousFollowup agent not in registry — skipping")
        return

    start = time.time()
    try:
        contacted_leads = get_leads(status_filter="CONTACTED")
        autonomous_leads = [l for l in contacted_leads if l.get("division", "").upper() == "AUTONOMOUS"]
        if not autonomous_leads:
            logger.info("run_autonomous_followup: no CONTACTED autonomous leads to follow up")
            return

        agent.run(
            f"You have {len(autonomous_leads)} CONTACTED autonomous-division leads awaiting a follow-up. "
            "For each lead that has not replied within 3 days, send a personalized second-touch email "
            "focused on operations ROI, time saved, and AI deployment results. Update the lead status to "
            "CONTACTED after sending. Gate any message containing contract/proposal/SOW/MSA language "
            "into the approval queue instead of sending directly."
        )
        duration = int((time.time() - start) * 1000)
        audit_log("autonomousFollowupRun", "autonomousFollowup", "OK", duration_ms=duration,
                  message=f"leads_eligible={len(autonomous_leads)}")
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        audit_log("autonomousFollowupRun", "autonomousFollowup", "ERROR", duration_ms=duration,
                  message=str(e)[:400])
        logger.error(f"run_autonomous_followup: {e}")


# ---------------------------------------------------------------------------
# Scraper write_lead flush — moves CONSULTING_50 staged leads into LEADS tab
# ---------------------------------------------------------------------------

_last_scraper_flush = 0.0
SCRAPER_FLUSH_INTERVAL = 3600  # 1 hour


def run_scraper_write_lead(agents):
    """Dispatch scraper agent to flush CONSULTING_50 staging into live LEADS."""
    global _last_scraper_flush
    now = time.time()
    if now - _last_scraper_flush < SCRAPER_FLUSH_INTERVAL:
        return
    _last_scraper_flush = now

    agent = agents.get("scraper")
    if not agent:
        logger.warning("scraper agent not in registry — skipping write_lead flush")
        return

    start = time.time()
    try:
        agent.run(
            "Read all rows from the CONSULTING_50 staging sheet. "
            "For each row, call write_lead() with division=CONSULTING and the lead's name, company, "
            "and email. write_lead() will deduplicate automatically — do not pre-filter. "
            "After flushing, log how many leads were inserted vs skipped as duplicates."
        )
        duration = int((time.time() - start) * 1000)
        audit_log("scraperWriteLeadFlush", "scraper", "OK", duration_ms=duration,
                  message="CONSULTING_50 flush complete")
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        audit_log("scraperWriteLeadFlush", "scraper", "ERROR", duration_ms=duration,
                  message=str(e)[:400])
        logger.error(f"run_scraper_write_lead: {e}")


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

        # Run inbox manager every tick + advance LEADS stages
        run_inbox_mgr(agents)
        advance_stages(agents)

        # Flush CONSULTING_50 into LEADS every hour
        run_scraper_write_lead(agents)

        # Dispatch followup agents every 6 hours
        run_consulting_followup(agents)
        run_autonomous_followup(agents)

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
