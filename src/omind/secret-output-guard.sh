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
command -v jq >/dev/null 2>&1 || exit 0   # omi-guard fails-closed for Bash separately
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null)"
[ -z "$cmd" ] && exit 0

# explicit, audited override
printf '%s' "$cmd" | grep -q 'OMI_SECRET_OK=1' && exit 0

READ='pass[[:space:]]+show([[:space:]]|$)|pass[[:space:]]+[A-Za-z0-9_.@-]+/|gh[[:space:]]+auth[[:space:]]+token([[:space:]]|$)'

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
if printf '%s' "$cmd" | grep -Eq '(gh[pousr]|ghu)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{18,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----'; then
  block "the command text contains a literal credential/token."
fi

# 2) A secret-read substitution fed to a printing command -> transcript.
if printf '%s' "$cmd" | grep -Eq '\b(echo|printf|print|cat|tee|head|tail|xxd|od|base64|hexdump)\b[^|;&]*(\$\(|`)[^)`]*('"$READ"')'; then
  block "a secret read is piped into a command that prints to stdout."
fi

# 3) A bare / piped secret-read that is not captured and not redirected.
#    Strip command substitutions; whatever read remains runs to the shell stdout.
bare="$(printf '%s' "$cmd" | sed -E 's/\$\([^)]*\)//g; s/`[^`]*`//g')"
if printf '%s' "$bare" | grep -Eq "$READ"; then
  if printf '%s' "$bare" | grep -Eq '>[[:space:]]*/dev/null|>>?[[:space:]]*[^|&[:space:]]'; then
    : # redirected off the transcript (to /dev/null or a file) — allow
  else
    block "a secret read (pass show / pass <path> / gh auth token) prints to stdout."
  fi
fi

exit 0
