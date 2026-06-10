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
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from omind import seeds
from omind.hooks import HANDLED_EVENTS, HOOK_MARKER, JOURNAL_DIRNAME
from omind.journal import find_stray_journals, migrate_journals

Logger = Callable[[str], None]


class ProvisionError(Exception):
    """A setup precondition failed (missing tool, bad vault layout, ...)."""


def default_vault_path() -> Path:
    """The conventional Obsidian vault location, cross-platform."""
    return Path.home() / "Documents" / "Obsidian Vault"


def claude_config_path() -> Path:
    """Path to the Claude Code CLI config that holds ``mcpServers``.

    When ``CLAUDE_CONFIG_DIR`` is set, the CLI keeps this file at
    ``$CLAUDE_CONFIG_DIR/.claude.json`` — unconditionally, so omind must
    mirror that even if a stale ``~/.claude.json`` also exists (reading the
    stale file made ``doctor`` report a false "not registered" and ``setup``
    re-run ``claude mcp add`` into an "already exists" error).

    Without the env var, Claude Code stores it at ``~/.claude.json``. Earlier
    omind versions looked inside ``~/.claude/`` — a directory that never holds
    the user-scope MCP registration absent ``CLAUDE_CONFIG_DIR`` — producing
    the same false negatives. Fall back to that old location only if the
    canonical file is absent.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / ".claude.json"
    primary = Path.home() / ".claude.json"
    legacy = Path.home() / ".claude" / ".claude.json"
    if not primary.is_file() and legacy.is_file():
        return legacy
    return primary


def claude_settings_path() -> Path:
    """Path to Claude Code's ``settings.json`` — where hooks live.

    Distinct from :func:`claude_config_path` (``~/.claude.json``, which holds
    ``mcpServers``). Hooks, theme, and permission config live in
    ``~/.claude/settings.json`` — or ``$CLAUDE_CONFIG_DIR/settings.json``
    when the env var relocates the config directory.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / "settings.json"
    return Path.home() / ".claude" / "settings.json"


def _entry_has_omind_marker(entry: object) -> bool:
    """True if a hooks-array entry was installed by omind (command has the marker)."""
    if not isinstance(entry, dict):
        return False
    hook_list = entry.get("hooks")
    if not isinstance(hook_list, list):
        return False
    for hook in hook_list:
        if isinstance(hook, dict):
            command = hook.get("command")
            if isinstance(command, str) and HOOK_MARKER in command:
                return True
    return False


def _entry_command_text(entry: object) -> str:
    """Concatenated command strings of a hooks-array entry (for path inspection)."""
    parts: list[str] = []
    if isinstance(entry, dict):
        hook_list = entry.get("hooks")
        if isinstance(hook_list, list):
            for hook in hook_list:
                if isinstance(hook, dict):
                    command = hook.get("command")
                    if isinstance(command, str):
                        parts.append(command)
    return " ".join(parts)


# obsidian-mcp version pinned to the install layout below (entry at
# build/main.js). Bump together with any move to a newer release.
OBSIDIAN_MCP_VERSION = "1.0.6"


def mcp_servers_dir() -> Path:
    """Stable home for omind-managed MCP server installs and the EOF guard.

    Deliberately *not* the npx cache (``~/.npm/_npx/<hash>/``), which npm can
    garbage-collect out from under a registered server.
    """
    return Path.home() / ".claude" / "mcp-servers"


def server_install_dir() -> Path:
    """Where ``obsidian-mcp`` is installed (an npm prefix we control)."""
    return mcp_servers_dir() / "obsidian"


def obsidian_mcp_entry() -> Path:
    """The obsidian-mcp entry script inside :func:`server_install_dir`."""
    return server_install_dir() / "node_modules" / "obsidian-mcp" / "build" / "main.js"


def eof_guard_path() -> Path:
    """The stdin-EOF preload that stops the server orphaning on exit."""
    return mcp_servers_dir() / seeds.EOF_GUARD_FILENAME


@dataclass
class SetupConfig:
    vault: Path
    folder: str = "OMI"
    server_name: str = "obsidian"
    dry_run: bool = False
    force: bool = False
    agent: str = "claude"

    @property
    def omi_dir(self) -> Path:
        return self.vault / self.folder


@dataclass
class Provisioner:
    config: SetupConfig
    log: Logger = print
    actions: list[str] = field(default_factory=list)

    #: tool -> why it is needed; subclasses for other agents override this.
    REQUIRED_TOOLS: ClassVar[dict[str, str]] = {
        "node": "obsidian-mcp runs on Node.js",
        "npm": "npm installs the obsidian-mcp package",
        "claude": "the Claude Code CLI registers the MCP server",
    }
    DONE_MESSAGE: ClassVar[str] = "Done. Restart Claude Code to load the OMI memory tools."

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

    def check_prereqs(self) -> None:
        """Raise (unless dry-run) when a required executable is missing."""
        required = self.REQUIRED_TOOLS
        missing = [tool for tool in required if shutil.which(tool) is None]
        if missing:
            details = "; ".join(f"{t} ({required[t]})" for t in missing)
            message = (
                f"missing required tool(s): {details}. Install them, then re-run."
            )
            if self.config.dry_run:
                self.log(f"  WARNING: {message}")
            else:
                raise ProvisionError(message)
        else:
            self.log(f"  prerequisites present: {', '.join(required)}")

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

    def migrate_journal_notes(self) -> None:
        """Move stray daily journals (vault-folder root, legacy ``logs/``) into
        ``Journal/`` and regenerate the index. Idempotent and lock-protected —
        see :func:`omind.journal.migrate_journals`.
        """
        strays = find_stray_journals(self.config.omi_dir)
        if not strays:
            self.log("  session journals already in Journal/, nothing to migrate")
            return
        self._record(
            f"move {len(strays)} session journal(s) into "
            f"{self.config.omi_dir / JOURNAL_DIRNAME} and reindex"
        )
        if not self.config.dry_run:
            migrate_journals(self.config.omi_dir)

    def ensure_server_install(self) -> None:
        """Install obsidian-mcp to a stable prefix and write the stdin-EOF guard.

        Registering a direct ``node`` command needs both a path that won't be
        garbage-collected (unlike the npx cache) and the preload that stops the
        server orphaning when Claude Code exits.
        """
        self._write_if_absent(eof_guard_path(), seeds.EOF_GUARD_JS)
        entry = obsidian_mcp_entry()
        if entry.is_file() and not self.config.force:
            self.log(f"  obsidian-mcp already installed: {entry}")
            return
        install_dir = server_install_dir()
        self._record(f"install obsidian-mcp@{OBSIDIAN_MCP_VERSION} into {install_dir}")
        if not self.config.dry_run:
            install_dir.mkdir(parents=True, exist_ok=True)
            self._run(
                [
                    "npm", "install", "--prefix", str(install_dir),
                    f"obsidian-mcp@{OBSIDIAN_MCP_VERSION}", "--no-audit", "--no-fund",
                ]
            )

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

    @staticmethod
    def _is_direct_node(server: dict[str, object]) -> bool:
        """True if the server uses the leak-free ``node --require`` command.

        The old ``npx -y obsidian-mcp`` form orphans the Node process on exit
        (see docs/troubleshooting.md), so we treat it as out of date.
        """
        if server.get("command") != "node":
            return False
        args = server.get("args")
        return isinstance(args, list) and "--require" in args

    def _server_command(self, target: str) -> list[str]:
        return [
            "node",
            "--require", str(eof_guard_path()),
            str(obsidian_mcp_entry()),
            target,
        ]

    def register_mcp(self) -> None:
        target = str(self.config.omi_dir)
        existing = self.registered_server()
        up_to_date = (
            existing is not None
            and self._server_path(existing) == target
            and self._is_direct_node(existing)
        )
        if up_to_date and not self.config.force:
            self.log(f"  MCP server '{self.config.server_name}' already points at {target}")
            return
        if existing is not None:
            form = "node" if self._is_direct_node(existing) else "legacy npx"
            self._record(
                f"remove existing MCP server '{self.config.server_name}' "
                f"({form}, was {self._server_path(existing)!r})"
            )
            self._run(["claude", "mcp", "remove", self.config.server_name, "-s", "user"])
        self._record(
            f"register MCP server '{self.config.server_name}' -> "
            f"node --require {eof_guard_path()} {obsidian_mcp_entry()} {target!r} (user scope)"
        )
        self._run(
            ["claude", "mcp", "add", "-s", "user", self.config.server_name, "--"]
            + self._server_command(target)
        )

    def _hook_command(self, event: str) -> str:
        """The shell command Claude Code runs for one hook event.

        Uses the absolute path to the ``omind`` executable when resolvable, so
        the hook fires even if the shell Claude Code spawns lacks ``~/.local/bin``
        on PATH. Falls back to bare ``omind`` (still contains ``HOOK_MARKER``).
        """
        omind_exe = shutil.which("omind") or "omind"
        # The "<exe> hook" prefix always contains HOOK_MARKER ("omind hook"),
        # which provision uses to find/replace omind's own entries.
        return (
            f'{omind_exe} hook {event} --vault "{self.config.vault}" '
            f"--folder {self.config.folder}"
        )

    def _omind_hook_entries(self) -> dict[str, list[dict[str, Any]]]:
        """The hooks-array entry omind owns, per handled event."""
        entries: dict[str, list[dict[str, Any]]] = {}
        for event in HANDLED_EVENTS:
            entry: dict[str, Any] = {
                "hooks": [{"type": "command", "command": self._hook_command(event)}]
            }
            if event == "PostToolUse":
                entry = {"matcher": "*", **entry}
            entries[event] = [entry]
        return entries

    def _read_settings(self, path: Path) -> dict[str, Any]:
        """Load settings.json as a dict; raise rather than clobber bad/foreign JSON."""
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProvisionError(
                f"{path} is not valid JSON ({exc}); refusing to overwrite. "
                "Fix or remove it and re-run."
            ) from exc
        if not isinstance(data, dict):
            raise ProvisionError(
                f"{path} does not contain a JSON object; refusing to overwrite."
            )
        return data

    def ensure_hooks_installed(self) -> None:
        """Idempotently merge omind's auto-memory hooks into settings.json.

        Replaces only omind's own entries (identified by ``HOOK_MARKER``), leaving
        user-authored hooks and every other settings key untouched. Re-registers
        when the embedded vault path drifts. Writes only when something changed
        (or ``--force``).
        """
        path = claude_settings_path()
        data = self._read_settings(path)
        hooks_cfg = data.get("hooks")
        if not isinstance(hooks_cfg, dict):
            hooks_cfg = {}
        desired = self._omind_hook_entries()

        changed = False
        for event in HANDLED_EVENTS:
            existing = hooks_cfg.get(event)
            existing_list = existing if isinstance(existing, list) else []
            kept = [e for e in existing_list if not _entry_has_omind_marker(e)]
            merged = kept + desired[event]
            if merged != existing_list:
                changed = True
            hooks_cfg[event] = merged

        if not changed and not self.config.force:
            self.log(f"  auto-memory hooks already installed in {path}")
            return

        data["hooks"] = hooks_cfg
        self._record(
            "install auto-memory hooks (PostToolUse, Stop, SessionStart) in "
            f"{path}"
        )
        if not self.config.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

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
        if os.name == "nt":
            # CreateProcess won't resolve npm.cmd / claude.cmd from a bare
            # name; shutil.which finds the shim with its extension.
            resolved = shutil.which(cmd[0])
            if resolved:
                cmd = [resolved, *cmd[1:]]
        try:
            return subprocess.run(
                cmd,
                check=check,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:  # claude / npm / node vanished mid-run
            raise ProvisionError(f"command not found: {cmd[0]}") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            raise ProvisionError(f"command failed: {' '.join(cmd)}\n{detail}") from exc

    # -- orchestration ------------------------------------------------------

    def integrate(self) -> None:
        """The agent-specific wiring; subclasses for other agents override this."""
        self.register_mcp()
        self.ensure_hooks_installed()

    def run(self) -> list[str]:
        self.log(f"omind setup -> {self.config.omi_dir}")
        self.check_prereqs()
        self.ensure_vault()
        self.ensure_obsidian_config()
        self.seed_memory_files()
        self.migrate_journal_notes()
        self.ensure_server_install()
        self.integrate()
        self.verify()
        if not self.config.dry_run:
            self.log(self.DONE_MESSAGE)
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


def _diagnose_tools(tools: dict[str, str]) -> list[CheckResult]:
    """One PATH check per required tool (tool -> why it is needed)."""
    results: list[CheckResult] = []
    for tool, why in tools.items():
        if shutil.which(tool) is not None:
            results.append(CheckResult(f"tool:{tool}", "ok", f"{tool} found on PATH"))
        else:
            results.append(CheckResult(f"tool:{tool}", "fail", f"{tool} not found — {why}"))
    return results


def _diagnose_omi_folder(config: SetupConfig) -> list[CheckResult]:
    """The agent-independent checks: OMI folder, Obsidian config, seed files."""
    results: list[CheckResult] = []
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
    return results


def _diagnose_eof_guard() -> CheckResult:
    guard = eof_guard_path()
    if guard.is_file():
        return CheckResult("eof_guard", "ok", f"stdin-EOF guard present: {guard}")
    return CheckResult(
        "eof_guard",
        "warn",
        f"missing stdin-EOF guard {guard} — run `omind setup` "
        "(the server may orphan when the agent exits)",
    )


def diagnose(config: SetupConfig) -> list[CheckResult]:
    """Inspect the wiring `omind setup` creates and report what's healthy.

    Pure inspection — touches nothing. ``fail`` means memory won't work until
    fixed; ``warn`` means it'll work but something is off or merely cosmetic.
    """
    results = _diagnose_tools(Provisioner.REQUIRED_TOOLS)
    results.extend(_diagnose_omi_folder(config))
    omi = config.omi_dir

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
        if path != target:
            results.append(
                CheckResult(
                    "mcp_registration",
                    "warn",
                    f"MCP server '{name}' points at {path!r}, expected {target!r}",
                )
            )
        elif not Provisioner._is_direct_node(server):
            results.append(
                CheckResult(
                    "mcp_registration",
                    "warn",
                    f"MCP server '{name}' uses the leak-prone npx command; "
                    "run `omind setup` to migrate it to the direct-node form",
                )
            )
        else:
            results.append(
                CheckResult("mcp_registration", "ok", f"MCP server '{name}' -> {target}")
            )

    results.append(_diagnose_eof_guard())

    results.append(_diagnose_hooks(claude_settings_path(), config))

    return results


def _diagnose_hooks(settings_path: Path, config: SetupConfig) -> CheckResult:
    """Inspect settings.json for omind's auto-memory hooks (pure read)."""
    if not settings_path.is_file():
        return CheckResult(
            "hooks",
            "fail",
            f"auto-memory hooks not installed: {settings_path} missing (run `omind setup`)",
        )
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return CheckResult("hooks", "fail", f"{settings_path} is not valid JSON")
    hooks_cfg = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks_cfg, dict):
        return CheckResult(
            "hooks", "fail", "no auto-memory hooks in settings.json (run `omind setup`)"
        )

    expected_vault = str(config.vault)
    missing: list[str] = []
    path_mismatch = False
    for event in HANDLED_EVENTS:
        entries = hooks_cfg.get(event)
        found = None
        if isinstance(entries, list):
            found = next((e for e in entries if _entry_has_omind_marker(e)), None)
        if found is None:
            missing.append(event)
        elif expected_vault not in _entry_command_text(found):
            path_mismatch = True

    if missing:
        return CheckResult(
            "hooks",
            "fail",
            f"auto-memory hook(s) missing for {', '.join(missing)} (run `omind setup`)",
        )
    if path_mismatch:
        return CheckResult(
            "hooks",
            "warn",
            f"auto-memory hooks point at a different vault than {expected_vault!r}; "
            "run `omind setup`",
        )
    return CheckResult(
        "hooks", "ok", "auto-memory hooks installed (PostToolUse, Stop, SessionStart)"
    )


def run_doctor(
    config: SetupConfig,
    log: Logger = print,
    diagnose_fn: Callable[[SetupConfig], list[CheckResult]] = diagnose,
) -> int:
    """Print the diagnostic checklist; return an exit code (0 = healthy)."""
    log(f"omind doctor -> {config.omi_dir}")
    symbols = {"ok": "✓", "warn": "!", "fail": "✗"}
    results = diagnose_fn(config)
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
