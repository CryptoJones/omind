# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Idempotently wire up the OMI memory integration for Claude Code.

`omind setup` reproduces, on any machine, the manual steps that register
omind's own node MCP server (`omind node`, see docs/mesh.md) with the Claude
Code CLI at user scope, scaffold the OMI folder, initialize it as a mesh
node, and install the `omind` skill (which teaches Claude the memory workflow
and how to drive the CLI — complementing the MCP server's tools). Every step
is safe to re-run: user files are never clobbered, the MCP server is only
(re)registered when its command differs, and omind-managed files (hook
scripts, the skill) are refreshed only when their content drifts.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.resources
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, TextIO

from omind import __version__, guard, paths, policy, seeds
from omind.hooks import HANDLED_EVENTS, HOOK_MARKER, JOURNAL_DIRNAME
from omind.hooks import failure_log_path as hook_failure_log_path
from omind.journal import find_stray_journals, migrate_journals
from omind.proc import run_command

#: Identifies enforce-hook commands inside hook entries.
ENFORCE_HOOK_MARKER = "omi-enforce.py"


def _enforce_hook_dest() -> Path:
    """Where omind writes the enforcement hook script on this machine."""
    return Path.home() / ".claude" / "hooks" / "omi-enforce.py"


def _guard_hook_dest() -> Path:
    """Where omind writes the fresh-base git guard hook script on this machine."""
    return Path.home() / ".claude" / "hooks" / "git-fresh-base.sh"


def _omi_guard_dest() -> Path:
    """Where omind writes the OMI-compliance guard adapter on this machine."""
    return Path.home() / ".claude" / "hooks" / "omi-guard.sh"


def _omi_gate_reset_dest() -> Path:
    """Where omind writes the OMI gate-reset adapter on this machine."""
    return Path.home() / ".claude" / "hooks" / "omi-gate-reset.sh"


def _fleet_sudo_dest() -> Path:
    """The `fleet-sudo` wrapper install path — on PATH beside the omind binary."""
    return Path.home() / ".local" / "bin" / "fleet-sudo"


def _legacy_omi_guard_dest() -> Path:
    """The retired hand-rolled guard adapter (pre-omind-core prototype). Provision
    strips it from settings.json and deletes this file so a machine that ran the
    prototype converges onto the shipped ``omi-guard.sh``."""
    return Path.home() / ".claude" / "hooks" / "omi-git-guard.sh"


def _managed_guard_hooks() -> dict[str, Path]:
    """package-data resource name -> install destination for the OMI-compliance
    guard hook-set that issues #86/#87 track."""
    return {
        "omi-guard.sh": _omi_guard_dest(),
        "omi-gate-reset.sh": _omi_gate_reset_dest(),
    }


def _shipped_hook_sha(resource: str) -> str | None:
    """sha256 of a managed hook's *package-data* (pre-substitution) content, so
    two machines on the same omind version share the same sha."""
    try:
        content = (
            importlib.resources.files("omind").joinpath(resource).read_text(encoding="utf-8")
        )
    except (OSError, ModuleNotFoundError):
        return None
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _shipped_hook_shas() -> dict[str, str]:
    shas: dict[str, str] = {}
    for resource in _managed_guard_hooks():
        sha = _shipped_hook_sha(resource)
        if sha is not None:
            shas[resource] = sha
    return shas


def _provision_manifest_path() -> Path:
    """Stamp of the installed guard hook-set (omind version + shipped hook shas),
    beside the hooks. Lets an upgrade (#87) and ``omind doctor`` (#86) tell a
    current install from a stale one."""
    return Path.home() / ".claude" / "hooks" / ".omind-provision.json"


def write_provision_manifest() -> None:
    """Record that the guard hook-set shipped by *this* omind is now installed."""
    payload = {"omind_version": __version__, "hooks": _shipped_hook_shas()}
    path = _provision_manifest_path()
    with contextlib.suppress(OSError):
        _guard_test_isolation(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_provision_manifest() -> dict[str, Any]:
    try:
        data = json.loads(_provision_manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def hookset_drift() -> str | None:
    """Reason the installed guard hook-set is stale vs what this omind ships, or
    ``None`` when current. Cheap and offline: compares the provision manifest to
    the running binary's version + shipped hook shas. Drives #87 self-heal and the
    #86 doctor check."""
    manifest = read_provision_manifest()
    if not manifest:
        return "guard hook-set never stamped by this omind (no provision manifest)"
    recorded_version = str(manifest.get("omind_version") or "")
    if recorded_version != __version__:
        return f"omind {recorded_version or '?'} -> {__version__}: hook-set may be stale"
    recorded = manifest.get("hooks")
    recorded = recorded if isinstance(recorded, dict) else {}
    for name, sha in _shipped_hook_shas().items():
        if recorded.get(name) != sha:
            return f"hook {name} differs from the shipped version"
    return None


def claude_skill_dir() -> Path:
    """Directory holding omind's Claude Code skill (honors ``CLAUDE_CONFIG_DIR``).

    Claude Code discovers user-scope skills under ``~/.claude/skills/`` — or
    ``$CLAUDE_CONFIG_DIR/skills/`` when the env var relocates the config dir,
    matching :func:`claude_settings_path`.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config_dir) if config_dir else Path.home() / ".claude"
    return base / "skills" / "omind"


Logger = Callable[[str], None]


class ProvisionError(Exception):
    """A setup precondition failed (missing tool, bad vault layout, ...)."""


def _guard_test_isolation(target: Path) -> None:
    """Belt-and-suspenders against the test-isolation footgun (2.40.1).

    Under pytest, refuse to write a managed config/hook file outside the temp
    dir, so a test that forgot to isolate ``HOME``/``CLAUDE_CONFIG_DIR`` fails
    LOUDLY instead of silently clobbering the developer's real ``~/.claude`` —
    which twice rewrote this machine's live ``omi-guard.sh`` to a pytest temp
    path and wedged the consult gate. No-op outside a pytest run."""
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return
    tmp = Path(tempfile.gettempdir()).resolve()
    try:
        resolved = target.expanduser().resolve()
    except OSError:
        return
    if resolved != tmp and tmp not in resolved.parents:
        raise ProvisionError(
            f"omind refused to write {target} outside {tmp} during a test — "
            "isolate HOME and CLAUDE_CONFIG_DIR in the test fixture (2.40.1 guard)"
        )


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


# A hook command we own: the omind executable (Windows resolves it to
# omind.EXE / omind.cmd, so the literal HOOK_MARKER substring isn't enough)
# followed by the `hook` subcommand.
_HOOK_COMMAND_RE = re.compile(r"omind(?:\.exe|\.cmd|\.bat)?[\"']?\s+hook\b", re.IGNORECASE)


def _command_is_omind_hook(command: str) -> bool:
    return HOOK_MARKER in command or bool(_HOOK_COMMAND_RE.search(command))


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
            if isinstance(command, str) and _command_is_omind_hook(command):
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


#: The retired 1.x server registration (obsidian-mcp); setup removes it.
LEGACY_SERVER_NAME = "obsidian"

#: Marker identifying omind's PreToolUse(Bash) fresh-base guard entry in
#: settings.json — the guard script's filename always appears in its command.
GUARD_HOOK_MARKER = "git-fresh-base.sh"
#: Seconds Claude Code waits for the guard (it runs `git fetch` before deciding).
GUARD_HOOK_TIMEOUT = 20

#: Markers identifying omind's OMI-compliance guard entries in settings.json
#: (each adapter's filename always appears in its command).
OMI_GUARD_MARKER = "omi-guard.sh"
OMI_GATE_RESET_MARKER = "omi-gate-reset.sh"
#: The retired hand-rolled PreToolUse('*') guard, replaced by ``omi-guard.sh``.
#: Provision strips it from settings.json so a prototype machine doesn't run two
#: guards. (Its name is NOT a substring of OMI_GUARD_MARKER, so the two markers
#: match independently.)
LEGACY_OMI_GUARD_MARKER = "omi-git-guard.sh"
#: Seconds Claude Code waits for the OMI guard adapter (may shell out to
#: `omind guard check` for a Bash command).
OMI_GUARD_TIMEOUT = 15


@dataclass
class SetupConfig:
    vault: Path
    folder: str = "OMI"
    server_name: str = "omi"
    dry_run: bool = False
    force: bool = False
    agent: str = "claude"
    no_mesh: bool = False

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
        "claude": "the Claude Code CLI registers the MCP server",
        "git": "the mesh replicates the memory folder over git",
    }
    DONE_MESSAGE: ClassVar[str] = (
        "Done. Restart Claude Code to load the OMI memory tools. To replicate "
        "to other machines: `omind mesh add-peer`, then `omind mesh install-service`."
    )

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
            _guard_test_isolation(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def _write_managed(self, path: Path, content: str) -> None:
        """Write a Managed-by-omind file, refreshing it whenever its content drifts.

        Unlike user-owned seeds (``_write_if_absent``), managed files carry
        omind's own code; leaving stale copies in place means existing installs
        never receive fixes (issue #49 shipped a guard fix this way).
        """
        if path.exists():
            try:
                current: str | None = path.read_text(encoding="utf-8")
            except OSError:
                current = None
            if current == content:
                self.log(f"  up to date: {path}")
                return
            self._record(f"update {path}")
        else:
            self._record(f"write {path}")
        if not self.config.dry_run:
            _guard_test_isolation(path)
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
                f"{obsidian_dir} exists but is not a directory; the folder needs a "
                ".obsidian/ config to open directly as an Obsidian vault. Move or "
                "remove that file and re-run."
            )
        self._record(f"ensure {obsidian_dir}/ with app.json, core-plugins.json, appearance.json")
        if not self.config.dry_run:
            obsidian_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in seeds.OBSIDIAN_CONFIG_FILES.items():
            self._write_if_absent(obsidian_dir / filename, content)

    def seed_memory_files(self) -> None:
        self._write_if_absent(
            self.config.omi_dir / paths.MEMORY_TEMPLATE_FILENAME,
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
        self._write_if_absent(self.config.omi_dir / paths.INDEX_FILENAME, index_seed)

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

    def ensure_mesh(self) -> None:
        """Initialize the folder as a mesh node (git repo, merge driver, identity).

        Optional via ``--no-mesh``; idempotent like everything else here. The
        replication daemon is NOT started as a side effect — that is the
        explicit `omind mesh install-service` step.
        """
        if self.config.no_mesh:
            self.log("  --no-mesh: leaving the folder un-replicated")
            return
        self._record(f"initialize mesh node in {self.config.omi_dir}")
        if not self.config.dry_run:
            from omind.mesh import mesh_init

            mesh_init(self.config.omi_dir, log=lambda m: self.log(f"  {m}"))

    def registered_server(self) -> dict[str, object] | None:
        server = _read_mcp_servers().get(self.config.server_name)
        return server if isinstance(server, dict) else None

    def _server_command(self) -> list[str]:
        """The `omind node` invocation the agent runs as its MCP server.

        Absolute omind path when resolvable: the agent's spawn environment may
        lack ~/.local/bin on PATH.
        """
        omind_exe = shutil.which("omind") or "omind"
        return [
            omind_exe,
            "node",
            "--vault", str(self.config.vault),
            "--folder", self.config.folder,
        ]

    def desired_server_entry(self) -> dict[str, Any]:
        """The stdio MCP server entry, in the shared command/args shape."""
        command = self._server_command()
        return {"command": command[0], "args": command[1:]}

    def _matches_desired(self, server: dict[str, object]) -> bool:
        desired = self.desired_server_entry()
        return server.get("command") == desired["command"] and server.get("args") == list(
            desired["args"]
        )

    def _legacy_server(self) -> dict[str, object] | None:
        """The retired obsidian-mcp registration, when still present."""
        if self.config.server_name == LEGACY_SERVER_NAME:
            return None
        entry = _read_mcp_servers().get(LEGACY_SERVER_NAME)
        if isinstance(entry, dict) and "obsidian-mcp" in json.dumps(entry):
            return entry
        return None

    def retire_legacy_server(self) -> None:
        """Remove the 1.x obsidian-mcp registration the node server replaces."""
        if self._legacy_server() is None:
            return
        self._record(
            f"remove retired MCP server '{LEGACY_SERVER_NAME}' (obsidian-mcp, "
            "replaced by `omind node`)"
        )
        self._run(["claude", "mcp", "remove", LEGACY_SERVER_NAME, "-s", "user"])

    def register_mcp(self) -> None:
        existing = self.registered_server()
        if existing is not None and self._matches_desired(existing) and not self.config.force:
            self.log(
                f"  MCP server '{self.config.server_name}' already runs "
                f"`omind node` for {self.config.omi_dir}"
            )
            return
        if existing is not None:
            self._record(
                f"remove existing MCP server '{self.config.server_name}' "
                "(command differs from the omind node form)"
            )
            self._run(["claude", "mcp", "remove", self.config.server_name, "-s", "user"])
        self._record(
            f"register MCP server '{self.config.server_name}' -> "
            f"{' '.join(self._server_command())} (user scope)"
        )
        self._run(
            ["claude", "mcp", "add", "-s", "user", self.config.server_name, "--"]
            + self._server_command()
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
        # Both values are quoted: the hook string goes through a shell, and an
        # unquoted folder like "My Memory" word-splits into a stray positional.
        return (
            f'{omind_exe} hook {event} --vault "{self.config.vault}" '
            f'--folder "{self.config.folder}"'
        )

    def _omind_hook_entries(self) -> dict[str, list[dict[str, Any]]]:
        """The hooks-array entry omind owns, per handled event."""
        entries: dict[str, list[dict[str, Any]]] = {}
        for event in HANDLED_EVENTS:
            hooks_list: list[dict[str, Any]] = [
                {"type": "command", "command": self._hook_command(event)}
            ]
            if event == "PostToolUse":
                # Enforcement hook runs immediately after the omind journal hook so
                # any built-in memory file written this turn is migrated to OMI
                # before the file is deleted — guaranteeing no data loss.
                hooks_list.append({
                    "type": "command",
                    "command": f"python3 {_enforce_hook_dest()}",
                })
                entry: dict[str, Any] = {"matcher": "*", "hooks": hooks_list}
            else:
                entry = {"hooks": hooks_list}
            entries[event] = [entry]
        return entries

    def _write_enforce_hook_script(self) -> None:
        """Write the enforcement hook script from package data to ~/.claude/hooks/."""
        dest = _enforce_hook_dest()
        try:
            content = (
                importlib.resources.files("omind")
                .joinpath("_omi_enforce.py")
                .read_text(encoding="utf-8")
            )
        except Exception as exc:
            self.log(f"  WARNING: could not read enforcement hook from package data: {exc}")
            return
        self._write_managed(dest, content)

    def _write_guard_hook_script(self) -> None:
        """Write the fresh-base git guard hook from package data to ~/.claude/hooks/."""
        dest = _guard_hook_dest()
        try:
            content = (
                importlib.resources.files("omind")
                .joinpath("git-fresh-base.sh")
                .read_text(encoding="utf-8")
            )
        except Exception as exc:
            self.log(f"  WARNING: could not read git guard hook from package data: {exc}")
            return
        self._write_managed(dest, content)
        if not self.config.dry_run:
            with contextlib.suppress(OSError):
                dest.chmod(0o755)

    def _write_fleet_sudo_script(self) -> None:
        """Install the `fleet-sudo` wrapper from package data to ~/.local/bin/.

        Agents run `fleet-sudo <cmd>` instead of raw sudo: it reads the fleet sudo
        password from pass (resolving the per-host entry itself), so no instance
        ever guesses the entry or hands CJ a command to run. The guard blocks raw
        `sudo` and points here; see the OMI Playbook.
        """
        dest = _fleet_sudo_dest()
        try:
            content = (
                importlib.resources.files("omind")
                .joinpath("fleet-sudo.sh")
                .read_text(encoding="utf-8")
            )
        except Exception as exc:
            self.log(f"  WARNING: could not read fleet-sudo from package data: {exc}")
            return
        if not self.config.dry_run:
            with contextlib.suppress(OSError):
                dest.parent.mkdir(parents=True, exist_ok=True)
        self._write_managed(dest, content)
        if not self.config.dry_run:
            with contextlib.suppress(OSError):
                dest.chmod(0o755)

    def install_claude_skill(self) -> None:
        """Install omind's Claude Code skill (OMI memory workflow + CLI ops).

        The MCP server gives Claude the memory *tools*; this skill teaches it the
        *procedure* — search-before-save, the single-writer ``omind note`` path,
        and managing the omind CLI. Managed like the hook scripts (not
        write-if-absent) so existing installs pick up edits to omind's guidance.
        """
        skill = claude_skill_dir() / paths.AGENT_SKILL_FILENAME
        content = seeds.CLAUDE_SKILL_TEMPLATE.format(
            vault=self.config.vault,
            folder=self.config.folder,
            omi_dir=self.config.omi_dir,
        )
        self._write_managed(skill, content)

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
            _guard_test_isolation(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def ensure_guard_hook_installed(self) -> None:
        """Idempotently register the PreToolUse(Bash) fresh-base git guard.

        Identifies omind's guard entry by ``GUARD_HOOK_MARKER`` (the script's
        filename in the command), so a user's own PreToolUse Bash hooks are kept
        and re-runs never accumulate duplicates. Writes only when something
        changed (or ``--force``).
        """
        path = claude_settings_path()
        data = self._read_settings(path)
        hooks_cfg = data.get("hooks")
        if not isinstance(hooks_cfg, dict):
            hooks_cfg = {}

        desired: dict[str, Any] = {
            "matcher": "Bash",
            "hooks": [
                {
                    "type": "command",
                    "command": str(_guard_hook_dest()),
                    "timeout": GUARD_HOOK_TIMEOUT,
                }
            ],
        }
        existing = hooks_cfg.get("PreToolUse")
        existing_list = existing if isinstance(existing, list) else []
        kept = [e for e in existing_list if GUARD_HOOK_MARKER not in _entry_command_text(e)]
        merged = kept + [desired]

        if merged == existing_list and not self.config.force:
            self.log(f"  fresh-base git guard hook already installed in {path}")
            return

        hooks_cfg["PreToolUse"] = merged
        data["hooks"] = hooks_cfg
        self._record(f"install PreToolUse(Bash) fresh-base git guard in {path}")
        if not self.config.dry_run:
            _guard_test_isolation(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def _remove_legacy_omi_guard(self) -> None:
        """Delete the retired hand-rolled ``omi-git-guard.sh`` prototype if present,
        so a machine that ran it converges onto the shipped ``omi-guard.sh``."""
        dest = _legacy_omi_guard_dest()
        if not dest.exists():
            return
        self._record(f"remove legacy guard adapter {dest}")
        if not self.config.dry_run:
            with contextlib.suppress(OSError):
                dest.unlink()

    def _write_omi_guard_scripts(self) -> None:
        """Write the OMI-compliance guard + gate-reset adapters from package data,
        substituting the omind binary path and this machine's OMI folder. Also
        retires the legacy prototype adapter and stamps the provision manifest so
        upgrades can detect hook-set drift (#86/#87)."""
        self._remove_legacy_omi_guard()
        omind_exe = shutil.which("omind") or "omind"
        omi_dir = str(self.config.omi_dir)
        for resource, dest in (
            ("omi-guard.sh", _omi_guard_dest()),
            ("omi-gate-reset.sh", _omi_gate_reset_dest()),
        ):
            try:
                content = (
                    importlib.resources.files("omind")
                    .joinpath(resource)
                    .read_text(encoding="utf-8")
                )
            except Exception as exc:
                self.log(f"  WARNING: could not read {resource} from package data: {exc}")
                continue
            content = content.replace("__OMIND_BIN__", omind_exe).replace(
                "__OMI_DIR__", omi_dir
            )
            self._write_managed(dest, content)
            if not self.config.dry_run:
                with contextlib.suppress(OSError):
                    dest.chmod(0o755)
        # Stamp what we just installed so #87 self-heal / #86 doctor can detect a
        # later binary shipping a newer hook-set. Silent (not a recorded action),
        # so re-stamping after a no-op version bump doesn't look like a change.
        if not self.config.dry_run:
            write_provision_manifest()
            # Scaffold the SEED ruleset for inspection (the guard reads the seed
            # from code, so this file is informational, not load-bearing).
            policy.write_seed_policy()

    def ensure_omi_guard_installed(self) -> None:
        """Idempotently register the OMI-compliance guard: a PreToolUse('*')
        adapter and a UserPromptSubmit gate-reset, plus an allow-list for OMI
        reads so the gate's clear-path can never be permission-denied. omind's
        entries are found by the adapter filename in the command, so a user's
        own hooks are preserved and re-runs never duplicate.
        """
        path = claude_settings_path()
        data = self._read_settings(path)
        hooks_cfg = data.get("hooks")
        if not isinstance(hooks_cfg, dict):
            hooks_cfg = {}

        desired: dict[str, dict[str, Any]] = {
            "PreToolUse": {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": str(_omi_guard_dest()),
                        "timeout": OMI_GUARD_TIMEOUT,
                    }
                ],
            },
            "UserPromptSubmit": {
                "hooks": [{"type": "command", "command": str(_omi_gate_reset_dest())}]
            },
        }
        strip_markers = {
            "PreToolUse": (OMI_GUARD_MARKER, LEGACY_OMI_GUARD_MARKER),
            "UserPromptSubmit": (OMI_GATE_RESET_MARKER,),
        }

        changed = False
        for event, entry in desired.items():
            existing = hooks_cfg.get(event)
            existing_list = existing if isinstance(existing, list) else []
            kept = [
                e
                for e in existing_list
                if not any(m in _entry_command_text(e) for m in strip_markers[event])
            ]
            merged = kept + [entry]
            if merged != existing_list:
                changed = True
            hooks_cfg[event] = merged

        perms = data.get("permissions")
        if not isinstance(perms, dict):
            perms = {}
        allow = perms.get("allow")
        allow_list = list(allow) if isinstance(allow, list) else []
        # Prune stale Read(...) allow-rules pointing under the temp dir — but NOT
        # this vault's own rule (a test vault legitimately lives under the temp
        # dir). These accumulate when a mis-isolated test run provisions against a
        # pytest vault (the 2.40.1 footgun left 3 such rules in this machine's
        # live settings.json); a real OMI vault never lives under the temp dir, so
        # this only removes litter, and a plain `omind setup` self-cleans.
        desired_read = f"Read({self.config.omi_dir}/**)"
        tmp_prefix = f"Read({Path(tempfile.gettempdir())}/"
        deparented = [
            r
            for r in allow_list
            if r == desired_read or not (isinstance(r, str) and r.startswith(tmp_prefix))
        ]
        if deparented != allow_list:
            allow_list = deparented
            changed = True
        for rule in (
            f"Read({self.config.omi_dir}/**)",
            "mcp__omi__read-note",
            "mcp__omi__search-vault",
            "mcp__omi__list-notes",
        ):
            if rule not in allow_list:
                allow_list.append(rule)
                changed = True
        perms["allow"] = allow_list

        if not changed and not self.config.force:
            self.log(f"  OMI-compliance guard already installed in {path}")
            return

        data["hooks"] = hooks_cfg
        data["permissions"] = perms
        self._record(
            f"install OMI-compliance guard (PreToolUse '*' + UserPromptSubmit) in {path}"
        )
        if not self.config.dry_run:
            _guard_test_isolation(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def verify(self) -> None:
        if self.config.dry_run:
            return
        result = self._run(
            ["claude", "mcp", "get", self.config.server_name],
            check=False,
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
    ) -> subprocess.CompletedProcess[str]:
        if self.config.dry_run:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return run_command(cmd, error=ProvisionError, check=check)

    # -- orchestration ------------------------------------------------------

    def integrate(self) -> None:
        """The agent-specific wiring; subclasses for other agents override this."""
        self.retire_legacy_server()
        self.register_mcp()
        self._write_enforce_hook_script()
        self._write_guard_hook_script()
        self._write_fleet_sudo_script()
        self.ensure_hooks_installed()
        self.ensure_guard_hook_installed()
        self._write_omi_guard_scripts()
        self.ensure_omi_guard_installed()
        self.install_claude_skill()

    def run(self) -> list[str]:
        self.log(f"omind setup -> {self.config.omi_dir}")
        self.check_prereqs()
        self.ensure_vault()
        self.ensure_obsidian_config()
        self.seed_memory_files()
        self.migrate_journal_notes()
        self.ensure_mesh()
        self.integrate()
        self.verify()
        if not self.config.dry_run:
            self.log(self.DONE_MESSAGE)
        return self.actions


def heal_omi_guard(
    vault: Path | None = None,
    folder: str = "OMI",
    *,
    log: Logger = lambda _msg: None,
) -> bool:
    """Idempotently (re)install the OMI-compliance guard hook-set and restamp the
    provision manifest. Returns ``True`` if anything actually changed. Shared by
    #87 startup self-heal and ``omind setup``; preserves the user's own hooks."""
    config = SetupConfig(vault=vault or default_vault_path(), folder=folder)
    prov = Provisioner(config=config, log=log)
    prov._write_omi_guard_scripts()
    prov.ensure_omi_guard_installed()
    return bool(prov.actions)


#: Set to disable the on-startup self-heal (for users who manage hooks by hand).
_AUTOHEAL_DISABLE_ENV = "OMIND_NO_AUTOHEAL"


def autoheal_on_startup(vault: Path, folder: str = "OMI", *, out: TextIO | None = None) -> None:
    """#87: when ``omind node`` starts on a newer binary than the installed guard
    hook-set, idempotently re-provision it so a machine is never silently left
    unprotected between ``omind setup`` runs. Opt out with ``OMIND_NO_AUTOHEAL=1``.

    Fully fail-open: the MCP server start must never break, and it only ever writes
    to stderr — stdout is the MCP protocol channel."""
    stream = out if out is not None else sys.stderr
    try:
        if os.environ.get(_AUTOHEAL_DISABLE_ENV):
            return
        reason = hookset_drift()
        if reason is None:
            return
        if heal_omi_guard(vault=vault, folder=folder):
            print(f"omind: healed OMI-guard hook drift ({reason}).", file=stream)
    except Exception:
        return


def _read_mcp_servers() -> dict[str, Any]:
    """The `mcpServers` mapping from ~/.claude.json, or {} on any miss.

    The one shared reader: doctor (via registered_server) and the legacy
    retirement path must always agree on what is registered.
    """
    path = claude_config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    servers = data.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


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
                "warn",
                f"missing {app_json} — the folder won't open directly as an "
                "Obsidian vault (run `omind setup`)",
            )
        )

    missing_seeds = [
        name
        for name in (paths.MEMORY_TEMPLATE_FILENAME, paths.INDEX_FILENAME)
        if not (omi / name).is_file()
    ]
    if missing_seeds:
        results.append(
            CheckResult("seeds", "warn", f"missing seed file(s): {', '.join(missing_seeds)}")
        )
    else:
        results.append(CheckResult("seeds", "ok", "seed files present (template + index)"))
    return results


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
    if server is None:
        results.append(
            CheckResult(
                "mcp_registration",
                "fail",
                f"MCP server '{name}' not registered at user scope (run `omind setup`)",
            )
        )
    elif not prov._matches_desired(server):
        results.append(
            CheckResult(
                "mcp_registration",
                "warn",
                f"MCP server '{name}' differs from the expected "
                f"`omind node` command (run `omind setup`)",
            )
        )
    else:
        results.append(
            CheckResult("mcp_registration", "ok", f"MCP server '{name}' -> {omi}")
        )

    if prov._legacy_server() is not None:
        results.append(
            CheckResult(
                "legacy_server",
                "warn",
                f"retired '{LEGACY_SERVER_NAME}' (obsidian-mcp) registration still "
                "present — run `omind setup` to remove it",
            )
        )

    results.append(_diagnose_hooks(claude_settings_path(), config))

    results.append(_diagnose_omi_guard(claude_settings_path(), config))

    results.extend(_diagnose_enforcement())

    results.append(_diagnose_claude_skill())

    results.append(_diagnose_hook_failures())

    return results


def _diagnose_claude_skill() -> CheckResult:
    """Inspect whether omind's Claude Code skill is installed (pure read)."""
    skill = claude_skill_dir() / paths.AGENT_SKILL_FILENAME
    if skill.is_file():
        return CheckResult("claude_skill", "ok", f"omind skill installed: {skill}")
    return CheckResult(
        "claude_skill", "warn", f"omind skill missing: {skill} (run `omind setup`)"
    )


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
    # Check the enforcement hook is present and the script exists on disk.
    enforce_dest = _enforce_hook_dest()
    post_entries = hooks_cfg.get("PostToolUse")
    enforce_wired = False
    if isinstance(post_entries, list):
        for e in post_entries:
            if _entry_has_omind_marker(e):
                cmd_text = _entry_command_text(e)
                if ENFORCE_HOOK_MARKER in cmd_text:
                    enforce_wired = True
                    break
    if not enforce_wired:
        return CheckResult(
            "hooks",
            "warn",
            "enforcement hook not in PostToolUse (run `omind setup`)",
        )
    if not enforce_dest.is_file():
        return CheckResult(
            "hooks",
            "warn",
            f"enforcement hook script missing at {enforce_dest} (run `omind setup`)",
        )
    return CheckResult(
        "hooks", "ok",
        "auto-memory hooks installed (PostToolUse, Stop, SessionStart) + enforcement hook"
    )


def _diagnose_omi_guard(settings_path: Path, config: SetupConfig) -> CheckResult:
    """#86: verify the OMI-compliance guard block-path is actually wired — not just
    the auto-memory hooks. A green here must mean the per-turn consult gate and the
    hard blocks really run, so a missing/unwired guard is a ``fail``, not a silent
    pass."""
    missing = [str(p) for p in _managed_guard_hooks().values() if not p.is_file()]
    if missing:
        return CheckResult(
            "omi_guard",
            "fail",
            f"OMI-compliance guard adapter(s) missing: {', '.join(missing)} — the "
            "consult gate + hard blocks are NOT enforced (run `omind setup`)",
        )
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return CheckResult(
            "omi_guard",
            "fail",
            f"OMI-compliance guard not verifiable: cannot read {settings_path} "
            "(run `omind setup`)",
        )
    hooks_cfg = data.get("hooks") if isinstance(data, dict) else None
    hooks_cfg = hooks_cfg if isinstance(hooks_cfg, dict) else {}
    pre_list = hooks_cfg.get("PreToolUse")
    pre_list = pre_list if isinstance(pre_list, list) else []
    pre_ok = any(
        isinstance(e, dict)
        and e.get("matcher") == "*"
        and OMI_GUARD_MARKER in _entry_command_text(e)
        for e in pre_list
    )
    ups_list = hooks_cfg.get("UserPromptSubmit")
    ups_list = ups_list if isinstance(ups_list, list) else []
    ups_ok = any(OMI_GATE_RESET_MARKER in _entry_command_text(e) for e in ups_list)
    if not (pre_ok and ups_ok):
        unwired: list[str] = []
        if not pre_ok:
            unwired.append("PreToolUse '*' guard")
        if not ups_ok:
            unwired.append("UserPromptSubmit gate-reset")
        return CheckResult(
            "omi_guard",
            "fail",
            f"OMI-compliance guard not wired in settings.json ({', '.join(unwired)}) "
            "— the per-turn consult gate is OFF (run `omind setup`)",
        )
    # Live block-path smoke test: an unconsulted, non-OMI action must be denied.
    if guard.decide({"command": "ls", "session": "__omind_doctor__"}).allow:
        return CheckResult(
            "omi_guard",
            "fail",
            "OMI-compliance guard policy engine allowed an unconsulted action — "
            "the block-path is broken",
        )
    if any(LEGACY_OMI_GUARD_MARKER in _entry_command_text(e) for e in pre_list):
        return CheckResult(
            "omi_guard",
            "warn",
            "legacy hand-rolled omi-git-guard.sh is still registered alongside the "
            "shipped guard — run `omind setup` to migrate",
        )
    drift = hookset_drift()
    if drift:
        return CheckResult(
            "omi_guard",
            "warn",
            f"OMI-compliance guard installed but stale ({drift}) — run `omind setup`",
        )
    return CheckResult(
        "omi_guard",
        "ok",
        "OMI-compliance guard wired (PreToolUse '*' + UserPromptSubmit gate-reset); "
        "block-path live",
    )


def _diagnose_enforcement() -> list[CheckResult]:
    """Report the data-driven enforcement state: policy size, the compliance log
    rollup, and whether the verifier's model backend is available.

    The verifier check is a ``warn`` (not ``fail``) when ``claude`` is absent: the
    verifier fails open to its deterministic prefilter, so it still works — just
    without the model tiebreaker for the ambiguous middle band."""
    from omind import compliance, policy

    results: list[CheckResult] = []

    learned = policy.load_learned()
    results.append(
        CheckResult(
            "policy",
            "ok",
            f"policy: {len(policy.SEED_RULES)} seed + {len(learned)} learned rule(s)",
        )
    )

    summary = compliance.summary()
    if summary["total"]:
        top = ", ".join(f"{rid}×{n}" for rid, n in summary["top_rules"][:3]) or "none"
        results.append(
            CheckResult(
                "compliance_log",
                "ok",
                f"compliance log: {summary['total']} event(s), {summary['denies']} deny, "
                f"{summary['violations']} violation(s); top: {top}",
            )
        )
    else:
        results.append(
            CheckResult("compliance_log", "ok", "compliance log: no violations recorded yet")
        )

    if shutil.which("claude"):
        results.append(
            CheckResult("verifier_backend", "ok", "verifier model backend: `claude` on PATH")
        )
    else:
        results.append(
            CheckResult(
                "verifier_backend",
                "warn",
                "verifier model backend: `claude` not on PATH — the relevance "
                "verifier runs deterministic-only (fails open, no model tiebreaker)",
            )
        )
    return results


#: A failure-log entry younger than this many days makes doctor warn.
_HOOK_FAILURE_FRESH_DAYS = 7


def _diagnose_hook_failures() -> CheckResult:
    """Surface the hooks' swallowed-error breadcrumbs (pure read).

    The hook handlers must never fail the agent, so they swallow errors into
    :func:`omind.hooks.failure_log_path`; doctor is where that becomes visible.
    """
    path = hook_failure_log_path()
    try:
        stat = path.stat()
    except OSError:
        return CheckResult("hook_failures", "ok", "no recorded hook failures")
    if stat.st_size == 0:
        return CheckResult("hook_failures", "ok", "no recorded hook failures")
    age_days = (time.time() - stat.st_mtime) / 86400
    if age_days > _HOOK_FAILURE_FRESH_DAYS:
        return CheckResult(
            "hook_failures",
            "ok",
            f"hook failures recorded, but none in the last "
            f"{_HOOK_FAILURE_FRESH_DAYS} days ({path})",
        )
    return CheckResult(
        "hook_failures",
        "warn",
        f"hook failure(s) recorded in the last {_HOOK_FAILURE_FRESH_DAYS} days — "
        f"journaling may be silently failing; see {path}",
    )


def _doctor_symbols() -> dict[str, str]:
    """Check-line markers, degraded to ASCII when stdout can't encode them.

    Windows consoles often report cp1252, which has no ✓/✗ — printing them
    would crash doctor on the exact machine being diagnosed.
    """
    symbols = {"ok": "✓", "warn": "!", "fail": "✗"}
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "✓✗".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return {"ok": "+", "warn": "!", "fail": "x"}
    return symbols


def run_doctor(
    config: SetupConfig,
    log: Logger = print,
    diagnose_fn: Callable[[SetupConfig], list[CheckResult]] = diagnose,
) -> int:
    """Print the diagnostic checklist; return an exit code (0 = healthy)."""
    log(f"omind doctor -> {config.omi_dir}")
    symbols = _doctor_symbols()
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
