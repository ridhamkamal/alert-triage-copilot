# Alert Triage Copilot

An AI recommendation layer for network operations triage. When an on-call
engineer opens an alert, it answers one question: **what did we do the last
time something looked like this?**

Built on public data. Independent work — no proprietary or employer data is
used anywhere in this project.

---

## Scope, stated up front

This runs on the **2,000-line BGL sample** that Loghub publishes on GitHub,
not the full 4,747,963-line corpus distributed through Zenodo. The sample is
thinned across the log's full 214-day span rather than contiguous.

That boundary decides what this project can and cannot claim:

| Claim | Status |
| --- | --- |
| Signature structure, concentration, repetition | Measured on real data |
| Retrieval coverage and cold-start rate | Measured on real data |
| Which remediation actions are correct | **Synthetic** — see below |
| Alert arrival rate, burstiness, queue depth | **Not measurable here.** Needs the full corpus |
| Whether the failure mix drifts over time | **Open question.** Suggestive result only |

The analysis pipeline runs unchanged on the full file. Everything in the
"not measurable" row is one download away, and the method is already written.

### The synthetic layer

No public dataset records what an engineer *did* in response to an alert —
which is precisely the signal a retrieval layer needs. So remediation history
is generated from a deterministic rule set with realistic noise: wrong first
attempts, actions that did not help, and disagreement between engineers.

It is labelled synthetic in the code, in the UI, and here. **Any accuracy
figure measured against it says the model learned the generator, not that it
would help a real engineer.** Coverage and cold-start rate come from the real
data and are the numbers worth quoting.

---

## What it does

Two recommendation problems share one screen, and they are not the same
problem:

- **Retrieval** — for the alert in front of you, find prior incidents with the
  same signature and surface what resolved them, with the evidence attached.
  This is what is built.
- **Ranking** — order the queue by expected cost of delay. Designed, not built.
  Its premise is that alerts arrive faster than they can be worked, and the
  available data cannot show that.

The design principle running through both: **the system shows its reasoning
and its uncertainty, never just its answer.** Every queue row carries why it
was ranked there. Every suggestion carries a confidence band and the specific
prior incidents behind it. When nothing matches, the panel says so and offers
nothing — an empty answer beats a confident bad one, and the first wrong
suggestion costs more than the hundred right ones earned.

---

## Findings

Run on the sample, 11 October 2026.

**Signatures concentrate hard.** 143 alerts across 15 distinct signatures.
`data TLB error interrupt` and `data storage interrupt` are 63% of all volume
between them. Report per-signature performance, not an aggregate — an
aggregate here mostly measures those two.

**Coverage depends entirely on how you measure it, and the flattering number
is the wrong one.** Walking forward and checking each alert against everything
before it gives 87%. Splitting chronologically — train on the first 70% of the
timeline, test on the last 30%, which is the only arrangement matching what the
product faces in production — gives **9.3%**. Eight of the nine signatures in
the test period had never appeared in training.

**That gap is concept drift, not a parser bug.** First suspicion was the
signature normaliser: node names in one BGL convention were being collapsed and
another left intact. Fixing it cut distinct signatures from 24 to 15 and
singletons from 58% to 20%. Coverage did not move. The failure mix genuinely
shifted — training period dominated by TLB and storage errors, test period by
`ciod` message-prefix and Lustre mount failures.

**Caveat that matters:** 43 test alerts is too few to conclude from, and a
thinned sample may not preserve temporal structure. This is a flagged
hypothesis, not a result.

**If it holds at scale, it changes the product.** Case-base freshness stops
being an implementation detail: the system would need to know when its own
case base is stale and say so, cold start would recur rather than resolve, and
coverage would become a primary tracked metric rather than a diagnostic.

---

## Running it

No dependencies beyond the standard library.

```bash
# analysis: arrival profile, concentration, time-split retrieval evaluation
python3 bgl_analysis.py BGL_2k.log

# build the UI's dataset
python3 build_triage_data.py BGL_2k.log -o triage_data.json
```

`bgl_analysis.py` streams, so the full 4.7M-line corpus works unchanged:

```bash
python3 bgl_analysis.py BGL.log --split 0.7 --service-rate 12 --out results/
```

Two values are judgement calls rather than measurements, both at the top of
`bgl_analysis.py`: `SEVERITY_WEIGHT`, which encodes how much worse a missed
FATAL is than a missed INFO, and the noise rates in the synthetic generator.
Every weighted number inherits those choices.

---

## Files

| File | What it is |
| --- | --- |
| `bgl_analysis.py` | Analysis pipeline. Signature extraction, arrival profile, queue-pressure model, time-split retrieval evaluation with per-signature breakdown |
| `build_triage_data.py` | Runs retrieval over the log and emits the UI's dataset |
| `triage_ui.html` | The triage screen. Self-contained, data embedded |
| `triage_data.json` | Generated — ranked queue with retrieved suggestions |

---

## What I would do differently

The first scope bundled alert correlation into v1, reasoning that correlated
alerts are the obvious cause of queue noise. That would have made the
experiment unreadable — two mechanisms moving the same metric with no way to
attribute the change. Cutting it was right, but it also removed the mitigation
for the major-incident case, where fifty correlated alerts arrive at once. That
is now an open risk rather than a solved one.

The first coverage measurement was also wrong in a way worth recording: it used
a cumulative walk, which quietly let early-period signatures carry the result.
The 87% it produced looked like a green light. The correct chronological split
gave 9.3% and pointed at drift. Had I not re-run it, the project would have
been built on a number that was never measuring the right thing.

The substitution of supercomputer RAS data for network telemetry is the
weakest part of the project and no amount of careful modelling fixes it. If
this were real work, the next step would be a design partner with NOC data
under NDA, and everything downstream of the data section would be provisional
until then.

---

## Sources

- Loghub — [github.com/logpai/loghub](https://github.com/logpai/loghub).
  Zhu, He, He, Liu, Lyu et al., *Loghub: A Large Collection of System Log
  Datasets for AI-driven Log Analytics*. Research use; cite the Loghub paper.
- BGL: Blue Gene/L supercomputer RAS log, 4,747,963 lines over 214.7 days.
  Full corpus on Zenodo; the 2,000-line sample used here is on GitHub.
