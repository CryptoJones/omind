# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Name hints from tool output (#388, part of epic #384).

No hook used to read tool *results* for recall. The PostToolUse path did
accounting, compliance and the mid-turn nudge, which queries the turn's task
and a trail of commands — never what the tool printed. A name that appears
**only** in tool output (a volume label in ``diskutil list``, a host in ``ssh``
output, a serial in ``ioreg``) could never bring its notes forward. That is the
2026-09-30 incident: the agent saw ``As30p`` in ``diskutil`` output while the
vault held the history of that label moving between three drives, nothing
reached it, and it wrote a wrong "correction" into a new note.

This module closes that gap cheaply enough to run on every tool call:

* identifier-shaped tokens are taken from the first :data:`MAX_SCAN_CHARS` of
  the tool response with the **same** extractor the name index uses
  (:func:`omind.entities.extract`), and looked up with **one** read-only query
  (:func:`omind.searchindex.entity_lookups_readonly` — no model load, no refresh);
* tokens that are identifier-shaped but never names (formats, platforms,
  commit hashes, MIME types, wikilink titles already spelled out) are skipped,
  and a name no note is titled after must be rare (:data:`UNTITLED_MAX_DF`);
* a name is hinted **at most once per session** (names the preflight already
  hinted for a prompt count too, and so do names a ``create-note``/``edit-note``
  response listed under ``related_by_entity`` or ``name_timelines``, #403), at
  most :data:`MAX_NAMES_PER_CALL` per call,
  only while it is under the index's rarity ceiling;
* a name whose facts changed (a superseded note, a correction) gets its dated
  history instead (:mod:`omind.timeline`, #390), still one line;
* the hint is **titles only** — one line per name — and is charged to the
  **push** budget (``operation`` :data:`OPERATION`), so it stops when the
  session's push budget is spent;
* the agent's own OMI reads (``mcp__omi__*`` results, ``omind`` CLI output, the
  vault's files) are pull, never a trigger.

``OMIND_TOOL_NAME_HINTS=0`` turns it off.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import re
import time
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Kill switch (``0``/``off``/``false``/``no``). On by default — see the #388 PR
#: for the precision and latency measurements that decision rests on.
ENABLE_ENV = "OMIND_TOOL_NAME_HINTS"
#: Ledger operation for a name hint: its own row in ``omind audit`` and part of
#: the push budget (:data:`omind.ai_usage.CONTEXT_OPERATIONS`).
OPERATION = "namehint"
#: Only the head of a tool response is scanned. A name that matters is almost
#: always near the top of ``diskutil``/``ssh``/``ls`` output, and the bound
#: keeps extraction well inside the per-call latency budget on a huge dump.
MAX_SCAN_CHARS = 16_000
#: At most this many names hinted for one tool call.
MAX_NAMES_PER_CALL = 3
#: At most this many distinct new names looked up for one tool call.
MAX_LOOKUPS_PER_CALL = 200
#: Titles shown per name.
MAX_TITLES = 2
#: Name hints stop for a session once they have sent this many characters
#: (~2,000 tokens, a thirtieth of the push budget). Bounds the tail: on the
#: replay behind #388 the worst session would otherwise have taken ~21,000.
SESSION_BUDGET_CHARS = 8_000
#: Each title is cut here (``…``). recall-note resolves a title prefix.
TITLE_CHARS = 140

#: A name no note is titled after is hinted only while this rare. Past it the
#: token is vocabulary the vault uses in passing (``python3``, ``127.0.0.1``),
#: not a thing the vault knows about.
UNTITLED_MAX_DF = 10

#: Salience. In a long output a name is hinted only if it recurs: one stray
#: mention in a 5 KB log is usually incidental, a label printed twice is what
#: the output is about. A short output is all salient. Chosen on a replay of
#: 259 real sessions (see the #388 PR): precision 46% -> 59%.
SALIENT_MIN_COUNT = 2
SHORT_OUTPUT_CHARS = 500

_OFF = frozenset({"0", "off", "false", "no"})
_FILE_TOOLS = frozenset(
    {"Read", "Grep", "Glob", "Write", "Edit", "MultiEdit", "NotebookEdit", "NotebookRead"}
)
#: Identifier-shaped tokens that tool output is full of and that name a
#: format, a platform or a placeholder, never a thing. Lower-case keys.
_GENERIC = frozenset(
    {
        "utf-8",
        "utf8",
        "utf-16",
        "ascii",
        "latin-1",
        "iso-8859-1",
        "python3",
        "python2",
        "pip3",
        "sqlite3",
        "ipv4",
        "ipv6",
        "http2",
        "http3",
        "arm64",
        "aarch64",
        "x86_64",
        "x86-64",
        "amd64",
        "i386",
        "i686",
        "win32",
        "win64",
        "usb2",
        "usb3",
        "usb-c",
        "h264",
        "h265",
        "x264",
        "x265",
        "mp3",
        "mp4",
        "m4a",
        "m4b",
        "sha1",
        "sha256",
        "sha512",
        "md5",
        "base64",
        "ed25519",
        "rsa4096",
        "aes256",
        "1080p",
        "720p",
        "4k",
        "2fa",
        "oauth2",
        "s3",
        "ec2",
        "k8s",
        "l10n",
        "i18n",
        "data",
        "untitled",
        "localhost",
    }
)
_MIME_OWNERS = frozenset(
    {"application", "audio", "font", "image", "message", "model", "multipart", "text", "video"}
)
#: Commit SHAs, colours, temp-file suffixes: lower-case hex with a digit.
_HEX_PART_RE = re.compile(r"(?:^|[-_.])(?=[0-9a-f]*\d)[0-9a-f]{6,}(?:$|[-_.])")
_OMI_TOOL_PREFIXES = ("mcp__omi__", "mcp_omi_")
#: A shell command that runs omind is the agent reading its own memory (pull).
_OMIND_CMD_RE = re.compile(r"(?:^|[\s;&|(/])omind\s+[a-z]")


def enabled() -> bool:
    """Whether tool-output name hints are on (default on)."""
    return os.environ.get(ENABLE_ENV, "").strip().lower() not in _OFF


@dataclass
class NameHint:
    """One name found in a tool response, and what the vault holds about it."""

    #: The spelling seen in the tool output.
    name: str
    #: Normalised lookup key (:func:`omind.entities.normalize`).
    key: str
    #: Live notes that mention it.
    df: int
    #: Titles shown, best first: notes *about* the name (it is in their
    #: title), newest first, then the newest notes that merely mention it.
    titles: list[str] = field(default_factory=list)
    #: How many of the notes name it in their title.
    titled: int = 0
    #: The name's dated history when its facts changed (#390); it replaces the
    #: plain titles line. ``None`` for a name with no history.
    timeline: Any = None

    def line(self) -> str:
        if self.timeline is not None:
            return str(self.timeline.line())
        shown = "; ".join(f"[[{_cut(title)}]]" for title in self.titles)
        what = "about it" if self.titled else "newest"
        return (
            f"OMI: {self.name} → {self.df} note{'s' if self.df != 1 else ''}; "
            f"{what}: {shown}. recall-note before asserting facts about {self.name}."
        )


def _cut(title: str) -> str:
    title = " ".join(title.split())
    return title if len(title) <= TITLE_CHARS else title[: TITLE_CHARS - 1].rstrip() + "…"


def _noise(key: str, spelling: str) -> bool:
    """A token that is identifier-shaped but never a name worth a hint."""
    if any(ch.isspace() for ch in spelling):
        # A ``[[wikilink]]`` target the output already spells out in full: the
        # agent is looking at the note's title, it does not need a hint for it.
        return True
    if key in _GENERIC or _HEX_PART_RE.search(spelling):
        return True
    owner, slash, _ = key.partition("/")
    return bool(slash) and owner in _MIME_OWNERS


def _word_re(spelling: str) -> re.Pattern[str]:
    """``spelling`` as a whole name, not inside a longer token or a path."""
    return re.compile(r"(?<![\w/.-])" + re.escape(spelling) + r"(?![\w/-])", re.IGNORECASE)


def _salient(spelling: str, text: str) -> bool:
    if len(text) < SHORT_OUTPUT_CHARS:
        return True
    pattern = re.compile(r"(?<![\w-])" + re.escape(spelling) + r"(?![\w-])", re.IGNORECASE)
    hits = itertools.islice(pattern.finditer(text), SALIENT_MIN_COUNT)
    return sum(1 for _ in hits) >= SALIENT_MIN_COUNT


def _names_in_title(spelling: str, title: str) -> bool:
    """``title`` names ``spelling`` as a word, not as a path component.

    ``WD Blue As30p drive check`` is about the drive; ``61 repos moved to
    /Volumes/As30p/source/repos`` is about the repos.
    """
    return _word_re(spelling).search(unicodedata.normalize("NFC", title)) is not None


def response_text(response: Any, limit: int = MAX_SCAN_CHARS) -> str:
    """The text of a tool response, head first, at most ``limit`` chars.

    Harnesses shape responses differently (Claude Code Bash: ``{"stdout",
    "stderr", …}``; Read: ``{"file": {"content"}}``; MCP: content blocks), so
    every string value is collected depth-first in order. Never raises.
    """
    parts: list[str] = []
    budget = limit

    def walk(value: Any, depth: int) -> None:
        nonlocal budget
        if budget <= 0 or depth > 8:
            return
        if isinstance(value, str):
            if value:
                parts.append(value[:budget])
                budget -= len(parts[-1]) + 1
        elif isinstance(value, dict):
            for item in value.values():
                walk(item, depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, depth + 1)

    with contextlib.suppress(Exception):
        walk(response, 0)
    return "\n".join(parts)[:limit]


def is_pull(event: dict[str, Any], omi_dir: Path | str | None) -> bool:
    """Whether this tool call was the agent reading its own memory.

    Its result is pull (#387): hinting names out of it would answer the agent
    with what it just read.
    """
    tool = str(event.get("tool_name") or "")
    if tool.startswith(_OMI_TOOL_PREFIXES):
        return True
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    command = tool_input.get("command")
    if isinstance(command, str) and _OMIND_CMD_RE.search(command):
        return True
    if omi_dir is None:
        return False
    for key in ("file_path", "path", "notebook_path"):
        target = tool_input.get(key)
        if isinstance(target, str) and target:
            with contextlib.suppress(OSError, ValueError, RuntimeError):
                vault = Path(omi_dir).expanduser().resolve()
                path = Path(target).expanduser().resolve()
                if path == vault or vault in path.parents:
                    return True
    return False


def written_names(event: dict[str, Any]) -> set[str]:
    """Names an OMI write response (``create-note``/``edit-note``) already put
    in front of the agent under ``related_by_entity`` or ``name_timelines``
    (#389, #390), so the PostToolUse hint does not repeat them (#403).

    The response may arrive as a dict, a JSON string, or MCP content blocks
    wrapping one. Anything else yields ``set()``. Never raises.
    """
    tool = str(event.get("tool_name") or "")
    if not tool.startswith(_OMI_TOOL_PREFIXES):
        return set()
    if tool.removeprefix("mcp__omi__").removeprefix("mcp_omi_").replace("_", "-") not in (
        "create-note",
        "edit-note",
    ):
        return set()
    from omind import writecontext

    names: set[str] = set()

    def walk(value: Any, depth: int) -> None:
        # Same depth cap as :func:`response_text`; decoding a JSON string
        # costs a level, so no response nests deeper here than it does there.
        if depth > 8:
            return
        if isinstance(value, str):
            if value.lstrip().startswith(("{", "[")):
                with contextlib.suppress(ValueError, RecursionError):
                    walk(json.loads(value), depth + 1)
        elif isinstance(value, dict):
            for key in (writecontext.FIELD, writecontext.TIMELINE_FIELD):
                entries = value.get(key)
                if isinstance(entries, list):
                    names.update(
                        str(e["name"])
                        for e in entries
                        if isinstance(e, dict) and isinstance(e.get("name"), str)
                    )
            for item in value.values():
                walk(item, depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, depth + 1)

    with contextlib.suppress(Exception):
        walk(event.get("tool_response"), 0)
    return names


def input_names(tool_input: Any) -> set[str]:
    """Names the agent typed into the call itself. It chose them, so they are
    not news; the hint is for names that arrive *only* in the output. Never
    raises."""
    if not tool_input:
        return set()
    try:
        from omind import entities

        blob = tool_input if isinstance(tool_input, str) else json.dumps(tool_input)
        return set(entities.extract(blob[:MAX_SCAN_CHARS]))
    except Exception:
        return set()


def skipped_tool(tool: str) -> bool:
    """File tools: their output is a file the agent chose to open (source code
    is dense with identifier-shaped tokens) or its own write echoed back."""
    return tool in _FILE_TOOLS


def pick_hints(
    text: str,
    omi_dir: Path | str,
    *,
    already: Iterable[str] = (),
    as_of: str = "",
    limit: int = MAX_NAMES_PER_CALL,
) -> list[NameHint]:
    """Names in ``text`` the vault has notes about, not yet hinted.

    READ-ONLY and deterministic: no state, no ledger. Shared by the live hook
    and ``omind bench --tool-hints`` so the two cannot disagree on what a hint
    is. ``as_of`` (``YYYY-MM-DD``) hides notes dated after it — a replay of an
    old transcript must not be helped by notes written later. Names some note
    is titled after come first, then rarer names, then order of appearance.
    Fails open to ``[]``.
    """
    if not text:
        return []
    try:
        from omind import entities, searchindex, timeline

        if not entities.enabled():
            return []
        found = entities.extract(text)
        if not found:
            return []
        seen = set(already)
        fresh = [
            (key, spelling)
            for key, spelling in found.items()
            if key not in seen and not _noise(key, spelling)
        ]
        fresh = fresh[:MAX_LOOKUPS_PER_CALL]
        if not fresh:
            return []
        lookups = searchindex.entity_lookups_readonly(omi_dir, [key for key, _ in fresh])
        if not lookups:
            return []
        picked: list[tuple[int, int, int, NameHint, list[Any]]] = []
        for order, (key, spelling) in enumerate(fresh):
            lookup = lookups.get(key)
            if lookup is None or lookup.common or not lookup.notes:
                continue
            if not _salient(spelling, text):
                continue
            notes = [n for n in lookup.notes if not as_of or n.last_seen[:10] <= as_of]
            if not notes:
                continue
            about = [n for n in notes if _names_in_title(spelling, n.title)]
            if not about and len(notes) > UNTITLED_MAX_DF:
                continue
            rest = [n for n in notes if n not in about]
            titles = [n.title for n in (about + rest)[:MAX_TITLES] if n.title]
            if not titles:
                continue
            hint = NameHint(name=spelling, key=key, df=len(notes), titles=titles, titled=len(about))
            picked.append((0 if about else 1, len(notes), order, hint, notes))
        # Names the vault has notes *about* first (the drive label, not the
        # partition next to it), then rarer first, then order of appearance.
        picked.sort(key=lambda item: item[:3])
        chosen = picked[: max(0, limit)]
        if chosen and timeline.enabled():
            # #390: a name whose facts changed shows its dated history,
            # superseded notes marked rather than left out. Built only for the
            # hints actually emitted, so the note-head reads stay bounded.
            read = timeline._reader(omi_dir)
            for *_, hint, notes in chosen:
                hint.timeline = timeline.build(omi_dir, hint.name, notes, read=read)
        return [item[3] for item in chosen]
    except Exception:
        return []


# -- per-session state ------------------------------------------------------


def _state_path(session: str) -> Path:
    from omind import guard, paths

    return paths.state_dir() / f"namehints-{guard._safe_sid(session)}.json"


def _read_state(session: str) -> tuple[set[str], int]:
    """``(names hinted, characters hinted)`` for ``session``. Never raises."""
    try:
        data = json.loads(_state_path(session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set(), 0
    if not isinstance(data, dict):
        return set(), 0
    names = data.get("names")
    try:
        chars = max(0, int(data.get("chars") or 0))
    except (TypeError, ValueError):
        chars = 0
    return ({str(n) for n in names} if isinstance(names, list) else set()), chars


def hinted(session: str) -> set[str]:
    """Normalised names already hinted in ``session``. Never raises."""
    return _read_state(session)[0]


def mark_hinted(session: str, names: Iterable[str]) -> None:
    """Record names as hinted for ``session`` (keys or spellings), so the
    PostToolUse path does not repeat a name the preflight already hinted for
    the prompt. Never raises."""
    if not session:
        return
    with contextlib.suppress(Exception):
        from omind import entities

        keys = {entities.normalize(name) for name in names} - {""}
        if keys:
            _update_state(session, lambda known, chars: (known | keys, chars))


def _update_state(session: str, mutate: Any) -> tuple[set[str], int]:
    """Read-modify-write the session state under its sibling ``.lock`` (the
    repo-wide pattern): concurrent PostToolUse hooks otherwise lose each
    other's names and hint the same one twice. Returns the state as it was
    before ``mutate``. Raises ``OSError`` on a state dir it cannot write."""
    from omind import filelock, guard, paths

    path = _state_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    with filelock.exclusive(guard._sibling_lock(path)):
        before = _read_state(session)
        names, chars = mutate(*before)
        if (names, chars) != before:
            paths.atomic_write_text(
                path, json.dumps({"names": sorted(names), "chars": chars}), mode=0o600
            )
        return before


def _claim(session: str, hints: list[NameHint]) -> list[NameHint]:
    """The hints this call may still emit, atomically marked as emitted.

    A name another hook claimed since :func:`pick_hints` read the state is
    dropped; so is everything once the session's name hints reach
    :data:`SESSION_BUDGET_CHARS`. Fails closed to ``[]`` — an unrecordable
    hint would repeat on every call.
    """
    claimed: list[NameHint] = []

    def mutate(known: set[str], chars: int) -> tuple[set[str], int]:
        claimed.clear()
        spent = chars
        for hint in hints:
            size = len(hint.line()) + 1
            if hint.key in known or spent + size > SESSION_BUDGET_CHARS:
                continue
            claimed.append(hint)
            spent += size
        return known | {hint.key for hint in claimed}, spent

    try:
        _update_state(session, mutate)
    except Exception:
        return []
    return claimed


# -- the live hook ----------------------------------------------------------


def tool_hints(event: dict[str, Any], omi_dir: Path | str | None) -> str:
    """The ``additionalContext`` for one PostToolUse event, or ``""``.

    Charged to the push budget and recorded in the ledger as :data:`OPERATION`.
    Never raises: a broken hint is a missing hint, never a broken hook.
    """
    try:
        session = str(event.get("session_id") or event.get("session") or "")
        if not session or omi_dir is None or not enabled():
            return ""
        if is_pull(event, omi_dir):
            # #403: names a create-note/edit-note response already showed
            # (related_by_entity, name_timelines) are not hinted again.
            mark_hinted(session, written_names(event))
            return ""
        if skipped_tool(str(event.get("tool_name") or "")):
            return ""
        text = response_text(event.get("tool_response"))
        if not text:
            return ""
        known, spent = _read_state(session)
        if spent >= SESSION_BUDGET_CHARS:
            return ""
        hints = pick_hints(text, omi_dir, already=known | input_names(event.get("tool_input")))
        if not hints:
            return ""
        from omind import ai_usage, guard

        # The budget read is the slow part; only pay it when there is a hint.
        if guard.session_context_chars(omi_dir, session) >= guard.SESSION_INJECTION_BUDGET_CHARS:
            return ""
        hints = _claim(session, hints)
        if not hints:
            return ""
        context = "\n".join(hint.line() for hint in hints)
        ai_usage.record_context(omi_dir, OPERATION, len(context), session_id=session)
        return context
    except Exception:
        return ""


# -- replay (omind bench --tool-hints) ---------------------------------------


@dataclass
class ReplayHint:
    """A hint the live hook would have emitted at one point of a transcript."""

    #: Line number of the tool result in the ``.jsonl`` transcript.
    line: int
    tool: str
    hint: NameHint
    #: The agent named it itself later in the session (text or a tool call).
    used_later: bool = False
    #: The agent later read OMI about it (an ``mcp__omi__*`` call naming it or
    #: one of the hinted titles).
    consulted_later: bool = False


@dataclass
class Replay:
    """Everything a transcript replay measured."""

    transcript: str
    tool_results: int = 0
    pull_skipped: int = 0
    hints: list[ReplayHint] = field(default_factory=list)
    #: Milliseconds per non-pull tool result: extraction + lookup + formatting.
    latencies_ms: list[float] = field(default_factory=list)
    #: Lines of the transcript where the assistant spoke or acted.
    assistant_lines: int = 0

    @property
    def chars(self) -> int:
        return sum(len(item.hint.line()) + 1 for item in self.hints)


def _blocks(message: Any) -> list[dict[str, Any]]:
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def replay_transcript(path: Path | str, omi_dir: Path | str) -> Replay:
    """What the PostToolUse name hints would have said over a Claude Code
    ``.jsonl`` session, in order, without touching state or the ledger.

    Each tool result is matched to its ``tool_use`` (for the pull rule and the
    tool name) and replayed with :func:`pick_hints`, ``as_of`` the result's
    date, against the names already hinted earlier in the replay. Raises
    ``OSError``/``ValueError`` on an unreadable transcript — a CLI instrument
    says what is wrong.
    """
    source = Path(path).expanduser()
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    replay = Replay(transcript=str(source))
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    #: (line, casefolded text) of everything the assistant said or sent.
    spoken: list[tuple[int, str]] = []
    consults: list[tuple[int, str]] = []
    pending: list[tuple[int, str, dict[str, Any], Any, str]] = []
    for number, raw in enumerate(lines, start=1):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        message = entry.get("message")
        if kind == "assistant":
            said: list[str] = []
            for block in _blocks(message):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    said.append(block["text"])
                elif block.get("type") == "tool_use":
                    name = str(block.get("name") or "")
                    given = block.get("input")
                    tool_input: dict[str, Any] = given if isinstance(given, dict) else {}
                    calls[str(block.get("id") or "")] = (name, tool_input)
                    blob = json.dumps(tool_input, ensure_ascii=False)
                    said.append(blob)
                    if name.startswith(_OMI_TOOL_PREFIXES):
                        consults.append((number, blob.casefold()))
            if said:
                replay.assistant_lines += 1
                spoken.append((number, "\n".join(said).casefold()))
        elif kind == "user":
            stamp = str(entry.get("timestamp") or "")[:10]
            for block in _blocks(message):
                if block.get("type") != "tool_result":
                    continue
                name, tool_input = calls.get(str(block.get("tool_use_id") or ""), ("", {}))
                pending.append((number, name, tool_input, block.get("content"), stamp))
    already: set[str] = set()
    spent = 0
    for number, name, tool_input, content, stamp in pending:
        replay.tool_results += 1
        event = {"tool_name": name, "tool_input": tool_input}
        if is_pull(event, omi_dir):
            replay.pull_skipped += 1
            # #403, as the live hook does: names a create-note/edit-note
            # response already showed are not hinted again later.
            from omind import entities

            written = written_names({"tool_name": name, "tool_response": content})
            already.update({entities.normalize(n) for n in written} - {""})
            continue
        if skipped_tool(name):
            continue
        started = time.perf_counter()
        hints = pick_hints(
            response_text(content),
            omi_dir,
            already=already | input_names(tool_input),
            as_of=stamp,
        )
        replay.latencies_ms.append((time.perf_counter() - started) * 1000.0)
        for hint in hints:
            size = len(hint.line()) + 1
            if spent + size > SESSION_BUDGET_CHARS:
                continue
            spent += size
            already.add(hint.key)
            needle = hint.key
            shown = hint.timeline.titles if hint.timeline is not None else hint.titles
            titles = [title.casefold()[:60] for title in shown]
            replay.hints.append(
                ReplayHint(
                    line=number,
                    tool=name,
                    hint=hint,
                    used_later=any(at > number and needle in text for at, text in spoken),
                    consulted_later=any(
                        at > number and (needle in text or any(t in text for t in titles))
                        for at, text in consults
                    ),
                )
            )
    return replay
