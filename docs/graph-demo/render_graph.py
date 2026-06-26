#!/usr/bin/env python3
"""Render omind's graph JSON for the Neuromancer vault in the Cyberdeck theme."""
import json
import pathlib
import subprocess
import sys

demo = sys.argv[1]; out_dot = pathlib.Path(sys.argv[2])
data = json.loads(subprocess.check_output(
    ["omind", "graph", "export", "--format", "json", "--vault", demo, "--folder", "OMI"], text=True))
colors = json.loads((pathlib.Path(demo) / "colors.json").read_text())
titles = {n["id"]: n["title"] for n in data["nodes"]}

# Cyberdeck: near-black bg, dark-slate fills, neon strokes, light mono labels, light edges.
L = ['digraph omi {',
     '  bgcolor="#07090f";',
     '  layout=sfdp; overlap=prism; overlap_scaling=-4; splines=true; sep="+8";',
     '  node [shape=box, style="rounded,filled", fillcolor="#11151f", '
     'fontname="Menlo", fontsize=11, fontcolor="#d7e0ee", penwidth=1.6];',
     '  edge [color="#8aa0c6", arrowsize=0.45, penwidth=0.9];']  # brighter than #5a6678
for n in data["nodes"]:
    t = n["title"]
    L.append(f'  "{t}" [label="{t}", color="{colors.get(t, "#27d4ff")}"];')
for n in data["nodes"]:
    for tgt in n["out"]:
        tt = titles.get(tgt)
        if tt:
            L.append(f'  "{n["title"]}" -> "{tt}";')
L.append("}")
out_dot.write_text("\n".join(L) + "\n", encoding="utf-8")
print("wrote", out_dot)
