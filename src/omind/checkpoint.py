# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""``omind checkpoint`` — periodically record recent activity into a worklog note.

You cannot reliably *force* a running agent to do something on a wall clock —
agents are turn-driven and idle between messages. So the robust way to "record
recent work every N minutes" is **not** to ask the agent: it is a scheduled job
that mines the trails the hooks already capture and writes the summary itself.

This module is that job. It reads the two records omind already keeps —

* the **journal** (``Journal/Session Journal <date>.md``): one bullet per Claude
  Code action (``- HH:MM [session …] PostToolUse Bash -> `cmd` (ok)``), the full
  per-action work trail;
* the **compliance log** (``compliance.jsonl``): the cross-harness guard events
  (denies, violations, off-topic consults) the guard adapters write for every
  harness —

filters them to a recent window, and **upserts a per-day ``Worklog <date>``
note** with a timestamped section per run. One note per day (upsert by title), so
it occupies a single slot in the recent-memory index rather than flooding it.

``omind checkpoint install-timer --every 15m`` wires a systemd *user* timer
(the same mechanism ``omind backup``/``omind mesh`` use) so it runs unattended —
the agent's cooperation is never required, which is what makes it a real *force*.

Deterministic by default; ``--llm`` opt-in summarizes the window with headless
``claude -p`` (fail-open to the deterministic summary). Never raises into a timer.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from omind import compliance, hooks
from omind.notes import upsert_note
from omind.store import NoteError, NoteFields, OmiStore

_SINCE_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 60}  # bare number = minutes
#: A journal action bullet: ``- HH:MM [session <id>] <rest>``.
_BULLET_RE = re.compile(r"^-\s+(\d{1,2}):(\d{2})\s+\[session[^\]]*\]\s+(.*)$")
#: Keep at most this many checkpoint sections in a day's worklog (bounded growth).
_MAX_SECTIONS = 96
_DEFAULT_SINCE = "15m"
_LLM_TIMEOUT = 20

SERVICE_UNIT_NAME = "omind-checkpoint.service"
TIMER_UNIT_NAME = "omind-checkpoint.timer"

_WORKLOG_SUMMARY = (
    "Auto-recorded work checkpoints (`omind checkpoint`). Each section summarizes "
    "the prior window of activity from the journal + the cross-harness compliance "
    "log — written by a scheduled job, not the agent."
)


def parse_since(value: str) -> timedelta:
    """``"15m"`` / ``"1h"`` / ``"90"`` (bare = minutes) → a timedelta. Falls back
    to 15 minutes on anything unparseable."""
    match = _SINCE_RE.match(value or "")
    if not match:
        return timedelta(minutes=15)
    return timedelta(seconds=int(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()])


@dataclass
class Activity:
    """What happened in the window, gathered from both trails."""

    actions: list[dict[str, str]] = field(default_factory=list)  # journal bullets
    guard_events: list[dict[str, Any]] = field(default_factory=list)  # compliance records

    def is_empty(self) -> bool:
        return not self.actions and not self.guard_events


def _parse_ts(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    # The rest of the module works in NAIVE local time; a tz-aware log line (a
    # foreign writer, a hand edit) would raise "can't compare offset-naive and
    # offset-aware" mid-timer. Normalize to naive local.
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _journal_actions(omi_dir: Path | str, day: datetime, cutoff: datetime, now: datetime) -> list[
    dict[str, str]
]:
    """Action bullets from ``day``'s journal whose time falls in ``(cutoff, now]``."""
    path = hooks.journal_dir(omi_dir) / hooks.journal_name(day)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    # Bullets carry only HH:MM. Floor the cutoff to the minute for the comparison
    # so an action in the cutoff's own minute (journaled AFTER the previous run
    # already fired) is not silently dropped from EVERY window forever.
    cutoff_minute = cutoff.replace(second=0, microsecond=0)
    out: list[dict[str, str]] = []
    for line in text.splitlines():
        match = _BULLET_RE.match(line.strip())
        if not match:
            continue
        hour, minute, rest = int(match.group(1)), int(match.group(2)), match.group(3)
        try:
            when = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            continue  # a hand-edited bullet like "27:70 ..." must not crash the timer
        if when < cutoff_minute or when > now:
            continue
        tokens = rest.split()
        event = tokens[0] if tokens else ""
        tool = tokens[1] if len(tokens) > 1 and tokens[1] != "->" else ""
        out.append(
            {"time": f"{hour:02d}:{minute:02d}", "event": event, "tool": tool, "detail": rest}
        )
    return out


def gather_activity(omi_dir: Path | str, cutoff: datetime, now: datetime) -> Activity:
    """Collect journal actions + compliance events in ``(cutoff, now]``. Reads
    today's journal, plus yesterday's when the window crosses midnight."""
    actions = _journal_actions(omi_dir, now, cutoff, now)
    if cutoff.date() < now.date():
        actions = _journal_actions(omi_dir, now - timedelta(days=1), cutoff, now) + actions
    guard = [
        e
        for e in compliance.read_events()
        if (ts := _parse_ts(e.get("ts"))) and cutoff <= ts <= now
    ]
    return Activity(actions=actions, guard_events=guard)


def _llm_narrative(
    activity: Activity, since: str, omi_dir: Path | str | None = None
) -> str | None:
    """A one-paragraph narrative from headless ``claude -p``; ``None`` on any
    unavailability/error/timeout (caller falls back to the deterministic summary)."""
    from omind import ai_usage

    limits = ai_usage.policy(omi_dir) if omi_dir is not None else None
    action_limit = limits.checkpoint_actions if limits else 60
    guard_limit = limits.checkpoint_guard_events if limits else 30
    # A high-expense profile still builds the prompt shape so the skipped event
    # can report an avoided-token estimate without exposing its contents.
    lines = [
        f"{a['time']} {a['event']} {a['tool']} {a['detail']}"
        for a in activity.actions[-max(action_limit, 60 if action_limit == 0 else action_limit) :]
    ]
    guard = [
        f"{e.get('ts')} {e.get('outcome')} {e.get('tool')} {e.get('command')}"
        for e in activity.guard_events[
            -max(guard_limit, 30 if guard_limit == 0 else guard_limit) :
        ]
    ]
    prompt = (
        "Summarize what this agent worked on in the last "
        f"{since}, in 1-3 sentences, factual and concise. No preamble.\n\n"
        "ACTIONS:\n" + "\n".join(lines) + "\n\nGUARD EVENTS:\n" + "\n".join(guard) + "\n"
    )
    if omi_dir is not None:
        return ai_usage.run_claude(
            omi_dir,
            "checkpoint",
            prompt,
            timeout=_LLM_TIMEOUT,
            allowed=limits.checkpoint_llm if limits else True,
        )
    claude = shutil.which("claude")
    if not claude:
        return None
    try:
        result = subprocess.run(
            [claude, "-p", prompt], capture_output=True, text=True, timeout=_LLM_TIMEOUT
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return (result.stdout or "").strip() or None if result.returncode == 0 else None


def render_section(
    activity: Activity,
    since: str,
    now: datetime,
    *,
    llm: bool = False,
    omi_dir: Path | str | None = None,
) -> str:
    """One worklog section for this run (deterministic; ``llm`` adds a narrative)."""
    header = f"### {now.strftime('%H:%M')} — last {since}"
    if activity.is_empty():
        return f"{header}\n- no recorded activity in this window\n"
    lines = [header]
    if llm:
        narrative = _llm_narrative(activity, since, omi_dir)
        if narrative:
            lines.append(narrative)
    tools = Counter(a["tool"] for a in activity.actions if a["tool"])
    if activity.actions:
        breakdown = ", ".join(f"{t}×{n}" for t, n in tools.most_common())
        tail = f": {breakdown}" if breakdown else ""
        lines.append(f"- {len(activity.actions)} action(s){tail}")
    if activity.guard_events:
        denies = sum(1 for e in activity.guard_events if e.get("outcome") == "deny")
        viols = sum(1 for e in activity.guard_events if e.get("kind") == compliance.KIND_VIOLATION)
        lines.append(
            f"- guard: {len(activity.guard_events)} event(s) ({denies} deny, {viols} violation)"
        )
        for e in activity.guard_events[-5:]:
            cmd = str(e.get("command") or e.get("rule_id") or "").strip()
            if cmd:
                lines.append(f"  - {e.get('outcome') or e.get('kind')}: {cmd[:120]}")
    return "\n".join(lines) + "\n"


def _existing_details(omi_dir: Path | str, title: str) -> str:
    try:
        return OmiStore(omi_dir).read_fields(title).details
    except (NoteError, OSError):
        return ""


def _append_section(existing: str, section: str) -> str:
    """Append ``section`` to ``existing`` worklog details, keeping only the most
    recent :data:`_MAX_SECTIONS` so a busy day cannot grow the note without bound."""
    body = (existing.rstrip() + "\n\n" + section.rstrip()) if existing.strip() else section.rstrip()
    # Split before each "### " header (the delimiter stays with its section).
    sections = re.split(r"\n(?=### )", body)
    sections = sections[-_MAX_SECTIONS:]
    return "\n".join(s.rstrip() for s in sections) + "\n"


def write_checkpoint(
    omi_dir: Path | str,
    *,
    since: str = _DEFAULT_SINCE,
    now: datetime | None = None,
    llm: bool = False,
) -> tuple[str, str]:
    """Gather the window and upsert today's ``Worklog <date>`` note. Returns
    ``(action, filename)`` from :func:`omind.notes.upsert_note`."""
    now = now or datetime.now()
    cutoff = now - parse_since(since)
    activity = gather_activity(omi_dir, cutoff, now)
    section = render_section(activity, since, now, llm=llm, omi_dir=omi_dir)
    title = f"Worklog {now.strftime('%Y-%m-%d')}"
    details = _append_section(_existing_details(omi_dir, title), section)
    fields = NoteFields(
        title=title,
        summary=_WORKLOG_SUMMARY,
        details=details,
        tags=["worklog", "omind", "checkpoint", "auto"],
    )
    return upsert_note(Path(omi_dir), fields)


# -- systemd user timer -------------------------------------------------------------


def systemd_user_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "systemd" / "user"


def _systemctl(args: list[str]) -> None:
    """Best-effort ``systemctl --user``; swallow a missing/unusable systemd so the
    unit files are still written (and the user can reload manually)."""
    try:
        subprocess.run(["systemctl", "--user", *args], check=False, capture_output=True)
    except OSError:
        return


def install_timer(
    every: str, vault: Path, folder: str, *, log: Any = print, reload: bool = True
) -> None:
    """Install + enable a systemd user timer running ``omind checkpoint run`` every
    ``every`` (e.g. ``15m``). ``Type=oneshot`` with no dependents, so a failing
    checkpoint never blocks anything."""
    secs = int(parse_since(every).total_seconds())
    if secs < 60:
        raise ValueError(f"--every must be at least 60s to avoid a tight timer loop: {every!r}")
    unit_dir = systemd_user_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    # systemd requires an ABSOLUTE path in ExecStart; a bare "omind" fallback
    # produced a unit that fires and fails every interval while the install
    # reported success (a cron job silently failing forever). Fail loudly here.
    omind = shutil.which("omind")
    if not omind:
        raise FileNotFoundError(
            "omind is not on PATH — cannot write an absolute systemd ExecStart. "
            "Install omind so `which omind` resolves, then re-run."
        )
    service = (
        "[Unit]\n"
        "Description=omind activity checkpoint\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f'ExecStart={omind} checkpoint run --since {every} '
        f'--vault "{vault}" --folder "{folder}"\n'
    )
    timer = (
        "[Unit]\n"
        f"Description=omind activity checkpoint every {every}\n"
        "\n"
        "[Timer]\n"
        f"OnBootSec={secs}s\n"
        f"OnUnitActiveSec={secs}s\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    (unit_dir / SERVICE_UNIT_NAME).write_text(service, encoding="utf-8")
    (unit_dir / TIMER_UNIT_NAME).write_text(timer, encoding="utf-8")
    log(f"  wrote {unit_dir / SERVICE_UNIT_NAME} and {unit_dir / TIMER_UNIT_NAME}")
    if reload:
        _systemctl(["daemon-reload"])
        _systemctl(["enable", "--now", TIMER_UNIT_NAME])
    log(f"  enabled {TIMER_UNIT_NAME} (every {every})")


def uninstall_timer(*, log: Any = print, reload: bool = True) -> None:
    """Disable + remove the checkpoint timer units. Idempotent."""
    if reload:
        _systemctl(["disable", "--now", TIMER_UNIT_NAME])
    unit_dir = systemd_user_dir()
    removed = False
    for name in (TIMER_UNIT_NAME, SERVICE_UNIT_NAME):
        path = unit_dir / name
        if path.exists():
            path.unlink()
            removed = True
    if reload:
        _systemctl(["daemon-reload"])
    log("  removed checkpoint timer units" if removed else "  no checkpoint timer installed")
