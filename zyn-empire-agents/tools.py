"""Tool registry — what agents can actually do.

Every public tool follows three contracts:

  1. NEVER raise on a foreseeable failure. Return a typed empty value or a
     {"error": "..."} dict. Agents check truthiness and log to drift.
  2. ALWAYS log warnings/errors via loguru with the tool name as a tag.
  3. ALWAYS bound network calls with a timeout. No naked requests.get/post.

This is the trust boundary: above this line, the orchestrator + LLM see only
clean results or empty lists. Exceptions live below this line.
"""
from __future__ import annotations

import os
import json
import smtplib
import ssl
import time
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

# Network defaults — every outbound call must use one of these or stricter.
DEFAULT_TIMEOUT = (5, 25)  # (connect, read) seconds
SHORT_TIMEOUT = (5, 10)


# ---------------------------------------------------------------------------
# Google Sheets client (lazy, single-shot)
# ---------------------------------------------------------------------------

_gs_client = None


def gs():
    """Return a memoized gspread client, or raise once at startup if SA missing."""
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


def _open_tab(tab: str):
    """Open a worksheet by tab name. Returns None if missing."""
    try:
        sh = gs().open_by_key(SHEET_ID)
        return sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        logger.warning(f"sheets: tab {tab!r} not found in sheet {SHEET_ID}")
        return None
    except Exception as e:
        logger.error(f"sheets._open_tab({tab}): {e}")
        return None


# ---------------------------------------------------------------------------
# Sheets — read / write / append, all soft-fail
# ---------------------------------------------------------------------------


def sheets_read(tab: str, range_a1: Optional[str] = None) -> List[List[str]]:
    """Read a tab (or a range within it). Returns [] on any failure."""
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
    """Overwrite a range. Returns {ok: bool, error?: str}."""
    if not values:
        return {"ok": False, "error": "empty values"}
    ws = _open_tab(tab)
    if ws is None:
        return {"ok": False, "error": f"tab {tab!r} not found"}
    try:
        ws.update(range_a1, values, value_input_option="USER_ENTERED")
        return {"ok": True, "rows": len(values)}
    except Exception as e:
        logger.error(f"sheets_write({tab}, {range_a1}): {e}")
        return {"ok": False, "error": str(e)}


def sheets_append(tab: str, row: List[Any]) -> Dict[str, Any]:
    """Append one row. Returns {ok: bool, error?: str}."""
    if not row:
        return {"ok": False, "error": "empty row"}
    ws = _open_tab(tab)
    if ws is None:
        return {"ok": False, "error": f"tab {tab!r} not found"}
    try:
        ws.append_row(row, value_input_option="USER_ENTERED")
        return {"ok": True}
    except Exception as e:
        logger.error(f"sheets_append({tab}): {e}")
        return {"ok": False, "error": str(e)}


def sheets_stop_signal() -> bool:
    """Read CONTROL!A1. True if the kill switch is engaged."""
    rows = sheets_read("CONTROL", "A1")
    if not rows or not rows[0]:
        return False
    return str(rows[0][0]).strip().upper() == "STOP"


# ---------------------------------------------------------------------------
# Discord (via GAS proxy, never direct webhook URLs)
# ---------------------------------------------------------------------------


def discord_notify(
    agent: str, message: str, extra: Optional[Dict] = None
) -> Dict[str, Any]:
    """Send a message via the GAS proxy. Returns {ok, dropped?, error?}."""
    if not GAS_PROXY_URL:
        logger.warning("discord_notify: GAS_PROXY_URL not set, skipping")
        return {"ok": False, "error": "GAS_PROXY_URL not set"}
    payload = {
        "channel": (agent or "ops").lower(),
        "username": agent or "agent",
        "content": (message or "")[:1900],
    }
    if extra:
        payload["embeds"] = extra.get("embeds")
    url = GAS_PROXY_URL + ("?key=" + GAS_PROXY_KEY if GAS_PROXY_KEY else "")
    try:
        r = requests.post(url, json=payload, timeout=SHORT_TIMEOUT)
        if not r.ok:
            logger.warning(f"discord_notify: proxy returned {r.status_code}")
            return {"ok": False, "error": f"http {r.status_code}"}
        try:
            data = r.json()
        except ValueError:
            return {"ok": True}
        if data.get("dropped"):
            logger.info(f"discord_notify: STOP active, message dropped for {agent}")
        return data
    except requests.Timeout:
        logger.error("discord_notify: timeout")
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        logger.error(f"discord_notify: {e}")
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Web search (DuckDuckGo, free, no key)
# ---------------------------------------------------------------------------


def web_search(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """Free DuckDuckGo search. Returns [] on any failure or empty result."""
    if not query or not query.strip():
        return []
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = ddgs.text(query, max_results=max_results) or []
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", ""),
                }
                for r in results
            ]
    except Exception as e:
        logger.error(f"web_search({query!r}): {e}")
        return []


# ---------------------------------------------------------------------------
# sam.gov opportunities search
# ---------------------------------------------------------------------------


def sam_gov_search(
    naics: str, posted_from: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Search sam.gov for opportunities. Returns [] on any failure."""
    api_key = os.getenv("SAM_GOV_API_KEY", "")
    if not api_key:
        logger.warning("sam_gov_search: SAM_GOV_API_KEY not set")
        return []
    if not naics:
        return []
    params: Dict[str, Any] = {"api_key": api_key, "ncode": naics, "limit": 25}
    if posted_from:
        params["postedFrom"] = posted_from
    url = "https://api.sam.gov/opportunities/v2/search"
    # One retry on 5xx, no retry on 4xx (don't hammer rate limits)
    for attempt in (1, 2):
        try:
            r = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        except requests.Timeout:
            logger.warning(f"sam_gov_search: timeout (attempt {attempt})")
            if attempt == 2:
                return []
            time.sleep(2)
            continue
        except Exception as e:
            logger.error(f"sam_gov_search: {e}")
            return []
        if r.status_code == 429:
            logger.warning("sam_gov_search: rate limited")
            return []
        if 400 <= r.status_code < 500:
            logger.warning(f"sam_gov_search: client error {r.status_code}")
            return []
        if r.status_code >= 500:
            if attempt == 2:
                logger.error(f"sam_gov_search: server error {r.status_code}")
                return []
            time.sleep(2)
            continue
        try:
            return r.json().get("opportunitiesData", []) or []
        except ValueError as e:
            logger.error(f"sam_gov_search: invalid JSON ({e})")
            return []
    return []


# ---------------------------------------------------------------------------
# Email (Gmail SMTP)
# ---------------------------------------------------------------------------


def send_email(to: str, subject: str, body: str) -> Dict[str, Any]:
    """Send email via Gmail SMTP. Returns {ok, error?}."""
    user = os.getenv("GMAIL_USER", "")
    pwd = os.getenv("GMAIL_APP_PASSWORD", "")
    if not user or not pwd:
        logger.warning("send_email: GMAIL_USER / GMAIL_APP_PASSWORD not set")
        return {"ok": False, "error": "smtp credentials not set"}
    if not to or "@" not in to:
        return {"ok": False, "error": "invalid recipient"}
    if not subject:
        subject = "(no subject)"
    msg = MIMEText(body or "", _charset="utf-8")
    msg["Subject"] = subject[:200]
    msg["From"] = user
    msg["To"] = to
    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=20) as s:
            s.login(user, pwd)
            s.send_message(msg)
        return {"ok": True}
    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"send_email: auth failed ({e})")
        return {"ok": False, "error": "smtp auth failed"}
    except smtplib.SMTPRecipientsRefused as e:
        logger.warning(f"send_email: recipient refused ({e})")
        return {"ok": False, "error": "recipient refused"}
    except (smtplib.SMTPException, OSError, TimeoutError) as e:
        logger.error(f"send_email: {e}")
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Local files (sandbox to project dir)
# ---------------------------------------------------------------------------


def file_read(path: str) -> str:
    """Read a UTF-8 file. Returns '' on any failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.warning(f"file_read: {path!r} not found")
        return ""
    except Exception as e:
        logger.error(f"file_read({path}): {e}")
        return ""


def file_write(path: str, content: str) -> Dict[str, Any]:
    """Write a UTF-8 file (creating dirs as needed). Returns {ok, error?}."""
    try:
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content or "")
        return {"ok": True, "bytes": len(content or "")}
    except Exception as e:
        logger.error(f"file_write({path}): {e}")
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


TOOLS = {
    "sheets_read": sheets_read,
    "sheets_write": sheets_write,
    "sheets_append": sheets_append,
    "sheets_stop_signal": sheets_stop_signal,
    "discord_notify": discord_notify,
    "web_search": web_search,
    "sam_gov_search": sam_gov_search,
    "send_email": send_email,
    "file_read": file_read,
    "file_write": file_write,
}
