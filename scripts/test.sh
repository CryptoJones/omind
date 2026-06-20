#!/usr/bin/env bash
# scripts/test.sh — run the test suite in a sandboxed HOME so a provisioning test
# can never touch the developer's real ~/.claude (the 2.40.x footgun where a
# pytest run rewrote the live omi-guard.sh + settings.json, wedging the gate).
#
# A harness-level belt to the in-code `provision._guard_test_isolation` guard and
# the conftest HOME/CLAUDE_CONFIG_DIR isolation: `uv run` resolves with the real
# HOME (so its cache/venv work), then `env` runs pytest with HOME + the XDG dirs
# pointed at a throwaway sandbox and CLAUDE_CONFIG_DIR/HERMES_HOME unset.
set -euo pipefail

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT
mkdir -p "$SANDBOX/home" "$SANDBOX/state" "$SANDBOX/config"

cd "$(dirname "$0")/.."
exec uv run env \
  -u CLAUDE_CONFIG_DIR -u HERMES_HOME \
  HOME="$SANDBOX/home" \
  XDG_STATE_HOME="$SANDBOX/state" \
  XDG_CONFIG_HOME="$SANDBOX/config" \
  pytest "$@"
