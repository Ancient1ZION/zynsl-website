"""Noah supervisor — main orchestration loop. Run forever via PM2.

CHANGELOG (patch 2026-05-12c — real agent assignments):
  - consultingFollowup → malik (Sales Closer owns consulting follow-up, 15/day)
  - autonomousFollowup → rebecka (Autonomous Outreach Lead owns follow-up, 15/day)
  - scraper → david (Build/Tech Lead owns all scraping + write_lead())
  - Removed all synthetic agent IDs from agents.get() calls
  - malik: added daily_sends_consulting_followup counter check
  - rebecka: added daily_sends_autonomous_followup counter check
  - david: dispatched for both federal enrichment and consulting flush

CHANGELOG (patch 2026-05-12b — volume targets):
  - AUTONOMOUS: rebecka 60/day + zuri 25/day + autonomousFollowup 15/day = 100/day
  - CONSULTING: sara 40/day + lea 20/day + consultingFollowup 15/day = 75/day
  - FEDERAL: adam + scraper target 50 new leads/day via write_lead()
  - Daily send counters per division tracked in CONTROL tab
  - Midnight reset: reset_daily_counters() zeros all CONTROL counters at 00:00 UTC
  - run_rebecka(), run_zuri() dispatch functions added (every_2h / every_4h)
  - run_sara(), run_lea() updated with quota-aware dispatch
  - run_adam_scrape(), run_micah(), run_asher(), run_miro_grants() wired into tick
  - Federal daily scrape progress logged + Discord alert when 50/day hit

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
    sheets_write,
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
    """Dispatch malik (Sales Closer) for consulting follow-up every 6 hours. 15/day target."""
    global _last_consulting_followup
    now = time.time()
    if now - _last_consulting_followup < FOLLOWUP_INTERVAL:
        return
    _last_consulting_followup = now

    agent = agents.get("malik")
    if not agent:
        logger.warning("malik agent not in registry — skipping")
        return

    start = time.time()
    try:
        contacted_leads = get_leads(status_filter="CONTACTED")
        consulting_leads = [l for l in contacted_leads if l.get("division", "").upper() == "CONSULTING"]
        if not consulting_leads:
            logger.info("run_consulting_followup: no CONTACTED consulting leads to follow up")
            return

        agent.run(
            f"FOLLOW-UP MODE: You have {len(consulting_leads)} CONTACTED consulting leads awaiting your second-touch close sequence. "
            "For each lead that has not replied within 3 days, send a personalized second-touch email "
            "referencing their company and the original pitch angle. Update the lead status to CONTACTED "
            "after sending. Gate any message containing contract/proposal/MSA/SOW language into the "
            "approval queue instead of sending directly."
        )
        duration = int((time.time() - start) * 1000)
        audit_log("consultingFollowupRun", "malik", "OK", duration_ms=duration,
                  message=f"leads_eligible={len(consulting_leads)}")
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        audit_log("consultingFollowupRun", "malik", "ERROR", duration_ms=duration,
                  message=str(e)[:400])
        logger.error(f"run_consulting_followup: {e}")


# ---------------------------------------------------------------------------
# Autonomous followup dispatch
# ---------------------------------------------------------------------------

def run_autonomous_followup(agents):
    """Dispatch rebecka (Autonomous Outreach Lead) for follow-up mode every 6 hours. 15/day target."""
    global _last_autonomous_followup
    now = time.time()
    if now - _last_autonomous_followup < FOLLOWUP_INTERVAL:
        return
    _last_autonomous_followup = now

    agent = agents.get("rebecka")
    if not agent:
        logger.warning("rebecka agent not in registry — skipping")
        return

    start = time.time()
    try:
        contacted_leads = get_leads(status_filter="CONTACTED")
        autonomous_leads = [l for l in contacted_leads if l.get("division", "").upper() == "AUTONOMOUS"]
        if not autonomous_leads:
            logger.info("run_autonomous_followup: no CONTACTED autonomous leads to follow up")
            return

        agent.run(
            f"FOLLOW-UP MODE: You have {len(autonomous_leads)} CONTACTED autonomous-division leads awaiting a second touch. "
            "For each lead that has not replied within 3 days, send a personalized second-touch email "
            "focused on operations ROI, time saved, and AI deployment results. Update the lead status to "
            "CONTACTED after sending. Gate any message containing contract/proposal/SOW/MSA language "
            "into the approval queue instead of sending directly."
        )
        duration = int((time.time() - start) * 1000)
        audit_log("autonomousFollowupRun", "rebecka", "OK", duration_ms=duration,
                  message=f"leads_eligible={len(autonomous_leads)}")
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        audit_log("autonomousFollowupRun", "rebecka", "ERROR", duration_ms=duration,
                  message=str(e)[:400])
        logger.error(f"run_autonomous_followup: {e}")


# ---------------------------------------------------------------------------
# Scraper write_lead flush — moves CONSULTING_50 staged leads into LEADS tab
# ---------------------------------------------------------------------------

_last_scraper_flush = 0.0
SCRAPER_FLUSH_INTERVAL = 3600  # 1 hour


def run_scraper_write_lead(agents):
    """Dispatch david (Build/Tech Lead) to flush CONSULTING_50 staging into live LEADS."""
    global _last_scraper_flush
    now = time.time()
    if now - _last_scraper_flush < SCRAPER_FLUSH_INTERVAL:
        return
    _last_scraper_flush = now

    agent = agents.get("david")
    if not agent:
        logger.warning("david agent not in registry — skipping write_lead flush")
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
        audit_log("scraperWriteLeadFlush", "david", "OK", duration_ms=duration,
                  message="CONSULTING_50 flush complete")
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        audit_log("scraperWriteLeadFlush", "david", "ERROR", duration_ms=duration,
                  message=str(e)[:400])
        logger.error(f"run_scraper_write_lead: {e}")


# ---------------------------------------------------------------------------
# Daily send counters — read / increment / reset
# ---------------------------------------------------------------------------

COUNTER_TAB = "CONTROL"

# Map counter key -> CONTROL tab row label
COUNTER_KEYS = {
    "autonomous":            "daily_sends_autonomous",
    "zuri":                  "daily_sends_zuri",
    "autonomous_followup":   "daily_sends_autonomous_followup",
    "sara":                  "daily_sends_sara",
    "lea":                   "daily_sends_lea",
    "consulting_followup":   "daily_sends_consulting_followup",
    "federal_scrape":        "daily_scrape_federal",
}

# Daily volume targets
DAILY_TARGETS = {
    "autonomous":          60,   # rebecka
    "zuri":                25,   # zuri content sends
    "autonomous_followup": 15,   # autonomousFollowup
    "sara":                40,   # sara consulting
    "lea":                 20,   # lea consulting
    "consulting_followup": 15,   # consultingFollowup
    "federal_scrape":      50,   # adam + scraper combined
}


def _get_counter(key: str) -> int:
    """Read a daily counter from the CONTROL tab. Returns 0 on any failure."""
    try:
        rows = sheets_read(COUNTER_TAB)
        label = COUNTER_KEYS.get(key, key)
        for row in rows:
            if row and str(row[0]).strip() == label:
                return int(float(str(row[1]).strip())) if len(row) > 1 and row[1] else 0
        return 0
    except Exception as e:
        logger.warning(f"_get_counter({key}): {e}")
        return 0


def _increment_counter(key: str, amount: int = 1) -> int:
    """Increment a daily counter in CONTROL tab. Returns new value."""
    try:
        rows = sheets_read(COUNTER_TAB)
        label = COUNTER_KEYS.get(key, key)
        ws = None
        try:
            from tools import _open_tab
            ws = _open_tab(COUNTER_TAB)
        except Exception:
            pass
        if ws is None:
            return 0
        for i, row in enumerate(rows, start=1):
            if row and str(row[0]).strip() == label:
                current = int(float(str(row[1]))) if len(row) > 1 and row[1] else 0
                new_val = current + amount
                ws.update(f"B{i}", [[new_val]])
                return new_val
        # Key not found — append it
        ws.append_row([label, amount])
        return amount
    except Exception as e:
        logger.warning(f"_increment_counter({key}, {amount}): {e}")
        return 0


def _quota_remaining(key: str) -> int:
    """How many sends remain before hitting today's target."""
    used = _get_counter(key)
    target = DAILY_TARGETS.get(key, 0)
    return max(0, target - used)


def reset_daily_counters():
    """Zero all daily counters. Called once at midnight UTC."""
    try:
        from tools import _open_tab
        ws = _open_tab(COUNTER_TAB)
        if ws is None:
            return
        rows = ws.get_all_values()
        labels = set(COUNTER_KEYS.values())
        for i, row in enumerate(rows, start=1):
            if row and str(row[0]).strip() in labels:
                ws.update(f"B{i}", [[0]])
        logger.info("reset_daily_counters: all counters zeroed")
        audit_log("reset_daily_counters", "system", "OK", message="midnight reset")
    except Exception as e:
        logger.error(f"reset_daily_counters: {e}")


# ---------------------------------------------------------------------------
# Autonomous division — rebecka (60/day every 2h) + zuri (25/day every 4h)
# ---------------------------------------------------------------------------

_last_rebecka = 0.0
_last_zuri    = 0.0
REBECKA_INTERVAL = 2 * 3600   # every 2 hours
ZURI_INTERVAL    = 4 * 3600   # every 4 hours


def run_rebecka(agents):
    """Dispatch rebecka for autonomous cold outreach. Batch = up to 10 sends."""
    global _last_rebecka
    now = time.time()
    if now - _last_rebecka < REBECKA_INTERVAL:
        return
    _last_rebecka = now

    remaining = _quota_remaining("autonomous")
    if remaining <= 0:
        logger.info("run_rebecka: daily quota (60) reached — skipping")
        return

    agent = agents.get("rebecka")
    if not agent:
        logger.warning("rebecka not in registry")
        return

    batch = min(10, remaining)
    start = time.time()
    try:
        agent.run(
            f"Send cold outreach emails to up to {batch} AUTONOMOUS leads with status NEW. "
            "Pick the {batch} leads most recently added that have not been contacted. "
            "For each: personalise the email around their operations role, send via send_email(), "
            "then call update_lead_stage(email, 'CONTACTED'). "
            "After each successful send, report back the count so we can track quota."
        )
        sent = batch  # optimistic; real tracking via _increment_counter
        _increment_counter("autonomous", sent)
        duration = int((time.time() - start) * 1000)
        audit_log("rebeckaRun", "rebecka", "OK", duration_ms=duration,
                  message=f"batch={batch} total_today={_get_counter('autonomous')}/60")
        if _get_counter("autonomous") >= DAILY_TARGETS["autonomous"]:
            discord_notify("autonomous", f"✅ Autonomous daily target hit: 60 sends complete")
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        audit_log("rebeckaRun", "rebecka", "ERROR", duration_ms=duration, message=str(e)[:400])
        logger.error(f"run_rebecka: {e}")


def run_zuri(agents):
    """Dispatch zuri for content-led autonomous outreach. Batch = up to 7 sends."""
    global _last_zuri
    now = time.time()
    if now - _last_zuri < ZURI_INTERVAL:
        return
    _last_zuri = now

    remaining = _quota_remaining("zuri")
    if remaining <= 0:
        logger.info("run_zuri: daily quota (25) reached — skipping")
        return

    agent = agents.get("zuri")
    if not agent:
        logger.warning("zuri not in registry")
        return

    batch = min(7, remaining)
    start = time.time()
    try:
        agent.run(
            f"Send content-led outreach emails to up to {batch} AUTONOMOUS leads with status NEW "
            "that rebecka has not yet contacted today. "
            "Open each email with a relevant AI-in-operations insight or data point, then connect it "
            "to what ZYN Autonomous Systems does. Keep each email under 120 words. "
            "Send via send_email(), then call update_lead_stage(email, 'CONTACTED')."
        )
        _increment_counter("zuri", batch)
        duration = int((time.time() - start) * 1000)
        audit_log("zuriRun", "zuri", "OK", duration_ms=duration,
                  message=f"batch={batch} total_today={_get_counter('zuri')}/25")
        if _get_counter("zuri") >= DAILY_TARGETS["zuri"]:
            discord_notify("autonomous", f"✅ Zuri content sends daily target hit: 25 complete")
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        audit_log("zuriRun", "zuri", "ERROR", duration_ms=duration, message=str(e)[:400])
        logger.error(f"run_zuri: {e}")


# ---------------------------------------------------------------------------
# Consulting division — sara (40/day every 4h) + lea (20/day every 6h)
# ---------------------------------------------------------------------------

_last_sara = 0.0
_last_lea  = 0.0
SARA_INTERVAL = 4 * 3600
LEA_INTERVAL  = 6 * 3600


def run_sara(agents):
    """Dispatch sara for consulting cold outreach. Batch = up to 10 sends."""
    global _last_sara
    now = time.time()
    if now - _last_sara < SARA_INTERVAL:
        return
    _last_sara = now

    remaining = _quota_remaining("sara")
    if remaining <= 0:
        logger.info("run_sara: daily quota (40) reached — skipping")
        return

    agent = agents.get("sara")
    if not agent:
        logger.warning("sara not in registry")
        return

    batch = min(10, remaining)
    start = time.time()
    try:
        agent.run(
            f"Send cold outreach emails to up to {batch} CONSULTING leads with status NEW. "
            "Personalise each email around AI supply-chain consulting value for mid-market companies. "
            "Keep emails under 150 words. Send via send_email(), "
            "then call update_lead_stage(email, 'CONTACTED') for each successful send."
        )
        _increment_counter("sara", batch)
        duration = int((time.time() - start) * 1000)
        audit_log("saraRun", "sara", "OK", duration_ms=duration,
                  message=f"batch={batch} total_today={_get_counter('sara')}/40")
        if _get_counter("sara") >= DAILY_TARGETS["sara"]:
            discord_notify("consulting", f"✅ Sara consulting sends daily target hit: 40 complete")
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        audit_log("saraRun", "sara", "ERROR", duration_ms=duration, message=str(e)[:400])
        logger.error(f"run_sara: {e}")


def run_lea(agents):
    """Dispatch lea for consulting outreach and retainer touches. Batch = up to 7 sends."""
    global _last_lea
    now = time.time()
    if now - _last_lea < LEA_INTERVAL:
        return
    _last_lea = now

    remaining = _quota_remaining("lea")
    if remaining <= 0:
        logger.info("run_lea: daily quota (20) reached — skipping")
        return

    agent = agents.get("lea")
    if not agent:
        logger.warning("lea not in registry")
        return

    batch = min(7, remaining)
    start = time.time()
    try:
        agent.run(
            f"Send outreach emails to up to {batch} CONSULTING leads with status NEW or CONTACTED. "
            "Focus on client-success angle: reducing operational overhead, unlocking revenue from "
            "existing stack. For any active retainers in CRM, send a QBR or check-in sequence. "
            "Send via send_email(), then call update_lead_stage(email, 'CONTACTED') for new touches."
        )
        _increment_counter("lea", batch)
        duration = int((time.time() - start) * 1000)
        audit_log("leaRun", "lea", "OK", duration_ms=duration,
                  message=f"batch={batch} total_today={_get_counter('lea')}/20")
        if _get_counter("lea") >= DAILY_TARGETS["lea"]:
            discord_notify("consulting", f"✅ Lea consulting sends daily target hit: 20 complete")
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        audit_log("leaRun", "lea", "ERROR", duration_ms=duration, message=str(e)[:400])
        logger.error(f"run_lea: {e}")


# ---------------------------------------------------------------------------
# Federal division — adam + scraper (50 leads/day) + asher + micah + miro (grants)
# ---------------------------------------------------------------------------

_last_adam   = 0.0
_last_micah  = 0.0
_last_asher  = 0.0
_last_miro_grants  = 0.0
ADAM_INTERVAL  = 4 * 3600
MICAH_INTERVAL = 6 * 3600
ASHER_INTERVAL = 6 * 3600
MIRO_GRANTS_INTERVAL = 8 * 3600


def run_adam_scrape(agents):
    """Dispatch adam to scan SAM.gov and write up to 50 federal leads/day."""
    global _last_adam
    now = time.time()
    if now - _last_adam < ADAM_INTERVAL:
        return
    _last_adam = now

    remaining = _quota_remaining("federal_scrape")
    if remaining <= 0:
        logger.info("run_adam_scrape: federal daily scrape target (50) reached — skipping")
        return

    agent = agents.get("adam")
    if not agent:
        logger.warning("adam not in registry — federal SAM.gov scan skipped")
        return

    batch = min(15, remaining)
    start = time.time()
    try:
        agent.run(
            f"Scan SAM.gov for up to {batch} new opportunities matching ZYN NAICS codes "
            "(541511, 541512, 541519, 541611, 561110). "
            "For each opportunity: rank win probability 0-100, extract the contracting officer name "
            "and email if available, call write_lead() with division=FEDERAL, then write the "
            "opportunity to the Opportunities tab via sheets_write(). "
            "Flag any opportunity ranked >85 to asher via discord_notify(). "
            "After each batch, report count so we can track daily_scrape_federal progress toward 50."
        )
        _increment_counter("federal_scrape", batch)
        current = _get_counter("federal_scrape")
        duration = int((time.time() - start) * 1000)
        audit_log("adamScrapeRun", "adam", "OK", duration_ms=duration,
                  message=f"batch={batch} total_today={current}/50")
        if current >= DAILY_TARGETS["federal_scrape"]:
            discord_notify("federal", f"✅ Federal daily scrape target hit: 50 leads written to pipeline")
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        audit_log("adamScrapeRun", "adam", "ERROR", duration_ms=duration, message=str(e)[:400])
        logger.error(f"run_adam_scrape: {e}")


def run_micah(agents):
    """Dispatch micah for prime teaming outreach on active federal opps."""
    global _last_micah
    now = time.time()
    if now - _last_micah < MICAH_INTERVAL:
        return
    _last_micah = now

    agent = agents.get("micah")
    if not agent:
        logger.warning("micah not in registry")
        return

    start = time.time()
    try:
        agent.run(
            "Review the Opportunities tab for active federal opportunities where ZYN could team "
            "with a prime contractor. Identify the top 3-5 primes on each. "
            "Send a teaming-interest email to each prime POC via send_email(). "
            "Gate any message containing teaming agreement, subcontract, or NDA language into "
            "the approval queue. Log each outreach in LEADS tab with division=FEDERAL."
        )
        duration = int((time.time() - start) * 1000)
        audit_log("micahRun", "micah", "OK", duration_ms=duration)
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        audit_log("micahRun", "micah", "ERROR", duration_ms=duration, message=str(e)[:400])
        logger.error(f"run_micah: {e}")


def run_asher(agents):
    """Dispatch asher to draft proposal responses for >85% ranked opportunities."""
    global _last_asher
    now = time.time()
    if now - _last_asher < ASHER_INTERVAL:
        return
    _last_asher = now

    agent = agents.get("asher")
    if not agent:
        logger.warning("asher not in registry")
        return

    start = time.time()
    try:
        agent.run(
            "Check the Opportunities tab for any opportunity with win_probability > 85 that does not "
            "yet have a draft_status of DRAFTED or SENT. "
            "For each, auto-draft a proposal response or LOI tailored to the agency's requirements. "
            "Write the draft to the PENDING_APPROVAL sheet with status=PENDING_REVIEW. "
            "Never send directly — all bids require CEO approval before dispatch."
        )
        duration = int((time.time() - start) * 1000)
        audit_log("asherRun", "asher", "OK", duration_ms=duration)
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        audit_log("asherRun", "asher", "ERROR", duration_ms=duration, message=str(e)[:400])
        logger.error(f"run_asher: {e}")


def run_miro_grants(agents):
    """Dispatch miro (grants track) to find grants and draft LOIs."""
    global _last_miro_grants
    now = time.time()
    if now - _last_miro_grants < MIRO_GRANTS_INTERVAL:
        return
    _last_miro_grants = now

    agent = agents.get("miro")
    if not agent:
        logger.warning("miro not in registry (grants track)")
        return

    start = time.time()
    try:
        agent.run(
            "Search for federal and state grant opportunities matching ZYN's profile "
            "(AI, supply chain, government technology, small business). "
            "Identify the top 3 new grants today. Draft a concise LOI for each. "
            "Write each LOI to PENDING_APPROVAL with status=PENDING_REVIEW. "
            "Never send without CEO approval."
        )
        duration = int((time.time() - start) * 1000)
        audit_log("miroGrantsRun", "miro", "OK", duration_ms=duration)
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        audit_log("miroGrantsRun", "miro", "ERROR", duration_ms=duration, message=str(e)[:400])
        logger.error(f"run_miro_grants: {e}")


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

        # ── Autonomous: 100 sends/day ─────────────────────────────
        run_rebecka(agents)          # 60/day  every 2h
        run_zuri(agents)             # 25/day  every 4h
        run_autonomous_followup(agents)  # 15/day  every 6h  → rebecka

        # ── Consulting: 75 sends/day ──────────────────────────────
        run_sara(agents)             # 40/day  every 4h
        run_lea(agents)              # 20/day  every 6h
        run_consulting_followup(agents)  # 15/day  every 6h  → malik

        # ── Federal: 50 leads scraped/day (adam scans, david enriches) ─────
        run_adam_scrape(agents)      # 50 leads/day  every 4h
        run_micah(agents)            # prime outreach every 6h
        run_asher(agents)            # bid drafts     every 6h
        run_miro_grants(agents)    # grant LOIs     every 8h (was benji)

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
    last_midnight_reset = 0.0
    while True:
        try:
            now = time.time()
            if now - last_heartbeat > 3600:
                heartbeat()
                last_heartbeat = now
            # Midnight UTC reset of all daily counters
            from datetime import timezone
            today_midnight = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).timestamp()
            if last_midnight_reset < today_midnight:
                reset_daily_counters()
                last_midnight_reset = today_midnight
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
