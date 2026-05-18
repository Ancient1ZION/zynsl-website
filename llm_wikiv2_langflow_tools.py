"""
llm_wikiv2_langflow_tools.py

Importlib-loadable Langflow-compatible component and callable tool wrappers
for LLM WikiV2 endpoints.

Environment variables:
  LLM_WIKIV2_BASE_URL   Example: https://your-wiki-v2.example.com/api
  LLM_WIKIV2_API_KEY    Optional bearer token
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional


try:
    from langflow.custom import CustomComponent
except Exception:
    try:
        from langflow import CustomComponent
    except Exception:

        class CustomComponent:  # Safe fallback for importlib discovery outside Langflow.
            pass


class LLMWikiV2Error(Exception):
    """Non-fatal wrapper exception for LLM WikiV2 tool errors."""


class LLMWikiV2Client:
    """Small standard-library HTTP client for LLM WikiV2 endpoints."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 30,
    ) -> None:
        self.base_url = (base_url or os.getenv("LLM_WIKIV2_BASE_URL") or "").rstrip("/")
        self.api_key = api_key or os.getenv("LLM_WIKIV2_API_KEY") or ""
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        try:
            if not self.base_url:
                return {
                    "ok": False,
                    "error": "LLM_WIKIV2_BASE_URL is not configured.",
                }

            url = f"{self.base_url}/{path.lstrip('/')}"
            if query:
                clean_query = {k: v for k, v in query.items() if v is not None}
                url += "?" + urllib.parse.urlencode(clean_query)

            body = None
            headers = {"Accept": "application/json"}
            if payload is not None:
                body = json.dumps(payload).encode("utf-8")
                headers["Content-Type"] = "application/json"

            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            request = urllib.request.Request(
                url=url,
                data=body,
                headers=headers,
                method=method.upper(),
            )

            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                if not raw:
                    return {"ok": True, "data": None}
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = {"raw": raw}
                return {"ok": True, "data": parsed}

        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8")
            except Exception:
                detail = str(exc)
            return {
                "ok": False,
                "error": f"HTTP {exc.code}",
                "detail": detail,
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": exc.__class__.__name__,
                "detail": str(exc),
            }

    def search(
        self, query: str, limit: int = 8, namespace: Optional[str] = None
    ) -> Dict[str, Any]:
        return self._request(
            "GET",
            "/search",
            query={"q": query, "limit": limit, "namespace": namespace},
        )

    def get_page(self, page_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/pages/{urllib.parse.quote(page_id, safe='')}")

    def ask(
        self, question: str, context_limit: int = 6, namespace: Optional[str] = None
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/ask",
            payload={
                "question": question,
                "context_limit": context_limit,
                "namespace": namespace,
            },
        )

    def upsert_page(
        self,
        title: str,
        content: str,
        namespace: str = "default",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/pages",
            payload={
                "title": title,
                "content": content,
                "namespace": namespace,
                "metadata": metadata or {},
            },
        )


_DEFAULT_CLIENT = LLMWikiV2Client()


def llm_wikiv2_search(
    query: str, limit: int = 8, namespace: Optional[str] = None
) -> Dict[str, Any]:
    """
    Search LLM WikiV2 for relevant pages.

    Args:
        query: Search phrase or keywords.
        limit: Maximum number of results to return.
        namespace: Optional wiki namespace filter.

    Returns:
        JSON-compatible dictionary with search results or a non-fatal error payload.
    """
    try:
        return _DEFAULT_CLIENT.search(query=query, limit=limit, namespace=namespace)
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)}


def llm_wikiv2_get_page(page_id: str) -> Dict[str, Any]:
    """
    Retrieve a single LLM WikiV2 page by ID.

    Args:
        page_id: Unique page identifier.

    Returns:
        JSON-compatible dictionary containing the page or a non-fatal error payload.
    """
    try:
        return _DEFAULT_CLIENT.get_page(page_id=page_id)
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)}


def llm_wikiv2_ask(
    question: str,
    context_limit: int = 6,
    namespace: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Ask LLM WikiV2 a natural-language question using wiki context.

    Args:
        question: User question to answer from WikiV2 knowledge.
        context_limit: Maximum number of wiki context chunks/pages to use.
        namespace: Optional wiki namespace filter.

    Returns:
        JSON-compatible answer payload or a non-fatal error payload.
    """
    try:
        return _DEFAULT_CLIENT.ask(
            question=question,
            context_limit=context_limit,
            namespace=namespace,
        )
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)}


def llm_wikiv2_upsert_page(
    title: str,
    content: str,
    namespace: str = "default",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Create or update a page in LLM WikiV2.

    Args:
        title: Page title.
        content: Markdown or plain text page body.
        namespace: Wiki namespace.
        metadata: Optional structured metadata.

    Returns:
        JSON-compatible write result or a non-fatal error payload.
    """
    try:
        return _DEFAULT_CLIENT.upsert_page(
            title=title,
            content=content,
            namespace=namespace,
            metadata=metadata,
        )
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)}


def get_tools() -> List[Callable[..., Dict[str, Any]]]:
    """
    Return importlib-discoverable callable tools for agent runtimes.

    Returns:
        List of plain Python callables with docstrings and typed signatures.
    """
    return [
        llm_wikiv2_search,
        llm_wikiv2_get_page,
        llm_wikiv2_ask,
        llm_wikiv2_upsert_page,
    ]


class LLMWikiV2ToolsComponent(CustomComponent):
    """Langflow CustomComponent exposing LLM WikiV2 callable tools."""

    display_name = "LLM WikiV2 Tools"
    description = "Adds searchable, callable LLM WikiV2 tools to the runtime."
    icon = "book-open"

    def build_config(self) -> Dict[str, Any]:
        return {
            "base_url": {
                "display_name": "WikiV2 Base URL",
                "required": False,
                "info": "Defaults to LLM_WIKIV2_BASE_URL.",
            },
            "api_key": {
                "display_name": "API Key",
                "required": False,
                "password": True,
                "info": "Defaults to LLM_WIKIV2_API_KEY.",
            },
            "timeout": {
                "display_name": "Timeout Seconds",
                "required": False,
                "value": 30,
            },
        }

    def build(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 30,
    ) -> List[Callable[..., Dict[str, Any]]]:
        try:
            client = LLMWikiV2Client(
                base_url=base_url, api_key=api_key, timeout=int(timeout)
            )

            def search(
                query: str, limit: int = 8, namespace: Optional[str] = None
            ) -> Dict[str, Any]:
                """Search LLM WikiV2 by query, optional namespace, and result limit."""
                try:
                    return client.search(query=query, limit=limit, namespace=namespace)
                except Exception as exc:
                    return {
                        "ok": False,
                        "error": exc.__class__.__name__,
                        "detail": str(exc),
                    }

            def get_page(page_id: str) -> Dict[str, Any]:
                """Fetch one LLM WikiV2 page by page_id."""
                try:
                    return client.get_page(page_id=page_id)
                except Exception as exc:
                    return {
                        "ok": False,
                        "error": exc.__class__.__name__,
                        "detail": str(exc),
                    }

            def ask(
                question: str,
                context_limit: int = 6,
                namespace: Optional[str] = None,
            ) -> Dict[str, Any]:
                """Ask LLM WikiV2 a question using retrieved wiki context."""
                try:
                    return client.ask(
                        question=question,
                        context_limit=context_limit,
                        namespace=namespace,
                    )
                except Exception as exc:
                    return {
                        "ok": False,
                        "error": exc.__class__.__name__,
                        "detail": str(exc),
                    }

            def upsert_page(
                title: str,
                content: str,
                namespace: str = "default",
                metadata: Optional[Dict[str, Any]] = None,
            ) -> Dict[str, Any]:
                """Create or update an LLM WikiV2 page."""
                try:
                    return client.upsert_page(
                        title=title,
                        content=content,
                        namespace=namespace,
                        metadata=metadata,
                    )
                except Exception as exc:
                    return {
                        "ok": False,
                        "error": exc.__class__.__name__,
                        "detail": str(exc),
                    }

            return [search, get_page, ask, upsert_page]

        except Exception as exc:

            def component_error_tool() -> Dict[str, Any]:
                """Return component initialization error without crashing Langflow."""
                return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)}

            return [component_error_tool]
