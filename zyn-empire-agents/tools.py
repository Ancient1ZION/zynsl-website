"""Tool registry — what agents can actually do."""
import os, json, smtplib, ssl
from email.mime.text import MIMEText
from typing import Optional, List, Dict, Any
import requests
from loguru import logger
import gspread
from google.oauth2.service_account import Credentials

SHEET_ID = os.getenv("SHEET_ID","1WHN438mjORT4HnGiXapWv78uomVy6KMhHsl0llxaeQk")
GAS_PROXY_URL = os.getenv("GAS_PROXY_URL","")
SA_PATH = os.getenv("GOOGLE_SA_JSON_PATH","")

_gs_client = None
def gs():
    global _gs_client
    if _gs_client: return _gs_client
    if not SA_PATH or not os.path.exists(SA_PATH):
        raise RuntimeError(f"Service account JSON not found at {SA_PATH}")
    scopes=["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
    creds=Credentials.from_service_account_file(SA_PATH, scopes=scopes)
    _gs_client = gspread.authorize(creds)
    return _gs_client

def sheets_read(tab: str, range_a1: Optional[str] = None) -> List[List[str]]:
    sh = gs().open_by_key(SHEET_ID)
    ws = sh.worksheet(tab)
    if range_a1:
        return ws.get(range_a1)
    return ws.get_all_values()

def sheets_write(tab: str, range_a1: str, values: List[List[Any]]) -> Dict:
    sh = gs().open_by_key(SHEET_ID)
    ws = sh.worksheet(tab)
    ws.update(range_name=range_a1, values=values)
    return {"ok": True, "tab": tab, "range": range_a1, "rows": len(values)}

def sheets_append(tab: str, row: List[Any]) -> Dict:
    sh = gs().open_by_key(SHEET_ID)
    ws = sh.worksheet(tab)
    ws.append_row(row, value_input_option="USER_ENTERED")
    return {"ok": True, "tab": tab, "row": row}

def discord_notify(agent: str, message: str, extra: Optional[Dict]=None) -> Dict:
    if not GAS_PROXY_URL:
        logger.warning(f"GAS_PROXY_URL not set, skipping discord_notify({agent})")
        return {"skipped": True}
    payload = {"type":"discord","agent":agent,"message":message,"extra":extra or {}}
    try:
        r = requests.post(GAS_PROXY_URL, data=json.dumps(payload), headers={"Content-Type":"text/plain"}, timeout=15)
        return {"ok": r.ok, "status": r.status_code, "body": r.text[:200]}
    except Exception as e:
        logger.error(f"discord_notify failed: {e}")
        return {"error": str(e)}

def web_search(query: str, max_results: int = 5) -> List[Dict]:
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            return [{"title":r.get("title",""),"url":r.get("href",""),"snippet":r.get("body","")} for r in ddgs.text(query, max_results=max_results)]
    except Exception as e:
        logger.error(f"web_search failed: {e}")
        return []

def sam_gov_search(naics: str, posted_from: Optional[str]=None) -> List[Dict]:
    api_key = os.getenv("SAM_GOV_API_KEY","")
    if not api_key:
        logger.warning("SAM_GOV_API_KEY not set")
        return []
    params = {"api_key": api_key, "ncode": naics, "limit": 25}
    if posted_from: params["postedFrom"] = posted_from
    try:
        r = requests.get("https://api.sam.gov/opportunities/v2/search", params=params, timeout=30)
        if not r.ok: return []
        return r.json().get("opportunitiesData", [])
    except Exception as e:
        logger.error(f"sam_gov_search failed: {e}")
        return []

def send_email(to: str, subject: str, body: str) -> Dict:
    user = os.getenv("GMAIL_USER","")
    pwd = os.getenv("GMAIL_APP_PASS","")
    if not user or not pwd:
        return {"skipped": True, "reason": "Gmail creds not set"}
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())
    return {"ok": True, "to": to, "subject": subject}

def file_read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def file_write(path: str, content: str) -> Dict:
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"ok": True, "path": path, "bytes": len(content)}

TOOLS = {
    "sheets_read": sheets_read,
    "sheets_write": sheets_write,
    "sheets_append": sheets_append,
    "discord_notify": discord_notify,
    "web_search": web_search,
    "sam_gov_search": sam_gov_search,
    "send_email": send_email,
    "file_read": file_read,
    "file_write": file_write,
}
