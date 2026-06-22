#!/usr/bin/env bash
# omi-guard.sh — Claude Code PreToolUse("*") adapter, installed by omind.
#
# Thin by design: the per-turn gate hot-path runs here in bash (no subprocess
# for the common case — a Read/Edit/Grep after the turn's first OMI consult
# exits instantly), and Bash commands delegate the hard-block policy to the
# harness-agnostic `omind guard check` so the rules live in ONE place.
#
# __OMIND_BIN__ and __OMI_DIR__ are substituted at install (provision). The
# sentinel lives in omind's state dir so this script and the Python core agree
# on the path. Fail-open on adapter errors; the core enforces the destructive
# blocks for Bash regardless.

set -u
OMIND='__OMIND_BIN__'
OMI_DIR='__OMI_DIR__'
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/omind"

input="$(cat 2>/dev/null)"
[ -z "$input" ] && exit 0
command -v jq >/dev/null 2>&1 || exit 0

tool="$(printf '%s' "$input" | jq -r '.tool_name // empty' 2>/dev/null)"
sid="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null | tr -cd 'A-Za-z0-9._-')"
[ -z "$sid" ] && sid="nosid"
SENT="$STATE/gate-$sid"

# Consulting OMI clears the per-turn gate (always allowed — the clear-path).
# `touch` (not `: >`) so we never TRUNCATE: the PostToolUse verifier records the
# turn's consults + relevance verdicts as JSON in this same file, and a second
# consult in the turn must not wipe the first.
case "$tool" in
  mcp__omi__*) mkdir -p "$STATE" 2>/dev/null; touch "$SENT" 2>/dev/null; exit 0 ;;
  # Tool-schema loading is never gated: deferred OMI MCP tools become callable
  # only via ToolSearch, so gating it deadlocks the turn (no consult possible).
  # Allow it through WITHOUT clearing the gate — loading a schema is not a consult.
  ToolSearch) exit 0 ;;
esac
if [ "$tool" = "Read" ]; then
  fp="$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty' 2>/dev/null)"
  case "$fp" in
    *"$OMI_DIR"*) mkdir -p "$STATE" 2>/dev/null; touch "$SENT" 2>/dev/null; exit 0 ;;
  esac
fi

# Bash commands: delegate hard-blocks + gate to the core (single source).
if [ "$tool" = "Bash" ]; then
  cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null)"
  jq -nc --arg c "$cmd" --arg s "$sid" \
    '{tool:"Bash", command:$c, session:$s, is_omi_consult:false}' 2>/dev/null \
    | "$OMIND" guard check
  exit $?
fi

# All other tools: the per-turn gate. The common post-consult case is a pure
# bash existence check (no subprocess). Only the first BLOCKED action of a turn
# pays one subprocess to name the notes relevant to this turn's task (Phase 3.2),
# falling back to the static message if omind can't be reached.
[ -e "$SENT" ] && exit 0
if msg="$(printf '%s' "$input" | "$OMIND" guard suggest --omi-dir "$OMI_DIR" 2>/dev/null)" \
   && [ -n "$msg" ]; then
  printf '%s\n' "$msg" >&2
else
  printf 'BLOCKED by omi-gate: consult OMI before acting this turn — call mcp__omi__search-vault / read-note, or Read any file under the OMI folder. One consult clears the rest of this turn. Consult the notes RELEVANT to your task; this is NOT a prompt to open the credential/auth notes.\n' >&2
fi
exit 2
