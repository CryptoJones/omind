#!/usr/bin/env bash
# git-fresh-base.sh — PreToolUse(Bash) guard.
#
# Prevents the recurring "branched off a stale local main" mistake: blocks
# creating a new branch off a LOCAL main/master/develop that is behind its
# origin/* counterpart. Forces `git checkout -b <name> origin/<branch>` after
# a fetch instead.
#
# Design: FAIL-OPEN. Any error, missing tool, non-git command, or branch
# created off a feature branch / an explicit origin ref -> exit 0 (allow).
# Only the clear-cut "new branch off stale local long-lived branch" blocks (2).

# Read the hook payload (PreToolUse JSON on stdin).
input="$(cat 2>/dev/null)"
[ -z "$input" ] && exit 0

# jq is how we safely pull the command out of JSON; without it, fail open.
command -v jq >/dev/null 2>&1 || exit 0

cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null)"
[ -z "$cmd" ] && exit 0

# Fast reject: only proceed for git branch-creating subcommands.
case "$cmd" in
  *git*checkout*|*git*switch*|*git*branch*) : ;;
  *) exit 0 ;;
esac

base=""
is_create=0

if printf '%s' "$cmd" | grep -qE '\bgit[[:space:]]+(checkout|switch)\b.*[[:space:]](-b|-B|-c|-C)[[:space:]]'; then
  is_create=1
  # capture optional <start-point> token after the new branch name
  base="$(printf '%s' "$cmd" | sed -nE 's/.*[[:space:]](-b|-B|-c|-C)[[:space:]]+[^[:space:]]+[[:space:]]+([^[:space:];&|>]+).*/\2/p' | head -n1)"
elif printf '%s' "$cmd" | grep -qE '\bgit[[:space:]]+branch[[:space:]]+[^-[:space:]]'; then
  # `git branch <new> [<start>]` — but not delete/move/list/etc.
  printf '%s' "$cmd" | grep -qE '\bgit[[:space:]]+branch[[:space:]]+(-d|-D|-m|-M|-c|-C|--delete|--move|--copy|--list|-a|-r|--all|--remotes|--set-upstream-to|-u)\b' && exit 0
  is_create=1
  base="$(printf '%s' "$cmd" | sed -nE 's/.*\bgit[[:space:]]+branch[[:space:]]+[^[:space:]]+[[:space:]]+([^[:space:];&|>]+).*/\1/p' | head -n1)"
fi

[ "$is_create" -eq 1 ] || exit 0

# Operate in the directory the command will run in.
cwd="$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null)"
[ -n "$cwd" ] && cd "$cwd" 2>/dev/null

# Must be inside a git work tree, else nothing to guard.
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

# Refresh remote refs (best effort; never hang, never prompt).
GIT_TERMINAL_PROMPT=0 timeout 15 git fetch origin --quiet 2>/dev/null

# Resolve which branch is the base we must validate.
if [ -n "$base" ]; then
  case "$base" in
    origin/*|refs/*|*/*) exit 0 ;;   # already a remote/qualified ref => fresh
  esac
  branch="$base"
else
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"   # no start-point => current HEAD
fi

# Only guard the canonical long-lived branches.
case "$branch" in
  main|master|develop) : ;;
  *) exit 0 ;;
esac

# origin/<branch> must exist to compare against.
git rev-parse --verify --quiet "refs/remotes/origin/$branch" >/dev/null 2>&1 || exit 0

behind="$(git rev-list --count "${branch}..origin/${branch}" 2>/dev/null)"
case "$behind" in ''|*[!0-9]*) exit 0 ;; esac   # non-numeric => fail open

if [ "$behind" -gt 0 ]; then
  echo "BLOCKED by git-fresh-base hook: local '$branch' is $behind commit(s) behind origin/$branch (refs just fetched). Do NOT branch off a stale local '$branch'. Branch off the fresh remote instead, e.g.:  git checkout -b <name> origin/$branch" >&2
  exit 2
fi

exit 0
