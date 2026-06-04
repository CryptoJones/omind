# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Idempotently wire up the OMI/Obsidian MCP integration for Claude Code.

`omind setup` reproduces, on any machine, the manual steps that point the
`obsidian-mcp` server at an OMI folder and register it with the Claude Code CLI
at user scope. Every step is safe to re-run: existing files are never
clobbered, and the MCP server is only (re)registered when its path differs.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from omind import seeds

Logger = Callable[[str], None]


class ProvisionError(Exception):
    """A setup precondition failed (missing tool, bad vault layout, ...)."""


def default_vault_path() -> Path:
    """The conventional Obsidian vault location, cross-platform."""
    return Path.home() / "Documents" / "Obsidian Vault"


def claude_config_path() -> Path:
    return Path.home() / ".claude" / ".claude.json"


@dataclass
class SetupConfig:
    vault: Path
    folder: str = "OMI"
    server_name: str = "obsidian"
    dry_run: bool = False
    force: bool = False

    @property
    def omi_dir(self) -> Path:
        return self.vault / self.folder


@dataclass
class Provisioner:
    config: SetupConfig
    log: Logger = print
    actions: list[str] = field(default_factory=list)

    # -- helpers ------------------------------------------------------------

    def _record(self, message: str) -> None:
        prefix = "[dry-run] would " if self.config.dry_run else ""
        line = f"{prefix}{message}"
        self.actions.append(line)
        self.log(line)

    def _write_if_absent(self, path: Path, content: str) -> None:
        if path.exists() and not self.config.force:
            self.log(f"  exists, leaving as-is: {path}")
            return
        self._record(f"write {path}")
        if not self.config.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    # -- steps --------------------------------------------------------------

    def check_prereqs(self) -> list[str]:
        """Return missing required executables; raise unless dry-run."""
        required = {
            "node": "obsidian-mcp runs on Node.js",
            "npx": "npx launches the obsidian-mcp package",
            "claude": "the Claude Code CLI registers the MCP server",
        }
        missing = [tool for tool in required if shutil.which(tool) is None]
        if missing:
            details = "; ".join(f"{t} ({required[t]})" for t in missing)
            message = (
                f"missing required tool(s): {details}. "
                "Install Node.js (for node/npx) and the Claude Code CLI, then re-run."
            )
            if self.config.dry_run:
                self.log(f"  WARNING: {message}")
            else:
                raise ProvisionError(message)
        else:
            self.log("  prerequisites present: node, npx, claude")
        return missing

    def ensure_vault(self) -> None:
        self._record(f"create OMI folder {self.config.omi_dir}")
        if not self.config.dry_run:
            self.config.omi_dir.mkdir(parents=True, exist_ok=True)

    def ensure_obsidian_config(self) -> None:
        obsidian_dir = self.config.omi_dir / ".obsidian"
        if obsidian_dir.exists() and not obsidian_dir.is_dir():
            raise ProvisionError(
                f"{obsidian_dir} exists but is not a directory; obsidian-mcp needs a "
                ".obsidian/ config folder. Move or remove that file and re-run."
            )
        self._record(f"ensure {obsidian_dir}/ with app.json, core-plugins.json, appearance.json")
        if not self.config.dry_run:
            obsidian_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in seeds.OBSIDIAN_CONFIG_FILES.items():
            self._write_if_absent(obsidian_dir / filename, content)

    def seed_memory_files(self) -> None:
        self._write_if_absent(
            self.config.omi_dir / seeds.MEMORY_TEMPLATE_FILENAME,
            seeds.MEMORY_TEMPLATE,
        )
        index_seed = (
            seeds.INDEX_INTRO.rstrip()
            + "\n\n"
            + seeds.INDEX_RECENT_HEADING
            + "\n"
            + seeds.INDEX_RECENT_COMMENT
            + "\n"
        )
        self._write_if_absent(self.config.omi_dir / seeds.INDEX_FILENAME, index_seed)

    def registered_server(self) -> dict[str, object] | None:
        path = claude_config_path()
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            return None
        server = servers.get(self.config.server_name)
        return server if isinstance(server, dict) else None

    @staticmethod
    def _server_path(server: dict[str, object]) -> str | None:
        args = server.get("args")
        if isinstance(args, list) and args:
            return str(args[-1])
        return None

    def register_mcp(self) -> None:
        target = str(self.config.omi_dir)
        existing = self.registered_server()
        if existing is not None and self._server_path(existing) == target and not self.config.force:
            self.log(f"  MCP server '{self.config.server_name}' already points at {target}")
            return
        if existing is not None:
            self._record(
                f"remove existing MCP server '{self.config.server_name}' "
                f"(was {self._server_path(existing)!r})"
            )
            self._run(["claude", "mcp", "remove", self.config.server_name, "-s", "user"])
        self._record(
            f"register MCP server '{self.config.server_name}' -> "
            f"npx -y obsidian-mcp {target!r} (user scope)"
        )
        self._run(
            [
                "claude", "mcp", "add", "-s", "user", self.config.server_name,
                "--", "npx", "-y", "obsidian-mcp", target,
            ]
        )

    def verify(self) -> None:
        if self.config.dry_run:
            return
        result = self._run(
            ["claude", "mcp", "get", self.config.server_name],
            check=False,
            capture=True,
        )
        out = (result.stdout or "") + (result.stderr or "")
        if "Connected" in out:
            self.log(f"  verified: '{self.config.server_name}' is Connected")
        else:
            self.log(
                f"  NOTE: could not confirm '{self.config.server_name}' is Connected. "
                "Restart Claude Code to load the new tools, then run "
                f"`claude mcp get {self.config.server_name}`."
            )

    def _run(
        self,
        cmd: list[str],
        *,
        check: bool = True,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if self.config.dry_run:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        try:
            return subprocess.run(
                cmd,
                check=check,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:  # claude / npx vanished mid-run
            raise ProvisionError(f"command not found: {cmd[0]}") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            raise ProvisionError(f"command failed: {' '.join(cmd)}\n{detail}") from exc

    # -- orchestration ------------------------------------------------------

    def run(self) -> list[str]:
        self.log(f"omind setup -> {self.config.omi_dir}")
        self.check_prereqs()
        self.ensure_vault()
        self.ensure_obsidian_config()
        self.seed_memory_files()
        self.register_mcp()
        self.verify()
        if not self.config.dry_run:
            self.log("Done. Restart Claude Code to load the OMI memory tools.")
        return self.actions


def run_setup(config: SetupConfig, log: Logger = print) -> list[str]:
    """Convenience wrapper: build a :class:`Provisioner` and run it."""
    return Provisioner(config=config, log=log).run()


@dataclass
class CheckResult:
    """One diagnostic line from :func:`diagnose`."""

    key: str
    level: str  # "ok" | "warn" | "fail"
    message: str


def diagnose(config: SetupConfig) -> list[CheckResult]:
    """Inspect the wiring `omind setup` creates and report what's healthy.

    Pure inspection — touches nothing. ``fail`` means memory won't work until
    fixed; ``warn`` means it'll work but something is off or merely cosmetic.
    """
    results: list[CheckResult] = []

    for tool, why in (
        ("node", "obsidian-mcp runs on Node.js"),
        ("npx", "npx launches the obsidian-mcp package"),
        ("claude", "the Claude Code CLI registers the MCP server"),
    ):
        if shutil.which(tool) is not None:
            results.append(CheckResult(f"tool:{tool}", "ok", f"{tool} found on PATH"))
        else:
            results.append(CheckResult(f"tool:{tool}", "fail", f"{tool} not found — {why}"))

    omi = config.omi_dir
    if omi.is_dir():
        results.append(CheckResult("omi_dir", "ok", f"OMI folder readable: {omi}"))
    else:
        results.append(
            CheckResult("omi_dir", "fail", f"OMI folder missing: {omi} (run `omind setup`)")
        )

    app_json = omi / ".obsidian" / "app.json"
    if app_json.is_file():
        results.append(CheckResult("obsidian_config", "ok", f"Obsidian config present: {app_json}"))
    else:
        results.append(
            CheckResult(
                "obsidian_config",
                "fail",
                f"missing {app_json} — obsidian-mcp needs it to start",
            )
        )

    missing_seeds = [
        name
        for name in (seeds.MEMORY_TEMPLATE_FILENAME, seeds.INDEX_FILENAME)
        if not (omi / name).is_file()
    ]
    if missing_seeds:
        results.append(
            CheckResult("seeds", "warn", f"missing seed file(s): {', '.join(missing_seeds)}")
        )
    else:
        results.append(CheckResult("seeds", "ok", "seed files present (template + index)"))

    prov = Provisioner(config=config, log=lambda _msg: None)
    server = prov.registered_server()
    name = config.server_name
    target = str(omi)
    if server is None:
        results.append(
            CheckResult(
                "mcp_registration",
                "fail",
                f"MCP server '{name}' not registered at user scope (run `omind setup`)",
            )
        )
    else:
        path = prov._server_path(server)
        if path == target:
            results.append(
                CheckResult("mcp_registration", "ok", f"MCP server '{name}' -> {target}")
            )
        else:
            results.append(
                CheckResult(
                    "mcp_registration",
                    "warn",
                    f"MCP server '{name}' points at {path!r}, expected {target!r}",
                )
            )

    return results


def run_doctor(config: SetupConfig, log: Logger = print) -> int:
    """Print the diagnostic checklist; return an exit code (0 = healthy)."""
    log(f"omind doctor -> {config.omi_dir}")
    symbols = {"ok": "✓", "warn": "!", "fail": "✗"}
    results = diagnose(config)
    for result in results:
        log(f"  [{symbols[result.level]}] {result.message}")
    fails = sum(1 for r in results if r.level == "fail")
    warns = sum(1 for r in results if r.level == "warn")
    if fails:
        log(f"\n{fails} problem(s), {warns} warning(s). Run `omind setup` to repair the wiring.")
        return 1
    if warns:
        log(f"\nHealthy, with {warns} warning(s) above.")
        return 0
    log("\nAll checks passed.")
    return 0
