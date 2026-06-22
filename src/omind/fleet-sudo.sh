#!/usr/bin/env bash
# fleet-sudo — run a command under sudo using the fleet sudo password from `pass`.
#
# Installed by `omind setup`. Agents MUST use this instead of raw `sudo`, so no
# instance ever guesses the per-host `pass` entry or hands the user a command to
# run ("homework"). The omind guard blocks raw `sudo` and points here.
#
#   fleet-sudo <command> [args...]
#   ssh <host> fleet-sudo <command>        # works remotely too
#
# Entry resolution (first hit wins): $FLEET_SUDO_ENTRY, then
# ~/.config/omind/sudo-pass-entry, then a probe of the known fleet entries. The
# password is piped into `sudo -S` and never printed.
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "usage: fleet-sudo <command> [args...]" >&2
  exit 64
fi
command -v pass >/dev/null 2>&1 || { echo "fleet-sudo: 'pass' not found on PATH" >&2; exit 1; }

entry="${FLEET_SUDO_ENTRY:-}"
cfg="${XDG_CONFIG_HOME:-$HOME/.config}/omind/sudo-pass-entry"
if [ -z "$entry" ] && [ -r "$cfg" ]; then
  entry="$(head -n1 "$cfg" | tr -d '[:space:]')"
fi
if [ -z "$entry" ]; then
  for cand in pluto_linux/sudo makemake_macos/sudo sudo/akclark sudo; do
    if pass show "$cand" >/dev/null 2>&1; then entry="$cand"; break; fi
  done
fi
if [ -z "$entry" ]; then
  echo "fleet-sudo: no sudo password in pass (set FLEET_SUDO_ENTRY or ~/.config/omind/sudo-pass-entry)" >&2
  exit 1
fi

printf '%s\n' "$(pass show "$entry" | head -n1)" | sudo -S -p '' "$@"
