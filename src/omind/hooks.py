# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Record Claude Code actions into an OMI session-journal note.

`omind setup` installs Claude Code hooks (PostToolUse, Stop, SessionStart) that
invoke ``omind hook <event>``. Each PostToolUse/Stop invocation appends one
bullet to a per-day journal note in the OMI folder, giving a deterministic
"every action" trail that complements Dix's hand-authored curated notes.

Design constraints:

* **Never block or fail the agent.** Every entry point swallows errors and the
  CLI handler always exits 0. A garbled or empty stdin is tolerated.
* **Hot-path cheap.** The journal is written with a raw ``O_APPEND`` + advisory
  ``flock`` so rapid, concurrent hook fire serializes without interleaving. We
  deliberately bypass :class:`omind.store.OmiStore` (whose every write
  re-renders ``index.md``) on this path.
* **Parses cleanly.** The note is template-shaped (``# title`` / ``## Metadata``
  / ``## Summary`` / ``## Actions``); :func:`omind.store.parse_note` ignores the
  unknown ``## Actions`` section, so the journal never corrupts parsing.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

HOOK_MARKER = "omind hook"  # substring used by provision.py to find our entries
HANDLED_EVENTS = ("PostToolUse", "Stop", "SessionStart")
JOURNAL_TAGS = ("session-journal", "omi")
_TARGET_LIMIT = 80

# Notes whose *content* is injected verbatim at SessionStart so OMI is in
# context whether or not the agent remembers to read it. Order = priming order.
PRIMING_FILES = ("index.md", "Memory Workflow.md", "CLAUDE CODE PERSONALITY.md")
_PRIMING_FILE_CHAR_CAP = 16_000  # per-file guard so a runaway note can't flood context


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now()


def _date_str(now: datetime | None = None) -> str:
    return _now(now).strftime("%Y-%m-%d")


def journal_name(now: datetime | None = None) -> str:
    """Deterministic per-day journal filename: ``Session Journal YYYY-MM-DD.md``."""
    return f"Session Journal {_date_str(now)}.md"


def short_session_id(session_id: object) -> str:
    """First 8 alphanumerics of a session id; ``'unknown'`` when empty."""
    cleaned = "".join(c for c in str(session_id) if c.isalnum())[:8]
    return cleaned or "unknown"


def journal_header(name: str, now: datetime | None = None) -> str:
    """Template-shaped header for a fresh journal note (parses under parse_note)."""
    title = name[:-3] if name.endswith(".md") else name
    tags = " ".join(f"#{t}" for t in JOURNAL_TAGS)
    return (
        f"# {title}\n\n"
        "## Metadata\n"
        f"- Created: {_date_str(now)}\n"
        f"- Tags: {tags}\n"
        "- Related to:\n\n"
        "## Summary\n"
        "Auto-recorded per-action journal written by omind hooks. "
        "One bullet per Claude Code action.\n\n"
        "## Actions\n"
    )


def read_event(stream: TextIO | None = None) -> dict[str, Any]:
    """Parse the hook's stdin JSON; return ``{}`` on EOF/garbage (never raises)."""
    src = stream if stream is not None else sys.stdin
    try:
        raw = src.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _truncate(text: str, limit: int = _TARGET_LIMIT) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _extract_target(tool_input: object) -> str:
    """Best-effort target for a tool call: file path, command, url/pattern/query."""
    if not isinstance(tool_input, dict):
        return ""
    for key in ("file_path", "path", "notebook_path", "filename"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return _truncate(value.strip())
    command = tool_input.get("command")
    if isinstance(command, str) and command.strip():
        return "`" + _truncate(command.strip()) + "`"
    for key in ("url", "pattern", "query"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return _truncate(value.strip())
    return ""


def _extract_outcome(tool_response: object) -> str:
    """``error`` if the response looks failed, else ``ok``."""
    if isinstance(tool_response, dict) and (
        tool_response.get("error")
        or tool_response.get("is_error")
        or tool_response.get("stderr")
        or tool_response.get("success") is False
    ):
        return "error"
    return "ok"


def format_entry(
    event: dict[str, Any],
    *,
    event_name: str | None = None,
    now: datetime | None = None,
) -> str | None:
    """Build one journal bullet from a hook event.

    Returns ``None`` for events that should not be journaled (SessionStart).
    ``event_name`` (passed by the CLI) overrides the stdin ``hook_event_name``.
    """
    name = (event_name or str(event.get("hook_event_name") or "")).strip()
    if name == "SessionStart":
        return None

    timestamp = _now(now).strftime("%H:%M")
    session = short_session_id(event.get("session_id") or "")

    if name == "Stop":
        return f"- {timestamp} [session {session}] Stop -> turn ended"

    label = name or "PostToolUse"
    tool = str(event.get("tool_name") or "?").strip() or "?"
    target = _extract_target(event.get("tool_input"))
    outcome = _extract_outcome(event.get("tool_response"))
    if target:
        return f"- {timestamp} [session {session}] {label} {tool} -> {target} ({outcome})"
    return f"- {timestamp} [session {session}] {label} {tool} ({outcome})"


def append_entry(omi_dir: Path | str, line: str, now: datetime | None = None) -> None:
    """Append one bullet to today's journal under an exclusive lock. Never raises.

    Creates the note with :func:`journal_header` on first write (header + bullet
    are written together under the lock, so a torn header can't occur). Uses
    ``O_APPEND`` + ``flock(LOCK_EX)`` so concurrent hook processes serialize.
    """
    try:
        directory = Path(omi_dir)
        directory.mkdir(parents=True, exist_ok=True)
        name = journal_name(now)
        path = directory / name
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if os.fstat(fd).st_size == 0:
                os.write(fd, journal_header(name, now).encode("utf-8"))
            text = line if line.endswith("\n") else line + "\n"
            os.write(fd, text.encode("utf-8"))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    except OSError:
        return


def _read_priming_note(path: Path) -> str | None:
    """Return a note's text (capped), or ``None`` if unreadable. Never raises."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(text) > _PRIMING_FILE_CHAR_CAP:
        text = text[:_PRIMING_FILE_CHAR_CAP] + "\n…[truncated]"
    return text


def build_session_start_context(omi_dir: Path | str) -> str:
    """Build the SessionStart ``additionalContext`` payload.

    Injects the *content* of the OMI priming notes (:data:`PRIMING_FILES`)
    directly so the vault is in context at session start without depending on
    the agent issuing reads. Falls back to a read-the-vault reminder if no
    priming note can be read. Never raises.
    """
    directory = Path(omi_dir)
    sections: list[str] = []
    for name in PRIMING_FILES:
        body = _read_priming_note(directory / name)
        if body is not None:
            sections.append(f"===== OMI/{name} =====\n{body.rstrip()}")

    header = (
        "OMI memory is the source of truth (do NOT use Claude Code's built-in "
        "memory). The OMI vault lives at "
        f"{directory}. Its priming notes are injected below — treat them as "
        "already read. Read any [[wikilinked]] note you need before acting."
    )
    if not sections:
        return (
            header
            + " (Priming notes could not be read this session; read index.md, "
            "Memory Workflow.md, and CLAUDE CODE PERSONALITY.md from the vault "
            "directly.)"
        )
    return header + "\n\n" + "\n\n".join(sections)


def emit_session_start_context(omi_dir: Path | str, out: TextIO | None = None) -> None:
    """Emit OMI priming-note content as SessionStart ``additionalContext``. Never raises."""
    sink = out if out is not None else sys.stdout
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": build_session_start_context(omi_dir),
        }
    }
    try:
        sink.write(json.dumps(payload) + "\n")
    except Exception:
        return


def run_hook(
    event_name: str,
    omi_dir: Path | str,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Dispatch one hook invocation. ALWAYS returns 0 so the agent never blocks."""
    try:
        if event_name == "SessionStart":
            emit_session_start_context(omi_dir, out=stdout)
            return 0
        event = read_event(stdin)
        line = format_entry(event, event_name=event_name)
        if line:
            append_entry(omi_dir, line)
    except Exception:
        return 0
    return 0
