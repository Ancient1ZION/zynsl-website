"""
noah_discord_website_workflow_tools.py

Importlib-loadable workflow tools that connect Noah/all agents to the ZYN
Discord channel and public website. Discord write/read calls use environment
credentials when available and fail safely when credentials are missing.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional


DISCORD_GUILD_ID = os.getenv("ZYN_DISCORD_GUILD_ID", "1490888092708769995")
DISCORD_CHANNEL_ID = os.getenv("ZYN_DISCORD_CHANNEL_ID", "1492443239314362438")
DISCORD_CHANNEL_URL = os.getenv(
    "ZYN_DISCORD_CHANNEL_URL",
    f"https://discord.com/channels/{DISCORD_GUILD_ID}/{DISCORD_CHANNEL_ID}",
)
WEBSITE_URL = os.getenv("ZYN_WEBSITE_URL", "https://ancient1zion.github.io/zynsl-website/")
DASHBOARD_URL = os.getenv(
    "ZYN_DASHBOARD_URL",
    "https://ancient1zion.github.io/zynsl-website/dashboard.html",
)


def _json_request(
    url: str,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 20,
    allow_insecure_ssl_retry: bool = True,
) -> Dict[str, Any]:
    try:
        data = None
        req_headers = {"Accept": "application/json", **(headers or {})}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.URLError as exc:
            if "CERTIFICATE_VERIFY_FAILED" not in str(exc) or not allow_insecure_ssl_retry:
                raise
            context = ssl._create_unverified_context()
            response = urllib.request.urlopen(request, timeout=timeout, context=context)

        with response:
            raw = response.read().decode("utf-8", errors="replace")
            parsed: Any
            try:
                parsed = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            return {
                "ok": True,
                "status": getattr(response, "status", None),
                "url": response.geturl(),
                "content_type": response.headers.get("content-type"),
                "data": parsed,
            }
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        return {"ok": False, "status": exc.code, "error": f"HTTP {exc.code}", "detail": detail}
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)}


def get_workflow_links() -> Dict[str, Any]:
    """
    Return canonical Discord, website, and dashboard links for Noah workflow.

    Returns:
        JSON-compatible workflow link payload.
    """
    return {
        "ok": True,
        "discord_guild_id": DISCORD_GUILD_ID,
        "discord_channel_id": DISCORD_CHANNEL_ID,
        "discord_channel_url": DISCORD_CHANNEL_URL,
        "website_url": WEBSITE_URL,
        "dashboard_url": DASHBOARD_URL,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def website_health_check(url: str = WEBSITE_URL) -> Dict[str, Any]:
    """
    Check whether the public website or dashboard is reachable.

    Args:
        url: Website URL to check.

    Returns:
        Non-fatal status payload with HTTP details.
    """
    result = _json_request(url, headers={"Accept": "text/html,application/json"}, timeout=20)
    if result.get("ok"):
        return {
            "ok": True,
            "url": result.get("url"),
            "status": result.get("status"),
            "content_type": result.get("content_type"),
        }
    return result


def discord_send_message(
    message: str,
    webhook_url: Optional[str] = None,
    channel_id: str = DISCORD_CHANNEL_ID,
    username: str = "ZYN Noah",
) -> Dict[str, Any]:
    """
    Send a message to Discord using a webhook URL or bot token.

    Args:
        message: Message content to send.
        webhook_url: Optional Discord webhook URL. Defaults to ZYN_DISCORD_WEBHOOK_URL.
        channel_id: Discord channel ID for bot-token sends.
        username: Webhook display username.

    Returns:
        Send result. If credentials are missing, returns ok=False without crashing.
    """
    try:
        text = str(message or "").strip()
        if not text:
            return {"ok": False, "error": "Message is empty."}

        hook = webhook_url or os.getenv("ZYN_DISCORD_WEBHOOK_URL") or os.getenv("DISCORD_WEBHOOK_URL")
        if hook:
            return _json_request(
                hook,
                method="POST",
                payload={"content": text[:1900], "username": username},
                headers={"Accept": "application/json"},
            )

        token = os.getenv("ZYN_DISCORD_BOT_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")
        if token:
            return _json_request(
                f"https://discord.com/api/v10/channels/{urllib.parse.quote(channel_id)}/messages",
                method="POST",
                payload={"content": text[:1900]},
                headers={"Authorization": f"Bot {token}"},
            )

        return {
            "ok": False,
            "error": "Discord credentials missing.",
            "detail": "Set ZYN_DISCORD_WEBHOOK_URL or ZYN_DISCORD_BOT_TOKEN to enable sends.",
            "discord_channel_url": DISCORD_CHANNEL_URL,
        }
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)}


def discord_fetch_recent(channel_id: str = DISCORD_CHANNEL_ID, limit: int = 10) -> Dict[str, Any]:
    """
    Fetch recent Discord channel messages using a bot token.

    Args:
        channel_id: Discord channel ID.
        limit: Number of messages to fetch, capped at 50.

    Returns:
        Recent message payload or a safe credential-missing error.
    """
    try:
        token = os.getenv("ZYN_DISCORD_BOT_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")
        if not token:
            return {
                "ok": False,
                "error": "Discord bot token missing.",
                "detail": "Set ZYN_DISCORD_BOT_TOKEN to enable channel reads.",
                "discord_channel_url": DISCORD_CHANNEL_URL,
            }
        safe_limit = max(1, min(int(limit), 50))
        url = (
            f"https://discord.com/api/v10/channels/{urllib.parse.quote(channel_id)}/messages"
            f"?limit={safe_limit}"
        )
        return _json_request(url, headers={"Authorization": f"Bot {token}"})
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)}


def workflow_notify(
    event: str,
    message: str,
    severity: str = "info",
    website_url: str = WEBSITE_URL,
) -> Dict[str, Any]:
    """
    Send a workflow event summary to Discord when credentials are configured.

    Args:
        event: Short event name, such as deploy, error, lead, or repair.
        message: Event details.
        severity: info, ok, warn, or error.
        website_url: Related website URL.

    Returns:
        Notification result plus canonical workflow links.
    """
    prefix = {
        "ok": "[OK]",
        "warn": "[WARN]",
        "error": "[ERROR]",
        "err": "[ERROR]",
        "info": "[INFO]",
    }.get(str(severity).lower(), "[INFO]")
    body = f"{prefix} {event}: {message}\nWebsite: {website_url}\nDashboard: {DASHBOARD_URL}"
    result = discord_send_message(body)
    result["workflow_links"] = get_workflow_links()
    return result


def get_tools() -> List[Callable[..., Dict[str, Any]]]:
    """Return importlib-discoverable Discord, website, and workflow tools."""
    return [
        get_workflow_links,
        website_health_check,
        discord_send_message,
        discord_fetch_recent,
        workflow_notify,
    ]
