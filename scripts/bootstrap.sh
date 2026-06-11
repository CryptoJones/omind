#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
#
# omind bootstrap: check (and where safe, install) the dependencies omind needs,
# then install the `omind` CLI and verify the wiring.
#
# omind has NO Docker and NO Node.js dependency. It is a pure-Python CLI that
# shells out to git (the mesh replicates over it) and the `claude` CLI (to
# register the MCP server). The dependencies this script handles are exactly:
#
#   - uv      : installs omind in an isolated venv and bootstraps Python >=3.10
#               (auto-installed here; it is user-local and needs no root)
#   - git     : the mesh replicates the memory folder over git (checked)
#   - claude  : the Claude Code CLI registers the server (checked; install guidance)
#
# Usage:
#   scripts/bootstrap.sh [--remote github|codeberg] [--vault PATH] [--no-setup]
#
# Examples:
#   scripts/bootstrap.sh
#   scripts/bootstrap.sh --remote codeberg --vault "$HOME/Documents/Obsidian Vault"

set -euo pipefail

# ---- config / args ---------------------------------------------------------
REMOTE="github"
VAULT="${HOME}/Documents/Obsidian Vault"
RUN_SETUP=1

while [ $# -gt 0 ]; do
  case "$1" in
    --remote) REMOTE="${2:?--remote needs github|codeberg}"; shift 2 ;;
    --vault)  VAULT="${2:?--vault needs a path}"; shift 2 ;;
    --no-setup) RUN_SETUP=0; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

case "$REMOTE" in
  github)   GIT_URL="git+https://github.com/CryptoJones/omind.git" ;;
  codeberg) GIT_URL="git+https://codeberg.org/CryptoJones/omind.git" ;;
  *) echo "--remote must be 'github' or 'codeberg' (got: $REMOTE)" >&2; exit 2 ;;
esac

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()    { printf '  \033[1;32m[ok]\033[0m %s\n' "$*"; }
warn()  { printf '  \033[1;33m[!]\033[0m %s\n' "$*"; }
die()   { printf '  \033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# ---- detect OS package-manager hint ----------------------------------------
pkg_hint() {
  # Best-effort "how to install <tool>" line for this machine.
  local tool="$1"
  if   command -v dnf  >/dev/null 2>&1; then echo "sudo dnf install -y $tool"
  elif command -v apt-get >/dev/null 2>&1; then echo "sudo apt-get install -y $tool"
  elif command -v pacman >/dev/null 2>&1; then echo "sudo pacman -S $tool"
  elif command -v brew >/dev/null 2>&1; then echo "brew install $tool"
  else echo "install '$tool' with your system package manager"
  fi
}

# ---- 1. uv (auto-install; user-local, no root) -----------------------------
info "Checking uv"
if command -v uv >/dev/null 2>&1; then
  ok "uv present: $(uv --version)"
else
  warn "uv missing — installing via the official astral.sh installer (user-local)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # The installer drops uv in ~/.local/bin; make it visible for the rest of this run.
  export PATH="${HOME}/.local/bin:${PATH}"
  command -v uv >/dev/null 2>&1 || die "uv install failed; see https://docs.astral.sh/uv/"
  ok "uv installed: $(uv --version)"
  warn "Ensure ~/.local/bin is on your PATH in new shells (add to ~/.bashrc if needed)."
fi

# ---- 2. node / npm / claude (checked; not force-installed) -----------------
MISSING=0
info "Checking runtime dependencies (node, npm, claude)"
if command -v node >/dev/null 2>&1; then ok "node: $(node --version)"
else warn "node missing — $(pkg_hint nodejs)"; MISSING=1; fi

if command -v npm >/dev/null 2>&1; then ok "npm: $(npm --version)"
else warn "npm missing — $(pkg_hint npm)"; MISSING=1; fi

if command -v claude >/dev/null 2>&1; then ok "claude CLI present"
else
  warn "claude CLI missing — install Claude Code:"
  warn "    npm install -g @anthropic-ai/claude-code   (or see https://claude.com/claude-code)"
  MISSING=1
fi

# ---- 3. install omind ------------------------------------------------------
info "Installing omind from ${REMOTE} (${GIT_URL})"
uv tool install --force "$GIT_URL"
ok "omind installed: $(omind --version 2>/dev/null || echo 'run: omind --version')"

# ---- 4. setup + verify -----------------------------------------------------
if [ "$MISSING" -ne 0 ]; then
  warn "Skipping 'omind setup' — install the missing dependencies above, then run:"
  warn "    omind setup --vault \"$VAULT\""
  exit 1
fi

if [ "$RUN_SETUP" -eq 1 ]; then
  info "Provisioning the MCP wiring (omind setup; idempotent)"
  omind setup --vault "$VAULT"
  info "Verifying (omind doctor)"
  omind doctor --vault "$VAULT"
  ok "Bootstrap complete. Restart Claude Code to load the OMI memory tools."
else
  ok "Bootstrap complete (skipped setup per --no-setup). Next: omind setup --vault \"$VAULT\""
fi
