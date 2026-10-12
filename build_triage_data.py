#!/usr/bin/env python3
"""
Build the triage screen's data: ranked queue + retrieved suggestions.

Reads a BGL log, runs the actual retrieval logic (signature match against a
chronologically-prior case base, with abstention), and writes one JSON file
the UI consumes. No model weights, no training step — the whole point of the
MVP is that the simplest honest version already shows the product.

    python3 build_triage_data.py BGL_2k.log -o triage_data.json
"""

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

from bgl_analysis import (
    ACTION_POOL,
    SEVERITY_WEIGHT,
    DEFAULT_WEIGHT,
    parse,
    signature,
    synthesise_remediations,
)

# How many prior cases before we stop calling a suggestion low-confidence.
CONFIDENCE_FLOOR = 2
CONFIDENCE_SOLID = 5


def rank_score(alert, sig_history):
    """Order the queue by expected cost of NOT looking at this next.

    Two terms, deliberately legible rather than learned — a PM can argue
    with each one, which is the point at this stage:

      severity   the asymmetric cost of a miss (FATAL >> INFO)
      novelty    an unprecedented alert is routed UP, inverting the usual
                 recommender instinct to favour the familiar

    A staleness term belongs here in production (how long it has waited),
    but this corpus is a historical replay where every alert is months old,
    so including it would add a constant and tell the reader nothing.
    """
    severity = SEVERITY_WEIGHT.get(alert["level"], DEFAULT_WEIGHT)
    seen_before = sig_history.get(alert["sig"], 0)
    novelty = 3.0 if seen_before == 0 else (1.5 if seen_before < 3 else 1.0)

    score = severity * novelty

    reasons = []
    if severity >= 8:
        reasons.append(f"{alert['level'].lower()} severity")
    elif severity >= 4:
        reasons.append(alert["level"].lower())
    if seen_before == 0:
        reasons.append("never seen before")
    elif seen_before < 3:
        reasons.append(f"only {seen_before} prior occurrence{'s' if seen_before > 1 else ''}")
    else:
        reasons.append(f"{seen_before} prior occurrences")
    if not reasons:
        reasons.append("routine, well-precedented")

    return score, ", ".join(reasons)


def build(logfile, limit):
    rows = list(parse(logfile))
    alerts = [r for r in rows if r["label"] != "-"]
    alerts.sort(key=lambda r: r["epoch"])

    records, canonical = synthesise_remediations(alerts)

    # Walk forward in time, building the case base as the product would.
    case_base = defaultdict(list)       # sig -> list of prior record indices
    sig_history = Counter()
    out = []

    for i, r in enumerate(records):
        priors = list(case_base[r["sig"]])

        # --- retrieval -----------------------------------------------------
        helped = [records[j] for j in priors if records[j]["action_helped"]]
        votes = Counter(p["recorded_action"] for p in helped)

        suggestions = []
        for action, n in votes.most_common(3):
            agreement = n / len(helped) if helped else 0
            if len(helped) >= CONFIDENCE_SOLID and agreement >= 0.6:
                band = "high"
            elif len(helped) >= CONFIDENCE_FLOOR:
                band = "medium"
            else:
                band = "low"
            evidence = [
                {
                    "when": datetime.fromtimestamp(p["epoch"], timezone.utc)
                                    .strftime("%Y-%m-%d %H:%M"),
                    "node": p["node"],
                    "helped": p["action_helped"],
                }
                for p in helped if p["recorded_action"] == action
            ][:4]
            suggestions.append({
                "action": action,
                "times_used": n,
                "agreement": round(agreement, 2),
                "confidence": band,
                "reason": (
                    f"Used {n} time{'s' if n > 1 else ''} on this signature; "
                    f"{int(agreement * 100)}% of prior resolutions that helped."
                ),
                "evidence": evidence,
            })

        abstained = not suggestions
        fallback = None
        if abstained:
            # Content-based fallback: no history to lean on, so reason from
            # the alert itself. In the full build this is the LLM path.
            fallback = {
                "note": "No precedent in the case base for this signature.",
                "component": r["component"],
                "suggestion": (
                    "Treat as novel. Capture what you do — this becomes the "
                    "first case for this signature."
                ),
            }

        score, why = rank_score(r, sig_history)

        out.append({
            "id": i,
            "ts": datetime.fromtimestamp(r["epoch"], timezone.utc)
                          .strftime("%Y-%m-%d %H:%M"),
            "epoch": r["epoch"],
            "node": r["node"],
            "component": r["component"],
            "level": r["level"],
            "category": r["label"],
            "content": r["content"],
            "signature": r["sig"],
            "priors": len(priors),
            "score": round(score, 2),
            "rank_reason": why,
            "suggestions": suggestions,
            "abstained": abstained,
            "fallback": fallback,
        })

        case_base[r["sig"]].append(i)
        sig_history[r["sig"]] += 1

    # Queue order: highest expected cost of delay first, newest breaking ties.
    ranked = sorted(out, key=lambda a: (-a["score"], -a["epoch"]))
    if limit:
        ranked = ranked[:limit]

    covered = sum(1 for a in out if not a["abstained"])
    stats = {
        "lines_ingested": len(rows),
        "alerts_extracted": len(alerts),
        "noise_suppressed": len(rows) - len(alerts),
        "distinct_signatures": len({a["signature"] for a in out}),
        "with_precedent": covered,
        "coverage": round(covered / len(out), 3) if out else 0,
        "span": (
            datetime.fromtimestamp(alerts[0]["epoch"], timezone.utc).strftime("%Y-%m-%d")
            + " to "
            + datetime.fromtimestamp(alerts[-1]["epoch"], timezone.utc).strftime("%Y-%m-%d")
        ) if alerts else "",
    }

    return {"stats": stats, "queue": ranked}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logfile")
    ap.add_argument("-o", "--out", default="triage_data.json")
    ap.add_argument("--limit", type=int, default=0, help="cap queue length (0 = all)")
    args = ap.parse_args()

    data = build(args.logfile, args.limit)
    with open(args.out, "w") as fh:
        json.dump(data, fh, indent=1)
    s = data["stats"]
    print(f"{s['alerts_extracted']} alerts from {s['lines_ingested']} lines, "
          f"{s['distinct_signatures']} signatures, coverage {s['coverage']:.1%}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
