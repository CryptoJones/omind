#!/usr/bin/env bash
# omi-gate-reset.sh — Claude Code UserPromptSubmit adapter, installed by omind.
#
# Runs omind's proactive turn preflight: capture the prompt, recall one compact
# relevant memory when available, inject it through UserPromptSubmit
# additionalContext, and satisfy the ordinary consult gate. Hard rule-specific
# prerequisites still run independently at PreToolUse. If the Python core is
# unavailable, fall back to the legacy pure-bash reset so enforcement remains
# armed rather than silently trusting stale state. Never blocks prompt handling.

set -u
# Default HOME so `set -u` can't crash the reset (which would leave the gate
# cleared from the previous turn); mirrors omi-guard.sh.
HOME="${HOME:-/tmp}"
OMIND='__OMIND_BIN__'
OMI_DIR='__OMI_DIR__'
input="$(cat 2>/dev/null)"
[ -z "$input" ] && exit 0

# Normal path: the core emits the exact UserPromptSubmit JSON on stdout. It also
# performs local deterministic retrieval only — no model call and no network.
if [ -x "$OMIND" ] || command -v "$OMIND" >/dev/null 2>&1; then
  printf '%s' "$input" | "$OMIND" guard preflight --omi-dir "$OMI_DIR" && exit 0
fi

# Fail-open fallback for prompt submission, but re-arm the gate in pure bash.
command -v jq >/dev/null 2>&1 || exit 0
sid="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null | tr -cd 'A-Za-z0-9._-')"
[ -z "$sid" ] && sid="nosid"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/omind"
rm -f "$STATE/gate-$sid" 2>/dev/null
# Reset the verifier's per-turn re-close counter (its anti-wedge cap is measured
# per turn; guard.py reads reclose-<sid>). Best-effort.
rm -f "$STATE/reclose-$sid" 2>/dev/null
# Clear the per-turn pending-intent, the git-freshness record, and the
# demanded-note marker too, matching guard.begin_turn(). Omitting these made
# the "same-turn freshness check" actually per-SESSION — one fetch at 9am
# satisfied a 6pm commit (a fail-open of the freshness control) — and left
# stale pending intent feeding the verifier.
rm -f "$STATE/pending-$sid.txt" "$STATE/git-fresh-$sid.json" "$STATE/demanded-$sid.txt" 2>/dev/null
# Capture this turn's task so the verifier/retrieval can judge consult relevance
# (guard.py reads turn-<sid>.txt). Best-effort; empty prompt is fine.
mkdir -p "$STATE" 2>/dev/null
printf '%s' "$(printf '%s' "$input" | jq -r '.prompt // empty' 2>/dev/null)" \
  > "$STATE/turn-$sid.txt" 2>/dev/null
# Reap legacy /tmp/omi-gate-* sentinels left by the pre-state-dir prototype guard
# (the canonical guard never writes /tmp, so any such file is stale litter).
rm -f /tmp/omi-gate-* 2>/dev/null
exit 0
