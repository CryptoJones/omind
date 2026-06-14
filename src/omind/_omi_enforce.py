#!/usr/bin/env python3
"""
omi-enforce.py — PostToolUse hook: prevent built-in Claude memory from persisting.

For each .md file found in ~/.claude/projects/*/memory/:
  1. Parse name + description from YAML frontmatter
  2. Check if a matching note already exists in the OMI vault (by filename)
  3. If NOT found → migrate via `omind note` first
  4. Delete the Claude memory file either way
"""
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


def omi_exists(title: str, slug: str) -> bool:
    """True if the OMI vault already has a note covering this memory."""
    if not OMI_DIR.exists():
        return False

    # Exact filename match (omind derives filename directly from title)
    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title).strip()
    if (OMI_DIR / f"{safe_title}.md").exists():
        return True

    # Fuzzy: ≥2 of the first 3 meaningful words appear in some filename
    words = [w.lower() for w in re.split(r"[-_]", slug) if len(w) > 3]
    if len(words) >= 2:
        for f in OMI_DIR.glob("*.md"):
            fname = f.stem.lower()
            if sum(1 for w in words[:3] if w in fname) >= 2:
                return True

    return False


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
    result = subprocess.run(cmd, input=body, text=True, capture_output=True)
    return result.returncode == 0


def main() -> None:
    pattern = str(CLAUDE_PROJECTS / "*/memory/*.md")
    for filepath in glob.glob(pattern):
        path = pathlib.Path(filepath)

        # Always nuke stale MEMORY.md index files
        if path.name == "MEMORY.md":
            path.unlink(missing_ok=True)
            continue

        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue

        fm, body = parse_frontmatter(content)
        slug = fm.get("name", "").strip()
        description = fm.get("description", "").strip()
        mem_type = fm.get("type", "").strip()

        if not slug:
            path.unlink(missing_ok=True)
            continue

        title = slug_to_title(slug)

        if omi_exists(title, slug):
            path.unlink(missing_ok=True)
        else:
            if migrate(title, description, body, mem_type):
                path.unlink(missing_ok=True)
            # If migration fails, leave the file — don't lose data


if __name__ == "__main__":
    main()
