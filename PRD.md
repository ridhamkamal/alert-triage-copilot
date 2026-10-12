# Alert Triage Copilot — PRD, Phase 1: Retrieval Panel

*Scope: the retrieval half only. Ranking is phase 2 and gated on evidence this
project does not yet have — see [BRIEF.md](BRIEF.md) for why. Strategy,
automation ladder, experiment design and risk analysis live in the brief; this
document is what an engineer builds from.*

Last updated 11 October 2026.

---

## Problem statement

An on-call NOC engineer opening an incident has no way to find out what the
team did the last time something similar happened. That knowledge exists — in
ticket comments, chat threads, and the heads of the three longest-tenured
engineers — but there is no path from "this looks familiar" to "here is what
worked." Junior engineers rediscover fixes that already exist, and senior ones
become single points of failure at 03:00.

The cost is time-to-remediation on incidents that were already solved once, and
an escalation rate inflated by uncertainty rather than difficulty.

---

## Goals

| Goal | How we know it succeeded |
| --- | --- |
| Cut median time-to-remediation on incidents with precedent | 15% reduction vs. the unranked control, measured per severity band |
| Engineers act on suggestions rather than escalating | Top-1 suggestion accepted in 40%+ of resolved incidents with coverage |
| The panel is trusted because it admits ignorance | Under 5% of suggestions marked "wrong" by the engineer who used them |
| Prior resolutions become reusable without extra work | 60%+ of resolved incidents produce a usable feedback signal |

The third goal is the one that is easy to drop and shouldn't be. A panel that
is right 90% of the time and confident 100% of the time is worse than one that
is right 90% of the time and says so.

---

## Non-goals

- **Executing or applying remediations.** Suggestion only. Trust is earned
  before the product touches the network; this is rung 1 of the ladder in the
  brief and phase 1 does not leave it.
- **Ranking the queue.** Separate problem, separate phase, premise untested.
  Phase 1 presents alerts in arrival order.
- **Alert correlation or deduplication.** A real adjacent problem and a
  different product. Bundling it would make the phase 1 experiment unreadable,
  because two mechanisms would be moving the same metric.
- **Root cause analysis.** The panel says what was done before, not why the
  incident happened. Claiming causality the data cannot support is the fastest
  way to lose the user.
- **Mobile.** Triage happens at a desk with multiple screens.

---

## User stories

**On-call NOC engineer** (primary — the only persona phase 1 designs for)

1. As an on-call engineer, I want to see what resolved this signature before,
   so that I don't re-derive a fix the team already knows.
2. As an on-call engineer, I want to see *why* each action is suggested and
   how often it worked, so that I can judge it rather than trust it blindly.
3. As an on-call engineer, I want the panel to tell me plainly when it has
   nothing, so that I don't waste time evaluating a guess.
4. As an on-call engineer, I want to flag a suggestion as used, unused, or
   wrong in one click, so that recording what happened costs me nothing
   mid-incident.
5. As an on-call engineer facing an unprecedented incident, I want that fact
   surfaced rather than hidden, so that I escalate early instead of late.

**Escalation / SME engineer** (secondary — shapes constraints, no dedicated UI)

6. As an escalation engineer pulled into an incident, I want to see which
   suggestions the on-call already tried, so that I don't repeat them.

**Ops manager** (secondary — reporting only)

7. As an ops manager, I want to see retrieval coverage over time, so that I can
   tell whether the case base is keeping up with the failure mix.

---

## Requirements

### P0 — Must have

**R1. Signature extraction**
Every incoming alert is reduced to a signature: a normalized template with
instance-specific values (node identifiers, hex addresses, IPs, paths, numeric
values) replaced by placeholders.

- [ ] Alerts differing only in node identity produce the same signature
- [ ] Alerts differing only in numeric or hex values produce the same signature
- [ ] Multiple node-naming conventions are collapsed consistently — a convention
      missed here silently inflates the cold-start rate (this happened during
      analysis; see BRIEF.md)
- [ ] Signature extraction is deterministic: the same alert always produces the
      same signature

**R2. Case base**
A store of prior incidents keyed by signature, with the action taken and
whether it resolved the incident.

- [ ] Only incidents chronologically prior to the current one are retrievable
- [ ] Only actions recorded as having helped are eligible as suggestions
- [ ] An incident's own resolution enters the case base once it closes

**R3. Retrieval**
Given a selected alert, return up to three candidate actions.

- Given an alert whose signature has prior resolutions
- When the engineer opens it
- Then up to three actions are shown, ordered by how often each resolved that
  signature

- [ ] Maximum three suggestions, never more
- [ ] Each carries the count of prior uses and the share of successful
      resolutions it represents
- [ ] Each links to the specific prior incidents it was drawn from, with
      timestamp and node, visible without an extra click
- [ ] Retrieval returns in under 500ms at p95

**R4. Abstention**
When similarity falls below threshold, the panel returns nothing and says so.

- Given an alert whose signature has no prior resolutions
- When the engineer opens it
- Then the panel states that nothing in the case base matches, and offers no
  candidate actions

- [ ] No partial-match or "closest guess" suggestion is shown below threshold
- [ ] The abstention state names the affected component as a starting point
- [ ] The abstention state tells the engineer their resolution will become the
      first case for this signature
- [ ] Abstention is visually distinct from "loading" and from "zero results due
      to error"

**R5. Confidence bands**
Every suggestion carries a confidence band derived from evidence volume and
agreement, not from model score.

| Band | Condition |
| --- | --- |
| High | 5+ prior successful resolutions and 60%+ agreement on the action |
| Medium | 2+ prior successful resolutions |
| Low | 1 prior successful resolution |

- [ ] The band is shown as a label, not a numeric score
- [ ] The underlying counts are visible so the band is auditable

**R6. Feedback capture**
One-click feedback on each suggestion: used, didn't use, wrong.

- [ ] Recording feedback takes exactly one click and no confirmation step
- [ ] Feedback is reversible by clicking again
- [ ] Feedback is captured per suggestion, not per incident
- [ ] No feedback is required to close an incident — an engineer mid-outage is
      never blocked by the panel

**R7. Novelty surfacing**
An alert with no precedent is marked as such in the queue, before it is opened.

- [ ] Alerts with no case-base match carry a visible marker in the list view
- [ ] The marker is distinguishable from severity

### P1 — Should have

**R8. Case-base staleness signal.** Surface when retrieval coverage for a
component or signature family has dropped sharply, as an early warning that the
failure mix has shifted. Promoted from P2 on the strength of the drift finding
in the brief; still P1 because the finding is not yet confirmed at scale.

**R9. Content-based fallback.** When retrieval abstains, an LLM reasoning over
alert text, component and topology proposes a diagnostic first step — labelled
explicitly as a diagnostic suggestion, never as a fix, and visually separated
from retrieved actions.

**R10. Coverage reporting.** A weekly view of retrieval coverage and acceptance
by signature family, for the ops manager persona.

**R11. Prior-attempt visibility.** Show the escalation engineer which
suggestions the on-call already marked used or wrong on this incident.

### P2 — Future considerations

Not built, but the phase 1 design should not foreclose them:

- **Similarity beyond exact signature match.** Phase 1 matches signatures
  exactly. Embedding-based nearest-neighbour retrieval is the obvious next step,
  and the abstention threshold should be expressed as a similarity score from
  the start so it survives the swap.
- **Device-class inheritance.** A newly deployed device inherits the case base
  of its class and role until it accumulates its own history.
- **Cross-team case sharing.** Keep the case base keyed by signature rather than
  by team, so merging across teams later does not require a migration.
- **One-click apply (rung 2).** Phase 3. Requires the evidence gate in the brief.

---

## Success metrics

**Leading** — evaluated weekly from launch

| Metric | Target | Method |
| --- | --- | --- |
| Suggestion acceptance (top-1) | 40%+ of covered incidents | Feedback events / covered incidents opened |
| Retrieval coverage | Tracked, not targeted | Covered incidents / total incidents |
| Feedback capture rate | 60%+ of resolved incidents | Incidents with ≥1 feedback event / resolved |
| "Wrong" rate | Under 5% of accepted suggestions | Wrong events / used events |
| Panel latency p95 | Under 500ms | Instrumentation |

Coverage is tracked rather than targeted deliberately. Targeting it creates
pressure to lower the abstention threshold, which is the one thing that would
break the product.

**Lagging** — evaluated at 6 weeks, per the experiment design in the brief

| Metric | Target | Notes |
| --- | --- | --- |
| Median time-to-remediation | 15% reduction vs. control | Per severity band, never pooled |
| Escalation rate | No increase | Counter-metric. A fall in remediation time bought by more escalations is not a win |
| Critical-incident miss rate | No detectable regression | Guardrail. Hard stop on the rollout regardless of other results |

---

## Open questions

**Blocking — needed before implementation starts**

- *Engineering:* Where does resolution data come from in a real deployment?
  Phase 1 assumes a structured record of action-taken per incident. If the
  ticketing system only has free-text comments, extracting that record is a
  project in itself and belongs in this scope.
- *Data:* What is the abstention threshold in production terms? Phase 1 uses
  exact signature match, which makes the threshold binary. Moving to similarity
  scoring requires a defensible cutoff, and picking it arbitrarily undermines R4.
- *Product/Design:* What does the panel show during a major incident when fifty
  correlated alerts arrive at once? The current design assumes independent
  incidents, which is exactly wrong in the moments that matter most.

**Non-blocking — resolve during implementation**

- *Design:* How are evidence links presented when a suggestion has twenty prior
  cases rather than three?
- *Engineering:* Does the case base need per-team partitioning at launch, or can
  that wait? See P2.
- *Data:* Should feedback from an engineer who did not resolve the incident be
  weighted differently from the resolver's?

---

## Timeline and dependencies

No hard external deadline. Phasing is gated on evidence rather than dates, which
is the point of the ladder in the brief.

**Dependencies**

- R2 depends on a structured source of resolution history. This is the single
  largest unknown in the phase and the first blocking question above.
- R9 depends on an LLM endpoint and on R4 shipping first — the fallback is only
  reachable through the abstention path.
- The 6-week experiment depends on a volunteer rotation and on the unranked
  control view (R0 implicitly: the queue must be switchable).

**Suggested sequencing**

1. R1 + R2 — signature extraction and case base. Nothing else works without them.
2. R3 + R5 — retrieval and confidence. First point at which the product is demoable.
3. R4 + R7 — abstention and novelty surfacing. Ship before any user sees it; a
   panel that guesses before it learns to abstain will lose trust in week one.
4. R6 — feedback capture, which begins accumulating the signal everything else
   improves on.
5. P1 items as fast-follows, R8 first if the drift finding holds at scale.
