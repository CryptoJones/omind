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

# jq parses the event on the fast path. If it is missing, DON'T wedge the host:
# the matcher is "*", so a fail-closed Bash block here would also block the very
# command that installs jq (the #107 bootstrap deadlock). Instead route the RAW
# event through `omind guard adapter`, which parses it in pure Python and applies
# the SAME hard-blocks + gate — enforcement is preserved, just slower. jq is now a
# performance optimization, not a hard dependency. Scream once so `doctor` users
# still see the misconfiguration.
if ! command -v jq >/dev/null 2>&1; then
  printf 'omi-guard: jq not found — using the slower pure-Python guard path (install jq for the fast path).\n' >&2
  if [ -x "$OMIND" ] || command -v "$OMIND" >/dev/null 2>&1; then
    printf '%s' "$input" | "$OMIND" guard adapter --harness claude --omi-dir "$OMI_DIR"
    rc=$?
    # Only a clean allow(0) / block(2) is authoritative; anything else means the
    # core didn't evaluate — fall through to the conservative last resort below.
    case "$rc" in
      0 | 2) exit "$rc" ;;
    esac
  fi
  # No jq AND no working omind core: the policy genuinely couldn't be evaluated.
  # Fail OPEN for non-Bash (so the host doesn't wedge on every Read/consult) but
  # CLOSED for Bash (a destructive command must never run unchecked).
  if printf '%s' "$input" | grep -q '"tool_name"[[:space:]]*:[[:space:]]*"Bash"'; then
    printf 'omi-guard: omind core unreachable too — BLOCKING this Bash command (fail-closed).\n' >&2
    exit 2
  fi
  exit 0
fi

tool="$(printf '%s' "$input" | jq -r '.tool_name // empty' 2>/dev/null)"
sid="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null | tr -cd 'A-Za-z0-9._-')"
[ -z "$sid" ] && sid="nosid"
prompt="$(printf '%s' "$input" | jq -r '.prompt // .user_prompt // .current_prompt // .turn_prompt // empty' 2>/dev/null)"
SENT="$STATE/gate-$sid"

# Consulting OMI clears the per-turn gate (always allowed — the clear-path).
# `touch` (not `: >`) so we never TRUNCATE: the PostToolUse verifier records the
# turn's consults + relevance verdicts as JSON in this same file, and a second
# consult in the turn must not wipe the first.
case "$tool" in
  # Navigation/listing tools (list-notes, list-tags, graph-*, backlinks) surface
  # no note CONTENT, so — like re-reading index.md — they must NOT clear the gate
  # (that was a verifier-proof gate-dodge). Allow them through without consulting.
  mcp__omi__list-notes | mcp__omi__list-tags | mcp__omi__graph-* | mcp__omi__backlinks)
    exit 0 ;;
  # Vault WRITES (create/edit/delete/restore) are acts, not consults of memory —
  # they used to clear the gate here and then get relevance-scored (and denied)
  # by the verifier (#148). Fall through to the generic delegation below, so
  # they're gated like any ordinary action: an edit follows a read-note anyway
  # (the version token), and a create follows a search (the dedup step), so the
  # turn's consult already exists in an honest flow.
  mcp__omi__create-note | mcp__omi__edit-note | mcp__omi__delete-note | mcp__omi__restore-note)
    : ;;
  mcp__omi__*)
    target="$(printf '%s' "$input" | jq -r '.tool_input.name // .tool_input.query // .tool_input.q // .tool_input.file_path // .tool_input.path // empty' 2>/dev/null)"
    # An empty target means a contentless call (nothing to consult); allow it but
    # do not clear the gate.
    [ -z "$target" ] && exit 0
    jq -nc --arg t "$tool" --arg s "$sid" --arg target "$target" \
      '{tool:$t, command:"", session:$s, is_omi_consult:true, consult_target:$target}' 2>/dev/null \
      | "$OMIND" guard check --omi-dir "$OMI_DIR" >/dev/null 2>&1
    exit 0
    ;;
  # Tool-schema loading is never gated: deferred OMI MCP tools become callable
  # only via ToolSearch, so gating it deadlocks the turn (no consult possible).
  # Allow it through WITHOUT clearing the gate — loading a schema is not a consult.
  ToolSearch) exit 0 ;;
esac
if [ "$tool" = "Read" ]; then
  fp="$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty' 2>/dev/null)"
  case "$fp" in
    *"$OMI_DIR"*)
      # A Read under the OMI folder clears the gate — EXCEPT the vault's
      # table-of-contents (index.md), the recent-memories MEMORY.md, and the
      # template. Those are "relevant to everything", which made re-reading the
      # index the gate-dodge: it cleared the gate without consulting a relevant
      # note. Allow the Read through (it is harmless) but do NOT clear the gate —
      # a REAL content note must be consulted. (Keep this basename list in sync
      # with paths.NON_CONSULT_FILENAMES.)
      case "${fp##*/}" in
        index.md|MEMORY.md|"Memory Template.md") exit 0 ;;
        *)
          jq -nc --arg s "$sid" --arg target "$fp" \
            '{tool:"Read", command:"", session:$s, is_omi_consult:true, consult_target:$target, consult_kind:"read", file_path:$target}' 2>/dev/null \
            | "$OMIND" guard check --omi-dir "$OMI_DIR" >/dev/null 2>&1
          exit 0
          ;;
      esac
      ;;
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
  jq -nc --arg c "$cmd" --arg s "$sid" --arg prompt "$prompt" \
    '{tool:"Bash", command:$c, session:$s, prompt:$prompt, is_omi_consult:false}' 2>/dev/null \
    | "$OMIND" guard check --omi-dir "$OMI_DIR"
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

# All other tools: delegate to the core so repo/global-config preconditions can
# inspect file paths. This is slower than the old sentinel-only fast path, but the
# policy now needs more context than "has OMI been consulted".
fp="$(printf '%s' "$input" | jq -r '.tool_input.file_path // .tool_input.path // .file_path // .path // empty' 2>/dev/null)"
jq -nc --arg t "$tool" --arg s "$sid" --arg fp "$fp" --arg prompt "$prompt" \
  '{tool:$t, command:"", session:$s, prompt:$prompt, is_omi_consult:false, file_path:$fp}' 2>/dev/null \
  | "$OMIND" guard check --omi-dir "$OMI_DIR"
rc=$?
case "$rc" in
  0 | 2) exit "$rc" ;;
esac
if msg="$(printf '%s' "$input" | "$OMIND" guard suggest --omi-dir "$OMI_DIR" 2>/dev/null)" \
   && [ -n "$msg" ]; then
  printf '%s\n' "$msg" >&2
else
  printf 'BLOCKED by omi-gate: ACTION BLOCKED. Next call OMI MCP search-vault with a focused query, then recall-note on one result and retry.\n' >&2
fi
exit 2
