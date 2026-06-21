# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Harness-agnostic OMI-compliance enforcement decision engine.

`omind guard` is the single place every agent harness asks "may I run this
action?". Thin per-harness adapters (Claude Code's ``omi-guard.sh``, Hermes'
``pre_llm_call`` adapter, ...) normalize their event into the action schema
below and pipe it to ``omind guard check``. The policy and the per-turn gate
live HERE, so a rule — or a later learned lesson — enforces identically across
every agent.

Action schema (JSON on stdin to ``check``)::

    {
      "tool": "Bash",          # the tool / operation name
      "command": "...",        # shell command, for Bash-like tools (optional)
      "session": "abc123",     # session id, for the per-turn gate (optional)
      "is_omi_consult": false  # adapter sets true when this action reads OMI
    }

Decision order:
  1. An OMI consult sets the per-turn sentinel and is always allowed (so the
     gate can never deadlock — the clear-path is always available).
  2. HARD BLOCKS — every ``hard`` rule in the data-driven policy
     (:mod:`omind.policy`): the destructive/forge seed set plus any learned rule
     the recidivism loop escalated. The ``github_push`` tier denies unless the
     command opts in with ``OMI_PUSH_GITHUB=1`` (a deliberate Codeberg mirror).
     ``soft`` rules never block here — the detector (Layer E) records them.
  3. THE GATE — block until OMI was consulted this turn; ``omind guard reset``
     (the harness's turn-start hook) clears the sentinel.

The policy lives in data, but the seed rules live in code, so the hard blocks
are always enforceable here on the raw command even on a blank machine — they
cannot be skipped by a broken adapter or a missing policy file.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from omind import compliance, paths, policy

GATE_MESSAGE = (
    "consult OMI before acting this turn — read a note relevant to your task "
    "(an OMI search or read), then retry. One consult clears the rest of the "
    "turn. This is NOT a prompt to open the credential/auth notes."
)


@dataclass(frozen=True)
class Verdict:
    """A guard decision: allow (exit 0) or deny (exit 2 + ``reason``).

    ``rule_id`` names the policy rule (or ``omi-gate``) responsible for a deny,
    so the compliance log and the recidivism loop can attribute it.
    """

    allow: bool
    reason: str = ""
    rule_id: str = ""

    @property
    def exit_code(self) -> int:
        return 0 if self.allow else 2


def _safe_sid(session: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "", session) or "nosid"


def _sentinel_path(session: str) -> Path:
    # Lives in omind's state dir (not /tmp) so the bash adapter and this Python
    # core agree on the path cross-platform — macOS's tempdir is not /tmp.
    return paths.state_dir() / f"gate-{_safe_sid(session)}"


def _turn_path(session: str) -> Path:
    """The turn's captured task (the user prompt), stamped by the turn-start
    reset so the verifier (Layer C) and retrieval know what the agent is working
    on. A sibling of the gate sentinel, so both turn-start paths agree."""
    return paths.state_dir() / f"turn-{_safe_sid(session)}.txt"


def begin_turn(session: str, task: str) -> None:
    """Record this turn's task (best-effort, never raises). Written by
    ``omind guard reset``; the Claude adapter writes the same file in pure bash.

    Also resets the per-turn re-close counter, so the verifier's anti-wedge cap is
    measured per turn (the bash turn-start hook clears the same counter file)."""
    _clear_reclose(session)
    with contextlib.suppress(OSError):
        path = _turn_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(task, encoding="utf-8")


def turn_task(session: str) -> str:
    """This turn's captured task, or ``""`` if none was stamped. Never raises."""
    try:
        return _turn_path(session).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _read_sentinel(session: str) -> dict[str, Any]:
    """The gate sentinel's JSON body ({} when empty/absent/garbage). The bash
    adapter creates the file empty (``touch``); Python enriches it with the
    turn's consult records."""
    try:
        raw = _sentinel_path(session).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_sentinel(session: str, data: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        path = _sentinel_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")


def mark_consulted(session: str) -> None:
    """Mark OMI consulted this turn — the sentinel's *existence* is the gate.
    Preserves any consult records already captured this turn."""
    data = _read_sentinel(session)
    data.setdefault("consults", [])
    _write_sentinel(session, data)


def record_consult(
    session: str, *, kind: str, target: str, relevant: bool | None = None
) -> None:
    """Append one OMI consult (note read / search) to the turn's sentinel with
    its relevance verdict (``None`` = not yet judged), and mark the gate
    consulted. Never raises."""
    data = _read_sentinel(session)
    existing = data.get("consults")
    consult_list = existing if isinstance(existing, list) else []
    consult_list.append({"kind": kind, "target": target, "relevant": relevant})
    data["consults"] = consult_list
    _write_sentinel(session, data)


def consults(session: str) -> list[dict[str, Any]]:
    """The consults recorded this turn (each ``{kind, target, relevant}``)."""
    raw = _read_sentinel(session).get("consults")
    return [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []


def consulted_this_turn(session: str) -> bool:
    return _sentinel_path(session).exists()


#: Pre-state-dir prototype guards wrote the per-turn sentinel to ``/tmp`` rather
#: than the state dir. The canonical guard never writes there, so any such file
#: is legacy litter the turn-start reset reaps — otherwise a machine upgrading
#: from the buggy version leaves stale ``/tmp/omi-gate-*`` files behind. A tuple
#: (not a hardcoded path) so tests can point it at a temp dir.
_LEGACY_SENTINEL_DIRS: tuple[Path, ...] = (Path("/tmp"), Path(tempfile.gettempdir()))
_LEGACY_SENTINEL_GLOB = "omi-gate-*"


def _reap_legacy_sentinels() -> None:
    """Delete leftover ``/tmp/omi-gate-*`` sentinels from the prototype guard."""
    seen: set[Path] = set()
    for directory in _LEGACY_SENTINEL_DIRS:
        if directory in seen:
            continue
        seen.add(directory)
        try:
            stale = list(directory.glob(_LEGACY_SENTINEL_GLOB))
        except OSError:
            continue
        for path in stale:
            with contextlib.suppress(OSError):
                path.unlink()


def clear_gate(session: str) -> None:
    """Clear the per-turn consult sentinel (the harness's turn-start reset).

    Also reaps legacy ``/tmp/omi-gate-*`` sentinels left by the pre-state-dir
    prototype guard, so a machine upgrading from that version does not keep stale
    sentinels around (the canonical guard never writes ``/tmp``).

    Does NOT touch the re-close counter — the verifier re-closes the gate by
    calling this, and the counter must survive across re-closes within a turn (it
    is reset only at turn start, by :func:`begin_turn`)."""
    with contextlib.suppress(OSError):
        _sentinel_path(session).unlink()
    _reap_legacy_sentinels()


def _reclose_path(session: str) -> Path:
    """Per-turn count of how many times REQUIRE-mode re-closed the gate. A sibling
    of the sentinel that SURVIVES :func:`clear_gate` (which the re-close calls), so
    the verifier can cap re-closes and never deadlock the agent. Reset at turn
    start, alongside the sentinel."""
    return paths.state_dir() / f"reclose-{_safe_sid(session)}"


def reclose_count(session: str) -> int:
    """How many times the gate was re-closed this turn (0 when none/absent)."""
    try:
        return int(_reclose_path(session).read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def bump_reclose(session: str) -> int:
    """Increment and return this turn's re-close count. Never raises."""
    nxt = reclose_count(session) + 1
    with contextlib.suppress(OSError):
        path = _reclose_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(nxt), encoding="utf-8")
    return nxt


def _clear_reclose(session: str) -> None:
    with contextlib.suppress(OSError):
        _reclose_path(session).unlink()


def clear_all_gates() -> None:
    """Clear EVERY per-turn sentinel + re-close counter — the recovery path for a
    by-hand ``omind guard reset`` with no session id (a human un-wedging the gate
    cannot know the live session id, so a single-session clear would miss it).
    Also reaps the legacy ``/tmp`` sentinels. Never raises."""
    state = paths.state_dir()
    for pattern in ("gate-*", "reclose-*"):
        try:
            stale = list(state.glob(pattern))
        except OSError:
            continue
        for path in stale:
            with contextlib.suppress(OSError):
                path.unlink()
    _reap_legacy_sentinels()


def decide(action: dict[str, Any]) -> Verdict:
    """The harness-agnostic policy. See the module docstring for the schema."""
    session = str(action.get("session") or "")
    command = str(action.get("command") or "")

    # 1) Consulting OMI sets the per-turn sentinel and is always allowed. When
    # the adapter knows what was consulted, record it (with target) so the
    # verifier can judge relevance; otherwise just mark the gate consulted.
    if action.get("is_omi_consult"):
        target = str(action.get("consult_target") or "")
        if target:
            record_consult(
                session, kind=str(action.get("consult_kind") or "consult"), target=target
            )
        else:
            mark_consulted(session)
        return Verdict(allow=True)

    # 2) Hard blocks — every ``hard`` rule in the data-driven policy. The
    # github_push tier is skipped when the command carries its opt-in token (a
    # deliberate Codeberg mirror). Soft rules never block here (Layer E records
    # them). The opt-in only skips its own rule, so it can never bypass a
    # destructive rule a command also matches.
    for rule in policy.load_policy():
        if rule.severity != policy.SEVERITY_HARD:
            continue
        if not rule.compiled().search(command):
            continue
        if rule.opt_in and re.search(rule.opt_in, command):
            continue
        return Verdict(
            allow=False,
            reason=f"omi-guard ({rule.label()}): {rule.message}",
            rule_id=rule.id,
        )

    # 3) The gate — block until OMI was consulted this turn.
    if consulted_this_turn(session):
        return Verdict(allow=True)
    return Verdict(allow=False, reason=f"omi-gate: {GATE_MESSAGE}", rule_id="omi-gate")


def check_action(action: dict[str, Any]) -> Verdict:
    """Decide an action and log a real policy-rule deny to the compliance log.

    The shared core behind ``omind guard check`` and the per-harness adapters
    (:mod:`omind.adapters`), so every harness logs + decides identically. The
    routine ``omi-gate`` "you didn't consult" deny is friction, not logged.
    """
    verdict = decide(action)
    if not verdict.allow and verdict.rule_id and verdict.rule_id != "omi-gate":
        compliance.log_event(
            compliance.KIND_DECISION,
            session=str(action.get("session") or ""),
            tool=str(action.get("tool") or ""),
            command=str(action.get("command") or ""),
            rule_id=verdict.rule_id,
            severity=policy.SEVERITY_HARD,
            outcome="deny",
        )
    return verdict


def _load(stream: TextIO) -> dict[str, Any]:
    # Reading an interactive terminal blocks forever — and a by-hand recovery run
    # (`omind guard reset` typed at a shell) has no piped payload. Treat a TTY
    # stdin as empty rather than hang. Hook input is always piped (never a TTY),
    # so the live path is unchanged; only a human running the command benefits.
    try:
        if stream.isatty():
            return {}
    except (AttributeError, ValueError, OSError):
        pass
    try:
        data = json.loads(stream.read() or "{}")
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def run_guard(
    action_name: str,
    stream: TextIO | None = None,
    *,
    omi_dir: Path | None = None,
    harness: str = "claude",
    limit: int = 20,
    command: str = "",
    explain: bool = False,
) -> int:
    """CLI entry for ``omind guard <action>``. Returns the process exit code.

    ``check`` reads an action descriptor on stdin and prints the deny reason to
    stderr when blocking. ``reset`` clears the session's per-turn sentinel.
    ``learn`` compiles a violation (stdin JSON) into a soft rule + OMI note;
    ``escalate`` walks the recidivism ladder. Unknown actions are a no-op (exit
    0) — a guard must never wedge the agent.
    """
    src = stream if stream is not None else sys.stdin
    if action_name == "reset":
        data = _load(src)
        session = str(data.get("session") or data.get("session_id") or "")
        if session:
            clear_gate(session)
            begin_turn(session, str(data.get("prompt") or ""))
        else:
            # No session id — a human running `omind guard reset` by hand to
            # recover a wedged gate. Clear every gate, since they can't know which
            # session is stuck. (The hook path always supplies a session.)
            clear_all_gates()
        return 0
    if action_name == "learn":
        return _run_learn(_load(src), omi_dir)
    if action_name == "escalate":
        return _run_escalate()
    if action_name == "log":
        return _run_log(limit)
    if action_name == "policy":
        return _run_policy()
    if action_name == "explain":
        return _run_explain(command)
    if action_name == "status":
        return _run_status()
    if action_name == "repair":
        return _run_repair(omi_dir)
    if action_name == "suggest":
        return _run_suggest(_load(src), omi_dir)
    if action_name == "verify":
        return _run_verify(_load(src), omi_dir, explain)
    if action_name == "adapter":
        from omind import adapters

        return adapters.run_adapter(src, omi_dir=omi_dir, harness=harness)
    if action_name == "selftest":
        from omind import harness as harness_mod

        results = harness_mod.run_selftest()
        for r in results:
            mark = "ok" if r["ok"] else "FAIL"
            sys.stdout.write(
                f"[{mark}] {r['harness']:8} {r['format']:12} "
                f"blocked={r['blocked']} :: {r['command']}\n"
            )
        return 0 if all(r["ok"] for r in results) else 1
    if action_name == "export-corpus":
        from omind import corpus

        count = corpus.export_corpus(sys.stdout)
        sys.stderr.write(f"exported {count} corpus example(s)\n")
        return 0
    if action_name == "check":
        verdict = check_action(_load(src))
        if not verdict.allow:
            sys.stderr.write(f"BLOCKED by {verdict.reason}\n")
        return verdict.exit_code
    return 0


def _run_learn(data: dict[str, Any], omi_dir: Path | None) -> int:
    """``omind guard learn``: compile a violation descriptor into enforcement."""
    from omind import learn

    pattern = str(data.get("pattern") or "").strip()
    message = str(data.get("message") or "").strip()
    if not pattern or not message:
        sys.stderr.write("guard learn: 'pattern' and 'message' are required\n")
        return 1
    result = learn.learn_violation(
        pattern=pattern,
        message=message,
        rule_id=(str(data["rule_id"]).strip() if data.get("rule_id") else None),
        omi_dir=omi_dir,
        note_title=(str(data["note_title"]) if data.get("note_title") else None),
        note_summary=str(data.get("note_summary") or ""),
        note_body=str(data.get("note_body") or ""),
    )
    msg = f"learned rule {result.rule_id}"
    if result.note_action:
        msg += f"; OMI note {result.note_action}"
    sys.stdout.write(msg + "\n")
    return 0


def _run_escalate() -> int:
    """``omind guard escalate``: apply the recidivism ladder to learned rules."""
    from omind import learn

    changes = learn.escalate()
    if not changes:
        sys.stdout.write("no learned rules crossed an escalation threshold\n")
        return 0
    for change in changes:
        verifier = " + verifier" if change.verify else ""
        sys.stdout.write(
            f"escalated {change.rule_id}: {change.from_severity} -> "
            f"{change.to_severity}{verifier} ({change.count} hits)\n"
        )
    return 0


def _run_suggest(data: dict[str, Any], omi_dir: Path | None) -> int:
    """``omind guard suggest``: print the gate-deny message naming the notes
    relevant to this turn's task (Phase 3.2). Prints to STDOUT and exits 0 so the
    bash adapter can capture it and emit the actual exit-2 deny itself."""
    session = str(data.get("session_id") or data.get("session") or "")
    task = turn_task(session)
    if omi_dir is not None:
        from omind import retrieve

        message = retrieve.suggest_message(task, omi_dir)
    else:
        message = GATE_MESSAGE
    sys.stdout.write(f"BLOCKED by omi-gate: {message}\n")
    return 0


def _run_verify(data: dict[str, Any], omi_dir: Path | None, explain: bool = False) -> int:
    """``omind guard verify``: judge an OMI-consult event's relevance (manual /
    test entry; the live path runs inside the PostToolUse hook). ``--explain``
    prints the score/thresholds/band/verdict diagnostic without side effects."""
    if omi_dir is None:
        sys.stdout.write("not-a-consult\n")
        return 0
    from omind import verify

    if explain:
        info = verify.explain_consult(data, omi_dir)
        sys.stdout.write((json.dumps(info, indent=2) if info else "not-a-consult") + "\n")
        return 0
    verdict = verify.verify_consult(data, omi_dir)
    sys.stdout.write((verdict or "not-a-consult") + "\n")
    return 0


def _run_log(limit: int) -> int:
    """``omind guard log``: human view of the compliance log + a rollup."""
    summary = compliance.summary()
    sys.stdout.write(
        f"compliance log: {summary['total']} event(s), {summary['denies']} deny, "
        f"{summary['violations']} violation(s)"
        + (f"; last {summary['last_ts']}" if summary["last_ts"] else "")
        + "\n"
    )
    if summary["top_rules"]:
        top = ", ".join(f"{rid}×{n}" for rid, n in summary["top_rules"])
        sys.stdout.write(f"top rules: {top}\n")
    for event in compliance.read_events(limit=limit):
        sys.stdout.write(
            f"  {event.get('ts', ''):19}  {str(event.get('kind', '')):9} "
            f"{str(event.get('outcome', '')):9} {str(event.get('rule_id', '')):24} "
            f"{event.get('command', '')}\n"
        )
    return 0


def _run_policy() -> int:
    """``omind guard policy``: list the active deny set (seed + learned)."""
    rules = policy.load_policy()
    for rule in rules:
        flag = " [verify]" if rule.verify else ""
        sys.stdout.write(
            f"  [{rule.severity:4}] {rule.tier:11} {rule.source:7} "
            f"hits={rule.hits:<3} {rule.id}{flag}\n"
        )
    learned = sum(1 for rule in rules if rule.source == "learned")
    sys.stdout.write(f"{len(rules)} rule(s): {len(rules) - learned} seed + {learned} learned\n")
    return 0


def _run_explain(command: str) -> int:
    """``omind guard explain --command "<cmd>"``: which policy rules a command
    hits + the verdict, WITHOUT touching the gate/sentinel (a pure dry-run)."""
    if not command:
        sys.stderr.write('guard explain: pass --command "<cmd>"\n')
        return 1
    matched: list[tuple[policy.Rule, bool]] = []
    for rule in policy.load_policy():
        if rule.compiled().search(command):
            opted_in = bool(rule.opt_in and re.search(rule.opt_in, command))
            matched.append((rule, opted_in))
    if not matched:
        sys.stdout.write(f"ALLOW (no policy rule matches): {command}\n")
        return 0
    for rule, opted_in in matched:
        state = "opt-in→allow" if opted_in else rule.severity
        sys.stdout.write(f"  [{state}] {rule.id} ({rule.tier}): {rule.message}\n")
    blocking = [r for r, opted in matched if r.severity == policy.SEVERITY_HARD and not opted]
    sys.stdout.write(("DENY" if blocking else "ALLOW") + f": {command}\n")
    return 0


def _run_status() -> int:
    """``omind guard status``: the harnesses omind can guard + their capability."""
    from omind import harness as harness_mod

    for name, spec in harness_mod.HARNESSES.items():
        sys.stdout.write(
            f"  {name:10} capability={spec.capability:11} "
            f"format={spec.block_format:12} — {spec.description}\n"
        )
    return 0


def _run_repair(omi_dir: Path | None) -> int:
    """``omind guard repair``: re-provision the OMI guard hook-set, fixing a
    clobbered/stale settings hook path or OMI_DIR mismatch (the wedge we hit)."""
    from omind.provision import heal_omi_guard

    vault = omi_dir.parent if omi_dir is not None else None
    folder = omi_dir.name if omi_dir is not None else "OMI"
    changed = heal_omi_guard(vault=vault, folder=folder, log=print)
    sys.stdout.write(
        "repaired the OMI guard hook-set\n"
        if changed
        else "OMI guard already healthy (nothing to repair)\n"
    )
    return 0
