# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Provision other AI agents (Hermes Agent, OpenClaw) to use OMI memory.

`omind setup --agent hermes|openclaw` mirrors what the Claude Code path does:
scaffold the OMI folder, initialize the mesh node, then wire the agent to
omind's own node MCP server (`omind node`). The agent-specific part is where the MCP server
gets declared — Hermes Agent reads a ``mcp_servers`` block in
``~/.hermes/config.yaml`` (YAML), OpenClaw a ``mcp.servers`` block in
``~/.openclaw/openclaw.json`` (JSON) — plus an ``omind-omi-memory`` skill
dropped into the agent's skills directory that teaches it to write memory
through the single-writer ``omind note`` path (see docs/mesh.md).

Neither agent ships a scriptable idempotent "mcp add" (Hermes' turns
interactive when the server already exists), so omind merges the config files
directly, the same way it merges Claude Code's settings.json: touch only the
entry it owns, refuse to overwrite a file it cannot parse.
"""

from __future__ import annotations

import contextlib
import importlib.resources
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, ClassVar

import yaml

from omind import paths, seeds
from omind.hooks import HOOK_MARKER
from omind.provision import (
    LEGACY_SERVER_NAME,
    CheckResult,
    Logger,
    Provisioner,
    ProvisionError,
    SetupConfig,
    _diagnose_omi_folder,
    _diagnose_tools,
    diagnose,
)

# -- agent locations ---------------------------------------------------------


def hermes_root() -> Path:
    """Hermes Agent's state directory (honors ``HERMES_HOME`` like Hermes does)."""
    env = os.environ.get("HERMES_HOME")
    return Path(env).expanduser() if env else Path.home() / ".hermes"


def hermes_config_path() -> Path:
    return hermes_root() / "config.yaml"


def hermes_skill_dir() -> Path:
    return hermes_root() / "skills" / "memory" / "omind-omi-memory"


def hermes_guard_script_path() -> Path:
    """Where omind writes Hermes' OMI-guard ``pre_tool_call`` adapter script."""
    return hermes_root() / "hooks" / "omi-guard-hermes.sh"


def hermes_allowlist_path() -> Path:
    """Hermes' shell-hook consent allowlist. omind pre-approves its own priming
    hook here so it loads without an interactive prompt (the user opted in by
    running setup); mirrors ``agent/shell_hooks.py`` in hermes-agent."""
    return hermes_root() / "shell-hooks-allowlist.json"


#: Current and legacy OpenClaw state-directory / config-file names (the
#: project was renamed Clawdbot -> Moltbot -> OpenClaw; old installs keep
#: their old paths).
OPENCLAW_ROOT_DIRNAMES = (".openclaw", ".clawdbot", ".moltbot")
OPENCLAW_CONFIG_FILENAMES = ("openclaw.json", "clawdbot.json", "moltbot.json")


def openclaw_root() -> Path:
    """OpenClaw's state directory: the first existing root, else ``~/.openclaw``."""
    home = Path.home()
    for name in OPENCLAW_ROOT_DIRNAMES:
        candidate = home / name
        if candidate.is_dir():
            return candidate
    return home / OPENCLAW_ROOT_DIRNAMES[0]


def openclaw_config_path() -> Path:
    """OpenClaw's config file: the first existing name, else ``openclaw.json``."""
    root = openclaw_root()
    for name in OPENCLAW_CONFIG_FILENAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return root / OPENCLAW_CONFIG_FILENAMES[0]


def openclaw_skill_dir() -> Path:
    return openclaw_root() / "skills" / "omind-omi-memory"


def openclaw_bootstrap_path() -> Path:
    """The OMI priming bootstrap file omind owns for OpenClaw. Kept in a folder
    omind controls (basename ``MEMORY.md`` so OpenClaw recognizes it) so a
    user-authored ``~/.openclaw/MEMORY.md`` is never touched."""
    return openclaw_root() / "omind" / "MEMORY.md"


def opencode_config_dir() -> Path:
    """OpenCode's config directory: ``$XDG_CONFIG_HOME/opencode`` or
    ``~/.config/opencode``."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "opencode"


def opencode_config_path() -> Path:
    return opencode_config_dir() / "opencode.json"


def opencode_guard_plugin_path() -> Path:
    """Where omind writes the OMI-guard OpenCode plugin (a plugin/ entry)."""
    return opencode_config_dir() / "plugin" / "omi-guard.js"


def codex_config_dir() -> Path:
    """OpenAI Codex CLI's config directory: ``$CODEX_HOME`` or ``~/.codex``."""
    base = os.environ.get("CODEX_HOME")
    return Path(base) if base else Path.home() / ".codex"


def codex_hooks_path() -> Path:
    """Codex's lifecycle-hooks file (same event schema as Claude Code hooks)."""
    return codex_config_dir() / "hooks.json"


#: Substring identifying omind's own Codex guard hook command, so a re-run finds
#: and replaces only our entry (and never duplicates) inside the user's hooks.json.
CODEX_GUARD_MARKER = "guard adapter --harness codex"


def gemini_config_dir() -> Path:
    """Gemini CLI's config directory: ``$GEMINI_HOME`` or ``~/.gemini``."""
    base = os.environ.get("GEMINI_HOME")
    return Path(base) if base else Path.home() / ".gemini"


def gemini_settings_path() -> Path:
    """Gemini CLI's settings file (``hooks`` + ``mcpServers`` both live here)."""
    return gemini_config_dir() / "settings.json"


#: Substring identifying omind's own Gemini guard hook command inside the user's
#: settings.json, so a re-run replaces only our entry and never duplicates it.
GEMINI_GUARD_MARKER = "guard adapter --harness gemini"

#: Substring identifying omind's own OpenClaw guard gateway hook in openclaw.json.
OPENCLAW_GUARD_MARKER = "guard adapter --harness openclaw"


# -- MCP-only agent locations (Claude Desktop, Kiro, VS Code, Amazon Q) --------
#
# These four register the omi MCP server into a JSON config file and nothing
# else (no guard, no skill). Claude Desktop and VS Code keep their config under
# the OS application-support dir; Kiro and Amazon Q under a fixed ``~`` subdir.


def _app_support_dir() -> Path:
    """The per-user directory GUI apps store config under: ``~/Library/Application
    Support`` (macOS), ``%APPDATA%`` (Windows), else ``$XDG_CONFIG_HOME`` /
    ``~/.config`` (Linux)."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return Path(base) if base else Path.home() / "AppData" / "Roaming"
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base) if base else Path.home() / ".config"


def claude_desktop_dir() -> Path:
    """Claude Desktop's config directory (per-OS application-support / Claude)."""
    return _app_support_dir() / "Claude"


def claude_desktop_config_path() -> Path:
    return claude_desktop_dir() / "claude_desktop_config.json"


def kiro_root() -> Path:
    """Kiro IDE's state directory (``~/.kiro``)."""
    return Path.home() / ".kiro"


def kiro_config_path() -> Path:
    """Kiro's user-level MCP config (``~/.kiro/settings/mcp.json``)."""
    return kiro_root() / "settings" / "mcp.json"


def vscode_user_dir() -> Path:
    """VS Code's per-user config directory (application-support / Code / User)."""
    return _app_support_dir() / "Code" / "User"


def vscode_config_path() -> Path:
    """VS Code's user-level MCP config (``<User>/mcp.json``)."""
    return vscode_user_dir() / "mcp.json"


def amazonq_root() -> Path:
    """Amazon Q Developer's config directory (``~/.aws/amazonq``)."""
    return Path.home() / ".aws" / "amazonq"


def amazonq_config_path() -> Path:
    """Amazon Q's global MCP config (``~/.aws/amazonq/mcp.json``)."""
    return amazonq_root() / "mcp.json"


# -- shared agent machinery ---------------------------------------------------


class AgentProvisioner(Provisioner):
    """Common shape for non-Claude agents: config-file merge + memory skill."""

    AGENT_LABEL: ClassVar[str] = ""
    INSTALL_HINT: ClassVar[str] = ""

    REQUIRED_TOOLS: ClassVar[dict[str, str]] = {
        "git": "the mesh replicates the memory folder over git",
    }

    def agent_root(self) -> Path:
        raise NotImplementedError

    def skill_dir(self) -> Path:
        raise NotImplementedError

    def check_prereqs(self) -> None:
        super().check_prereqs()
        root = self.agent_root()
        if not root.is_dir():
            message = (
                f"{self.AGENT_LABEL} not found: {root} does not exist. "
                f"{self.INSTALL_HINT}"
            )
            if self.config.dry_run:
                self.log(f"  WARNING: {message}")
            else:
                raise ProvisionError(message)
        else:
            self.log(f"  {self.AGENT_LABEL} found: {root}")

    def install_memory_skill(self) -> None:
        skill = self.skill_dir() / paths.AGENT_SKILL_FILENAME
        content = seeds.AGENT_SKILL_TEMPLATE.format(
            vault=self.config.vault,
            folder=self.config.folder,
            omi_dir=self.config.omi_dir,
        )
        self._write_if_absent(skill, content)

    def _drop_legacy_entry(self, servers: dict[str, Any]) -> None:
        """Remove the retired 1.x obsidian-mcp entry from an agent's servers map."""
        legacy = servers.get(LEGACY_SERVER_NAME)
        if (
            self.config.server_name != LEGACY_SERVER_NAME
            and isinstance(legacy, dict)
            and "obsidian-mcp" in json.dumps(legacy)
        ):
            del servers[LEGACY_SERVER_NAME]
            self._record(
                f"remove retired MCP server '{LEGACY_SERVER_NAME}' (obsidian-mcp, "
                "replaced by `omind node`)"
            )

    def _omind_hook_command(self, event: str) -> str:
        """The ``omind hook <event>`` invocation an agent runs for OMI priming.

        Absolute ``omind`` path when resolvable (the agent's spawn environment
        may lack ``~/.local/bin`` on PATH); the ``omind hook`` prefix always
        contains :data:`HOOK_MARKER` so re-runs find and replace our own entry.
        Both folder values are quoted so a path like ``My Vault`` cannot
        word-split into a stray positional.
        """
        omind_exe = shutil.which("omind") or "omind"
        return (
            f'{omind_exe} hook {event} --vault "{self.config.vault}" '
            f'--folder "{self.config.folder}"'
        )

    def integrate(self) -> None:
        # No `claude mcp` CLI here; the retired obsidian entry (if any) lives
        # in these agents' own config files and is dropped by register_mcp.
        self.register_mcp()
        self.install_memory_skill()
        self.install_priming()

    def install_priming(self) -> None:
        """Wire the agent's session-start OMI priming. Overridden per agent —
        Hermes installs a ``pre_llm_call`` hook, OpenClaw a bootstrap file."""
        return None

    def verify(self) -> None:
        """Read-back check; these agents have no `mcp get`-style CLI probe."""
        if self.config.dry_run:
            return
        if self.registered_server() == self.desired_server_entry():
            self.log(f"  verified: '{self.config.server_name}' wired into {self.AGENT_LABEL}")
        else:
            self.log(
                f"  NOTE: could not confirm '{self.config.server_name}' in the "
                f"{self.AGENT_LABEL} config; re-run with --force or wire it manually "
                "(`omind quickstart`)."
            )


# -- Hermes Agent --------------------------------------------------------------


class HermesProvisioner(AgentProvisioner):
    """Wire Hermes Agent: ``mcp_servers`` in config.yaml + the memory skill."""

    AGENT_LABEL = "Hermes Agent"
    INSTALL_HINT = "Install Hermes Agent (it creates ~/.hermes on first run), then re-run."
    DONE_MESSAGE = "Done. Restart Hermes Agent to load the OMI memory tools."

    def agent_root(self) -> Path:
        return hermes_root()

    def skill_dir(self) -> Path:
        return hermes_skill_dir()

    def _read_config(self) -> dict[str, Any]:
        """Load config.yaml as a dict; raise rather than clobber bad YAML."""
        path = hermes_config_path()
        if not path.is_file():
            return {}
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ProvisionError(
                f"{path} is not valid YAML ({exc}); refusing to overwrite. "
                "Fix or remove it and re-run."
            ) from exc
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ProvisionError(
                f"{path} does not contain a YAML mapping; refusing to overwrite."
            )
        return data

    def registered_server(self) -> dict[str, Any] | None:
        try:
            data = self._read_config()
        except ProvisionError:
            return None
        servers = data.get("mcp_servers")
        if not isinstance(servers, dict):
            return None
        server = servers.get(self.config.server_name)
        return server if isinstance(server, dict) else None

    def register_mcp(self) -> None:
        path = hermes_config_path()
        data = self._read_config()
        desired = self.desired_server_entry()
        existing = self.registered_server()
        if existing == desired and not self.config.force:
            self.log(
                f"  MCP server '{self.config.server_name}' already points at "
                f"{self.config.omi_dir}"
            )
            return
        servers = data.get("mcp_servers")
        if not isinstance(servers, dict):
            servers = {}
        self._drop_legacy_entry(servers)
        servers[self.config.server_name] = desired
        data["mcp_servers"] = servers
        self._record(
            f"register MCP server '{self.config.server_name}' in {path} -> "
            f"{self.config.omi_dir}"
        )
        if not self.config.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )

    def install_priming(self) -> None:
        """Install the OMI priming hook into Hermes' ``hooks.pre_llm_call`` and
        pre-approve it in the shell-hook allowlist.

        Hermes fires ``pre_llm_call`` before every LLM turn and injects any
        ``{"context": ...}`` the hook prints; ``omind hook pre_llm_call`` emits
        OMI priming once per session. We touch only our own entry (identified by
        :data:`HOOK_MARKER`), leaving any user-authored hooks in place.
        """
        path = hermes_config_path()
        data = self._read_config()
        command = self._omind_hook_command("pre_llm_call")
        desired = {"command": command, "timeout": 15}

        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}
        entries = hooks.get("pre_llm_call")
        existing = entries if isinstance(entries, list) else []
        kept = [
            e
            for e in existing
            if not (
                isinstance(e, dict)
                and isinstance(e.get("command"), str)
                and HOOK_MARKER in e["command"]
            )
        ]
        merged = kept + [desired]

        if merged != existing or self.config.force:
            hooks["pre_llm_call"] = merged
            data["hooks"] = hooks
            self._record(f"install OMI priming hook (pre_llm_call) in {path}")
            if not self.config.dry_run:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                    encoding="utf-8",
                )
        else:
            self.log(f"  OMI priming hook already installed in {path}")

        self._allowlist_hook("pre_llm_call", command, HOOK_MARKER)

    def integrate(self) -> None:
        super().integrate()
        self.install_guard()

    def _write_guard_script(self) -> None:
        """Write Hermes' OMI-guard ``pre_tool_call`` adapter from package data,
        substituting the omind binary + OMI folder. Managed (refreshed on drift)."""
        dest = hermes_guard_script_path()
        try:
            content = (
                importlib.resources.files("omind")
                .joinpath("omi-guard-hermes.sh")
                .read_text(encoding="utf-8")
            )
        except Exception as exc:
            self.log(f"  WARNING: could not read omi-guard-hermes.sh from package data: {exc}")
            return
        omind_exe = shutil.which("omind") or "omind"
        content = content.replace("__OMIND_BIN__", omind_exe).replace(
            "__OMI_DIR__", str(self.config.omi_dir)
        )
        self._write_managed(dest, content)
        if not self.config.dry_run:
            with contextlib.suppress(OSError):
                dest.chmod(0o755)

    def install_guard(self) -> None:
        """Install the OMI-compliance guard into Hermes' ``pre_tool_call`` hook.

        Hermes' ``pre_tool_call`` can BLOCK (it accepts Claude-Code-style
        ``{"decision":"block"}``), so this gives Hermes the same hard-blocks +
        per-turn consult gate the Claude adapter enforces; the per-turn RESET
        rides on the existing ``pre_llm_call`` priming hook (Hermes' turn
        boundary). The guard entry is identified by the script filename, so
        re-runs never duplicate and user hooks are preserved."""
        self._write_guard_script()
        path = hermes_config_path()
        data = self._read_config()
        command = str(hermes_guard_script_path())
        marker = hermes_guard_script_path().name
        desired = {"command": command, "timeout": 10}

        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}
        entries = hooks.get("pre_tool_call")
        existing = entries if isinstance(entries, list) else []
        kept = [
            e
            for e in existing
            if not (
                isinstance(e, dict)
                and isinstance(e.get("command"), str)
                and marker in e["command"]
            )
        ]
        merged = kept + [desired]
        if merged != existing or self.config.force:
            hooks["pre_tool_call"] = merged
            data["hooks"] = hooks
            self._record(f"install OMI guard hook (pre_tool_call) in {path}")
            if not self.config.dry_run:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                    encoding="utf-8",
                )
        else:
            self.log(f"  OMI guard hook already installed in {path}")
        self._allowlist_hook("pre_tool_call", command, marker)

    def _allowlist_hook(self, event: str, command: str, marker: str) -> None:
        """Pre-approve a hook command in Hermes' consent allowlist so it loads
        without a TTY prompt. Matching is by (event, command); replaces any prior
        omind-owned approval for that event (identified by ``marker`` in the
        command) so a drifted command can't leave a stale grant. Never overwrites
        a file it can't parse."""
        path = hermes_allowlist_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            self.log(
                f"  NOTE: {path} is unreadable/invalid; skipping allowlist "
                "pre-approval (approve the hook at Hermes' TTY prompt, or run "
                "Hermes with --accept-hooks once)."
            )
            return
        data = raw if isinstance(raw, dict) else {}
        approvals = data.get("approvals")
        approvals = approvals if isinstance(approvals, list) else []

        already = any(
            isinstance(e, dict) and e.get("event") == event and e.get("command") == command
            for e in approvals
        )
        if already and not self.config.force:
            return

        kept = [
            e
            for e in approvals
            if not (
                isinstance(e, dict)
                and e.get("event") == event
                and marker in str(e.get("command", ""))
            )
        ]
        kept.append({"event": event, "command": command})
        data["approvals"] = kept
        self._record(f"pre-approve OMI {event} hook in {path}")
        if not self.config.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )


# -- OpenClaw -------------------------------------------------------------------


class OpenClawProvisioner(AgentProvisioner):
    """Wire OpenClaw: ``mcp.servers`` in openclaw.json + the memory skill."""

    AGENT_LABEL = "OpenClaw"
    INSTALL_HINT = "Install OpenClaw (it creates ~/.openclaw on first run), then re-run."
    DONE_MESSAGE = "Done. Restart OpenClaw to load the OMI memory tools."

    def agent_root(self) -> Path:
        return openclaw_root()

    def skill_dir(self) -> Path:
        return openclaw_skill_dir()

    def registered_server(self) -> dict[str, Any] | None:
        path = openclaw_config_path()
        try:
            data = self._read_settings(path)
        except ProvisionError:
            return None
        mcp = data.get("mcp")
        servers = mcp.get("servers") if isinstance(mcp, dict) else None
        if not isinstance(servers, dict):
            return None
        server = servers.get(self.config.server_name)
        return server if isinstance(server, dict) else None

    def register_mcp(self) -> None:
        path = openclaw_config_path()
        data = self._read_settings(path)
        desired = self.desired_server_entry()
        existing = self.registered_server()
        if existing == desired and not self.config.force:
            self.log(
                f"  MCP server '{self.config.server_name}' already points at "
                f"{self.config.omi_dir}"
            )
            return
        mcp = data.get("mcp")
        if not isinstance(mcp, dict):
            mcp = {}
        servers = mcp.get("servers")
        if not isinstance(servers, dict):
            servers = {}
        self._drop_legacy_entry(servers)
        servers[self.config.server_name] = desired
        mcp["servers"] = servers
        data["mcp"] = mcp
        self._record(
            f"register MCP server '{self.config.server_name}' in {path} -> "
            f"{self.config.omi_dir}"
        )
        if not self.config.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def install_priming(self) -> None:
        """Wire OpenClaw to read OMI first each session.

        OpenClaw has no stdout-context hook (Claude's SessionStart, Hermes'
        pre_llm_call); it injects recognized "bootstrap" files (``MEMORY.md`` &
        co.) into the system prompt's Project Context on a session's first turn.
        So omind writes a managed ``MEMORY.md`` priming file in a folder it owns
        and registers it under ``hooks.internal.entries.bootstrap-extra-files``,
        touching only that entry's enable flag and our own path.
        """
        bootstrap = openclaw_bootstrap_path()
        content = seeds.AGENT_PRIMING_BOOTSTRAP_TEMPLATE.format(
            vault=self.config.vault,
            folder=self.config.folder,
            omi_dir=self.config.omi_dir,
        )
        # Managed (not write-if-absent): this carries omind's own priming text,
        # so existing installs must pick up edits rather than keep a stale copy.
        self._write_managed(bootstrap, content)

        path = openclaw_config_path()
        data = self._read_settings(path)
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}
        internal = hooks.get("internal")
        if not isinstance(internal, dict):
            internal = {}
        entries = internal.get("entries")
        if not isinstance(entries, dict):
            entries = {}
        extra = entries.get("bootstrap-extra-files")
        if not isinstance(extra, dict):
            extra = {}
        path_list = extra.get("paths")
        path_list = path_list if isinstance(path_list, list) else []

        wanted = str(bootstrap)
        if wanted in path_list and extra.get("enabled") is True and not self.config.force:
            self.log(f"  OMI bootstrap priming already registered in {path}")
            return
        if wanted not in path_list:
            path_list.append(wanted)
        extra["enabled"] = True
        extra["paths"] = path_list
        entries["bootstrap-extra-files"] = extra
        internal["entries"] = entries
        hooks["internal"] = internal
        data["hooks"] = hooks
        self._record(f"register OMI bootstrap priming (bootstrap-extra-files) in {path}")
        if not self.config.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def integrate(self) -> None:
        super().integrate()
        self.install_guard()

    def install_guard(self) -> None:
        """Register the OMI guard as an OpenClaw gateway hook in ``openclaw.json``.

        OpenClaw's hook transport is an HTTP/WebSocket gateway (POST /hooks/agent
        on :18789, loopback), not a stdout shell hook — so we register a command
        entry the gateway invokes as ``omind guard adapter --harness openclaw``;
        the adapter emits an ``{"allow","reason","rule_id"}`` verdict the gateway
        reads. Until that gateway is confirmed to ENFORCE a deny against a live
        instance, OpenClaw is wired DETECT-ONLY (issue #88) and the verdict is
        advisory. Touches only our own entry (by :data:`OPENCLAW_GUARD_MARKER`),
        preserving any user-authored hooks.
        """
        path = openclaw_config_path()
        data = self._read_settings(path)
        command = f"{shutil.which('omind') or 'omind'} guard adapter --harness openclaw"
        desired = {"event": "pre_tool", "command": command, "enabled": True}
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}
        agent_hooks = hooks.get("agent")
        existing = agent_hooks if isinstance(agent_hooks, list) else []
        kept = [
            e
            for e in existing
            if not (isinstance(e, dict) and OPENCLAW_GUARD_MARKER in json.dumps(e))
        ]
        merged = kept + [desired]
        if merged != existing or self.config.force:
            hooks["agent"] = merged
            data["hooks"] = hooks
            self._record(f"register OMI guard gateway hook (detect-only) in {path}")
            if not self.config.dry_run:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        else:
            self.log(f"  OMI guard gateway hook already installed in {path}")

    def _guard_wired(self) -> bool:
        try:
            data = self._read_settings(openclaw_config_path())
        except ProvisionError:
            return False
        hooks = data.get("hooks")
        agent_hooks = hooks.get("agent") if isinstance(hooks, dict) else None
        return any(
            isinstance(e, dict) and OPENCLAW_GUARD_MARKER in json.dumps(e)
            for e in (agent_hooks or [])
        )


# -- Gemini CLI -----------------------------------------------------------------


class GeminiProvisioner(AgentProvisioner):
    """Wire the OMI guard into the Google Gemini CLI via its ``BeforeTool`` hook.

    Gemini CLI's hooks live under a top-level ``hooks`` key in
    ``~/.gemini/settings.json``. ``BeforeTool`` is the PreToolUse analog and can
    HARD-BLOCK: omind mounts ``omind guard adapter --harness gemini`` matching
    every tool (``matcher: ".*"``). On a deny the adapter prints
    ``{"decision":"deny","reason":...}`` on stdout (exit 0), which Gemini enforces
    as a tool block.

    Guard-only: Gemini MCP-memory registration (``mcpServers`` in the same file)
    is a separate concern and intentionally not bundled here.
    """

    AGENT_LABEL = "Gemini CLI"
    INSTALL_HINT = "Install the Gemini CLI (`npm i -g @google/gemini-cli`), then re-run."
    DONE_MESSAGE = (
        "Done. Restart the Gemini CLI to load the OMI guard "
        "(needs a Gemini CLI with BeforeTool hook support)."
    )

    def agent_root(self) -> Path:
        return gemini_config_dir()

    def integrate(self) -> None:
        # Guard-only wiring (no MCP/skill/priming — see the class docstring).
        self.install_guard()

    def _guard_hook_group(self) -> dict[str, Any]:
        """One ``BeforeTool`` matcher group running the omind gemini adapter on
        every tool. Gemini pipes the event JSON on stdin; the adapter reads it."""
        omind = shutil.which("omind") or "omind"
        return {
            "matcher": ".*",
            "hooks": [
                {
                    "type": "command",
                    "command": f"{omind} guard adapter --harness gemini",
                    "name": "omind-omi-guard",
                    "timeout": 30000,
                }
            ],
        }

    def install_guard(self) -> None:
        """Merge omind's guard hook into ``~/.gemini/settings.json`` under
        ``hooks.BeforeTool``, replacing only our own entry (by
        :data:`GEMINI_GUARD_MARKER`) so user-authored hooks are preserved."""
        path = gemini_settings_path()
        data = self._read_settings(path)
        desired = self._guard_hook_group()
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}
        groups = hooks.get("BeforeTool")
        existing = groups if isinstance(groups, list) else []
        kept = [
            g
            for g in existing
            if not (isinstance(g, dict) and GEMINI_GUARD_MARKER in json.dumps(g))
        ]
        merged = kept + [desired]
        if merged != existing or self.config.force:
            hooks["BeforeTool"] = merged
            data["hooks"] = hooks
            self._record(f"install OMI guard hook (BeforeTool) in {path}")
            if not self.config.dry_run:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        else:
            self.log(f"  OMI guard hook already installed in {path}")

    def _guard_wired(self) -> bool:
        try:
            data = self._read_settings(gemini_settings_path())
        except ProvisionError:
            return False
        hooks = data.get("hooks")
        groups = hooks.get("BeforeTool") if isinstance(hooks, dict) else None
        return any(
            isinstance(g, dict) and GEMINI_GUARD_MARKER in json.dumps(g)
            for g in (groups or [])
        )

    def verify(self) -> None:
        if self.config.dry_run:
            return
        if self._guard_wired():
            self.log(f"  verified: OMI guard wired into Gemini CLI ({gemini_settings_path()})")
        else:
            self.log(
                "  NOTE: could not confirm the OMI guard in Gemini's settings.json; "
                "re-run with --force."
            )


# -- OpenCode -------------------------------------------------------------------


class OpenCodeProvisioner(AgentProvisioner):
    """Wire OpenCode: the ``omi`` MCP server in ``opencode.json`` + the OMI-guard
    plugin. OpenCode auto-loads any module under ``~/.config/opencode/plugin/``;
    the plugin's ``tool.execute.before`` hook throws on a hard-rule deny."""

    AGENT_LABEL = "OpenCode"
    INSTALL_HINT = "Install OpenCode (`npm i -g opencode-ai`), then re-run."
    DONE_MESSAGE = "Done. Restart OpenCode to load the OMI memory + guard plugin."

    def agent_root(self) -> Path:
        return opencode_config_dir()

    def skill_dir(self) -> Path:
        return opencode_config_dir() / "skill" / "omind-omi-memory"

    def registered_server(self) -> dict[str, Any] | None:
        try:
            data = self._read_settings(opencode_config_path())
        except ProvisionError:
            return None
        mcp = data.get("mcp")
        server = mcp.get(self.config.server_name) if isinstance(mcp, dict) else None
        return server if isinstance(server, dict) else None

    def desired_server_entry(self) -> dict[str, Any]:
        # OpenCode local MCP server: a `type: local` + command array.
        omind = shutil.which("omind") or "omind"
        return {
            "type": "local",
            "command": [
                omind,
                "node",
                "--vault",
                str(self.config.vault),
                "--folder",
                self.config.folder,
            ],
            "enabled": True,
        }

    def register_mcp(self) -> None:
        path = opencode_config_path()
        data = self._read_settings(path)
        desired = self.desired_server_entry()
        if self.registered_server() == desired and not self.config.force:
            self.log(
                f"  MCP server '{self.config.server_name}' already points at "
                f"{self.config.omi_dir}"
            )
            return
        mcp = data.get("mcp")
        if not isinstance(mcp, dict):
            mcp = {}
        mcp[self.config.server_name] = desired
        data["mcp"] = mcp
        self._record(
            f"register MCP server '{self.config.server_name}' in {path} -> {self.config.omi_dir}"
        )
        if not self.config.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def integrate(self) -> None:
        super().integrate()
        self.install_guard()

    def install_guard(self) -> None:
        """Write the OMI-guard plugin into OpenCode's auto-loaded ``plugin/`` dir,
        substituting the omind binary + OMI folder. Managed (refreshed on drift)."""
        dest = opencode_guard_plugin_path()
        try:
            content = (
                importlib.resources.files("omind")
                .joinpath("omi-guard.opencode.js")
                .read_text(encoding="utf-8")
            )
        except Exception as exc:
            self.log(f"  WARNING: could not read omi-guard.opencode.js from package data: {exc}")
            return
        omind_exe = shutil.which("omind") or "omind"
        content = content.replace("__OMIND_BIN__", omind_exe).replace(
            "__OMI_DIR__", str(self.config.omi_dir)
        )
        self._write_managed(dest, content)


# -- Codex CLI ------------------------------------------------------------------


class CodexProvisioner(AgentProvisioner):
    """Wire the OMI guard into OpenAI Codex CLI via its lifecycle hooks.

    Codex (>= 0.117) adopted the Claude-Code hook schema: ``PreToolUse`` /
    ``PermissionRequest`` command hooks loaded from ``~/.codex/hooks.json`` (the
    ``hooks`` feature is stable and on by default — no ``config.toml`` change
    needed). omind mounts ``omind guard adapter --harness codex`` on BOTH events
    (PreToolUse blocks at the tool call; PermissionRequest is the approval-path
    backstop); on a hard-rule deny the adapter emits Codex's
    ``permissionDecision: deny`` / ``decision.behavior: deny`` shape.

    Guard-only: Codex MCP-memory registration is a separate concern (``codex mcp
    add`` / ``config.toml`` ``[mcp_servers]``) and is intentionally not bundled
    here. Codex's trust model records hooks by hash and SKIPS untrusted ones, so
    the user must review + trust the hook once via ``/hooks``.
    """

    AGENT_LABEL = "Codex CLI"
    INSTALL_HINT = "Install Codex CLI (`npm i -g @openai/codex`, or the snap), then re-run."
    DONE_MESSAGE = (
        "Done. In Codex run `/hooks` and TRUST the omind guard hook — Codex skips "
        "untrusted hooks until reviewed. Requires Codex >= 0.117 (PreToolUse hooks)."
    )

    def agent_root(self) -> Path:
        return codex_config_dir()

    def integrate(self) -> None:
        # Guard-only wiring (no MCP/skill/priming — see the class docstring).
        self.install_guard()

    def _guard_hook_group(self) -> dict[str, Any]:
        """One Claude-schema matcher group running the omind codex adapter on all
        tools. Codex pipes the event JSON on stdin; the adapter reads it directly."""
        omind = shutil.which("omind") or "omind"
        return {
            "hooks": [
                {
                    "type": "command",
                    "command": f"{omind} guard adapter --harness codex",
                    "timeout": 30,
                }
            ]
        }

    def install_guard(self) -> None:
        """Merge omind's guard hook into ``~/.codex/hooks.json`` for ``PreToolUse``
        and ``PermissionRequest``, replacing only our own entry (by
        :data:`CODEX_GUARD_MARKER`) so user-authored hooks are preserved."""
        path = codex_hooks_path()
        data = self._read_settings(path)
        desired = self._guard_hook_group()
        changed = False
        for event in ("PreToolUse", "PermissionRequest"):
            groups = data.get(event)
            existing = groups if isinstance(groups, list) else []
            kept = [
                g
                for g in existing
                if not (isinstance(g, dict) and CODEX_GUARD_MARKER in json.dumps(g))
            ]
            merged = kept + [desired]
            if merged != existing:
                data[event] = merged
                changed = True
        if changed or self.config.force:
            self._record(
                f"install OMI guard hooks (PreToolUse + PermissionRequest) in {path}"
            )
            if not self.config.dry_run:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        else:
            self.log(f"  OMI guard hooks already installed in {path}")

    def _guard_wired(self) -> bool:
        try:
            data = self._read_settings(codex_hooks_path())
        except ProvisionError:
            return False
        return all(
            any(
                isinstance(g, dict) and CODEX_GUARD_MARKER in json.dumps(g)
                for g in (data.get(event) or [])
            )
            for event in ("PreToolUse", "PermissionRequest")
        )

    def verify(self) -> None:
        if self.config.dry_run:
            return
        if self._guard_wired():
            self.log(f"  verified: OMI guard wired into Codex ({codex_hooks_path()})")
            self.log("  NEXT: run `/hooks` in Codex and TRUST the omind hook.")
        else:
            self.log(
                "  NOTE: could not confirm the OMI guard in Codex's hooks.json; "
                "re-run with --force."
            )


# -- MCP-only targets (register the omi server, no guard / skill / priming) -----


class McpOnlyProvisioner(AgentProvisioner):
    """An agent wired purely by writing the ``omi`` MCP server into a JSON config
    block — no guard hook, no memory skill, no priming.

    Subclasses set :attr:`AGENT_LABEL`, :attr:`INSTALL_HINT`, :attr:`DONE_MESSAGE`,
    :meth:`agent_root` and :meth:`config_path`. Most use the standard
    ``{"command", "args"}`` stdio entry under an ``mcpServers`` block; VS Code
    overrides :attr:`BLOCK_KEY` to ``"servers"`` and :attr:`STDIO_TYPE` to emit an
    explicit ``"type": "stdio"`` field.
    """

    #: Top-level JSON key the agent reads its MCP servers from.
    BLOCK_KEY: ClassVar[str] = "mcpServers"
    #: Whether each server entry carries an explicit ``"type": "stdio"`` (VS Code).
    STDIO_TYPE: ClassVar[bool] = False

    def config_path(self) -> Path:
        raise NotImplementedError

    def registered_server(self) -> dict[str, Any] | None:
        try:
            data = self._read_settings(self.config_path())
        except ProvisionError:
            return None
        block = data.get(self.BLOCK_KEY)
        server = block.get(self.config.server_name) if isinstance(block, dict) else None
        return server if isinstance(server, dict) else None

    def desired_server_entry(self) -> dict[str, Any]:
        omind = shutil.which("omind") or "omind"
        entry: dict[str, Any] = {}
        if self.STDIO_TYPE:
            entry["type"] = "stdio"
        entry["command"] = omind
        entry["args"] = [
            "node",
            "--vault",
            str(self.config.vault),
            "--folder",
            self.config.folder,
        ]
        return entry

    def register_mcp(self) -> None:
        path = self.config_path()
        data = self._read_settings(path)
        desired = self.desired_server_entry()
        if self.registered_server() == desired and not self.config.force:
            self.log(
                f"  MCP server '{self.config.server_name}' already points at "
                f"{self.config.omi_dir}"
            )
            return
        block = data.get(self.BLOCK_KEY)
        if not isinstance(block, dict):
            block = {}
        self._drop_legacy_entry(block)
        block[self.config.server_name] = desired
        data[self.BLOCK_KEY] = block
        self._record(
            f"register MCP server '{self.config.server_name}' in {path} -> {self.config.omi_dir}"
        )
        if not self.config.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def integrate(self) -> None:
        # MCP registration only — no skill / priming / guard for these targets.
        self.register_mcp()


class ClaudeDesktopProvisioner(McpOnlyProvisioner):
    """Wire the Claude Desktop app: the ``omi`` server in its
    ``claude_desktop_config.json`` (``mcpServers`` block, stdio)."""

    AGENT_LABEL = "Claude Desktop"
    INSTALL_HINT = "Install the Claude Desktop app and launch it once, then re-run."
    DONE_MESSAGE = "Done. Restart Claude Desktop to load the OMI memory tools."

    def agent_root(self) -> Path:
        return claude_desktop_dir()

    def config_path(self) -> Path:
        return claude_desktop_config_path()


class KiroProvisioner(McpOnlyProvisioner):
    """Wire Kiro IDE: the ``omi`` server in ``~/.kiro/settings/mcp.json``
    (``mcpServers`` block, stdio)."""

    AGENT_LABEL = "Kiro"
    INSTALL_HINT = "Install Kiro (it creates ~/.kiro on first run), then re-run."
    DONE_MESSAGE = "Done. Restart Kiro (or reconnect MCP) to load the OMI memory tools."

    def agent_root(self) -> Path:
        return kiro_root()

    def config_path(self) -> Path:
        return kiro_config_path()


class VsCodeProvisioner(McpOnlyProvisioner):
    """Wire VS Code's native MCP: the ``omi`` server in the user-level
    ``mcp.json`` (a ``servers`` block with an explicit ``type: stdio``)."""

    AGENT_LABEL = "VS Code"
    INSTALL_HINT = "Install VS Code and launch it once (creates its User dir), then re-run."
    DONE_MESSAGE = "Done. Reload VS Code to load the OMI memory tools (needs MCP/agent mode)."
    BLOCK_KEY = "servers"
    STDIO_TYPE = True

    def agent_root(self) -> Path:
        return vscode_user_dir()

    def config_path(self) -> Path:
        return vscode_config_path()


class AmazonQProvisioner(McpOnlyProvisioner):
    """Wire the Amazon Q Developer CLI/IDE: the ``omi`` server in
    ``~/.aws/amazonq/mcp.json`` (``mcpServers`` block, stdio)."""

    AGENT_LABEL = "Amazon Q"
    INSTALL_HINT = "Install Amazon Q (it creates ~/.aws/amazonq), then re-run."
    DONE_MESSAGE = "Done. Restart Amazon Q to load the OMI memory tools."

    def agent_root(self) -> Path:
        return amazonq_root()

    def config_path(self) -> Path:
        return amazonq_config_path()


# -- diagnose -------------------------------------------------------------------


def _diagnose_agent(provisioner: AgentProvisioner) -> list[CheckResult]:
    """The agent-specific doctor checks shared by Hermes and OpenClaw."""
    config = provisioner.config
    label = provisioner.AGENT_LABEL
    key = label.split()[0].lower()
    results = _diagnose_tools(provisioner.REQUIRED_TOOLS)

    root = provisioner.agent_root()
    if root.is_dir():
        results.append(CheckResult(f"{key}_root", "ok", f"{label} found: {root}"))
    else:
        results.append(
            CheckResult(f"{key}_root", "fail", f"{label} not found: {root} does not exist")
        )

    results.extend(_diagnose_omi_folder(config))

    name = config.server_name
    server = provisioner.registered_server()
    if server is None:
        results.append(
            CheckResult(
                f"{key}_mcp_registration",
                "fail",
                f"MCP server '{name}' not in the {label} config "
                f"(run `omind setup --agent {key}`)",
            )
        )
    elif server != provisioner.desired_server_entry():
        results.append(
            CheckResult(
                f"{key}_mcp_registration",
                "warn",
                f"MCP server '{name}' in the {label} config differs from the "
                f"expected wiring (run `omind setup --agent {key}`)",
            )
        )
    else:
        results.append(
            CheckResult(f"{key}_mcp_registration", "ok", f"MCP server '{name}' -> {config.omi_dir}")
        )

    skill = provisioner.skill_dir() / paths.AGENT_SKILL_FILENAME
    if skill.is_file():
        results.append(CheckResult(f"{key}_skill", "ok", f"memory skill installed: {skill}"))
    else:
        results.append(
            CheckResult(
                f"{key}_skill",
                "warn",
                f"memory skill missing: {skill} (run `omind setup --agent {key}`)",
            )
        )
    return results


def diagnose_hermes(config: SetupConfig) -> list[CheckResult]:
    return _diagnose_agent(HermesProvisioner(config=config, log=lambda _msg: None))


def diagnose_openclaw(config: SetupConfig) -> list[CheckResult]:
    prov = OpenClawProvisioner(config=config, log=lambda _msg: None)
    results = _diagnose_agent(prov)
    if prov._guard_wired():
        results.append(
            CheckResult(
                "openclaw_guard",
                "ok",
                f"OMI guard (detect-only) wired into {openclaw_config_path()}",
            )
        )
    else:
        results.append(
            CheckResult(
                "openclaw_guard",
                "warn",
                "OMI guard not in openclaw.json (run `omind setup --agent openclaw`)",
            )
        )
    return results


def diagnose_opencode(config: SetupConfig) -> list[CheckResult]:
    return _diagnose_agent(OpenCodeProvisioner(config=config, log=lambda _msg: None))


def diagnose_codex(config: SetupConfig) -> list[CheckResult]:
    """Codex is guard-only (no MCP/skill), so its doctor checks the hooks.json
    guard wiring rather than the MCP-registration path the others share."""
    prov = CodexProvisioner(config=config, log=lambda _msg: None)
    results = _diagnose_tools(prov.REQUIRED_TOOLS)
    root = codex_config_dir()
    if root.is_dir():
        results.append(CheckResult("codex_root", "ok", f"Codex CLI found: {root}"))
    else:
        results.append(
            CheckResult("codex_root", "fail", f"Codex CLI not found: {root} does not exist")
        )
    results.extend(_diagnose_omi_folder(prov.config))
    if prov._guard_wired():
        results.append(
            CheckResult(
                "codex_guard",
                "ok",
                f"OMI guard wired into {codex_hooks_path()} (run `/hooks` in Codex to trust it)",
            )
        )
    else:
        results.append(
            CheckResult(
                "codex_guard",
                "fail",
                "OMI guard not in Codex hooks.json (run `omind setup --agent codex`)",
            )
        )
    return results


def diagnose_gemini(config: SetupConfig) -> list[CheckResult]:
    """Gemini is guard-only here (no MCP/skill), so its doctor checks the
    settings.json ``BeforeTool`` guard wiring rather than MCP registration."""
    prov = GeminiProvisioner(config=config, log=lambda _msg: None)
    results = _diagnose_tools(prov.REQUIRED_TOOLS)
    root = gemini_config_dir()
    if root.is_dir():
        results.append(CheckResult("gemini_root", "ok", f"Gemini CLI found: {root}"))
    else:
        results.append(
            CheckResult("gemini_root", "fail", f"Gemini CLI not found: {root} does not exist")
        )
    results.extend(_diagnose_omi_folder(prov.config))
    if prov._guard_wired():
        results.append(
            CheckResult("gemini_guard", "ok", f"OMI guard wired into {gemini_settings_path()}")
        )
    else:
        results.append(
            CheckResult(
                "gemini_guard",
                "fail",
                "OMI guard not in Gemini settings.json (run `omind setup --agent gemini`)",
            )
        )
    return results


def _diagnose_mcp_only(provisioner: McpOnlyProvisioner) -> list[CheckResult]:
    """Doctor checks for an MCP-registration-only target: tools + agent root +
    OMI folder + MCP registration. No skill check (these install none)."""
    config = provisioner.config
    label = provisioner.AGENT_LABEL
    key = config.agent
    results = _diagnose_tools(provisioner.REQUIRED_TOOLS)

    root = provisioner.agent_root()
    if root.is_dir():
        results.append(CheckResult(f"{key}_root", "ok", f"{label} found: {root}"))
    else:
        results.append(
            CheckResult(f"{key}_root", "fail", f"{label} not found: {root} does not exist")
        )

    results.extend(_diagnose_omi_folder(config))

    name = config.server_name
    server = provisioner.registered_server()
    if server is None:
        results.append(
            CheckResult(
                f"{key}_mcp_registration",
                "fail",
                f"MCP server '{name}' not in the {label} config "
                f"(run `omind setup --agent {key}`)",
            )
        )
    elif server != provisioner.desired_server_entry():
        results.append(
            CheckResult(
                f"{key}_mcp_registration",
                "warn",
                f"MCP server '{name}' in the {label} config differs from the "
                f"expected wiring (run `omind setup --agent {key}`)",
            )
        )
    else:
        results.append(
            CheckResult(f"{key}_mcp_registration", "ok", f"MCP server '{name}' -> {config.omi_dir}")
        )
    return results


def diagnose_claude_desktop(config: SetupConfig) -> list[CheckResult]:
    return _diagnose_mcp_only(ClaudeDesktopProvisioner(config=config, log=lambda _msg: None))


def diagnose_kiro(config: SetupConfig) -> list[CheckResult]:
    return _diagnose_mcp_only(KiroProvisioner(config=config, log=lambda _msg: None))


def diagnose_vscode(config: SetupConfig) -> list[CheckResult]:
    return _diagnose_mcp_only(VsCodeProvisioner(config=config, log=lambda _msg: None))


def diagnose_q(config: SetupConfig) -> list[CheckResult]:
    return _diagnose_mcp_only(AmazonQProvisioner(config=config, log=lambda _msg: None))


# -- dispatch -------------------------------------------------------------------

PROVISIONERS: dict[str, type[Provisioner]] = {
    "claude": Provisioner,
    "hermes": HermesProvisioner,
    "openclaw": OpenClawProvisioner,
    "opencode": OpenCodeProvisioner,
    "codex": CodexProvisioner,
    "gemini": GeminiProvisioner,
    "claude-desktop": ClaudeDesktopProvisioner,
    "kiro": KiroProvisioner,
    "vscode": VsCodeProvisioner,
    "q": AmazonQProvisioner,
}

DIAGNOSERS = {
    "claude": diagnose,
    "hermes": diagnose_hermes,
    "openclaw": diagnose_openclaw,
    "opencode": diagnose_opencode,
    "codex": diagnose_codex,
    "gemini": diagnose_gemini,
    "claude-desktop": diagnose_claude_desktop,
    "kiro": diagnose_kiro,
    "vscode": diagnose_vscode,
    "q": diagnose_q,
}

AGENT_CHOICES = tuple(PROVISIONERS)


def run_setup_for(config: SetupConfig, log: Logger = print) -> list[str]:
    """Run the provisioner for ``config.agent`` (claude, hermes, openclaw, opencode,
    codex, gemini, claude-desktop, kiro, vscode, q)."""
    return PROVISIONERS[config.agent](config=config, log=log).run()


def diagnose_for(config: SetupConfig) -> list[CheckResult]:
    """The doctor checks for ``config.agent``."""
    return DIAGNOSERS[config.agent](config)
