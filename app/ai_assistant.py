"""
ai_assistant.py — the brains behind the Admin AI Assistant.

Capabilities (all admin-gated at the route layer):
  * Scan / read the repository's frontend + backend source files.
  * Send a prompt to an LLM (Anthropic or OpenAI) to audit the codebase for
    logical/syntax bugs and propose fixes.
  * Return a unified diff PREVIEW of any proposed file changes.

Hard human-in-the-loop safeguard
---------------------------------
This module NEVER writes to disk as part of a proposal. The LLM's suggested
edits are held in an in-memory pending store keyed by ``proposal_id``. Files are
only written when the admin explicitly calls :func:`apply_proposal` (wired to an
"Approve" button); :func:`deny_proposal` discards them.

Sandboxing
----------
Every path is resolved and confirmed to live inside the repository root, and a
blocklist prevents touching secrets, VCS internals, binaries and dependencies.
The LLM API key is read from server env only and is never returned to the UI.
"""

from __future__ import annotations

import os
import json
import time
import difflib
import logging
import secrets
import threading
from datetime import datetime, timezone

import requests

logger = logging.getLogger("alphabot.ai")

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Only these extensions are considered source the assistant may read/edit.
_ALLOWED_EXT = {".py", ".js", ".css", ".html", ".txt", ".md", ".json", ".toml", ".cfg", ".ini", ".yml", ".yaml"}

# Directories/paths that are always off-limits.
_BLOCKED_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
_BLOCKED_NAMES = {".env", ".env.local", ".env.production"}
_BLOCKED_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".key", ".pem", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".docx", ".pdf")

MAX_FILE_BYTES = 400_000
MAX_CONTEXT_CHARS = int(os.getenv("AI_MAX_CONTEXT_CHARS", "180000"))

# proposal_id -> {summary, findings, edits, diffs, created_at}
_PENDING: dict[str, dict] = {}
_PENDING_LOCK = threading.Lock()


# ────────────────────────────────────────────────────────────────────
# Path sandbox
# ────────────────────────────────────────────────────────────────────
def _is_blocked(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    if any(p in _BLOCKED_DIRS for p in parts):
        return True
    name = parts[-1]
    if name in _BLOCKED_NAMES:
        return True
    if any(name.lower().endswith(s) for s in _BLOCKED_SUFFIXES):
        return True
    return False


def safe_path(rel: str) -> str:
    """Resolve ``rel`` inside the repo root or raise ValueError."""
    rel = (rel or "").strip().lstrip("/")
    if not rel:
        raise ValueError("Empty path.")
    abs_path = os.path.abspath(os.path.join(REPO_ROOT, rel))
    if abs_path != REPO_ROOT and not abs_path.startswith(REPO_ROOT + os.sep):
        raise ValueError("Path escapes repository root.")
    if _is_blocked(os.path.relpath(abs_path, REPO_ROOT)):
        raise ValueError(f"Path '{rel}' is blocked for safety.")
    ext = os.path.splitext(abs_path)[1].lower()
    if ext and ext not in _ALLOWED_EXT:
        raise ValueError(f"File type '{ext}' is not editable by the assistant.")
    return abs_path


def list_repo_files() -> list[dict]:
    out: list[dict] = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in _BLOCKED_DIRS]
        for f in files:
            abs_path = os.path.join(root, f)
            rel = os.path.relpath(abs_path, REPO_ROOT)
            if _is_blocked(rel):
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext and ext not in _ALLOWED_EXT:
                continue
            try:
                size = os.path.getsize(abs_path)
            except OSError:
                continue
            out.append({"path": rel.replace("\\", "/"), "size": size})
    out.sort(key=lambda x: x["path"])
    return out


def read_file(rel: str) -> str:
    abs_path = safe_path(rel)
    if not os.path.isfile(abs_path):
        raise ValueError(f"File not found: {rel}")
    if os.path.getsize(abs_path) > MAX_FILE_BYTES:
        raise ValueError(f"File too large to load: {rel}")
    with open(abs_path, "r", encoding="utf-8") as fh:
        return fh.read()


def write_file(rel: str, content: str) -> None:
    abs_path = safe_path(rel)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as fh:
        fh.write(content)


def unified_diff(rel: str, old: str, new: str) -> str:
    diff = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}",
    )
    return "".join(diff)


# ────────────────────────────────────────────────────────────────────
# LLM provider
# ────────────────────────────────────────────────────────────────────
def provider_status() -> dict:
    anthropic = bool(os.getenv("ANTHROPIC_API_KEY"))
    openai = bool(os.getenv("OPENAI_API_KEY"))
    provider = "anthropic" if anthropic else ("openai" if openai else None)
    return {"configured": bool(provider), "provider": provider}


def _call_anthropic(system: str, user: str) -> str:
    key = os.getenv("ANTHROPIC_API_KEY")
    model = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 8000,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=120,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Anthropic API error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    parts = data.get("content", [])
    return "".join(p.get("text", "") for p in parts if p.get("type") == "text")


def _call_openai(system: str, user: str) -> str:
    key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-4o")
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
        },
        timeout=120,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI API error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def call_llm(system: str, user: str) -> str:
    status = provider_status()
    if not status["configured"]:
        raise RuntimeError(
            "No LLM provider configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY "
            "in the server environment (Cursor/Railway variables)."
        )
    if status["provider"] == "anthropic":
        return _call_anthropic(system, user)
    return _call_openai(system, user)


# ────────────────────────────────────────────────────────────────────
# Context building + audit protocol
# ────────────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """You are a senior software engineer embedded in a FastAPI + \
vanilla-JS trading application. You can audit the provided source files for \
logical errors, syntax bugs, security issues and correctness problems, and you \
may propose precise fixes.

You MUST respond with a single JSON object and nothing else, matching this schema:
{
  "summary": "one paragraph overview of what you found / did",
  "findings": ["short bullet describing each issue or observation"],
  "edits": [
    {"path": "relative/file/path", "content": "THE COMPLETE NEW CONTENTS OF THE FILE"}
  ]
}

Rules:
- Only include a file in "edits" if you are changing it; provide the ENTIRE new file content, not a diff.
- Never invent files that were not provided unless explicitly asked to create one.
- If the user only asks a question or for an audit with no changes, return an empty "edits" array.
- Keep changes minimal and focused on the request. Do not reformat unrelated code.
- Respond with raw JSON only — no markdown fences, no prose outside the JSON.
"""


def _build_context(paths: list[str] | None) -> tuple[str, list[str]]:
    files = list_repo_files()
    if paths:
        wanted = {p.replace("\\", "/").lstrip("/") for p in paths}
        files = [f for f in files if f["path"] in wanted]
    tree = "\n".join(f"- {f['path']} ({f['size']} bytes)" for f in list_repo_files())
    blocks: list[str] = []
    included: list[str] = []
    total = len(tree)
    for f in files:
        try:
            content = read_file(f["path"])
        except Exception:
            continue
        block = f"\n===== FILE: {f['path']} =====\n{content}\n"
        if total + len(block) > MAX_CONTEXT_CHARS:
            continue
        blocks.append(block)
        included.append(f["path"])
        total += len(block)
    context = f"REPOSITORY FILE TREE:\n{tree}\n\nSOURCE FILES:\n{''.join(blocks)}"
    return context, included


def _parse_llm_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        # Strip a leading ```json / ``` fence and trailing fence if present.
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: text.rfind("```")]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def audit(prompt: str, paths: list[str] | None = None) -> dict:
    """
    Run an audit/edit request through the LLM and return a structured result
    INCLUDING per-file unified diffs. Any proposed edits are stored as a pending
    proposal that must be explicitly approved before files are written.
    """
    context, included = _build_context(paths)
    user_msg = (
        f"{context}\n\n===== TASK FROM ADMIN =====\n{prompt}\n\n"
        "Audit the relevant code and respond with the required JSON object."
    )
    raw = call_llm(_SYSTEM_PROMPT, user_msg)
    try:
        parsed = _parse_llm_json(raw)
    except Exception as e:
        return {
            "summary": "The assistant returned a response that could not be parsed as JSON.",
            "findings": [str(e)],
            "raw": raw[:4000],
            "edits": [],
            "proposal_id": None,
            "files_in_context": included,
        }

    edits_in = parsed.get("edits") or []
    diffs = []
    sanitized_edits = []
    for e in edits_in:
        rel = (e.get("path") or "").strip()
        new_content = e.get("content")
        if not rel or new_content is None:
            continue
        try:
            safe_path(rel)  # validate
        except ValueError as ve:
            diffs.append({"path": rel, "error": str(ve), "diff": ""})
            continue
        try:
            old = read_file(rel)
        except Exception:
            old = ""  # new file
        diffs.append({
            "path": rel,
            "diff": unified_diff(rel, old, new_content),
            "is_new": old == "",
        })
        sanitized_edits.append({"path": rel, "content": new_content})

    proposal_id = None
    if sanitized_edits:
        proposal_id = secrets.token_hex(8)
        with _PENDING_LOCK:
            _PENDING[proposal_id] = {
                "summary": parsed.get("summary", ""),
                "findings": parsed.get("findings", []),
                "edits": sanitized_edits,
                "diffs": diffs,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

    return {
        "summary": parsed.get("summary", ""),
        "findings": parsed.get("findings", []),
        "edits": [{"path": e["path"]} for e in sanitized_edits],
        "diffs": diffs,
        "proposal_id": proposal_id,
        "files_in_context": included,
    }


def get_proposal(proposal_id: str) -> dict | None:
    with _PENDING_LOCK:
        return _PENDING.get(proposal_id)


def apply_proposal(proposal_id: str) -> dict:
    """APPROVE: write every file in the pending proposal to disk."""
    with _PENDING_LOCK:
        proposal = _PENDING.pop(proposal_id, None)
    if proposal is None:
        raise ValueError("Proposal not found or already resolved.")
    written = []
    for edit in proposal["edits"]:
        write_file(edit["path"], edit["content"])
        written.append(edit["path"])
        logger.info("[AI] Applied edit to %s (proposal %s)", edit["path"], proposal_id)
    return {"applied": True, "written": written, "count": len(written)}


def deny_proposal(proposal_id: str) -> dict:
    """DENY: discard the pending proposal without touching disk."""
    with _PENDING_LOCK:
        existed = _PENDING.pop(proposal_id, None) is not None
    return {"denied": existed}


def cleanup_expired(max_age_seconds: int = 3600) -> None:
    now = time.time()
    with _PENDING_LOCK:
        for pid in list(_PENDING.keys()):
            try:
                created = datetime.fromisoformat(_PENDING[pid]["created_at"]).timestamp()
            except Exception:
                created = now
            if now - created > max_age_seconds:
                _PENDING.pop(pid, None)
