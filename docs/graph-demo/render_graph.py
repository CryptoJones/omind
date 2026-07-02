#!/usr/bin/env python3
"""Render omind's graph for the demo vault as a dark, force-directed PNG,
coloured by each note's OKF `type` — mirroring omind's web graph view.

Self-contained (no Graphviz needed). Run it with the two extra deps via uv:

    MPLBACKEND=Agg uv run --with networkx --with matplotlib \
        python render_graph.py /tmp/demo-vault ../graph.png
"""
import json
import pathlib
import subprocess
import sys

import matplotlib.pyplot as plt
import networkx as nx

demo = sys.argv[1]
out_png = pathlib.Path(sys.argv[2])

data = json.loads(subprocess.check_output(
    ["omind", "graph", "export", "--format", "json", "--vault", demo, "--folder", "OMI"],
    text=True))

# Neon Cyberdeck colour per OKF `type` — the node's colour IS its kind.
TYPE_COLOR = {
    "Character": "#27d4ff", "Place": "#55ff99", "Construct": "#a371f7",
    "Corp": "#ffb000", "Tech": "#ff4f4f",
}
DEFAULT = "#8aa0c6"
BG = "#07090f"

G = nx.DiGraph()
title, ntype = {}, {}
for n in data["nodes"]:
    G.add_node(n["id"])
    title[n["id"]] = n["title"] or n["id"][:-3]
    ntype[n["id"]] = n["type"]
for src, dst in data["edges"]:
    G.add_edge(src, dst)

# Deterministic force-directed layout (seeded so re-renders match).
pos = nx.spring_layout(G, seed=1984, k=0.9, iterations=240)
deg = dict(G.degree())

fig, ax = plt.subplots(figsize=(16, 9), dpi=150)
fig.patch.set_facecolor(BG)
ax.set_facecolor(BG)
ax.axis("off")

nx.draw_networkx_edges(G, pos, ax=ax, edge_color="#8aa0c6", width=0.6, alpha=0.45,
                       arrows=True, arrowsize=5, connectionstyle="arc3,rad=0.06")
nx.draw_networkx_nodes(
    G, pos, ax=ax,
    node_color=[TYPE_COLOR.get(ntype[n], DEFAULT) for n in G.nodes()],  # colour == OKF type
    node_size=[40 + 30 * deg[n] for n in G.nodes()],                    # size == link degree
    edgecolors=BG, linewidths=0.8)
# Label only the better-connected nodes so the hero image stays legible.
nx.draw_networkx_labels(
    G, pos, ax=ax, labels={n: title[n] for n in G.nodes() if deg[n] >= 3},
    font_size=6.5, font_family="monospace", font_color="#d7e0ee")

present = sorted({ntype[n] for n in G.nodes() if ntype[n]})
handles = [plt.Line2D([0], [0], marker="o", linestyle="", markersize=8, markeredgecolor="none",
                      markerfacecolor=TYPE_COLOR.get(t, DEFAULT), label=t) for t in present]
legend = ax.legend(handles=handles, title="OKF type", loc="upper left", frameon=False,
                   labelcolor="#d7e0ee", fontsize=8)
legend.get_title().set_color("#8aa0c6")

fig.savefig(out_png, facecolor=BG, bbox_inches="tight", pad_inches=0.2)
print("wrote", out_png)
