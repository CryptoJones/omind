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

import json
import os
from pathlib import Path
from typing import Any, ClassVar

import yaml

from omind import paths, seeds
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

    def integrate(self) -> None:
        # No `claude mcp` CLI here; the retired obsidian entry (if any) lives
        # in these agents' own config files and is dropped by register_mcp.
        self.register_mcp()
        self.install_memory_skill()

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
    return _diagnose_agent(OpenClawProvisioner(config=config, log=lambda _msg: None))


# -- dispatch -------------------------------------------------------------------

PROVISIONERS: dict[str, type[Provisioner]] = {
    "claude": Provisioner,
    "hermes": HermesProvisioner,
    "openclaw": OpenClawProvisioner,
}

DIAGNOSERS = {
    "claude": diagnose,
    "hermes": diagnose_hermes,
    "openclaw": diagnose_openclaw,
}

AGENT_CHOICES = tuple(PROVISIONERS)


def run_setup_for(config: SetupConfig, log: Logger = print) -> list[str]:
    """Run the provisioner for ``config.agent`` (claude, hermes, or openclaw)."""
    return PROVISIONERS[config.agent](config=config, log=log).run()


def diagnose_for(config: SetupConfig) -> list[CheckResult]:
    """The doctor checks for ``config.agent``."""
    return DIAGNOSERS[config.agent](config)
