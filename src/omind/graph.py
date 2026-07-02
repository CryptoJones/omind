# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Aaron K. Clark
"""``omind graph`` — the knowledge graph over the OMI vault's ``[[wikilinks]]``.

Every note is a node; every ``[[wikilink]]`` it makes is a directed edge. The
store already answers the inbound question (``backlinks``) and ``lint`` already
flags orphans and broken links one note at a time — this module assembles the
*whole-graph* view those leave out: forward links, multi-hop neighborhoods,
the shortest link path between two notes, and a JSON/Graphviz-DOT export.

Resolution mirrors :meth:`OmiStore.backlinks` and ``lint``: a link ``[[Target]]``
(its ``|alias`` and ``#heading`` stripped) resolves to a note when its lowercased
text equals that note's filename stem or its title. A link that resolves to no
live note is *dangling*. The graph ignores self-links and disabled notes.

No third-party graph library: the vault is small and the traversals are a plain
breadth-first search over adjacency sets. Read-only throughout — nothing here
edits a note.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from omind.paths import RESERVED_FILENAMES
from omind.store import _WIKILINK_RE, derive_okf_type, parse_note


def _link_target(raw: str) -> str:
    """The note a ``[[wikilink]]`` body names — the part before ``|`` (alias) or
    ``#`` (heading), trimmed. Mirrors the resolution in ``store``/``lint``."""
    return raw.split("|", 1)[0].split("#", 1)[0].strip()


@dataclass
class GraphNode:
    """One note and its resolved edges, both keyed by ``.md`` filename."""

    filename: str  # "Foo.md"
    title: str
    okf_type: str = ""  # the note's OKF ``type`` — for grouping/colouring the graph
    out: set[str] = field(default_factory=set)  # filenames this note links to
    inn: set[str] = field(default_factory=set)  # filenames that link to this note


@dataclass
class Graph:
    """The resolved knowledge graph: nodes by filename, plus unresolved links."""

    nodes: dict[str, GraphNode]
    dangling: list[tuple[str, str]]  # (source filename, raw link target) — resolves to nothing

    def resolve(self, name: str) -> str:
        """Map a user-supplied ``name`` (filename, stem, or title) to a node's
        filename. Raises :class:`ValueError` when nothing matches."""
        if name in self.nodes:
            return name
        with_ext = name if name.endswith(".md") else f"{name}.md"
        if with_ext in self.nodes:
            return with_ext
        needle = name[:-3].lower() if name.endswith(".md") else name.lower()
        for filename, node in self.nodes.items():
            if filename[:-3].lower() == needle or node.title.lower() == needle:
                return filename
        raise ValueError(f"note not found: {name!r}")


def build_graph(omi_dir: Path | str) -> Graph:
    """Parse every live note once and resolve its ``[[wikilinks]]`` into a graph."""
    omi = Path(omi_dir)
    # (filename, title, raw outbound targets) for each live note, plus an index
    # from every linkable identifier (stem + title, lowercased) to its filename.
    parsed: list[tuple[str, str, str, set[str]]] = []
    id_to_file: dict[str, str] = {}
    if omi.is_dir():
        for path in sorted(omi.glob("*.md")):
            if path.name in RESERVED_FILENAMES or path.name.startswith("."):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fields = parse_note(text)
            if fields.disabled:
                continue
            title = fields.title.strip()
            # The note's OKF type, deriving it from tags when undeclared — the
            # same rule render_fields uses — so every node carries a non-empty type.
            okf_type = fields.okf_type.strip() or derive_okf_type(fields.tags)
            targets = {t for t in (_link_target(m) for m in _WIKILINK_RE.findall(text)) if t}
            parsed.append((path.name, title, okf_type, targets))
            id_to_file[path.stem.strip().lower()] = path.name
            if title:
                id_to_file[title.lower()] = path.name

    nodes = {
        fn: GraphNode(filename=fn, title=title, okf_type=okf_type)
        for fn, title, okf_type, _ in parsed
    }
    dangling: list[tuple[str, str]] = []
    for src, _title, _type, targets in parsed:
        for target in sorted(targets):
            dest = id_to_file.get(target.lower())
            if dest is None:
                dangling.append((src, target))
            elif dest != src:  # a note linking itself is not an edge
                nodes[src].out.add(dest)
                nodes[dest].inn.add(src)
    return Graph(nodes=nodes, dangling=dangling)


def _adjacent(node: GraphNode, direction: str) -> set[str]:
    """The neighbours to follow for a traversal direction: ``out`` (links it
    makes), ``in`` (links to it), or ``both`` (undirected)."""
    if direction == "out":
        return node.out
    if direction == "in":
        return node.inn
    return node.out | node.inn


def neighbors(
    graph: Graph, start: str, depth: int = 1, direction: str = "both"
) -> list[tuple[str, int]]:
    """Notes within ``depth`` hops of ``start``, as ``(filename, distance)`` pairs
    ordered nearest-first then by name. Excludes ``start`` itself."""
    src = graph.resolve(start)
    dist = {src: 0}
    queue: deque[str] = deque([src])
    while queue:
        cur = queue.popleft()
        if dist[cur] >= depth:
            continue
        for nxt in _adjacent(graph.nodes[cur], direction):
            if nxt not in dist:
                dist[nxt] = dist[cur] + 1
                queue.append(nxt)
    return sorted(
        ((fn, d) for fn, d in dist.items() if fn != src),
        key=lambda pair: (pair[1], pair[0].lower()),
    )


def shortest_path(
    graph: Graph, source: str, target: str, direction: str = "both"
) -> list[str] | None:
    """The shortest link path from ``source`` to ``target`` as a list of filenames
    (inclusive of both ends), or ``None`` when no path connects them. Undirected
    by default; ``direction`` restricts which edges count."""
    src = graph.resolve(source)
    dst = graph.resolve(target)
    if src == dst:
        return [src]
    prev: dict[str, str | None] = {src: None}
    queue: deque[str] = deque([src])
    while queue:
        cur = queue.popleft()
        for nxt in sorted(_adjacent(graph.nodes[cur], direction)):
            if nxt in prev:
                continue
            prev[nxt] = cur
            if nxt == dst:
                path = [dst]
                step: str | None = cur
                while step is not None:
                    path.append(step)
                    step = prev[step]
                return list(reversed(path))
            queue.append(nxt)
    return None


def orphans(graph: Graph) -> list[str]:
    """Filenames of notes with neither inbound nor outbound links."""
    return sorted(fn for fn, node in graph.nodes.items() if not node.out and not node.inn)


def dangling_links(graph: Graph) -> list[tuple[str, str]]:
    """``(source filename, raw target)`` for every link that resolves to no note."""
    return sorted(graph.dangling)


def stats(graph: Graph) -> dict[str, int]:
    """Whole-graph counts: notes, directed links, orphans, and dangling links."""
    return {
        "notes": len(graph.nodes),
        "links": sum(len(node.out) for node in graph.nodes.values()),
        "orphans": len(orphans(graph)),
        "dangling": len(graph.dangling),
    }


def to_json(graph: Graph) -> dict[str, object]:
    """A JSON-serialisable view of the whole graph (nodes, edges, dangling)."""
    return {
        "nodes": [
            {
                "id": node.filename,
                "title": node.title,
                "type": node.okf_type,
                "out": sorted(node.out),
            }
            for node in sorted(graph.nodes.values(), key=lambda n: n.filename.lower())
        ],
        "edges": sorted(
            [src, dst] for src, node in graph.nodes.items() for dst in node.out
        ),
        "dangling": [[src, target] for src, target in sorted(graph.dangling)],
    }


def _dot_quote(text: str) -> str:
    """A safely double-quoted Graphviz ID/label."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


#: Pale fill per OKF ``type`` for the DOT/SVG export (and a hint for the web
#: legend). Unknown/producer-defined types fall back to :data:`_DEFAULT_TYPE_FILL`.
TYPE_FILL = {
    "User": "#dbeafe",
    "Feedback": "#fce7f3",
    "Project": "#dcfce7",
    "Reference": "#fef9c3",
    "Memory": "#e5e7eb",
    "Playbook": "#ede9fe",
    "Reference Card": "#ffedd5",
}
_DEFAULT_TYPE_FILL = "#f3f4f6"


def to_dot(graph: Graph) -> str:
    """The graph as Graphviz DOT (``dot -Tsvg``-renderable), filled by OKF type."""
    lines = ["digraph omi {", "  rankdir=LR;", "  node [shape=box, style=filled];"]
    for node in sorted(graph.nodes.values(), key=lambda n: n.filename.lower()):
        label = node.title or node.filename[:-3]
        fill = TYPE_FILL.get(node.okf_type, _DEFAULT_TYPE_FILL)
        lines.append(
            f"  {_dot_quote(node.filename)} "
            f"[label={_dot_quote(label)}, fillcolor={_dot_quote(fill)}];"
        )
    for src in sorted(graph.nodes):
        for dst in sorted(graph.nodes[src].out):
            lines.append(f"  {_dot_quote(src)} -> {_dot_quote(dst)};")
    lines.append("}")
    return "\n".join(lines)
