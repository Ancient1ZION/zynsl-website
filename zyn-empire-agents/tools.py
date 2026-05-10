"""Tool registry — what agents can actually do.

Every public tool follows three contracts:
  1. NEVER raise on a foreseeable failure. Return a typed empty value or a
     {"error": "..."} dict. Agents check truthiness and log to drift.
  2. ALWAYS log warnings/errors via loguru with the tool name as a tag.
  3. ALWAYS bound network calls with a timeout. No naked requests.get/post.

This is the trust boundary: above this line, the orchestrator + LLM see only
clean results or empty lists. Exceptions live below this line.

CHANGELOG (patch 2026-05-10):
  - ensure_tab(): auto-creates missing sheet tabs with correct headers
  - audit_log(): every trigger/agent action logged to Audit tab
  - inbox_write(): parsed replies written to Inbox tab
  - _open_tab(): now calls ensure_tab so missing tabs never silently fail
  - crm_sync(): promotes HOT/REPLIED/PROPOSAL/SIGNED/WON opps into CRM tab
  - get_leads() / get_opps(): typed dict accessors for sheet rows
"""
from __future__ import annotations

import os
import json
import smtplib
import ssl
import time
from datetime import datetime
from email.mime.text import MIMEText
from typing import Optional, List, Dict, Any

import requests
from loguru import logger

import gspread
from google.oauth2.service_account import Credentials

SHEET_ID = os.getenv("SHEET_ID", "1WHN438mjORT4HnGiXapWv78uomVy6KMhHsl0llxaeQk")
GAS_PROXY_URL = os.getenv("GAS_PROXY_URL", "")
GAS_PROXY_KEY = os.getenv("GAS_PROXY_KEY", "")
SA_PATH = os.getenv("GOOGLE_SA_JSON_PATH", "")

DEFAULT_TIMEOUT = (5, 25)
SHORT_TIMEOUT = (5, 10)

# Tab schemas: name -> header row
TAB_SCHEMAS: Dict[str, List[str]] = {
    "Audit": ["timestamp", "trigger", "agent", "status", "duration_ms", "message"],
    "Inbox": ["timestamp", "from_email", "from_name", "subject", "body_snippet",
              "matched_lead_id", "division", "status", "actioned_by"],
    "CRM":   ["lead_id", "name", "company", "email", "division", "stage",
              "value", "last_contact", "next_action", "notes", "agent",
              "created_at", "updated_at", "won_at", "lost_reason"],
    "CONTROL": ["signal", "updated_at", "updated_by"],
    "LEADS": ["id", "name", "company", "email", "division", "status", "agent",
              "value", "source", "added_at", "contacted_at", "notes", "tags", "unsubscribed"],
    "Opportunities": ["opp_id", "lead_id", "name", "company", "division", "stage",
                      "value", "probability", "agent", "created_at", "updated_at",
                      "close_date", "notes"],
}

# ---------------------------------------------------------------------------
# Google Sheets client (lazy singleton)
# ---------------------------------------------------------------------------

_gs_client = None


def gs():
    global _gs_client
    if _gs_client:
        return _gs_client
    if not SA_PATH or not os.path.exists(SA_PATH):
        raise RuntimeError(
            f"Service account JSON not found at {SA_PATH!r}. "
            "Set GOOGLE_SA_JSON_PATH in .env."
        )
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(SA_PATH, scopes=scopes)
    _gs_client = gspread.authorize(creds)
    return _gs_client


def ensure_tab(tab: str):
    """Return worksheet, creating it with schema headers if it does not exist."""
    try:
        sh = gs().open_by_key(SHEET_ID)
        try:
            ws = sh.worksheet(tab)
            return ws
        except gspread.WorksheetNotFound:
            logger.info(f"ensure_tab: creating missing tab {tab!r}")
            ws = sh.add_worksheet(title=tab, rows=1000, cols=20)
            headers = TAB_SCHEMAS.get(tab, ["created_at", "data"])
            ws.append_row(headers, value_input_option="USER_ENTERED")
            logger.info(f"ensure_tab: {tab!r} created with headers {headers}")
            return ws
    except Exception as e:
        logger.error(f"ensure_tab({tab}): {e}")
        return None


def _open_tab(tab: str):
    """Open worksheet by name; auto-creates via ensure_tab if missing."""
    return ensure_tab(tab)


# ---------------------------------------------------------------------------
# Sheets read / write / append — all soft-fail
# ---------------------------------------------------------------------------

def sheets_read(tab: str, range_a1: Optional[str] = None) -> List[List[str]]:
    ws = _open_tab(tab)
    if ws is None:
        return []
    try:
        if range_a1:
            return ws.get(range_a1) or []
        return ws.get_all_values() or []
    except Exception as e:
        logger.error(f"sheets_read({tab}, {range_a1}): {e}")
        return []


def sheets_write(tab: str, range_a1: str, values: List[List[Any]]) -> Dict[str, Any]:
    if not values:
        return {"ok": False, "error": "empty values"}
    ws = _open_tab(tab)
    if ws is None:
        return {"ok": False, "error": f"tab {tab!r} could not be opened or created"}
    try:
        ws.update(range_a1, values, value_input_option="USER_ENTERED")
        return {"ok": True, "rows": len(values)}
    except Exception as e:
        logger.error(f"sheets_write({tab}, {range_a1}): {e}")
        return {"ok": False, "error": str(e)}


def sheets_append(tab: str, row: List[Any]) -> Dict[str, Any]:
    if not row:
        return {"ok": False, "error": "empty row"}
    ws = _open_tab(tab)
    if ws is None:
        return {"ok": False, "error": f"tab {tab!r} could not be opened or created"}
    try:
        ws.append_row(row, value_input_option="USER_ENTERED")
        return {"ok": True}
    except Exception as e:
        logger.error(f"sheets_append({tab}): {e}")
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

def audit_log(
    trigger: str,
    agent: str,
    status: str,
    duration_ms: int = 0,
    message: str = "",
) -> Dict[str, Any]:
    """Append one execution record to the Audit tab."""
    row = [
        datetime.utcnow().isoformat() + "Z",
        trigger,
        agent,
        status,
        duration_ms,
        message[:500] if message else "",
    ]
    result = sheets_append("Audit", row)
    if not result.get("ok"):
        logger.warning(f"audit_log write failed: {result.get('error')}")
    return result


# ---------------------------------------------------------------------------
# Inbox — inbound reply capture
# ---------------------------------------------------------------------------

def inbox_write(
    from_email: str,
    from_name: str,
    subject: str,
    body_snippet: str,
    matched_lead_id: str = "",
    division: str = "",
    status: str = "UNREAD",
) -> Dict[str, Any]:
    """Append one parsed inbound reply to the Inbox tab."""
    row = [
        datetime.utcnow().isoformat() + "Z",
        from_email,
        from_name,
        subject,
        body_snippet[:1000] if body_snippet else "",
        matched_lead_id,
        division,
        status,
        "",
    ]
    result = sheets_append("Inbox", row)
    if not result.get("ok"):
        logger.warning(f"inbox_write failed: {result.get('error')}")
    return result


# ---------------------------------------------------------------------------
# CRM sync — promotes active opps into the CRM tab
# ---------------------------------------------------------------------------

def crm_sync() -> Dict[str, Any]:
    """
    For each Opportunity with stage in {HOT, REPLIED, PROPOSAL, SIGNED, WON},
    insert a row into the CRM tab if that opp_id is not already there.
    Returns {synced: int, errors: int}.
    """
    synced = 0
    errors = 0
    try:
        opps = get_opps()
        crm_ws = _open_tab("CRM")
        if crm_ws is None:
            return {"synced": 0, "errors": 1, "error": "CRM tab unavailable"}

        existing_rows = crm_ws.get_all_values()
        existing_ids = (
            {r[0] for r in existing_rows[1:] if r}
            if len(existing_rows) > 1
            else set()
        )

        active_stages = {"HOT", "REPLIED", "PROPOSAL", "SIGNED", "WON"}

        for opp in opps:
            stage = str(opp.get("stage", "")).upper()
            if stage not in active_stages:
                continue
            opp_id = opp.get("opp_id", "")
            if not opp_id or opp_id in existing_ids:
                continue
            now = datetime.utcnow().isoformat() + "Z"
            row = [
                opp_id,
                opp.get("name", ""),
                opp.get("company", ""),
                "",
                opp.get("division", ""),
                stage,
                opp.get("value", ""),
                now,
                "",
                opp.get("notes", ""),
                opp.get("agent", ""),
                opp.get("created_at", now),
                now,
                now if stage == "WON" else "",
                "",
            ]
            result = sheets_append("CRM", row)
            if result.get("ok"):
                synced += 1
                logger.info(f"crm_sync: promoted {opp_id} stage={stage}")
            else:
                errors += 1
                logger.error(f"crm_sync: failed {opp_id}: {result.get('error')}")

    except Exception as e:
        logger.error(f"crm_sync: {e}")
        errors += 1

    audit_log(
        "crm_sync",
        "system",
        "OK" if errors == 0 else "PARTIAL",
        message=f"synced={synced} errors={errors}",
    )
    return {"synced": synced, "errors": errors}


# ---------------------------------------------------------------------------
# Typed row accessors
# ---------------------------------------------------------------------------

def get_leads(status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return all LEADS rows as list of dicts, optionally filtered by status."""
    rows = sheets_read("LEADS")
    if not rows or len(rows) < 2:
        return []
    headers = [h.lower().strip() for h in rows[0]]
    result = []
    for row in rows[1:]:
        padded = row + [""] * max(0, len(headers) - len(row))
        d = dict(zip(headers, padded))
        if status_filter and d.get("status", "").upper() != status_filter.upper():
            continue
        result.append(d)
    return result


def get_opps(stage_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return all Opportunities rows as list of dicts, optionally filtered by stage."""
    rows = sheets_read("Opportunities")
    if not rows or len(rows) < 2:
        return []
    headers = [h.lower().strip() for h in rows[0]]
    result = []
    for row in rows[1:]:
        padded = row + [""] * max(0, len(headers) - len(row))
        d = dict(zip(headers, padded))
        if stage_filter and d.get("stage", "").upper() != stage_filter.upper():
            continue
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Email — SMTP send, soft-fail
# ---------------------------------------------------------------------------

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USER)


def send_email(
    to: str,
    subject: str,
    body: str,
    reply_to: Optional[str] = None,
) -> Dict[str, Any]:
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASS]):
        logger.warning("send_email: SMTP not configured")
        return {"ok": False, "error": "SMTP not configured"}
    try:
        msg = MIMEText(body, "plain")
        msg["Subject"] = subject
        msg["From"] = FROM_EMAIL
        msg["To"] = to
        if reply_to:
            msg["Reply-To"] = reply_to
        ctx = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(FROM_EMAIL, [to], msg.as_string())
        logger.info(f"send_email: sent to {to!r} subject={subject!r}")
        return {"ok": True}
    except Exception as e:
        logger.error(f"send_email({to}): {e}")
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Discord webhook notify, soft-fail
# ---------------------------------------------------------------------------

DISCORD_WEBHOOKS: Dict[str, str] = {}


def _load_discord_hooks() -> None:
    global DISCORD_WEBHOOKS
    raw = os.getenv("DISCORD_WEBHOOKS_JSON", "{}")
    try:
        DISCORD_WEBHOOKS = json.loads(raw)
    except Exception:
        logger.warning("DISCORD_WEBHOOKS_JSON parse failed; using empty map")


_load_discord_hooks()


def discord_notify(channel: str, message: str) -> Dict[str, Any]:
    hook = DISCORD_WEBHOOKS.get(channel, "")
    if not hook:
        logger.debug(f"discord_notify: no webhook for {channel!r}")
        return {"ok": False, "error": f"no webhook for {channel!r}"}
    try:
        r = requests.post(
            hook,
            json={"content": message[:2000]},
            timeout=SHORT_TIMEOUT,
        )
        r.raise_for_status()
        return {"ok": True}
    except Exception as e:
        logger.error(f"discord_notify({channel}): {e}")
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Web search via SerpAPI
# ---------------------------------------------------------------------------

SERP_KEY = os.getenv("SERPAPI_KEY", "")


def web_search(query: str, num: int = 5) -> List[Dict[str, str]]:
    if not SERP_KEY:
        logger.warning("web_search: SERPAPI_KEY not set")
        return []
    try:
        r = requests.get(
            "https://serpapi.com/search",
            params={"q": query, "num": num, "api_key": SERP_KEY},
            timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        results = r.json().get("organic_results", [])
        return [
            {"title": x.get("title", ""), "link": x.get("link", ""), "snippet": x.get("snippet", "")}
            for x in results
        ]
    except Exception as e:
        logger.error(f"web_search({query!r}): {e}")
        return []


# ---------------------------------------------------------------------------
# SAM.gov opportunity search
# ---------------------------------------------------------------------------

SAM_API_KEY = os.getenv("SAM_API_KEY", "")
SAM_BASE = "https://api.sam.gov/opportunities/v2/search"


def sam_gov_search(naics: str, limit: int = 10) -> List[Dict[str, Any]]:
    if not SAM_API_KEY:
        logger.warning("sam_gov_search: SAM_API_KEY not set")
        return []
    try:
        params = {
            "api_key": SAM_API_KEY,
            "ncode": naics,
            "limit": limit,
            "ptype": "o",
        }
        r = requests.get(SAM_BASE, params=params, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        return r.json().get("opportunitiesData", [])
    except Exception as e:
        logger.error(f"sam_gov_search({naics}): {e}")
        return []
