#!/usr/bin/env python3
"""
omi-enforce.py — PostToolUse hook: prevent built-in Claude memory from persisting.

For each .md file found in ~/.claude/projects/*/memory/:
  1. Parse name + description from YAML frontmatter
  2. Check if a matching note already exists in the OMI vault (by filename)
  3. If NOT found → migrate via `omind note` first
  4. Delete the Claude memory file either way
"""
# Lazy annotations so the builtin-generic hints (dict[str, str]) don't need
# evaluation at import — this ships as a hook run under the host's system
# python3, whose version `requires-python` does not govern.
from __future__ import annotations

import contextlib
import glob
import pathlib
import re
import subprocess

HOME = pathlib.Path.home()
OMIND = HOME / ".local/bin/omind"
VAULT = HOME / "Documents/Obsidian Vault"
OMI_DIR = VAULT / "OMI"
CLAUDE_PROJECTS = HOME / ".claude/projects"


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (fields_dict, body_text). Handles simple key: value and nested metadata.type."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fm_raw, body = parts[1], parts[2].strip()
    fields: dict[str, str] = {}
    in_metadata = False
    for line in fm_raw.splitlines():
        if line.strip() == "metadata:":
            in_metadata = True
            continue
        if in_metadata:
            m = re.match(r"\s+(\w+):\s*(.*)", line)
            if m:
                fields[m.group(1)] = m.group(2).strip()
                continue
            else:
                in_metadata = False
        if ":" in line and not line.startswith(" "):
            k, _, v = line.partition(":")
            fields[k.strip()] = v.strip()
    return fields, body


def slug_to_title(slug: str) -> str:
    return " ".join(w.capitalize() for w in re.split(r"[-_]", slug) if w)


def omi_exists(title: str) -> bool:
    """True only if the OMI vault already has a note with this EXACT filename.

    A fuzzy "≥2 of 3 slug words appear in some filename" match used to declare a
    memory already-covered and DELETE it without migrating its content — so
    ``docker-compose-tips`` was destroyed because a "Docker Compose Setup" note
    existed. Exact match only: anything else is migrated before deletion.
    """
    if not OMI_DIR.exists():
        return False
    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title).strip()
    return bool(safe_title) and (OMI_DIR / f"{safe_title}.md").exists()


def migrate(title: str, summary: str, body: str, mem_type: str) -> bool:
    """Create a note in OMI via the omind CLI. Returns True on success."""
    if not OMIND.exists():
        return False
    tags = f"claude-memory,{mem_type}" if mem_type else "claude-memory"
    cmd = [
        str(OMIND), "note",
        "--vault", str(VAULT),
        "--folder", "OMI",
        "--title", title,
        "--summary", summary or title,
        "--tags", tags,
    ]
    try:
        # Timeout so a hung `omind note` (vault lock held by mesh sync, gpg
        # pinentry, NFS stall) can't hang this PostToolUse hook — and therefore
        # every agent turn — indefinitely.
        result = subprocess.run(
            cmd, input=body, text=True, capture_output=True, timeout=30
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


def _safe_unlink(path: pathlib.Path) -> None:
    """Delete a migrated memory file; a permission/read-only-FS error must not
    crash a hook that fires on every tool call."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def main() -> None:
    pattern = str(CLAUDE_PROJECTS / "*/memory/*.md")
    for filepath in glob.glob(pattern):
        path = pathlib.Path(filepath)

        # Always nuke stale MEMORY.md index files (a generated pointer, no content)
        if path.name == "MEMORY.md":
            _safe_unlink(path)
            continue

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        fm, body = parse_frontmatter(content)
        slug = fm.get("name", "").strip()
        description = fm.get("description", "").strip()
        mem_type = fm.get("type", "").strip()

        # A file with no `name:` slug still holds memory content — derive a title
        # from its filename and MIGRATE it before deleting (never unlink blind).
        title = slug_to_title(slug) if slug else slug_to_title(path.stem)

        if omi_exists(title):
            _safe_unlink(path)  # content already in OMI under this exact title
        elif migrate(title, description, body, mem_type):
            _safe_unlink(path)
        # If migration fails (or omind is unavailable), LEAVE the file — the whole
        # point is to never lose memory content.


if __name__ == "__main__":
    main()
