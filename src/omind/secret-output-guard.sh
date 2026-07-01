#!/usr/bin/env bash
# ================================================================
# secret-output-guard.sh — stop credential VALUES reaching the transcript
# ================================================================
# PreToolUse / matcher "Bash".  Exit 2 = block.
#
# Closes the leak class that `git add`-style secret guards miss: a credential
# READ whose value flows to stdout (i.e. into the model's transcript). This is
# the exact mistake that burned a GitHub PAT — `pass show ... | head` printed
# the token into the conversation.
#
# BLOCKS:
#   pass show <name> | head            # value piped to a printer -> transcript
#   pass show <name>                   # bare read -> transcript
#   pass <user>/<entry>                # `pass` show-shorthand for an entry path
#   gh auth token                      # prints the token
#   echo  $(pass show <name>)          # substitution fed to a printer
#   printf ... $(gh auth token)
#   echo ghp_xxxxxxxx... / glpat-... / xoxb-... / AKIA... / BEGIN PRIVATE KEY
#                                       # a literal credential in the command
#
# ALLOWS (the safe forms):
#   TOK=$(pass show <name>)            # captured into a var, never printed
#   pass show <name> >/dev/null        # redirected (e.g. warm the gpg-agent)
#   pass show <name> > file            # written to a file, not the transcript
#   pass insert / pass ls / pass git   # not a value read
#   curl -H "Authorization: token $(pass show <name>)"   # token -> request, not stdout
#
# Deliberate, audited exception: prefix the command with OMI_SECRET_OK=1
# ================================================================
set -u

input="$(cat 2>/dev/null)"
[ -z "$input" ] && exit 0
# jq parses the event; without it this specific guard can't read the command.
# NOTE: the omind policy has no secret-output rules, so no-jq means NO
# secret-output protection here (the general omi-guard's fail-closed-for-Bash
# path does not cover this leak class). Install jq.
command -v jq >/dev/null 2>&1 || exit 0
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null)"
[ -z "$cmd" ] && exit 0

# Explicit, audited override — must be a REAL leading assignment (start or after
# a shell separator, optionally via `env`), not a substring forged in a comment
# or a quoted string (which would silently disable the guard).
printf '%s' "$cmd" | grep -Eq '(^|[;&|])[[:space:]]*(env[[:space:]]+)?OMI_SECRET_OK=1([[:space:]]|$)' && exit 0

# Anchor `pass`/`gh` to COMMAND POSITION — start, or after a shell separator
# (`;` `&` `|` `(`), past any leading `VAR=val` / `env` — so `grep "pass tests/"`,
# a commit message, and "bypass proxy/" no longer false-positive, while a real
# `pass show`, `; pass work/x`, or `env FOO=1 pass show` still matches.
BND='(^|[;&|(])[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:];&|]*[[:space:]]+|env[[:space:]]+)*'
READ="${BND}pass[[:space:]]+show([[:space:]]|\$)|${BND}pass[[:space:]]+[A-Za-z0-9_.@-]+/|${BND}gh[[:space:]]+auth[[:space:]]+token([[:space:]]|\$)"

block() {
  {
    printf 'BLOCKED by secret-output-guard: %s\n\n' "$1"
    printf 'A credential value would reach the transcript. Use a safe form:\n'
    printf '  TOK=$(pass show <name>) ; use "$TOK"   # captured, never printed\n'
    printf '  pass show <name> >/dev/null             # redirect (just warm the agent)\n\n'
    printf 'Deliberate, audited exception: prefix the command with OMI_SECRET_OK=1\n'
  } >&2
  exit 2
}

# 1) A literal credential pasted into the command text.
if printf '%s' "$cmd" | grep -Eq 'gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{18,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----'; then
  block "the command text contains a literal credential/token."
fi

# git credential helpers legitimately feed the secret to git's credential
# protocol on stdin (e.g. credential.helper='!f(){ echo "password=$(pass X)"; }'),
# NOT to the transcript. Past the literal-token check above, don't flag the
# echo/read inside a helper definition.
printf '%s' "$cmd" | grep -Eq 'credential\.helper' && exit 0

# 2) A secret-read substitution fed to a printing command -> transcript.
if printf '%s' "$cmd" | grep -Eq '\b(echo|printf|print|cat|tee|head|tail|xxd|od|base64|hexdump)\b[^|;&]*(\$\(|`)[^)`]*('"$READ"')'; then
  block "a secret read is piped into a command that prints to stdout."
fi

# 3) A bare / piped secret-read that is not captured and not safely redirected.
#    Flatten newlines first so a multi-line `TOK=$(\n pass show x\n)` capture is
#    stripped (line-based sed missed it and false-blocked the captured read),
#    then strip command substitutions; whatever read remains runs to shell stdout.
flat="$(printf '%s' "$cmd" | tr '\n' ' ')"
bare="$(printf '%s' "$flat" | sed -E 's/\$\([^)]*\)//g; s/`[^`]*`//g')"
if printf '%s' "$bare" | grep -Eq "$READ"; then
  if printf '%s' "$bare" | grep -Eq '\|'; then
    # The read's stdout is piped into another command -> transcript. Not safe,
    # even with a `2>/dev/null` stderr redirect (the exact `pass show X 2>/dev/null
    # | head` leak that a bare `>`-means-redirected check waved through).
    block "a secret read is piped to another command; its value reaches the transcript."
  elif printf '%s' "$bare" | grep -Eq '(^|[^0-9])(1?>|&>)[[:space:]]*([^|&[:space:]]|/dev/null)'; then
    # STDOUT redirected off the transcript (to a file or /dev/null) — allow. A
    # bare `2>...` only redirects STDERR, so it does NOT count here.
    :
  else
    block "a secret read (pass show / pass <path> / gh auth token) prints to stdout."
  fi
fi

exit 0
