# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Data-driven OMI-compliance policy — the deny set as appendable data.

Phase 2 of the enforcement roadmap promotes the guard's in-code deny set to a
DATA table the learning loop appends to. The SEED rules still live in code, so a
blank machine enforces with no files on disk (cold-start safe); *learned* rules
are read from / written to ``state_dir()/policy.json`` under the same advisory
file lock every omind writer uses.

A rule's :attr:`Rule.pattern` is matched against a normalized action command.
:attr:`Rule.severity` decides the verdict:

* ``hard`` — deny outright (the destructive/forge set + github-push tier).
* ``soft`` — recorded by the detector (Layer E) but does **not** block; the
  recidivism loop (:mod:`omind.learn`) can escalate a soft rule to ``hard``.

The ``github_push`` tier denies unless the command carries the rule's
:attr:`Rule.opt_in` token (``OMI_PUSH_GITHUB=1``) — the deliberate-mirror path.
The verdict label the guard prints is derived here so the wording lives in one
place: ``github-push`` for that tier, otherwise the severity.
"""

from __future__ import annotations

import bisect
import contextlib
import functools
import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from omind import filelock, paths

SEVERITY_HARD = "hard"
SEVERITY_SOFT = "soft"

TIER_DESTRUCTIVE = "destructive"
TIER_GITHUB_PUSH = "github_push"
TIER_SUDO = "sudo"
TIER_LEARNED = "learned"


def opt_in_satisfied(opt_in: str, command: str) -> bool:
    """True only when the ``VAR=VALUE`` opt-in token appears as a REAL leading
    environment assignment — at the command start, right after a shell separator
    (``;`` / ``&&`` / ``|`` / a NEWLINE), or via ``env`` — so it actually takes effect.

    A bare substring match (the old behaviour) let the token be forged in a comment
    or a string arg (``rm -rf / # OMI_SUDO_OK=1``, ``echo "OMI_SUDO_OK=1"``) and
    silently bypass a hard rule without ever setting the variable. That is not a
    deliberate opt-in, so it must not skip the deny.

    A newline IS a shell command boundary, so a line-leading assignment inside a
    multi-line script (``…\\n  OMI_PUSH_GITHUB=1 git push …``) is legitimate and must
    be recognised — omitting ``\\n`` from the separator class wrongly rejected it
    (3.0.2). A plain space is NOT a separator, so a mid-line ``echo OMI_SUDO_OK=1``
    still doesn't count.

    The optional ``env `` prefix must ITSELF be at command position — otherwise
    ``echo "use env OMI_SUDO_OK=1" && sudo …`` forged the opt-in from inside a
    string (the ``\\benv``-anywhere bug) and skipped a hard rule.

    Lives here (not in guard.py) so both the guard's deny path and the compliance
    detector's Layer-E recording share one strict matcher (2026-08-27 review)."""
    pattern = r"(?:^|[;&|\n])[ \t]*(?:env[ \t]+)?" + re.escape(opt_in) + r"(?=\s|$)"
    return re.search(pattern, command) is not None


#: Tokens that transparently precede the real binary when deciding whether a
#: heredoc's owner is a shell (``env FOO=1 bash <<EOF``).
_HEREDOC_OWNER_SKIP = frozenset({"env", "command", "exec", "nohup", "time", "builtin"})

#: Words in command position that are not the program a stage runs: wrappers
#: that exec the next word (``xargs``, ``sudo``, ``env``, ``timeout`` …) and
#: shell keywords that introduce a command (``do``, ``then``, ``{`` …). The
#: value is the wrapper's switches that take a SEPARATE argument, so
#: ``sudo -u bob sed -i`` and ``xargs -I {} sed -i`` still reach the editor.
#: One table for the guard's stage parser and :data:`_CMD_WRAPPERS` (#430).
#: No listed switch takes an OPTIONAL value (getopt ``x::``); adding one needs
#: its own branch in ``_wrapper_pattern`` and ``guard._switch_width`` (#451).
STAGE_WRAPPERS: dict[str, frozenset[str]] = {
    **{name: frozenset() for name in _HEREDOC_OWNER_SKIP},
    **{kw: frozenset() for kw in ("do", "then", "else", "elif", "if", "while", "until", "{", "!")},
    "xargs": frozenset({"-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a"}),
    "sudo": frozenset({"-u", "-g", "-C", "-h", "-p", "-U", "-r", "-t", "-D"}),
    # Not `-S`: its value is itself a command (`env -S sudo id`), so the word
    # after it stays in command position (#430 review).
    "env": frozenset({"-u", "-C", "--unset", "--chdir"}),
    "nice": frozenset({"-n"}),
    "timeout": frozenset({"-s", "-k"}),
    "time": frozenset({"-f", "-o"}),
    # #434: wrappers that also exec their trailing command; #444 review:
    # `stdbuf --output L sudo id`.
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "caffeinate": frozenset({"-t", "-w"}),
    "ionice": frozenset({"-c", "-n", "-p", "-P", "-u"}),
    "chronic": frozenset(),
    # #444: execs the next word (`setsid sudo id`).
    "setsid": frozenset(),
}
#: Wrappers that take one positional argument before the command (``timeout 5``).
WRAPPER_POSITIONAL = frozenset({"timeout"})


#: Wrappers whose ``-v``/``-V`` makes them a LOOKUP, not an exec:
#: ``command -v sudo`` asks whether sudo is installed and runs nothing.
_LOOKUP_WRAPPERS = frozenset({"command", "builtin"})


def _wrapper_pattern(name: str, takes_arg: frozenset[str]) -> str:
    """One :data:`STAGE_WRAPPERS` entry as a regex: an optional path
    (``/usr/bin/env``), the name, its switches (a switch that takes a separate
    argument consumes it, even a negative value such as ``nice -n -5``), and
    ``timeout``'s positional duration, which more switches and ``--`` may
    follow (``timeout 5s -k 2s``, ``timeout 5 --``). ``command``/``builtin``
    with ``-v``/``-V`` is a lookup, so those switches end the match.

    Each word can match only one way, so a run of wrapper words cannot make the
    pattern backtrack exponentially: a value-taking switch is kept out of the
    generic ``-…`` branch and always consumes the next word, and the duration
    must start with a digit where every switch starts with ``-``.

    A short cluster reads like getopt, as ``guard._switch_width`` reads it
    (#451): the first value-taking letter takes the rest of the word, or the
    next word when it ends the cluster, so ``env -iC /x sudo id`` and
    ``xargs -0I {} sudo id`` still reach the program. A cluster ending in a
    value-taking letter holds no other one before it, so it matches only the
    value branch."""
    switch = r"-[^\svV]*" if name in _LOOKUP_WRAPPERS else r"-\S*"
    if takes_arg:
        long_names = sorted(opt for opt in takes_arg if len(opt) != 2)
        letters = "".join(sorted(opt[1] for opt in takes_arg if len(opt) == 2))
        valued = [re.escape(opt) for opt in long_names]
        if letters:
            letter_class = re.escape(letters)
            valued.append(rf"-(?!-)[^\s{letter_class}]*[{letter_class}]")
        names = "|".join(valued)
        switch = rf"(?:{names})[ \t]+\S+|(?!(?:{names})(?:\s|$)){switch}"
    switches = rf"(?:[ \t]+(?:{switch}))*"
    pattern = r"(?:[./][^\s;&|`()]*/)?" + re.escape(name) + switches
    if name in WRAPPER_POSITIONAL:
        pattern += r"[ \t]+\d[\d.]*[smhd]?" + switches
    return pattern


#: Shell wrapper/keyword tokens that transparently precede the real command, so
#: the anchored token is still in command position after them:
#: ``if/while … ; then sudo …``, ``exec sudo …``, ``nohup sudo …``,
#: ``xargs sudo …``, ``env sudo …``, ``timeout 5 sudo …``. Without these,
#: ``if true; then sudo rm`` sailed past the sudo hard rule (a fail-open of a
#: hard control). Derived from :data:`STAGE_WRAPPERS` so the two lists cannot
#: drift (#430: ``env``, ``nice`` and ``timeout`` were missing here).
_CMD_WRAPPERS = "|".join(
    _wrapper_pattern(name, args) for name, args in sorted(STAGE_WRAPPERS.items())
)

#: Prefix that anchors a ``match="command"`` pattern to COMMAND POSITION: the
#: command start, or immediately after a shell separator (``;`` ``&`` ``|``
#: NEWLINE ``(`` backtick — single chars suffice since ``&&`` / ``||`` / ``$(``
#: all END in a char in the class), skipping any leading ``VAR=val`` environment
#: assignments and shell wrapper keywords (:data:`_CMD_WRAPPERS`), and an
#: optional absolute/relative path to the binary (``/usr/bin/sudo``,
#: ``./x/sudo``). This is how a token like ``sudo`` is matched only when it is
#: the command being run — not when it appears as a grep arg, a path segment, a
#: filename, a commit message, or a ``pass show sudo/...`` value (the #98/#108
#: false-positive class). It mirrors the leading-assignment idea already proven
#: in ``guard._opt_in_satisfied``. Use ``[ \t]`` (not ``\s``) so the
#: assignment-skip never crosses a newline into another command.
_CMD_POSITION = (
    r"(?:^|[\n;&|`(])[ \t]*"
    r"(?:(?:\w+=\S*|" + _CMD_WRAPPERS + r")[ \t]+)*"
    r"(?:[./][^\s;&|`()]*/)?"
)

#: The most work :func:`command_search` lets one ``_CMD_POSITION`` search do,
#: in :func:`_cmd_position_cost` units (about a character step each). The
#: prefix's ``\w+=\S*`` assignment skip rescans a blank-free run from every
#: separator inside it, so ``x=1;`` repeated 10,000 times took over ten seconds
#: per hard rule and timed the hook out (#445). Ordinary commands cost a few
#: thousand units; a search above the budget fails closed instead of running.
CMD_SEARCH_BUDGET = 2_000_000
_SEPARATOR_CHARS = "\n;&|`("
#: A word that may be a link in that chain: an assignment, a switch, a
#: duration, or a wrapper (optionally by path).
_CHAIN_LINK_RE = re.compile(
    r"\w+=|-|\d|(?:[./][^\s;&|`()]*/)?(?:"
    + "|".join(re.escape(name) for name in sorted(STAGE_WRAPPERS))
    + r")"
)
_CHAIN_GAP_RE = re.compile(r"[ \t]+")
_WORD_SPAN_RE = re.compile(r"\S+")


@functools.lru_cache(maxsize=256)
def _bare_pattern(pattern: str) -> re.Pattern[str] | None:
    """``pattern`` compiled on its own, or ``None`` when only its anchored
    form compiles."""
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _cmd_position_cost(text: str) -> int:
    """An upper bound on the work a ``_CMD_POSITION`` search of ``text`` does
    (#445). The search tries every separator as a start, and from each one
    skips a chain of assignment and wrapper words, each a blank-free run
    joined by blanks; so the bound sums, over the starts, the extent of the
    chain each could skip. A word counts as a link unless it cannot be an
    assignment, a wrapper, a switch or a switch's value, so the bound only
    overestimates. Linear; a short text returns its trivial bound unscanned."""
    n = len(text)
    if n * n <= CMD_SEARCH_BUDGET:
        return n * n
    spans = [(m.start(), m.end()) for m in _WORD_SPAN_RE.finditer(text)]
    count = len(spans)
    # joined[i]: word i + 1 follows word i across blanks only (`[ \t]+`).
    joined = [
        bool(_CHAIN_GAP_RE.fullmatch(text, spans[i][1], spans[i + 1][0])) for i in range(count - 1)
    ] + [False]
    # reach[i]: how far a chain that enters word i can run.
    reach = [n] * (count + 1)
    for i in range(count - 1, -1, -1):
        start, end = spans[i]
        link = bool(_CHAIN_LINK_RE.match(text, start)) or (
            i > 0 and text[spans[i - 1][0]] == "-"  # a switch's value
        )
        reach[i] = reach[i + 1] if link and joined[i] else end
    cost = n
    previous_end = 0
    for i, (start, end) in enumerate(spans):
        # A newline before this word starts a chain at it; `^` at offset 0.
        cost += (text.count("\n", previous_end, start) + (i == 0)) * (reach[i] - previous_end)
        # A separator inside it starts a chain at the rest of it.
        seps = sum(text.count(ch, start, end) for ch in _SEPARATOR_CHARS if ch != "\n")
        if seps:
            cost += seps * ((reach[i + 1] if joined[i] else end) - start)
        previous_end = end
    return cost


class SearchBudgetExceededError(Exception):
    """A command-position search that could exceed :data:`CMD_SEARCH_BUDGET`
    on a text holding the rule's keyword: the command was not judged (#445)."""


def command_search(compiled: re.Pattern[str], pattern: str, text: str) -> bool:
    """``compiled.search(text)`` for ``compiled``, a ``_CMD_POSITION``-anchored
    ``pattern``, in bounded time (#445). A text where ``pattern`` occurs
    nowhere cannot match its anchored form, so it is answered at once. Where
    it does occur and the anchored search could exceed
    :data:`CMD_SEARCH_BUDGET`, raises :class:`SearchBudgetExceededError`: the
    caller decides. The hard rules deny such a command with a reason of its
    own (fail CLOSED); everything else treats it as not judged, no match."""
    bare = _bare_pattern(pattern)
    if bare is not None and bare.search(text) is None:
        return False
    if _cmd_position_cost(text) > CMD_SEARCH_BUDGET:
        raise SearchBudgetExceededError(pattern)
    return compiled.search(text) is not None


#: Interpreters whose heredoc body IS shell code for THIS shell to run, so its
#: separators are real and its body must stay visible to
#: :func:`shell_code_text`. Anything else (``cat``, ``python``, ``gh issue
#: create --body``) receives the body as DATA.
_SHELL_HEREDOC_BINARIES = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "ash"})

#: A heredoc redirection: ``<<EOF``, ``<<-EOF``, ``<<'EOF'``, ``<<"EOF"``.
_HEREDOC_RE = re.compile(
    r"<<(-?)[ \t]*(?:(['\"])([A-Za-z_][A-Za-z0-9_]*)\2|([A-Za-z_][A-Za-z0-9_]*))"
)


#: The characters a heredoc's owning command starts after.
_OWNER_SEP_RE = re.compile(r"[\n;&|(`]")


def _heredoc_owner_is_shell(command: str, start: int) -> bool:
    """True when the simple command owning the heredoc at ``start`` is a shell.

    Scans back to the nearest separator and takes the first token that is not a
    ``VAR=val`` assignment or a transparent wrapper, comparing its basename.
    One heredoc's answer; :class:`_HeredocOwners` answers a run of them.
    """
    return _HeredocOwners(command).is_shell(start)


def _owner_word_skipped(token: str) -> bool:
    """Whether a word before a heredoc's owner is passed over: a ``VAR=val``
    assignment or a transparent wrapper."""
    if "=" in token and not token.startswith("-"):
        return True
    return token.rsplit("/", 1)[-1] in _HEREDOC_OWNER_SKIP


#: The longest name the owner lookup compares a basename against: a longer
#: one is neither skipped nor a shell.
_OWNER_NAME_MAX = max(len(name) for name in _HEREDOC_OWNER_SKIP | _SHELL_HEREDOC_BINARIES)


class _HeredocOwners:
    """:func:`_heredoc_owner_is_shell` for every heredoc in ``command``, in
    linear time overall (#445). Each separator-bounded segment's words are
    found once, with the first one that is not skipped; a heredoc's owner is
    that word when it ends before the heredoc. Walking the words from the
    separator per heredoc was quadratic in a run of assignments followed by
    a run of heredocs (``a=1 a=1 … <<E <<E …``)."""

    def __init__(self, command: str) -> None:
        self.command = command
        self.seps = [m.start() for m in _OWNER_SEP_RE.finditer(command)]
        # Per segment (keyed by its separator): its words' spans, their
        # ends, and the index of the first word not skipped (or the count).
        self.segments: dict[int, tuple[list[tuple[int, int]], list[int], int]] = {}

    def _segment(self, k: int) -> tuple[list[tuple[int, int]], list[int], int]:
        cut = self.seps[k - 1] if k else -1
        found = self.segments.get(cut)
        if found is None:
            stop = self.seps[k] if k < len(self.seps) else len(self.command)
            spans = [m.span() for m in _WORD_SPAN_RE.finditer(self.command, cut + 1, stop)]
            first = len(spans)
            for idx, (ws, we) in enumerate(spans):
                if not _owner_word_skipped(self.command[ws:we]):
                    first = idx
                    break
            found = (spans, [we for _ws, we in spans], first)
            self.segments[cut] = found
        return found

    def is_shell(self, start: int) -> bool:
        command = self.command
        spans, ends, first = self._segment(bisect.bisect_left(self.seps, start))
        # The words wholly before the heredoc are read as they are; the one
        # it starts inside (`bash<<E`) is read up to it, as a scan bounded at
        # the heredoc would.
        whole = bisect.bisect_right(ends, start)
        if first < whole:
            ws, we = spans[first]
            return command[ws:we].rsplit("/", 1)[-1] in _SHELL_HEREDOC_BINARIES
        if whole == len(spans) or spans[whole][0] >= start:
            return False
        ws = spans[whole][0]
        # Its base is the text after its last `/` before the heredoc; only a
        # short one can be a name, so read no further back than that.
        slash = command.rfind("/", max(ws, start - _OWNER_NAME_MAX - 1), start)
        base_at = slash + 1 if slash >= 0 else ws
        if start - base_at > _OWNER_NAME_MAX:
            return False  # neither skipped nor a shell
        base = command[base_at:start]
        assignment = command[ws] != "-" and command.find("=", ws, start) >= 0
        if assignment or base in _HEREDOC_OWNER_SKIP:
            return False  # skipped, and no word follows it before the heredoc
        return base in _SHELL_HEREDOC_BINARIES


#: What :func:`shell_code_text` takes in one step: in code context, a stretch
#: of plain text and whole single-quoted strings; in a double-quoted body, the
#: text before the next character it acts on. So a megabyte of plain text is
#: not walked a character at a time (#445).
_CODE_STRETCH_RE = re.compile(r"(?:[^\\'\"$`)<\n]+|'[^']*')+")
_DQUOTE_STOP_RE = re.compile(r"[\\\"$`]")


def _blank_single_quoted(text: str) -> str:
    """``text`` (plain shell code and whole ``'…'`` strings) with every
    single-quoted body blanked to spaces, in C-level passes (#445)."""
    parts = text.split("'")  # odd parts are the bodies
    parts[1::2] = map(" ".__mul__, map(len, parts[1::2]))
    return "'".join(parts)


@functools.lru_cache(maxsize=16)
def shell_code_text(command: str) -> str:
    """``command`` with every DATA region blanked out, length-preserving.

    A quoted string's body, and a heredoc body fed to something that is not a
    shell, are payload — text handed to another program or another host. They
    are *not* commands this shell runs, so the separators inside them are not
    separators (#317). Blanking them (to spaces, so offsets and any surrounding
    structure survive) is what lets a command-position test mean what it says.

    This is the anchoring primitive behind ``match="command"`` and the guard's
    local-repo classifiers. Without it, the ``&&`` inside
    ``ssh host 'cd /p && git commit …'`` made a REMOTE commit look like local
    repo work, and a newline inside a ``gh issue create --body "$(cat <<'MD' …``
    prose block put every line of that prose in command position — both
    reproduced live while writing #317.

    Deliberately NOT applied to the side-effect classifiers: a
    ``ssh host 'systemctl restart …'`` is a real side effect, merely a remote
    one, and masking there would be a fail-open rather than a fix. Shell
    heredocs (``bash <<EOF``) keep their bodies for the same reason.

    Command substitutions inside a double-quoted string (``"$(cat …)"``,
    ``"`cmd`"``) are code again — the shell runs them — so the scanner steps
    back into code context for their extent.
    """
    out = list(command)
    stack: list[str] = []
    heredocs: list[tuple[str, bool, bool]] = []
    owners: _HeredocOwners | None = None
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if stack and stack[-1] in "'\"":
            # --- DATA context: blank everything up to the closing quote. ---
            # Skip straight to the next character that can end or re-open
            # code (#445); everything before it is blanked.
            if stack[-1] == "'":
                stop = command.find("'", i)
                stop = n if stop == -1 else stop
            else:
                found = _DQUOTE_STOP_RE.search(command, i)
                stop = n if found is None else found.start()
            if stop > i:
                out[i:stop] = " " * (stop - i)
                i = stop
                continue
            if stack[-1] == '"':
                # Only a double-quoted body re-enters code for a substitution,
                # and only there does a backslash escape the next character.
                if ch == "\\" and i + 1 < n:
                    out[i] = out[i + 1] = " "
                    i += 2
                    continue
                if command.startswith("$(", i):
                    stack.append("(")
                    i += 2
                    continue
                if ch == "`":
                    stack.append("`")
                    i += 1
                    continue
            if ch == stack[-1]:
                stack.pop()
            else:
                # The newline is blanked too: it is the separator that put
                # prose lines in command position.
                out[i] = " "
            i += 1
            continue
        # --- CODE context. ---
        if ch not in '\\"$`)<\n':
            # Plain text and whole single-quoted strings, which hold nothing
            # to act on, are taken as one stretch: its quoted bodies are
            # blanked at once, not a character or a quote at a time (#445).
            stretch = _CODE_STRETCH_RE.match(command, i)
            if stretch is None:  # a quote that never closes: data to the end
                out[i + 1 :] = " " * (n - i - 1)
                break
            if "'" in stretch.group():
                out[i : stretch.end()] = _blank_single_quoted(stretch.group())
            i = stretch.end()
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch in "'\"":
            stack.append(ch)
            i += 1
            continue
        if command.startswith("$(", i):
            stack.append("(")
            i += 2
            continue
        if ch == "`":
            if stack and stack[-1] == "`":
                stack.pop()
            else:
                stack.append("`")
            i += 1
            continue
        if ch == ")" and stack and stack[-1] == "(":
            stack.pop()
            i += 1
            continue
        match = _HEREDOC_RE.match(command, i)
        if match:
            if owners is None:  # every heredoc's owner, found in one pass
                owners = _HeredocOwners(command)
            delimiter = match.group(3) or match.group(4)
            heredocs.append((delimiter, bool(match.group(1)), not owners.is_shell(i)))
            i = match.end()
            continue
        if ch == "\n" and heredocs:
            i = _blank_heredoc_bodies(command, out, i + 1, heredocs)
            continue
        i += 1
    return "".join(out)


def _blank_heredoc_bodies(
    command: str, out: list[str], pos: int, heredocs: list[tuple[str, bool, bool]]
) -> int:
    """Consume the pending heredoc bodies starting at ``pos``, blanking those
    marked as data. Returns the offset just past the last one. An UNTERMINATED
    heredoc consumes the rest of the string — it never runs as this shell's
    code, so leaving its tail in command position would only re-open #317."""
    n = len(command)
    for delimiter, strip_tabs, blank in heredocs:
        while pos < n:
            eol = command.find("\n", pos)
            end = n if eol == -1 else eol
            line = command[pos:end]
            if (line.lstrip("\t") if strip_tabs else line).strip() == delimiter:
                pos = end if eol == -1 else eol + 1
                break
            if blank:
                for k in range(pos, end if eol == -1 else eol + 1):
                    out[k] = " "
            pos = end if eol == -1 else eol + 1
    heredocs.clear()
    return pos


@functools.lru_cache(maxsize=512)
def _compile_rule(pattern: str, match: str) -> re.Pattern[str]:
    """A rule's regex, anchored to command position for ``match="command"``.
    Memoised: the hard rules test every subject of a command, thousands for a
    long one, and rebuilding the anchored pattern each time cost more than
    the search (#445). Raises ``re.error`` on a bad pattern, every time."""
    if match == "command":
        return re.compile(_CMD_POSITION + r"(?:" + pattern + r")")
    return re.compile(pattern)


@dataclass
class Rule:
    """One policy rule. ``seed`` rules ship in code; ``learned`` rules persist
    to ``policy.json`` and can be escalated by the recidivism loop."""

    id: str
    pattern: str
    message: str
    severity: str = SEVERITY_HARD
    tier: str = TIER_DESTRUCTIVE
    opt_in: str | None = None
    #: ``"search"`` (default) matches ``pattern`` anywhere in the command.
    #: ``"command"`` wraps ``pattern`` in :data:`_CMD_POSITION` so it only fires
    #: when the token is in command position (start / after a shell separator,
    #: past leading env-assignments) — for escalation-keyword rules that must not
    #: false-positive on the keyword appearing as an argument or in a string.
    match: str = "search"
    source: str = "seed"
    created: str = ""
    hits: int = 0
    #: Set by escalation once a rule recurs past the verifier threshold: the
    #: action-type is flagged for Layer C scrutiny even when it would otherwise
    #: pass the gate. Carried in data so the learning loop owns the decision.
    verify: bool = False

    def compiled(self) -> re.Pattern[str]:
        return _compile_rule(self.pattern, self.match)

    def matches(self, command: str) -> bool:
        """True when this rule fires on ``command``.

        A ``match="command"`` rule is tested against :func:`shell_code_text`, so
        the keyword must be in command position in code the LOCAL shell runs —
        not inside a quoted payload or a prose heredoc body (#317). A
        ``match="search"`` rule keeps the raw text: those patterns are
        deliberately substring searches. A command too costly to judge is no
        match here (fail open); see :meth:`judge`.
        """
        return self.judge(command) is True

    def judge(self, command: str) -> bool | None:
        """:meth:`matches`, or ``None`` when ``command`` is too costly to
        judge (:class:`SearchBudgetExceededError`, #445). Only the hard-rule
        enforcement path acts on ``None`` (it denies); compliance logging and
        soft rules read it as no match."""
        if self.match == "command":
            try:
                return command_search(self.compiled(), self.pattern, shell_code_text(command))
            except SearchBudgetExceededError:
                return None
        return bool(self.compiled().search(command))

    def label(self) -> str:
        """The parenthetical the guard prints: ``github-push`` for that tier,
        else the severity (preserved wording for existing reasons)."""
        if self.tier == TIER_GITHUB_PUSH:
            return "github-push"
        if self.tier == TIER_SUDO:
            return "sudo"
        return self.severity


#: The destructive / forge deny set + the github-push opt-in tier, ported
#: verbatim from the original in-code ``guard`` rules. This is the seed of the
#: data-driven policy; the learning loop appends to ``policy.json`` over the top.
SEED_RULES: tuple[Rule, ...] = (
    Rule(
        id="gh-auth-setup-git",
        # match="command" (#101): anchor to command position so `grep -rn "gh
        # auth setup-git"`, a commit message, or a heredoc writing this rule no
        # longer false-block — routine when working on omind itself.
        pattern=r"gh\s+auth\s+setup-git\b",
        match="command",
        message=(
            "never 'gh auth setup-git'. GitHub auth = the gh-YOLO PAT from pass via "
            "a one-shot (per-command) credential helper. Read OMI: github-auth-ssh."
        ),
    ),
    Rule(
        id="gh-repo-delete",
        pattern=r"gh\s+repo\s+delete\b",
        match="command",
        message=(
            "never delete a repo from a hook-reachable command. Typed-name "
            "confirmation only. Read OMI: Operational Rules - Git Repos and Secrets."
        ),
    ),
    Rule(
        id="gh-api-repo-delete",
        # Order-independent (red-team #B1): two lookaheads after `gh api`, so
        # `gh api repos/o/r -X DELETE` (path before method) is caught as well as
        # `gh api -X DELETE repos/o/r`. Both lookaheads stay within one simple
        # command (no pipe/;/&), so an unrelated later command can't trip it.
        # Command-anchored (#101) so the phrase in a grep/commit message is safe.
        pattern=r"gh\s+api(?=[^|;&]*(?:-X\s*|--method\s*)DELETE)(?=[^|;&]*repos/)",
        match="command",
        message=(
            "never DELETE a repo via the API. Typed-name confirmation only. "
            "Read OMI: Operational Rules - Git Repos and Secrets."
        ),
    ),
    Rule(
        id="curl-api-repo-delete",
        # red-team #B1: the gh rules only cover `gh`; a raw `curl -X DELETE
        # https://api.github.com/repos/...` deleted a repo (or sub-resource)
        # straight past them. Order-independent like the gh-api rule.
        pattern=(
            r"curl(?=[^|;&]*(?:-X\s*|--request\s*)DELETE)"
            r"(?=[^|;&]*api\.github\.com/repos/)"
        ),
        match="command",
        message=(
            "never DELETE a GitHub repo/resource via the raw API. Use the reviewed "
            "path; typed-name confirmation only. Read OMI: Operational Rules - Git "
            "Repos and Secrets."
        ),
    ),
    Rule(
        id="sudo-use-fleet-sudo",
        # #98/#108: match `sudo` only in COMMAND POSITION (see _CMD_POSITION), not
        # as any token in the string — so `grep sudo`, `cat /var/log/sudo.log`,
        # `git commit -m "fix sudo"`, `pass show sudo/akclark`, and the sanctioned
        # `fleet-sudo --entry akclark/sudo` no longer false-positive, while
        # `sudo …`, `; sudo …`, `a && sudo …`, `a | sudo …`, `$(sudo …)`, and
        # `FOO=1 sudo …` still block. `fleet-sudo` never matches (it is not a
        # command-position `sudo` token), so no lookbehind is needed.
        pattern=r"sudo(?:edit)?\b",
        match="command",
        message=(
            "raw sudo is blocked — run `fleet-sudo <cmd>` instead (it reads the "
            "fleet sudo password from pass; never guess the per-host entry, never "
            "hand CJ a command to run). Deliberate raw sudo opts in with "
            "OMI_SUDO_OK=1. See the OMI Playbook."
        ),
        tier=TIER_SUDO,
        opt_in="OMI_SUDO_OK=1",
    ),
    Rule(
        id="privesc-alternatives",
        # red-team #B1: only the literal `sudo` was blocked, so pkexec / doas /
        # run0 / su walked straight past. Same tier + opt-in as raw sudo, and the
        # same command-position anchoring (#98/#108) so `man su`, `git log --grep
        # su`, `cat doas.conf`, `tmux new -s run0` don't false-positive while
        # `pkexec …` / `doas …` / `su -c … root` (at command position) still block.
        pattern=r"(?:pkexec|doas|run0|su)\b",
        match="command",
        message=(
            "raw privilege escalation is blocked — run `fleet-sudo <cmd>` instead "
            "(pkexec/doas/run0/su included). Deliberate raw escalation opts in with "
            "OMI_SUDO_OK=1. See the OMI Playbook."
        ),
        tier=TIER_SUDO,
        opt_in="OMI_SUDO_OK=1",
    ),
)

#: Persisted-rule field names (the dataclass attributes). Used to filter unknown
#: keys out of on-disk data so a forward-compat field can't crash the loader.
_RULE_FIELDS = frozenset(Rule.__dataclass_fields__)


def policy_path() -> Path:
    """The machine-local learned-rules table the learning loop appends to."""
    return paths.state_dir() / "policy.json"


def seed_policy_path() -> Path:
    """Where ``omind setup`` writes the SEED ruleset for transparency/editing.

    The guard never reads this — the seed lives in code so a blank machine
    enforces with no files — but exposing it makes the active policy inspectable.
    """
    return paths.state_dir() / "seed-policy.json"


def _rule_from_dict(data: dict[str, object]) -> Rule | None:
    """Build a Rule from on-disk data, dropping unknown keys. ``None`` if it
    lacks the required ``id``/``pattern``/``message`` (a corrupt entry is skipped,
    never fatal).

    A learned/hand-edited rule whose ``pattern`` does not compile — or matches
    the empty string (a trailing ``|``) — would crash or hard-block the guard on
    EVERY tool call (a bricked machine). It is dropped here so a bad table can
    never reach the guard hot path. A wrong-typed ``severity`` (a JSON number)
    that would silently demote a hard rule to non-blocking is likewise rejected.
    """
    kwargs = {k: v for k, v in data.items() if k in _RULE_FIELDS}
    required = ("id", "pattern", "message")
    if not all(isinstance(kwargs.get(k), str) and kwargs.get(k) for k in required):
        return None
    # Optional string fields must be strings if present (a JSON number for
    # ``severity`` would demote a hard rule; a bad ``match`` mode would confuse
    # the anchor logic).
    for key in ("severity", "tier", "match", "source", "created"):
        if key in kwargs and not isinstance(kwargs[key], str):
            return None
    try:
        rule = Rule(**kwargs)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    try:
        compiled = rule.compiled()
    except re.error:
        return None
    if compiled.search(""):  # matches everything → would block every action
        return None
    return rule


def _rule_to_dict(rule: Rule) -> dict[str, object]:
    return asdict(rule)


def load_learned() -> list[Rule]:
    """The learned rules from ``policy.json``; ``[]`` on any miss (never raises).

    Any exception, not just a read/parse error: resolving the path goes through
    ``paths.state_dir()``, which raises ``RuntimeError`` with no resolvable home
    directory. Letting that escape would take :func:`load_policy` — and with it
    the SEED rules, which live in code — down too, so every hard rule would
    fail open (#420).
    """
    try:
        raw = json.loads(policy_path().read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    rules: list[Rule] = []
    for item in raw:
        if isinstance(item, dict):
            rule = _rule_from_dict(item)
            if rule is not None:
                rule.source = "learned"
                rules.append(rule)
    return rules


def load_policy() -> list[Rule]:
    """The active policy: SEED rules first, then learned rules. SEED is always
    present (it lives in code), so this is safe on a blank machine."""
    return [*SEED_RULES, *load_learned()]


def _mutate_learned(fn: Callable[[list[Rule]], list[Rule]]) -> None:
    """Load, transform, and atomically rewrite ``policy.json`` under the lock.

    Best-effort: a filesystem error leaves the table unchanged rather than
    raising into a hook. The lock serializes concurrent learners (Claude + the
    web UI + cron) exactly like the OMI store's ``.omi.lock``.
    """
    path = policy_path()
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.parent / "policy.lock"
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            filelock.lock_fd(fd)
            new_rules = fn(load_learned())
            tmp = path.parent / "policy.json.tmp"
            tmp.write_text(
                json.dumps([_rule_to_dict(r) for r in new_rules], indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, path)
        finally:
            filelock.unlock_fd(fd)
            os.close(fd)


def append_learned_rule(rule: Rule, *, now: datetime | None = None) -> None:
    """Add (or replace by id) a learned rule. Idempotent: re-learning the same
    id overwrites rather than duplicating. Stamps ``created`` if unset."""
    rule.source = "learned"
    if not rule.created:
        rule.created = (now or datetime.now()).isoformat(timespec="seconds")

    def apply(rules: list[Rule]) -> list[Rule]:
        return [*(r for r in rules if r.id != rule.id), rule]

    _mutate_learned(apply)


def update_learned_rule(
    rule_id: str,
    *,
    severity: str | None = None,
    hits: int | None = None,
    verify: bool | None = None,
) -> bool:
    """Patch a learned rule in place. Returns ``True`` if it existed and changed.

    Only learned rules are mutable — SEED rules are immutable code. Escalation
    (soft→hard, then ``verify=True``) goes through here.
    """
    found = False

    def apply(rules: list[Rule]) -> list[Rule]:
        nonlocal found
        for rule in rules:
            if rule.id == rule_id:
                found = True
                if severity is not None:
                    rule.severity = severity
                if hits is not None:
                    rule.hits = hits
                if verify is not None:
                    rule.verify = verify
        return rules

    _mutate_learned(apply)
    return found


def write_seed_policy() -> None:
    """Write the SEED ruleset to :func:`seed_policy_path` (scaffold-on-install).
    Best-effort; the guard does not depend on the file existing."""
    path = seed_policy_path()
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([_rule_to_dict(r) for r in SEED_RULES], indent=2) + "\n",
            encoding="utf-8",
        )
