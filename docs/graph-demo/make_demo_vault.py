#!/usr/bin/env python3
"""Build a Neuromancer-themed demo OMI vault: proper-noun nodes + random cross-links.
Only node labels (names/places) end up in the rendered graph; prose stays local."""
import collections
import pathlib
import random
import re
import sys

txt = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8", errors="ignore")
VAULT = pathlib.Path(sys.argv[2]); OMI = VAULT / "OMI"
OMI.mkdir(parents=True, exist_ok=True)
random.seed(1984)

STOP = set(["The", "He", "She", "It", "They", "And", "But", "Then", "When", "Now", "His", "Her", "A", "An", "In", "On", "At", "As", "So", "There", "That", "This", "What", "You", "We", "If", "For", "Of", "To", "With", "Was", "Were", "Had", "Has", "Said", "Like", "Up", "Down", "Out", "Into", "Over", "Under", "Their", "Its", "Him", "Them", "Not", "No", "Yes", "Maybe", "Just", "Only", "Still", "Even", "Once", "Twice", "Three", "One", "Two", "Some", "All", "Each", "Every", "More", "Most", "Less", "Than", "Then", "Here", "Where", "Why", "How", "Who", "Which", "While", "After", "Before", "About", "Above", "Below", "Across", "Behind", "Beside", "Between", "Beyond", "During", "Through", "Without", "Within", "Around", "Against", "Off", "I'd", "He'd", "She'd", "It's", "He's", "She's", "They'd", "We'd", "You'd", "Don't", "Didn't", "Couldn't", "Wouldn't"])

words = re.findall(r"[A-Z][A-Za-z'\-]{2,}", txt)
lower = collections.Counter(w.lower() for w in re.findall(r"[a-z][a-z'\-]{2,}", txt))
cap = collections.Counter(w for w in words if w not in STOP)
# keep words that read as proper nouns: frequent, and rare-or-never as a lowercase word
names = [w for w, c in cap.items()
         if c >= 5 and lower.get(w.lower(), 0) <= c and "'" not in w]
names = sorted(names, key=lambda w: -cap[w])[:70]

# real lines (for flavor bodies, kept local only)
lines = [ln.strip() for ln in txt.splitlines() if 30 < len(ln.strip()) < 90]

# neon Cyberdeck categorical palette for node strokes
PALETTE = ["#27d4ff", "#55ff99", "#ffb000", "#ff4f4f", "#a371f7", "#4c9aff", "#ff8ad8"]
colors = {n: PALETTE[i % len(PALETTE)] for i, n in enumerate(sorted(names))}

for n in names:
    k = random.randint(1, 3)  # random out-links -> dense, organic graph
    targets = random.sample([m for m in names if m != n], k)
    body = [f"# {n}", "", random.choice(lines), ""]
    body += [f"- [[{t}]]" for t in targets]
    safe = re.sub(r"[^\w '\-]", "", n)
    (OMI / f"{safe}.md").write_text("\n".join(body) + "\n", encoding="utf-8")

# emit the color map for the renderer
import json

(VAULT / "colors.json").write_text(json.dumps(colors), encoding="utf-8")
print(f"wrote {len(names)} Neuromancer nodes to {OMI}")
print("sample:", ", ".join(names[:12]))
