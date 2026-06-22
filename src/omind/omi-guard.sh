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
# An unset HOME would trip `set -u` at the STATE expansion below and crash the
# hook (exit 1 = non-blocking in Claude => the action would PROCEED unchecked).
# Default it so the guard can't be silently disabled by a missing HOME.
HOME="${HOME:-/tmp}"
OMIND='__OMIND_BIN__'
OMI_DIR='__OMI_DIR__'
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/omind"

input="$(cat 2>/dev/null)"
[ -z "$input" ] && exit 0

# jq is required to parse the event. If it is missing we cannot read the tool /
# command — fail OPEN for non-destructive tools (so a misconfigured host doesn't
# wedge every Read/consult) but fail CLOSED (block) for anything that looks like a
# Bash command: a destructive command must never run with its hard-rules
# unevaluated. Always scream so the misconfiguration is visible (omind setup +
# doctor also check for jq).
if ! command -v jq >/dev/null 2>&1; then
  printf 'omi-guard: jq not found — cannot evaluate this action. Install jq (omind setup/doctor check for it).\n' >&2
  if printf '%s' "$input" | grep -q '"tool_name"[[:space:]]*:[[:space:]]*"Bash"'; then
    printf 'omi-guard: BLOCKING this Bash command (fail-closed: hard-rules could not be checked).\n' >&2
    exit 2
  fi
  exit 0
fi

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

# Bash commands: delegate hard-blocks + gate to the core (single source). This
# path fails CLOSED: a destructive Bash command must never run with its hard-rules
# unevaluated, so if the core can't be reached or doesn't return a clean verdict
# we BLOCK rather than let it through.
if [ "$tool" = "Bash" ]; then
  cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null)"
  if [ ! -x "$OMIND" ] && ! command -v "$OMIND" >/dev/null 2>&1; then
    printf 'omi-guard: omind not found at %s — BLOCKING this Bash command (fail-closed).\n' "$OMIND" >&2
    exit 2
  fi
  jq -nc --arg c "$cmd" --arg s "$sid" \
    '{tool:"Bash", command:$c, session:$s, is_omi_consult:false}' 2>/dev/null \
    | "$OMIND" guard check
  rc=$?
  # Only a clean allow(0) / block(2) from the core is authoritative. ANY other
  # code (crash, broken pipe, missing binary, OOM) means the policy was NOT
  # evaluated — fail CLOSED instead of the old `exit $?` that let 127 through.
  case "$rc" in
    0 | 2) exit "$rc" ;;
    *)
      printf 'omi-guard: guard core exited %s (policy not evaluated) — BLOCKING this Bash command (fail-closed).\n' "$rc" >&2
      exit 2
      ;;
  esac
fi

# Operator pause (`omind guard pause`): a time-boxed fast window skips the gate
# for non-Bash tools with NO subprocess. Bash was already delegated above, so the
# core still enforces the HARD destructive blocks even while the gate is paused.
PAUSE="$STATE/paused"
if [ -f "$PAUSE" ]; then
  exp="$(cat "$PAUSE" 2>/dev/null)"; now="$(date +%s 2>/dev/null)"
  if [ -n "$exp" ] && [ -n "$now" ] && [ "$exp" -gt "$now" ] 2>/dev/null; then exit 0; fi
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
