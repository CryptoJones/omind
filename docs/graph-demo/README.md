# Graph demo — how `docs/graph.png` was made

The README hero is `omind graph` rendered over a **synthetic** demo vault (no private
data), with nodes **coloured by each note's OKF `type`** — the same signal omind's web
graph view uses. Two small scripts build it:

1. **`make_demo_vault.py`** — generates an OMI vault of notes whose `[[wikilinks]]` form
   a connected graph. It pulls proper-noun node names from a plain-text corpus you point
   it at, assigns each note an illustrative OKF `type` (Character / Place / Construct /
   Corp / Tech), and adds a few random cross-links per note. Only node **names** and their
   `type` reach the rendered image — no prose is published.

   ```bash
   python3 make_demo_vault.py /path/to/corpus.txt /tmp/demo-vault
   ```

2. **`render_graph.py`** — calls `omind graph export --format json` on that vault and draws
   a dark, force-directed PNG whose nodes are **coloured by OKF `type`** (and sized by link
   degree), with a type legend, in the
   [Cyberdeck](https://codeberg.org/CryptoJones/cyberdeck-theme) palette. Self-contained —
   no Graphviz needed; run it with its two extra deps via `uv`:

   ```bash
   MPLBACKEND=Agg uv run --with networkx --with matplotlib \
       python render_graph.py /tmp/demo-vault ../graph.png
   ```

The shipped image used William Gibson's *Neuromancer* as the corpus — hence Case, Molly,
Wintermute, and Straylight in the graph.

---

*Proudly Made in Nebraska. Go Big Red! 🌽 <https://xkcd.com/2347/>*
