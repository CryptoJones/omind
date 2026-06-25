# Graph demo — how `docs/graph.png` was made

The README hero is `omind graph` rendered over a **synthetic** demo vault (no private
data). Two small, dependency-free scripts build it:

1. **`make_demo_vault.py`** — generates an OMI vault of notes whose `[[wikilinks]]` form
   a connected graph. It pulls proper-noun node names from a plain-text corpus you point
   it at and adds a few random cross-links per note. Only the node **names** end up in the
   rendered image — no prose is published.

   ```bash
   python3 make_demo_vault.py /path/to/corpus.txt /tmp/demo-vault
   ```

2. **`render_graph.py`** — calls `omind graph export --format json` on that vault and emits
   a [Cyberdeck](https://codeberg.org/CryptoJones/cyberdeck-theme)-themed Graphviz `dot`
   (near-black `#07090f` background, neon node strokes, light edges), which you render with
   `sfdp`:

   ```bash
   python3 render_graph.py /tmp/demo-vault graph.dot
   sfdp -Tpng -Gsize="16,9!" -Gratio=compress -Gdpi=150 graph.dot -o graph.png
   ```

The shipped image used William Gibson's *Neuromancer* as the corpus — hence Case, Molly,
Wintermute, and Straylight in the graph.

---

*Proudly Made in Nebraska. Go Big Red! 🌽 <https://xkcd.com/2347/>*
