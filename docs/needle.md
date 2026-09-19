# `omind bench --needle` — does the preflight cost the model recall?

[#321](https://github.com/CryptoJones/omind/issues/321) measured what the per-turn
preflight **shipped**: ~3.4M tokens of unrequested recall at ~25% precision.
`omind bench --precision` and `omind audit` keep measuring that. None of them
measure the thing the bug report was actually about — *"the model has gotten worse
in long sessions"*. This instrument does.

It replays a real long transcript twice:

- **preflight off** — the session as it was;
- **preflight on** — the same session, with what the preflight would have pushed
  inserted after every user prompt, exactly where the harness puts it.

Into both it plants one synthetic **fact** and one synthetic **instruction** at a
known depth — 20%, 50% and 80% of the context — then asks a model to retrieve the
fact and obey the instruction. Same haystack, same needle, same question; the only
difference between the arms is omind's own text.

```sh
omind bench --needle --transcript ~/.claude/projects/<project>/<session>.jsonl
omind bench --needle --transcript SESSION.jsonl --mode inject   # the pre-9.2.0 push #321 measured
omind bench --needle --transcript SESSION.jsonl --trials 5      # more needle sets per depth
omind bench --needle --transcript SESSION.jsonl --emit ./arms   # also write every exact prompt
```

| flag | meaning |
|---|---|
| `--transcript` | a Claude Code `.jsonl` session, or any text file (a plain haystack with no prompts to replay against) |
| `--mode` | which preflight the ON arm replays: `hint` (shipped default, a one-line pointer) or `inject` (the full-excerpt push) |
| `--trials` | needle sets per depth; each is freshly generated, so more trials means a tighter number, not a repeated one |
| `--max-chars` | how much of the transcript's **tail** to keep (default 200,000 ≈ 50k tokens) |
| `--emit` | write each arm's exact prompt, to inspect or to run against another model by hand |

## What it reports

- the size of each arm, and how much of the ON arm is replayed preflight;
- **recall** at each depth, per arm — the planted passphrase appears in the answer;
- **adherence** at each depth, per arm — the answer's last line is the planted
  acknowledgement token, as the planted instruction demanded;
- the **delta (on − off)** for both. Negative means omind's own context cost the
  model accuracy on that transcript.

## How it stays honest

- **The replay is read-only.** The live path (`guard.preflight_turn`) records a
  consult, a compliance event and a usage row every time it speaks. Replaying
  through it would write hundreds of phantom turns into the very ledger
  `omind audit` reads. The harness uses `bench.preflight_pick` — the same
  retrieval, minimum-overlap and stale-note abstain rules, with no side effects —
  which `--precision` shares, so the two instruments cannot drift on what
  "preflight would have said" means. The one thing recorded is the model call
  itself, as a `needle` row in the AI-usage ledger, because that is real spend.
- **Needles cannot be guessed or memorised.** Each is derived from
  `(trial, depth)` by hash: a project name, a three-part passphrase and an
  `ACK-XXXXXX` token that exist nowhere else.
- **No model, no number.** Without a configured backend (`OMI_MODEL_CMD` /
  `OMI_MODEL_CLI`, or a `claude` CLI on `PATH`) the arms are still built and
  sized, and the report says nothing was scored. It never substitutes a
  heuristic for the model.
- **The needle is a user message on a segment boundary**, where a human would
  really say it — not spliced mid-sentence into a tool dump.

## Reading a result

One trial is three questions per arm. That is an anecdote, not a measurement:
use `--trials` before believing a delta, and prefer a transcript from the kind of
session you actually run. A delta near zero in `hint` mode is the expected,
healthy outcome — the 9.2.0 push→pull rework exists to make the ON arm nearly
identical to the OFF arm. `--mode inject` is the control that shows what the
harness looks like when the preflight *is* heavy.

## First end-to-end run (2026-09-19)

A smoke test of the instrument, **not** a finding. A real omind development session
replayed against the author's vault, `--mode inject --max-chars 60000`, one trial,
the `claude` CLI as the backend:

| | preflight off | preflight on |
|---|---:|---:|
| context | 59,948 chars (~15.0k tokens) | 76,337 chars (~19.1k tokens) |
| replayed preflight | — | 16,345 chars (21.4% of the arm, 7 prompts) |
| recall @ 20 / 50 / 80% | 1/1, 1/1, 1/1 | 1/1, 1/1, 1/1 |
| adherence @ 20 / 50 / 80% | 1/1, 1/1, 1/1 | 1/1, 1/1, 1/1 |

Delta 0 on both metrics. What that establishes: the loader, the replay, the planting,
the model leg and the scoring all work on real data, and a fifth of the ON arm being
omind's own text did not hurt a current model at under 20k tokens. What it does not
establish: anything about long sessions. Three questions per arm at under a third of the
default window cannot show context rot either way — the run that answers #321's
question is `--max-chars` at the size of a real long session with `--trials` of five
or more, and it costs real model spend, so it is left to be run deliberately.

*Proudly Made in Nebraska. Go Big Red! 🌽 <https://xkcd.com/2347/>*
