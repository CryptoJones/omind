# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""Identifier extraction for the name index (#385, part of epic #384).

The name index answers one question that ranked search cannot: *which notes
mention this exact name?* A drive label (``As30p``), a host (``ronin28``,
``linear-algebra.cryptojones.dev``), a serial (``WX81A255JFYD``), a repo
(``CryptoJones/omind``) or an IP address shares one term with a query, and BM25
dilutes it among every other word. An exact-token lookup does not.

Everything here is **deterministic** — regular expressions over the note's
title, tags and body, no embeddings, no model — so the lookup is cheap enough to
run on every tool call (the later children of #384 do exactly that).

Only *identifier-shaped* tokens are extracted, so plain words never become
entities by construction:

* mixed letters and digits (``As30p``, ``WX81A255JFYD``, ``RTL8814AU``), minus
  units/ordinals (``16GB``, ``3rd``), bare versions (``v10``) and long hex
  hashes;
* dotted hostnames/domains under a known TLD (``soundcloud.com``,
  ``pluto.local``) — never filenames (``store.py``, ``README.md``);
* IPv4 addresses;
* ``owner/repo`` slugs (from forge URLs, or bare when they look like one);
* ``/Volumes/<label>`` mount labels;
* ``[[wikilink]]`` targets.

The second filter is **rarity**: a token in more than :func:`df_ceiling` of the
vault's notes is not informative and is never reported as an entity, even if it
is identifier-shaped (``utf8``, ``sha256``). That ceiling is applied at query
time, because document frequency changes as the vault grows.

The index is on by default — it injects nothing, so its only cost is index size
and build time (measured in the #385 PR). ``OMI_ENTITY_INDEX=0`` turns it off:
no rows are written, existing rows are dropped on the next refresh, and lookups
return ``None``.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Iterable

#: Kill switch. Any of ``0``/``off``/``false``/``no`` disables the name index.
ENABLE_ENV = "OMI_ENTITY_INDEX"
#: Override for the document-frequency ceiling, as a fraction of the vault (0, 1].
MAX_DF_ENV = "OMI_ENTITY_MAX_DF"
#: Default ceiling: a token in more than this fraction of notes is too common to
#: be an entity. Generous on purpose — the label at the heart of #384 (``As30p``)
#: is in ~10% of the reference vault, because it is also a mount path — but well
#: below the frequency of a vault-wide boilerplate token.
DEFAULT_MAX_DF_FRACTION = 0.15
#: Small vaults: never set the ceiling below this many notes, or a 20-note vault
#: would reject a name that appears in four of them.
MIN_DF_CEILING = 10
#: Bump when extraction rules change: the next refresh re-extracts every note
#: (cheap — no re-embedding, no schema wipe).
EXTRACTOR_VERSION = "1"
#: Bounds on what one note can contribute, so a pasted log can't bloat the index.
MAX_ENTITIES_PER_NOTE = 1000
_MAX_TOKEN_LEN = 120

_OFF = frozenset({"0", "off", "false", "no"})

#: TLDs accepted for a dotted hostname. An allowlist, not a pattern: ``os.environ``
#: and ``self.omi_dir`` are dotted too. Excludes TLDs that collide with file
#: extensions in practice (``.md``, ``.py``, ``.sh``, ``.rs``, ``.pl``).
_TLDS = frozenset(
    [
        "com",
        "net",
        "org",
        "edu",
        "gov",
        "mil",
        "int",
        "info",
        "biz",
        "name",
        "pro",
        "io",
        "dev",
        "ai",
        "co",
        "me",
        "tv",
        "cc",
        "xyz",
        "cloud",
        "site",
        "online",
        "tech",
        "page",
        "blog",
        "live",
        "run",
        "us",
        "uk",
        "de",
        "fr",
        "nl",
        "ca",
        "au",
        "nz",
        "jp",
        "eu",
        "ch",
        "se",
        "no",
        "fi",
        "dk",
        "es",
        "it",
        "be",
        "at",
        "ie",
        "in",
        "br",
        "mx",
        "local",
        "lan",
        "internal",
        "home",
        "arpa",
        "onion",
        "test",
        "example",
        "localhost",
    ]
)
#: Extensions that mark a dotted or slashed token as a file, not a name.
_FILE_EXTS = frozenset(
    [
        "md",
        "txt",
        "py",
        "pyc",
        "js",
        "mjs",
        "cjs",
        "ts",
        "tsx",
        "jsx",
        "json",
        "jsonl",
        "toml",
        "yaml",
        "yml",
        "ini",
        "cfg",
        "conf",
        "lock",
        "log",
        "sh",
        "zsh",
        "bash",
        "fish",
        "ps1",
        "bat",
        "cmd",
        "html",
        "htm",
        "css",
        "scss",
        "svg",
        "png",
        "jpg",
        "jpeg",
        "gif",
        "webp",
        "pdf",
        "epub",
        "mobi",
        "zip",
        "gz",
        "tgz",
        "tar",
        "xz",
        "bz2",
        "7z",
        "db",
        "sqlite",
        "sqlite3",
        "plist",
        "csv",
        "tsv",
        "xml",
        "rs",
        "go",
        "c",
        "h",
        "cpp",
        "hpp",
        "java",
        "kt",
        "rb",
        "php",
        "pl",
        "swift",
        "wav",
        "mp3",
        "mp4",
        "flac",
        "m4a",
        "mov",
        "mkv",
        "iso",
        "dmg",
        "img",
        "bin",
        "exe",
        "dll",
        "so",
        "dylib",
        "whl",
        "git",
        "cs",
        "sln",
        "slnx",
        "csproj",
        "doc",
        "docx",
        "xls",
        "xlsx",
        "ppt",
        "pptx",
        "odt",
        "rtf",
        "m4b",
        "ogg",
        "opus",
        "aac",
        "aiff",
        "webm",
        "avi",
        "heic",
        "tif",
        "tiff",
        "ico",
        "ttf",
        "otf",
        "woff",
        "woff2",
        "ipynb",
        "lua",
        "sql",
        "vue",
        "svelte",
        "scala",
        "dart",
        "zig",
    ]
)

_MIXED_RE = re.compile(r"(?<![A-Za-z0-9-])([A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)(?![A-Za-z0-9-])")
_HOST_RE = re.compile(
    r"(?<![\w.@-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(?![\w-]|\.[a-z0-9])",
    re.IGNORECASE,
)
_IPV4_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w]|\.\d)")
_FORGE_SLUG_RE = re.compile(
    r"(?:github\.com|codeberg\.org|gitlab\.com)[/:]([A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*)",
    re.IGNORECASE,
)
_SLUG_RE = re.compile(r"(?<![\w./~:-])([A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*)(?![\w/-])")
_VOLUME_RE = re.compile(r"/Volumes/([^/\s`'\"<>()\[\]|,;]+)")
_WIKILINK_RE = re.compile(r"\[\[([^\]\n]+)\]\]")
#: Bookkeeping lines: ``- Rev: 3@ronin28-19b4f0`` stamps the *writer's* node on
#: nearly every note, which made ``ronin28`` "appear" in 877 of 1,405 notes. Who
#: wrote a note is not what it is about.
_BOOKKEEPING_RE = re.compile(r"^[ \t]*(?:- )?(?:rev|agent)[ \t]*:.*$", re.IGNORECASE | re.MULTILINE)
_DIGIT_RE = re.compile(r"\d")
_ALPHA_RE = re.compile(r"[^\W\d_]")
#: Whitespace-and-punctuation split. Scanning every regex over the whole note
#: cost ~1.4 s on a 7 MiB vault; classifying each *distinct* token instead is
#: several times cheaper and matches the same things, because none of the
#: patterns above can span these characters.
_TOKEN_RE = re.compile(r"[^\s\"'`<>()\[\]{}|,;*=!?]+")

#: ``16GB``, ``100ms``, ``3rd``, ``2e`` — a number with a short unit suffix.
_UNIT_RE = re.compile(r"^\d+(?:\.\d+)?[A-Za-z]{1,3}$")
#: ``v10``, ``V2`` — a bare version, not a name.
_VERSION_RE = re.compile(r"^[vV]\d+$")
#: Long pure-hex runs are hashes/UUID parts: unique, never something an agent names.
_HEX_RE = re.compile(r"^[0-9a-fA-F-]{16,}$")


def enabled() -> bool:
    """Whether the name index is built and consulted (``OMI_ENTITY_INDEX``)."""
    return os.environ.get(ENABLE_ENV, "").strip().lower() not in _OFF


def df_ceiling(total_notes: int) -> int:
    """The most notes a token may appear in and still count as an entity."""
    fraction = DEFAULT_MAX_DF_FRACTION
    raw = os.environ.get(MAX_DF_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if 0.0 < value <= 1.0:
            fraction = value
    return max(MIN_DF_CEILING, int(total_notes * fraction))


def normalize(token: str) -> str:
    """The lookup key: NFC (macOS hands back NFD), case-folded, trimmed.

    Matching is exact on the *token*, case-insensitive on its letters: hostnames
    and volume labels are case-insensitive where they live, and ``As30p`` /
    ``as30p`` name the same thing in a note and a URL.
    """
    return unicodedata.normalize("NFC", token).strip().strip(".").casefold()


def _mixed_ok(token: str) -> bool:
    if not (4 <= len(token) <= _MAX_TOKEN_LEN):
        return False
    if not _DIGIT_RE.search(token) or not _ALPHA_RE.search(token):
        return False
    return not (_UNIT_RE.match(token) or _VERSION_RE.match(token) or _HEX_RE.match(token))


def _prose_compound(token: str) -> bool:
    """``6-month-old``, ``8-step``, ``2-gpu``: a number glued to words, not a name.

    Kept when some part is itself a name (``3-qwen3``); ``30b`` is a unit and
    does not count.
    """
    parts = token.split("-")
    return len(parts) > 1 and parts[0][:1].isdigit() and not any(_mixed_ok(part) for part in parts)


def _mixed(text: str) -> Iterable[str]:
    for match in _MIXED_RE.finditer(text):
        token = match.group(1)
        if _mixed_ok(token) and not _prose_compound(token):
            yield token
        if "-" in token and not _HEX_RE.match(token):  # ``WD-As30p`` names As30p too
            for part in token.split("-"):
                if _mixed_ok(part):
                    yield part


def _hosts(text: str) -> Iterable[str]:
    for match in _HOST_RE.finditer(text):
        host = match.group(1).rstrip(".")
        if host.rsplit(".", 1)[-1].lower() in _TLDS:
            yield host


def _ipv4(text: str) -> Iterable[str]:
    for match in _IPV4_RE.finditer(text):
        address = match.group(1)
        if all(int(octet) <= 255 for octet in address.split(".")):
            yield address


def _slug_ok(slug: str) -> bool:
    owner, _, repo = slug.partition("/")
    if not owner or not repo or len(slug) > _MAX_TOKEN_LEN:
        return False
    for side in (owner, repo):
        if not _ALPHA_RE.search(side) or _UNIT_RE.match(side):
            return False  # ``1/2``, ``09/30``, ``16k/20k``, ``1980s/90s``
    if "." in repo and repo.rsplit(".", 1)[1].lower() in _FILE_EXTS:
        return False  # ``tests/test_x.py``
    # ``soundcloud.com/as30p`` is a URL path, not a slug.
    return not ("." in owner and owner.rsplit(".", 1)[1].lower() in _TLDS)


def _slugs(text: str) -> Iterable[str]:
    for match in _FORGE_SLUG_RE.finditer(text):
        slug = match.group(1).rstrip(".")
        slug = slug[:-4] if slug.lower().endswith(".git") else slug
        if _slug_ok(slug):
            yield slug
    for match in _SLUG_RE.finditer(text):
        slug = match.group(1).rstrip(".")
        if _slug_ok(slug) and _repo_shaped(slug):
            yield slug


#: A camelCase hump (``CryptoJones``) or a digit: what separates a bare repo slug
#: (``CryptoJones/omind``, ``openai/gpt-oss-20b``) from prose (``and/or``,
#: ``Section/Chapter``, ``read-only/read-write``). A forge URL needs no such proof.
_REPO_SHAPE_RE = re.compile(r"\d|[a-z][A-Z]")


def _repo_shaped(slug: str) -> bool:
    return _REPO_SHAPE_RE.search(slug) is not None


def _volumes(text: str) -> Iterable[str]:
    for match in _VOLUME_RE.finditer(text):
        label = match.group(1).rstrip(".")
        if 2 <= len(label) <= _MAX_TOKEN_LEN:
            yield label


def _wikilinks(text: str) -> Iterable[str]:
    for raw in _WIKILINK_RE.findall(text):
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if 2 <= len(target) <= _MAX_TOKEN_LEN * 2:
            yield target


def extract(text: str, *, title: str = "", tags: Iterable[str] = ()) -> dict[str, str]:
    """Identifier-shaped tokens in a note: ``{lookup key: first spelling seen}``.

    Deterministic: the same note always yields the same mapping. Bounded by
    :data:`MAX_ENTITIES_PER_NOTE` (first occurrences win, title and tags first).
    """
    found: dict[str, str] = {}
    text = _BOOKKEEPING_RE.sub("", text)
    for source in (title, *tags, text):
        if not source:
            continue
        source = unicodedata.normalize("NFC", source)
        for token in _candidates(source):
            key = normalize(token)
            if key and key not in found:
                found[key] = token
                if len(found) >= MAX_ENTITIES_PER_NOTE:
                    return found
    return found


def _candidates(source: str) -> Iterable[str]:
    yield from _wikilinks(source)
    yield from _volumes(source)
    for word in dict.fromkeys(_TOKEN_RE.findall(source)):
        has_digit = _DIGIT_RE.search(word) is not None
        if "/" in word:
            yield from _slugs(word)
        if "." in word:
            yield from _hosts(word)
            if has_digit:
                yield from _ipv4(word)
        if has_digit:
            yield from _mixed(word)
