# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Record Claude Code actions into an OMI session-journal note.

`omind setup` installs Claude Code hooks (PostToolUse, Stop, SessionStart) that
invoke ``omind hook <event>``. Each PostToolUse/Stop invocation appends one
bullet to a per-day journal note in the OMI folder's ``Journal/`` subfolder,
giving a deterministic "every action" trail that complements Dix's
hand-authored curated notes. Keeping dailies one level down means the
top-level-only glob in :meth:`omind.store.OmiStore._note_paths` skips them, so
they never accumulate in listings or the regenerated ``index.md`` — while
Obsidian ``[[wikilinks]]`` stay folder-agnostic, so existing
``[[Session Journal …]]`` links keep resolving.

Design constraints:

* **Never block or fail the agent.** Every entry point swallows errors and the
  CLI handler always exits 0. A garbled or empty stdin is tolerated. Swallowed
  errors are not silent, though: each leaves a one-line breadcrumb in
  :func:`failure_log_path` (best-effort), and ``omind doctor`` warns when that
  log has recent entries — otherwise a full disk or a permissions change means
  the journal just silently stops existing.
* **Hot-path cheap.** The journal is written with a raw ``O_APPEND`` + advisory
  ``flock`` so rapid, concurrent hook fire serializes without interleaving. We
  deliberately bypass :class:`omind.store.OmiStore` (whose every write
  re-renders ``index.md``) on this path.
* **Parses cleanly.** The note is template-shaped (``# title`` / ``## Metadata``
  / ``## Summary`` / ``## Actions``); :func:`omind.store.parse_note` ignores the
  unknown ``## Actions`` section, so the journal never corrupts parsing.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from omind import filelock, paths

HOOK_MARKER = "omind hook"  # substring used by provision.py to find our entries
HANDLED_EVENTS = ("PostToolUse", "Stop", "SessionStart")
#: Hermes Agent has no SessionStart hook; it fires ``pre_llm_call`` before every
#: LLM turn and consumes a ``{"context": ...}`` payload on stdout. omind installs
#: this event to inject the same priming the Claude SessionStart hook does — but
#: only once per session (see :func:`emit_pre_llm_call_context`).
HERMES_PRIME_EVENT = "pre_llm_call"
#: Every event the ``omind hook`` CLI accepts (Claude's three + Hermes' one).
ALL_HOOK_EVENTS = HANDLED_EVENTS + (HERMES_PRIME_EVENT,)
JOURNAL_DIRNAME = "Journal"  # subfolder keeping dailies out of listings/index
JOURNAL_TAGS = ("session-journal", "omi")
_TARGET_LIMIT = 80

# Notes whose *content* is injected verbatim at SessionStart so OMI is in
# context whether or not the agent remembers to read it. Order = priming order.
PRIMING_FILES = ("index.md", "Memory Workflow.md", "CLAUDE CODE PERSONALITY.md")
_PRIMING_FILE_CHAR_CAP = 16_000  # per-file guard so a runaway note can't flood context

# Dynamic priming: the newest handoff note and the tail of today's auto-journal
# are injected after the static files, under a whole-payload budget. Static
# files always win the budget; dynamic sections truncate (or drop) first.
_SESSION_STATE_GLOB = "Session State *.md"
_JOURNAL_GLOB = paths.JOURNAL_GLOB
_JOURNAL_TAIL_BULLETS = 20
_TOTAL_CONTEXT_CHAR_CAP = 48_000
_TRUNCATION_MARKER = "\n…[truncated]"


#: Past this size the failure log restarts instead of appending — a repeating
#: failure (cron + full disk) must never grow it unbounded.
_FAILURE_LOG_CAP_BYTES = 262_144


def failure_log_path() -> Path:
    """Where swallowed hook errors leave a trace, outside the (possibly broken)
    vault: ``$XDG_STATE_HOME/omind/hook-failures.log`` (default
    ``~/.local/state/omind/hook-failures.log``). Derived from
    :func:`omind.paths.state_dir` — doctor reads this log; the writer and the
    reader must never resolve the directory differently."""
    return paths.state_dir() / "hook-failures.log"


def _record_failure(context: str, exc: BaseException) -> None:
    """Append a one-line breadcrumb for a swallowed error. Never raises.

    Deliberately not the vault (whose unwritability is the most likely cause)
    and deliberately tiny: timestamp, where, repr of the error.
    """
    try:
        path = failure_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a"
        try:
            if path.stat().st_size > _FAILURE_LOG_CAP_BYTES:
                mode = "w"
        except OSError:
            pass
        stamp = datetime.now().isoformat(timespec="seconds")
        with open(path, mode, encoding="utf-8") as fh:
            fh.write(f"{stamp} {context}: {exc!r}\n")
    except Exception:
        return


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now()


def _date_str(now: datetime | None = None) -> str:
    return _now(now).strftime("%Y-%m-%d")


def journal_name(now: datetime | None = None) -> str:
    """Deterministic per-day journal filename: ``Session Journal YYYY-MM-DD.md``."""
    return f"{paths.JOURNAL_PREFIX} {_date_str(now)}.md"


def journal_dir(omi_dir: Path | str) -> Path:
    """The journal subfolder: ``<omi_dir>/Journal``.

    ``OmiStore._note_paths`` only globs top-level ``*.md``, so notes in here
    drop out of listings and the regenerated index automatically.
    """
    return Path(omi_dir) / JOURNAL_DIRNAME


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
    """``error`` only on explicit failure indicators, else ``ok``.

    Trusted signals (in order): truthy ``is_error``, ``success is False``,
    non-empty ``error``, nonzero ``exit_code``/``returncode``. ``stderr`` alone
    is NOT evidence of failure — healthy tools (git, curl, npm, dnf…) write
    progress and warnings there.
    """
    if not isinstance(tool_response, dict):
        return "ok"
    if (
        tool_response.get("is_error")
        or tool_response.get("success") is False
        or tool_response.get("error")
    ):
        return "error"
    for key in ("exit_code", "returncode"):
        code = tool_response.get(key)
        if isinstance(code, int) and code != 0:
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
    """Append one bullet to today's journal (in ``Journal/``) under an exclusive
    lock. Never raises.

    Creates the note with :func:`journal_header` on first write (header + bullet
    are written together under the lock, so a torn header can't occur). Uses
    ``O_APPEND`` + ``flock(LOCK_EX)`` so concurrent hook processes serialize.
    """
    try:
        directory = journal_dir(omi_dir)
        directory.mkdir(parents=True, exist_ok=True)
        name = journal_name(now)
        path = directory / name
        # O_BINARY: on Windows, os.open defaults to the CRT's text mode, which
        # would rewrite the \n in our bytes to \r\n mid-write.
        binary = getattr(os, "O_BINARY", 0)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | binary, 0o644)
        try:
            filelock.lock_fd(fd)
            if os.fstat(fd).st_size == 0:
                os.write(fd, journal_header(name, now).encode("utf-8"))
            text = line if line.endswith("\n") else line + "\n"
            os.write(fd, text.encode("utf-8"))
        finally:
            filelock.unlock_fd(fd)
            os.close(fd)
    except OSError as exc:
        _record_failure(f"append_entry({omi_dir})", exc)


def _read_priming_note(path: Path) -> str | None:
    """Return a note's text (capped), or ``None`` if unreadable. Never raises."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(text) > _PRIMING_FILE_CHAR_CAP:
        text = text[:_PRIMING_FILE_CHAR_CAP] + _TRUNCATION_MARKER
    return text


def _latest_by_name(directory: Path, pattern: str) -> Path | None:
    """Newest note matching ``pattern``, by filename descending. Never raises.

    Filenames embed ``YYYY-MM-DD``, so a plain lexicographic sort is also a
    chronological sort.
    """
    try:
        matches = sorted(directory.glob(pattern), key=lambda p: p.name, reverse=True)
    except OSError:
        return None
    return matches[0] if matches else None


def action_bullets(text: str) -> list[str]:
    """The ``- `` bullets under a journal's ``## Actions`` heading.

    Only that section counts: ``## Metadata`` list lines are not actions, and
    the scan resets at the next heading. Owned here, next to the writer that
    defines the journal format; :mod:`omind.journal` reuses it.
    """
    bullets: list[str] = []
    in_actions = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_actions = line.strip() == "## Actions"
            continue
        if in_actions and line.startswith("- "):
            bullets.append(line)
    return bullets


def _journal_tail(path: Path, limit: int = _JOURNAL_TAIL_BULLETS) -> str | None:
    """Last ``limit`` action bullets of a journal note, or ``None``. Never raises."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    bullets = action_bullets(text)
    if not bullets:
        return None
    return "\n".join(bullets[-limit:])


def build_session_start_context(omi_dir: Path | str) -> str:
    """Build the SessionStart ``additionalContext`` payload.

    Injects the *content* of the OMI priming notes (:data:`PRIMING_FILES`)
    directly so the vault is in context at session start without depending on
    the agent issuing reads, then two dynamic sections: the newest
    ``Session State YYYY-MM-DD`` handoff note and the last
    :data:`_JOURNAL_TAIL_BULLETS` bullets of the newest auto-journal. The whole
    payload is capped at :data:`_TOTAL_CONTEXT_CHAR_CAP` chars — static files
    always win the budget; dynamic sections truncate (or drop) first. Falls
    back to a read-the-vault reminder if nothing can be read. Never raises.
    """
    directory = Path(omi_dir)
    sections: list[str] = []
    for name in PRIMING_FILES:
        body = _read_priming_note(directory / name)
        if body is not None:
            sections.append(f"===== OMI/{name} =====\n{body.rstrip()}")

    dynamic: list[str] = []
    state_path = _latest_by_name(directory, _SESSION_STATE_GLOB)
    if state_path is not None:
        body = _read_priming_note(state_path)
        if body is not None:
            dynamic.append(
                f"===== OMI/{state_path.name} (latest session state) =====\n{body.rstrip()}"
            )
    # Journals live in Journal/ since the relocation; fall back to the vault
    # root for a not-yet-migrated vault.
    journal_path = _latest_by_name(journal_dir(directory), _JOURNAL_GLOB) or _latest_by_name(
        directory, _JOURNAL_GLOB
    )
    if journal_path is not None:
        tail = _journal_tail(journal_path)
        if tail is not None:
            dynamic.append(
                f"===== OMI/{journal_path.name} — recent actions (auto-journal) =====\n{tail}"
            )

    header = (
        "OMI memory is the source of truth (do NOT use Claude Code's built-in "
        "memory). The OMI vault lives at "
        f"{directory}. Its priming notes are injected below — treat them as "
        "already read. Read any [[wikilinked]] note you need before acting."
    )
    if not sections and not dynamic:
        return (
            header
            + " (Priming notes could not be read this session; read index.md, "
            "Memory Workflow.md, and CLAUDE CODE PERSONALITY.md from the vault "
            "directly.)"
        )

    payload = "\n\n".join([header, *sections])  # static sections are never cut
    for section in dynamic:
        remaining = _TOTAL_CONTEXT_CHAR_CAP - len(payload) - len("\n\n")
        if remaining <= len(_TRUNCATION_MARKER):
            break  # no useful room left for dynamic content
        if len(section) > remaining:
            section = section[: remaining - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
        payload += "\n\n" + section
    return payload


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
    except Exception as exc:
        _record_failure(f"emit_session_start_context({omi_dir})", exc)


def _prime_marker_dir() -> Path:
    """Where per-session "already primed" markers live (outside the vault):
    ``$XDG_STATE_HOME/omind/session-primed/``. Sibling to the hook-failure log."""
    return paths.state_dir() / "session-primed"


def _already_primed(session_id: object) -> bool:
    """True when this session has already been primed; otherwise mark it and
    return False. Never raises.

    Hermes' ``pre_llm_call`` fires before *every* LLM turn, so without a guard
    omind would re-inject the (large) priming payload on each turn. A marker
    file per session id makes priming fire exactly once. When no session id is
    supplied we cannot tell turns apart, so we prime this call rather than risk
    never priming (an empty id would otherwise collide every session onto one
    marker).
    """
    cleaned = "".join(c for c in str(session_id) if c.isalnum())
    if not cleaned:
        return False
    try:
        directory = _prime_marker_dir()
        directory.mkdir(parents=True, exist_ok=True)
        marker = directory / cleaned
        if marker.exists():
            return True
        marker.touch()
    except OSError as exc:
        _record_failure("_already_primed", exc)
    return False


def emit_pre_llm_call_context(
    omi_dir: Path | str,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> None:
    """Emit OMI priming as Hermes' ``pre_llm_call`` ``{"context": ...}`` payload,
    once per session. Silent no-op on later turns of the same session. Never
    raises — a broken priming hook must never wedge the agent."""
    sink = stdout if stdout is not None else sys.stdout
    try:
        event = read_event(stdin)
        if _already_primed(event.get("session_id") or ""):
            return
        payload = {"context": build_session_start_context(omi_dir)}
        sink.write(json.dumps(payload) + "\n")
    except Exception as exc:
        _record_failure(f"emit_pre_llm_call_context({omi_dir})", exc)


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
        if event_name == HERMES_PRIME_EVENT:
            emit_pre_llm_call_context(omi_dir, stdin=stdin, stdout=stdout)
            return 0
        event = read_event(stdin)
        line = format_entry(event, event_name=event_name)
        if line:
            append_entry(omi_dir, line)
    except Exception as exc:
        _record_failure(f"run_hook({event_name}, {omi_dir})", exc)
        return 0
    return 0
