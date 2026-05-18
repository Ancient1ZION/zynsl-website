"""
noah_code_editor_graphify_tools.py

Importlib-loadable tools that let Noah and the agent runtime open code editors
and use Graphify without relying on PATH being configured.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


WORKSPACE_ROOT = Path(
    os.getenv(
        "ZYN_WORKSPACE_ROOT",
        r"C:\Users\coach\Documents\Codex\2026-05-17\repair-dashboard-https-ancient1zion-github-io",
    )
)
OPENCODE_EXE = Path(
    os.getenv(
        "OPENCODE_EXE",
        r"C:\Users\coach\AppData\Local\Programs\OpenCode\OpenCode.exe",
    )
)
VSCODE_CMD = Path(
    os.getenv(
        "VSCODE_CMD",
        r"C:\Users\coach\AppData\Local\Programs\Microsoft VS Code\bin\code.cmd",
    )
)
GRAPHIFY_EXE = Path(
    os.getenv(
        "GRAPHIFY_EXE",
        r"C:\Users\coach\AppData\Local\Python\pythoncore-3.14-64\Scripts\graphify.exe",
    )
)


def _safe_path(path: Optional[str] = None) -> Path:
    base = WORKSPACE_ROOT.resolve()
    target = (base / (path or ".")).resolve() if path else base
    try:
        target.relative_to(base)
    except ValueError:
        raise ValueError(f"Path is outside the allowed workspace: {target}")
    return target


def _run(
    command: List[str],
    cwd: Optional[Path] = None,
    timeout: int = 120,
    background: bool = False,
) -> Dict[str, Any]:
    try:
        if background:
            proc = subprocess.Popen(
                command,
                cwd=str(cwd or WORKSPACE_ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
            )
            return {"ok": True, "pid": proc.pid, "command": command}

        proc = subprocess.run(
            command,
            cwd=str(cwd or WORKSPACE_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "command": command,
        }
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc), "command": command}


def open_opencode_editor(path: str = ".") -> Dict[str, Any]:
    """
    Open OpenCode Desktop against a workspace-relative path.

    Args:
        path: File or directory path relative to the configured workspace.

    Returns:
        Non-fatal launch result with process ID or error details.
    """
    try:
        if not OPENCODE_EXE.exists():
            return {"ok": False, "error": "OpenCode executable not found", "path": str(OPENCODE_EXE)}
        target = _safe_path(path)
        return _run([str(OPENCODE_EXE), str(target)], cwd=WORKSPACE_ROOT, background=True)
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)}


def open_vscode_editor(path: str = ".") -> Dict[str, Any]:
    """
    Open VS Code against a workspace-relative path.

    Args:
        path: File or directory path relative to the configured workspace.

    Returns:
        Non-fatal launch result with process ID or error details.
    """
    try:
        if not VSCODE_CMD.exists():
            return {"ok": False, "error": "VS Code command not found", "path": str(VSCODE_CMD)}
        target = _safe_path(path)
        return _run([str(VSCODE_CMD), str(target)], cwd=WORKSPACE_ROOT, background=True)
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)}


def graphify_update_project(path: str = ".", force: bool = False, no_cluster: bool = False) -> Dict[str, Any]:
    """
    Rebuild or update the Graphify graph for a workspace-relative project path.

    Args:
        path: Project path relative to the configured workspace.
        force: Allow overwriting graph output after large refactors.
        no_cluster: Skip clustering and write raw extraction only.

    Returns:
        Graphify command result with stdout/stderr captured.
    """
    try:
        if not GRAPHIFY_EXE.exists():
            return {"ok": False, "error": "Graphify executable not found", "path": str(GRAPHIFY_EXE)}
        target = _safe_path(path)
        cmd = [str(GRAPHIFY_EXE), "update", str(target)]
        if force:
            cmd.append("--force")
        if no_cluster:
            cmd.append("--no-cluster")
        return _run(cmd, cwd=target, timeout=600)
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)}


def graphify_query(question: str, graph_path: str = "graphify-out/graph.json", budget: int = 2000) -> Dict[str, Any]:
    """
    Query a Graphify graph with a natural-language question.

    Args:
        question: Question to ask of the project graph.
        graph_path: Workspace-relative path to graph.json.
        budget: Token budget for the answer.

    Returns:
        Graphify query result with stdout/stderr captured.
    """
    try:
        if not GRAPHIFY_EXE.exists():
            return {"ok": False, "error": "Graphify executable not found", "path": str(GRAPHIFY_EXE)}
        graph = _safe_path(graph_path)
        cmd = [str(GRAPHIFY_EXE), "query", question, "--graph", str(graph), "--budget", str(int(budget))]
        return _run(cmd, cwd=WORKSPACE_ROOT, timeout=180)
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)}


def graphify_explain(node: str, graph_path: str = "graphify-out/graph.json") -> Dict[str, Any]:
    """
    Explain a Graphify node and its neighbors.

    Args:
        node: Node label or identifier to explain.
        graph_path: Workspace-relative path to graph.json.

    Returns:
        Graphify explain result with stdout/stderr captured.
    """
    try:
        if not GRAPHIFY_EXE.exists():
            return {"ok": False, "error": "Graphify executable not found", "path": str(GRAPHIFY_EXE)}
        graph = _safe_path(graph_path)
        return _run([str(GRAPHIFY_EXE), "explain", node, "--graph", str(graph)], cwd=WORKSPACE_ROOT)
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)}


def graphify_install_platform(platform: str = "opencode") -> Dict[str, Any]:
    """
    Install Graphify integration for a supported local platform.

    Args:
        platform: Graphify platform target, such as codex, opencode, cursor, or vscode.

    Returns:
        Graphify install command result.
    """
    try:
        allowed = {"codex", "opencode", "cursor", "vscode", "claude", "aider"}
        if platform not in allowed:
            return {"ok": False, "error": f"Unsupported platform: {platform}", "allowed": sorted(allowed)}
        if not GRAPHIFY_EXE.exists():
            return {"ok": False, "error": "Graphify executable not found", "path": str(GRAPHIFY_EXE)}
        return _run([str(GRAPHIFY_EXE), "install", "--platform", platform], cwd=WORKSPACE_ROOT)
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)}


def get_tools() -> List[Callable[..., Dict[str, Any]]]:
    """Return importlib-discoverable code editor and Graphify tools."""
    return [
        open_opencode_editor,
        open_vscode_editor,
        graphify_update_project,
        graphify_query,
        graphify_explain,
        graphify_install_platform,
    ]
