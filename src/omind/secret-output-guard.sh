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
if printf '%s' "$cmd" | grep -Eq 'gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{18,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|sk-or-v1-[A-Za-z0-9]{24,}|sk-ant-[A-Za-z0-9_-]{24,}|sk-proj-[A-Za-z0-9_-]{24,}|nsec1[02-9ac-hj-np-z]{20,}|mg_[0-9]_[A-Za-z0-9_-]{24,}'; then
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

# 4) A credential-bearing CONFIG FILE read whose stdout reaches the transcript.
#    Added 2026-08-09 after two leaks the rules above structurally could not see:
#    an OpenRouter key out of Buzz's global-agent-config.json and a merge gateway
#    key out of managed-agents.json. Neither command contained a credential or a
#    `pass` read — the secret arrived in the OUTPUT — so nothing fired.
#    NARROWED 2026-08-09: v1 matched all of Application Support/Buzz/, which blocked
#    reading run-agents.zsh -- a script holding NO credentials (it pulls them from
#    pass at runtime). Over-blocking is how a guard gets muted, so match credential
#    bearing FILES, not the directory tree they sit in.
#    Anything matching a known secret-store path must have its stdout redirected,
#    be visibly redacting, or carry the audited override.
SECRET_FILES='(managed-agents[^[:space:]]*\.json|global-agent-config\.json|oauth_creds\.json|/\.antigravity/|xyz\.block\.buzz\.app[^[:space:]]*/agents/[^[:space:]]*\.json|/\.env([^[:alnum:]]|$)|credentials\.json|\.netrc|id_[a-z]+[a-z0-9]*(_[a-z0-9]+)?$)'
if printf '%s' "$flat" | grep -Eq "$SECRET_FILES"; then
  # Visibly redacting reads are the safe form and stay allowed.
  if printf '%s' "$flat" | grep -Eqi 'redact|<redacted|_REDACTED|sanitiz'; then
    :
  elif printf '%s' "$flat" | grep -Eq '(^|[^0-9])(1?>|&>)[[:space:]]*([^|&[:space:]]|/dev/null)'; then
    :
  elif printf '%s' "$flat" | grep -Eq '^[[:space:]]*(ls|stat|file|find|test|\[)([[:space:]]|$)'; then
    # Path-only operations do not print file CONTENT.
    :
  else
    {
      printf 'BLOCKED by secret-output-guard: reading a credential-bearing config file.\n\n'
      printf 'These files hold live keys (Buzz agent definitions, OAuth creds, .env).\n'
      printf 'Print them only through a redactor:\n\n'
      printf '  python3 -c "..., print({k: (\x27<redacted>\x27 if any(t in k.upper()\n'
      printf '      for t in (\x27KEY\x27,\x27TOKEN\x27,\x27SECRET\x27)) else v) ...})"\n\n'
      printf 'or redirect stdout to a file, or prefix OMI_SECRET_OK=1 if you have\n'
      printf 'confirmed the file holds no live credential.\n'
    } >&2
    exit 2
  fi
fi

exit 0
