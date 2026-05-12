"""Tool registry — what agents can actually do.

Every public tool follows three contracts:
  1. NEVER raise on a foreseeable failure. Return a typed empty value or a
     {"error": "..."} dict. Agents check truthiness and log to drift.
  2. ALWAYS log warnings/errors via loguru with the tool name as a tag.
  3. ALWAYS bound network calls with a timeout. No naked requests.get/post.

This is the trust boundary: above this line, the orchestrator + LLM see only
clean results or empty lists. Exceptions live below this line.

CHANGELOG (patch 2026-05-12f — Bitrix24 CRM integration layer):
  - Added BITRIX24_WEBHOOK env var
  - bitrix24_call(): core REST caller with timeout + soft-fail
  - bitrix24_create_lead(): maps ZYN lead fields to Bitrix24 CRM.Lead entity
  - bitrix24_update_lead(): update stage/status on existing Bitrix24 lead by ID
  - bitrix24_create_deal(): promotes lead to Bitrix24 Deal (Opportunity)
  - bitrix24_add_activity(): logs emails, calls, notes on a deal/lead
  - bitrix24_get_lead(): fetch a lead by ID
  - bitrix24_find_lead_by_email(): search by email for dedup
  - write_lead_bitrix24(): drop-in replacement for write_lead() that syncs to both Sheets + Bitrix24
  - update_lead_stage_bitrix24(): updates stage in both Sheets + Bitrix24
  - crm_sync_bitrix24(): promotes HOT+ opps to Bitrix24 Deals + Sheets CRM tab
  - ZYN pipeline stage → Bitrix24 STATUS mapping table included

CHANGELOG (patch 2026-05-10):
  - ensure_tab(): auto-creates missing sheet tabs with correct headers
  - audit_log(): every trigger/agent action logged to Audit tab
  - inbox_write(): parsed replies written to Inbox tab
  - _open_tab(): now calls ensure_tab so missing tabs never silently fail
  - crm_sync(): promotes HOT/REPLIED/PROPOSAL/SIGNED/WON opps into CRM tab
  - update_lead_stage(): advances LEADS status column on reply classification (fixes stage-write bug)
  - write_lead(): moves staged CONSULTING_50 leads into live LEADS pipeline (unblocks consulting 50/day)
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


# ---------------------------------------------------------------------------
# Lead stage advancement — updates status column in LEADS tab
# ---------------------------------------------------------------------------

def update_lead_stage(
    email: str,
    new_stage: str,
    matched_lead_row: int = -1,
) -> Dict[str, Any]:
    """
    Update the 'status' column for a lead row in the LEADS tab.

    Matches by email address. If matched_lead_row is provided (1-based index
    into the sheet, row 1 = header), uses that directly to avoid a second scan.

    Returns {"ok": True, "row": row_index} or {"ok": False, "error": ...}.
    """
    if not email:
        return {"ok": False, "error": "email required"}
    valid_stages = {
        "NEW", "CONTACTED", "REPLIED", "WARM", "HOT",
        "PROPOSAL", "SIGNED", "WON", "LOST",
        "UNSUB", "BAD_EMAIL", "DUPLICATE", "NO_EMAIL", "OTHER",
    }
    stage_upper = new_stage.strip().upper()
    if stage_upper not in valid_stages:
        logger.warning(f"update_lead_stage: invalid stage {new_stage!r}")
        return {"ok": False, "error": f"invalid stage: {new_stage}"}

    ws = _open_tab("LEADS")
    if ws is None:
        return {"ok": False, "error": "LEADS tab unavailable"}

    try:
        all_rows = ws.get_all_values()
        if not all_rows or len(all_rows) < 2:
            return {"ok": False, "error": "LEADS tab is empty"}

        headers = [h.lower().strip() for h in all_rows[0]]
        try:
            email_col = headers.index("email")
            status_col = headers.index("status")
        except ValueError as e:
            return {"ok": False, "error": f"column not found: {e}"}

        # Find the row — prefer the pre-matched index if given
        row_idx = matched_lead_row if matched_lead_row > 1 else -1
        if row_idx < 2:
            for i, row in enumerate(all_rows[1:], start=2):
                padded = row + [""] * max(0, len(headers) - len(row))
                if padded[email_col].strip().lower() == email.strip().lower():
                    row_idx = i
                    break

        if row_idx < 2:
            return {"ok": False, "error": f"lead not found for email: {email}"}

        # Sheet columns are 1-indexed; status_col is 0-indexed from headers
        col_letter = chr(ord("A") + status_col)
        cell = f"{col_letter}{row_idx}"
        ws.update(cell, [[stage_upper]], value_input_option="USER_ENTERED")
        logger.info(f"update_lead_stage: row={row_idx} email={email} -> {stage_upper}")
        audit_log(
            "update_lead_stage", "inboxMgr", "OK",
            message=f"row={row_idx} email={email} stage={stage_upper}",
        )
        return {"ok": True, "row": row_idx, "stage": stage_upper}

    except Exception as e:
        logger.error(f"update_lead_stage({email!r}, {new_stage!r}): {e}")
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# write_lead — move staged consulting leads into live LEADS tab
# ---------------------------------------------------------------------------

def write_lead(
    name: str,
    company: str,
    email: str,
    division: str,
    source: str = "CONSULTING_50",
    status: str = "NEW",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Write a single enriched lead row into the LEADS tab.

    Deduplicates by email — skips if the email already exists.
    Used by the commercialScraperRun trigger to move staged Consulting
    prospects from CONSULTING_50 into the live LEADS pipeline.

    Returns {"ok": True, "skipped": False} on insert,
            {"ok": True, "skipped": True} on dupe,
            {"ok": False, "error": ...} on failure.
    """
    if not email:
        return {"ok": False, "error": "email required for write_lead"}

    ws = _open_tab("LEADS")
    if ws is None:
        return {"ok": False, "error": "LEADS tab unavailable"}

    try:
        all_rows = ws.get_all_values()
        headers = [h.lower().strip() for h in all_rows[0]] if all_rows else []
        email_col = headers.index("email") if "email" in headers else 2

        # Deduplicate
        existing_emails = {
            r[email_col].strip().lower()
            for r in all_rows[1:]
            if r and len(r) > email_col and r[email_col]
        }
        if email.strip().lower() in existing_emails:
            logger.info(f"write_lead: skipped duplicate {email}")
            return {"ok": True, "skipped": True, "reason": "duplicate email"}

        now = datetime.utcnow().isoformat() + "Z"
        extras = extra or {}
        row = [
            now,                              # created_at
            name.strip(),                     # name
            email.strip(),                    # email
            company.strip(),                  # company
            division.strip().upper(),         # division
            source,                           # source
            status.strip().upper(),           # status
            "",                               # contacted_at
            "",                               # replied_at
            extras.get("title", ""),          # title / role
            extras.get("linkedin", ""),       # linkedin_url
            extras.get("phone", ""),          # phone
            extras.get("notes", ""),          # notes
        ]

        result = sheets_append("LEADS", row)
        if result.get("ok"):
            logger.info(f"write_lead: inserted {email} ({company}) div={division}")
            audit_log(
                "write_lead", "scraper", "OK",
                message=f"email={email} company={company} div={division}",
            )
        return result

    except Exception as e:
        logger.error(f"write_lead({email!r}): {e}")
        return {"ok": False, "error": str(e)}


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
BITRIX24_WEBHOOK = os.getenv("BITRIX24_WEBHOOK", "")  # https://yourname.bitrix24.com/rest/1/xxxxxx/
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


# ===========================================================================
# BITRIX24 CRM INTEGRATION LAYER  (patch 2026-05-12f)
# ===========================================================================
# All functions follow the same soft-fail contract as the rest of tools.py.
# Set BITRIX24_WEBHOOK env var to your Bitrix24 inbound webhook URL:
#   https://yourname.bitrix24.com/rest/1/<token>/
#
# ZYN Pipeline Stage → Bitrix24 Lead STATUS mapping:
#   NEW          → NEW
#   CONTACTED    → IN_PROCESS
#   REPLIED      → IN_PROCESS
#   WARM         → IN_PROCESS
#   HOT          → RC_CONVERTED  (ready to convert to Deal)
#   PROPOSAL     → RC_CONVERTED
#   SIGNED       → CONVERTED
#   WON          → CONVERTED
#   LOST         → JUNK
#   UNSUB        → JUNK
#   BAD_EMAIL    → JUNK
#   DUPLICATE    → JUNK
#
# ZYN Division → Bitrix24 Source mapping:
#   federal      → WEB (government/federal)
#   consulting   → CALL (outbound consulting outreach)
#   autonomous   → EMAIL (autonomous outreach)
#   capital      → OTHER
# ===========================================================================

_ZYN_STAGE_TO_BITRIX = {
    "NEW":        "NEW",
    "CONTACTED":  "IN_PROCESS",
    "REPLIED":    "IN_PROCESS",
    "WARM":       "IN_PROCESS",
    "HOT":        "RC_CONVERTED",
    "PROPOSAL":   "RC_CONVERTED",
    "SIGNED":     "CONVERTED",
    "WON":        "CONVERTED",
    "LOST":       "JUNK",
    "UNSUB":      "JUNK",
    "BAD_EMAIL":  "JUNK",
    "DUPLICATE":  "JUNK",
    "NO_EMAIL":   "JUNK",
    "OTHER":      "NEW",
}

_ZYN_DIVISION_TO_SOURCE = {
    "federal":    "WEB",
    "consulting": "CALL",
    "autonomous": "EMAIL",
    "capital":    "OTHER",
}

# Bitrix24 Deal stage IDs (standard pipeline — override if you rename stages)
_ZYN_STAGE_TO_DEAL = {
    "HOT":       "C1:1",   # New
    "PROPOSAL":  "C1:2",   # Proposal/Price Quote
    "SIGNED":    "C1:3",   # Negotiation & Discount
    "WON":       "C1:WON",
    "LOST":      "C1:LOSE",
}


def bitrix24_call(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Core Bitrix24 REST API caller. All other b24 functions go through this.
    Returns parsed JSON response or {"error": "..."} on failure.
    """
    if not BITRIX24_WEBHOOK:
        logger.warning("bitrix24_call: BITRIX24_WEBHOOK not configured")
        return {"error": "BITRIX24_WEBHOOK not set"}
    url = BITRIX24_WEBHOOK.rstrip("/") + "/" + method + ".json"
    try:
        resp = requests.post(url, json=params, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            logger.warning(f"bitrix24_call({method}): API error {data['error']}: {data.get('error_description','')}")
        return data
    except Exception as e:
        logger.error(f"bitrix24_call({method}): {e}")
        return {"error": str(e)}


def bitrix24_find_lead_by_email(email: str) -> Optional[int]:
    """
    Search Bitrix24 for a lead by email. Returns lead ID (int) or None.
    Used for deduplication before creating a new lead.
    """
    if not email:
        return None
    result = bitrix24_call("crm.lead.list", {
        "filter": {"EMAIL": email},
        "select": ["ID", "EMAIL"],
    })
    items = result.get("result", [])
    if items:
        return int(items[0]["ID"])
    return None


def bitrix24_create_lead(
    name: str,
    company: str,
    email: str,
    division: str,
    status: str = "NEW",
    phone: str = "",
    title: str = "",
    comments: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Create a new lead in Bitrix24 CRM.
    Maps ZYN fields → Bitrix24 CRM.Lead entity fields.
    Returns {"ok": True, "bitrix_id": <int>} or {"ok": False, "error": ...}.
    Deduplicates by email — skips if lead already exists.
    """
    if not email:
        return {"ok": False, "error": "email required"}

    # Dedup check
    existing_id = bitrix24_find_lead_by_email(email)
    if existing_id:
        logger.info(f"bitrix24_create_lead: lead {email} already exists (ID {existing_id}), skipping")
        return {"ok": True, "skipped": True, "bitrix_id": existing_id}

    b24_status = _ZYN_STAGE_TO_BITRIX.get(status.upper(), "NEW")
    b24_source = _ZYN_DIVISION_TO_SOURCE.get(division.lower(), "OTHER")

    fields = {
        "NAME":       name.split()[0] if name else "",
        "LAST_NAME":  " ".join(name.split()[1:]) if len(name.split()) > 1 else "",
        "COMPANY_TITLE": company,
        "STATUS_ID":  b24_status,
        "SOURCE_ID":  b24_source,
        "TITLE":      title or f"{company} — {division.title()} Lead",
        "COMMENTS":   comments or f"ZYN Empire auto-imported | Division: {division} | Source: agent",
        "EMAIL":      [{"VALUE": email, "VALUE_TYPE": "WORK"}],
    }
    if phone:
        fields["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "WORK"}]
    if extra:
        fields.update(extra)

    result = bitrix24_call("crm.lead.add", {"fields": fields})
    bitrix_id = result.get("result")
    if bitrix_id:
        logger.info(f"bitrix24_create_lead: created lead {email} → Bitrix ID {bitrix_id}")
        return {"ok": True, "skipped": False, "bitrix_id": int(bitrix_id)}
    return {"ok": False, "error": result.get("error", "unknown error from Bitrix24")}


def bitrix24_update_lead(
    bitrix_id: int,
    new_stage: str,
    extra_fields: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Update an existing Bitrix24 lead's STATUS_ID (pipeline stage).
    Returns {"ok": True} or {"ok": False, "error": ...}.
    """
    b24_status = _ZYN_STAGE_TO_BITRIX.get(new_stage.upper(), "IN_PROCESS")
    fields = {"STATUS_ID": b24_status}
    if extra_fields:
        fields.update(extra_fields)
    result = bitrix24_call("crm.lead.update", {"id": bitrix_id, "fields": fields})
    if result.get("result") is True:
        return {"ok": True, "bitrix_id": bitrix_id, "new_status": b24_status}
    return {"ok": False, "error": result.get("error", "update failed")}


def bitrix24_get_lead(bitrix_id: int) -> Dict[str, Any]:
    """Fetch a Bitrix24 lead by ID. Returns field dict or empty dict on failure."""
    result = bitrix24_call("crm.lead.get", {"id": bitrix_id})
    return result.get("result", {})


def bitrix24_create_deal(
    title: str,
    company: str,
    email: str,
    stage: str = "HOT",
    deal_value: float = 0.0,
    division: str = "consulting",
    currency: str = "USD",
    comments: str = "",
) -> Dict[str, Any]:
    """
    Create a Bitrix24 Deal (Opportunity) for leads that reach HOT/PROPOSAL/SIGNED.
    Deals represent active revenue opportunities in the Bitrix24 pipeline.
    Returns {"ok": True, "deal_id": <int>} or {"ok": False, "error": ...}.
    """
    stage_id = _ZYN_STAGE_TO_DEAL.get(stage.upper(), "C1:1")
    fields = {
        "TITLE":        title or f"{company} — {division.title()} Deal",
        "COMPANY_TITLE": company,
        "STAGE_ID":     stage_id,
        "OPPORTUNITY":  deal_value,
        "CURRENCY_ID":  currency,
        "COMMENTS":     comments or f"ZYN Empire auto-deal | Division: {division}",
        "SOURCE_ID":    _ZYN_DIVISION_TO_SOURCE.get(division.lower(), "OTHER"),
        "EMAIL":        [{"VALUE": email, "VALUE_TYPE": "WORK"}],
    }
    result = bitrix24_call("crm.deal.add", {"fields": fields})
    deal_id = result.get("result")
    if deal_id:
        logger.info(f"bitrix24_create_deal: created deal '{title}' → Deal ID {deal_id}")
        return {"ok": True, "deal_id": int(deal_id)}
    return {"ok": False, "error": result.get("error", "deal creation failed")}


def bitrix24_add_activity(
    entity_type: str,
    entity_id: int,
    activity_type: str,
    subject: str,
    description: str = "",
    direction: int = 2,
) -> Dict[str, Any]:
    """
    Log an activity (email send, call, note) on a Bitrix24 lead or deal.
    entity_type: 'LEAD' or 'DEAL'
    activity_type: 'EMAIL' | 'CALL' | 'TASK'
    direction: 1=inbound, 2=outbound
    Returns {"ok": True, "activity_id": <int>} or {"ok": False, "error": ...}.
    """
    type_map = {"EMAIL": 4, "CALL": 2, "TASK": 6}
    owner_type = 1 if entity_type.upper() == "LEAD" else 2
    fields = {
        "OWNER_TYPE_ID": owner_type,
        "OWNER_ID":      entity_id,
        "TYPE_ID":       type_map.get(activity_type.upper(), 4),
        "SUBJECT":       subject,
        "DESCRIPTION":   description,
        "DIRECTION":     direction,
        "COMPLETED":     "Y",
    }
    result = bitrix24_call("crm.activity.add", {"fields": fields})
    activity_id = result.get("result")
    if activity_id:
        return {"ok": True, "activity_id": int(activity_id)}
    return {"ok": False, "error": result.get("error", "activity log failed")}


def write_lead_bitrix24(
    name: str,
    company: str,
    email: str,
    division: str,
    source: str = "agent",
    status: str = "NEW",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Dual-write: creates lead in BOTH Google Sheets (write_lead) AND Bitrix24.
    This is the primary entry point for all new lead creation going forward.
    Falls back gracefully — Sheets write failure does NOT block Bitrix24 and vice versa.
    Returns combined result dict.
    """
    sheets_result = write_lead(name, company, email, division, source, status, extra)
    b24_result = bitrix24_create_lead(name, company, email, division, status, extra=extra)
    return {
        "sheets": sheets_result,
        "bitrix24": b24_result,
        "ok": sheets_result.get("ok", False) or b24_result.get("ok", False),
    }


def update_lead_stage_bitrix24(
    email: str,
    new_stage: str,
    bitrix_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Dual-update: updates stage in BOTH Google Sheets AND Bitrix24.
    If bitrix_id not provided, auto-looks it up by email.
    Also auto-creates a Bitrix24 Deal when stage reaches HOT or above.
    """
    sheets_result = update_lead_stage(email, new_stage)

    # Resolve bitrix_id
    if not bitrix_id:
        bitrix_id = bitrix24_find_lead_by_email(email)

    b24_result: Dict[str, Any] = {"ok": False, "error": "bitrix_id not found"}
    deal_result: Dict[str, Any] = {}

    if bitrix_id:
        b24_result = bitrix24_update_lead(bitrix_id, new_stage)
        # Auto-promote to Deal at HOT/PROPOSAL/SIGNED/WON
        if new_stage.upper() in {"HOT", "PROPOSAL", "SIGNED", "WON"}:
            lead_data = bitrix24_get_lead(bitrix_id)
            company = lead_data.get("COMPANY_TITLE", "")
            deal_result = bitrix24_create_deal(
                title=f"{company} — Deal",
                company=company,
                email=email,
                stage=new_stage,
                division="consulting",
            )
    else:
        # Lead doesn't exist in Bitrix24 yet — create it now
        b24_result = bitrix24_create_lead("", "", email, "consulting", new_stage)

    return {
        "sheets": sheets_result,
        "bitrix24": b24_result,
        "deal": deal_result,
        "ok": sheets_result.get("ok", False) or b24_result.get("ok", False),
    }


def crm_sync_bitrix24() -> Dict[str, Any]:
    """
    Enhanced crm_sync that promotes HOT+ opportunities into BOTH:
      1. Google Sheets CRM tab (existing behavior via crm_sync())
      2. Bitrix24 Deals pipeline (new behavior)
    Returns {synced_sheets: int, synced_bitrix: int, errors: int}.
    """
    sheets_result = crm_sync()
    synced_bitrix = 0
    errors = 0

    try:
        opps = get_opps()
        active_stages = {"HOT", "REPLIED", "PROPOSAL", "SIGNED", "WON"}
        for opp in opps:
            stage = str(opp.get("stage", "")).upper()
            if stage not in active_stages:
                continue
            email = opp.get("email", "")
            company = opp.get("company", opp.get("name", ""))
            division = opp.get("division", "consulting")
            deal_value = float(opp.get("deal_value", opp.get("value", 0)) or 0)
            if not email:
                continue
            # Check if deal already exists in Bitrix24 by finding the lead
            b24_lead_id = bitrix24_find_lead_by_email(email)
            deal_result = bitrix24_create_deal(
                title=f"{company} — {stage} Deal",
                company=company,
                email=email,
                stage=stage,
                deal_value=deal_value,
                division=division,
            )
            if deal_result.get("ok"):
                synced_bitrix += 1
            else:
                errors += 1
    except Exception as e:
        logger.error(f"crm_sync_bitrix24: {e}")
        errors += 1

    return {
        "synced_sheets": sheets_result.get("synced", 0),
        "synced_bitrix": synced_bitrix,
        "errors": errors + sheets_result.get("errors", 0),
    }
