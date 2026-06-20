#!/usr/bin/env bash
# omi-guard-hermes.sh — Hermes `pre_tool_call` adapter, installed by omind.
#
# The Hermes analogue of the Claude omi-guard.sh: it runs before every Hermes
# tool call and enforces the SAME harness-agnostic policy. The difference is the
# output contract — Hermes reads the hook's STDOUT JSON, so a deny is rendered as
# {"decision":"block","reason":...} (no output = allow), via
# `omind guard adapter --harness hermes`. An OMI consult (an mcp__omi__* tool, or
# a Read under the OMI folder) clears the per-turn gate in pure bash; the per-turn
# RESET is the existing `omind hook pre_llm_call` (Hermes' turn boundary).
#
# __OMIND_BIN__ / __OMI_DIR__ are substituted at install. Fail-open on any error
# (no decision emitted = Hermes allows), so a broken hook never wedges the agent.

set -u
OMIND='__OMIND_BIN__'
OMI_DIR='__OMI_DIR__'
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/omind"

input="$(cat 2>/dev/null)"
[ -z "$input" ] && exit 0
command -v jq >/dev/null 2>&1 || exit 0

tool="$(printf '%s' "$input" | jq -r '.tool_name // .tool // empty' 2>/dev/null)"
sid="$(printf '%s' "$input" | jq -r '.session_id // .session // empty' 2>/dev/null | tr -cd 'A-Za-z0-9._-')"
[ -z "$sid" ] && sid="nosid"
SENT="$STATE/gate-$sid"

# Consulting OMI clears the per-turn gate (always allowed — the clear-path).
# `touch` (not truncate) so the PostToolUse verifier's JSON survives the turn.
case "$tool" in
  mcp__omi__*) mkdir -p "$STATE" 2>/dev/null; touch "$SENT" 2>/dev/null; exit 0 ;;
esac
if [ "$tool" = "Read" ] || [ "$tool" = "read_file" ]; then
  fp="$(printf '%s' "$input" | jq -r '.tool_input.file_path // .tool_input.path // .extra.path // empty' 2>/dev/null)"
  case "$fp" in
    *"$OMI_DIR"*) mkdir -p "$STATE" 2>/dev/null; touch "$SENT" 2>/dev/null; exit 0 ;;
  esac
fi

# Everything else: the core decides + renders the Hermes block JSON (hard blocks,
# github-push opt-in, per-turn gate). A shell command may live in tool_input or
# extra depending on the tool.
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // .extra.command // .command // empty' 2>/dev/null)"
jq -nc --arg t "$tool" --arg c "$cmd" --arg s "$sid" \
  '{tool:$t, command:$c, session:$s, is_omi_consult:false}' 2>/dev/null \
  | "$OMIND" guard adapter --harness hermes --omi-dir "$OMI_DIR"
exit 0
