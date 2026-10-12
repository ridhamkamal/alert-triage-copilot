#!/usr/bin/env python3
"""
Alert Triage Copilot — data analysis pipeline for the BGL log.

Answers the questions the 2k sample could not:
  1. Arrival profile  — alerts per hour, and how bursty they are.
                        The ranking half only matters if alerts cluster.
  2. Queue pressure   — how deep a backlog gets at a given service rate.
  3. Retrieval        — time-split evaluation: train on the past, test on
                        the future, with a per-signature breakdown so two
                        dominant signatures cannot flatter the aggregate.

Standard library only. Streams the file, so the full 4.7M-line BGL is fine.

Usage:
    python3 bgl_analysis.py BGL.log
    python3 bgl_analysis.py BGL.log --split 0.7 --service-rate 12 --out results/

BGL line format (whitespace separated, 10 fields):
    Label Timestamp Date Node Time NodeRepeat Type Component Level Content
Label is "-" for a non-alert line, otherwise an alert category (KERNDTLB, ...).
"""

import argparse
import csv
import json
import math
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Severity weighting.
#
# This is the cost asymmetry made explicit: a missed FATAL is not the same
# error as a missed INFO. These weights are a judgement call, not a
# measurement, and every number downstream that uses them inherits that.
# Change them here and the weighted metrics move; that is the point.
# --------------------------------------------------------------------------
SEVERITY_WEIGHT = {
    "FATAL": 10.0,
    "SEVERE": 8.0,
    "ERROR": 4.0,
    "WARNING": 2.0,
    "INFO": 1.0,
}
DEFAULT_WEIGHT = 1.0


# --------------------------------------------------------------------------
# Parsing and signature extraction
# --------------------------------------------------------------------------

_SUBS = [
    (re.compile(r"0x[0-9a-fA-F]+"), "<HEX>"),
    (re.compile(r"\b[0-9a-fA-F]{8,}\b"), "<HEX>"),
    (re.compile(r"\b\d+\.\d+\.\d+\.\d+\b"), "<IP>"),
    # Node identities. BGL uses several conventions and missing any one of
    # them splits a single recurring fault into many one-off "signatures",
    # which silently inflates the cold-start rate. Order matters: the most
    # specific patterns run first.
    (re.compile(r"\bR\d+-M\d+-N\d+[-\w:]*\b"), "<NODE>"),      # R02-M1-N0-C:J12-U11
    (re.compile(r"\bbgl[a-z]*\d+\b", re.IGNORECASE), "<NODE>"),  # bglio559, bglsn12
    (re.compile(r"\b[a-z]+\d+:[a-z]+\d+\b", re.IGNORECASE), "<NODE>"),
    # Filesystem paths: the mount point varies per node, the fault does not.
    (re.compile(r"(?<=\s)/[\w./-]+"), "<PATH>"),
    (re.compile(r"\b\d+\b"), "<N>"),
    (re.compile(r"\s+"), " "),
]


def signature(text):
    """Collapse a log line to its template — the recurring shape, not the instance.

    Deliberately simple regex normalisation rather than a learned parser
    (Drain, Spell). Swapping in a real parser is a known improvement; doing
    it now would make the baseline harder to reason about.
    """
    s = text
    for pattern, repl in _SUBS:
        s = pattern.sub(repl, s)
    return s.strip()


def parse(path):
    """Stream the log, yielding one dict per parsable line."""
    skipped = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.rstrip("\n").split(None, 9)
            if len(parts) < 10:
                skipped += 1
                continue
            try:
                epoch = int(parts[1])
            except ValueError:
                skipped += 1
                continue
            yield {
                "label": parts[0],
                "epoch": epoch,
                "node": parts[3],
                "component": parts[7],
                "level": parts[8],
                "content": parts[9],
                "sig": signature(parts[9]),
            }
    if skipped:
        print(f"  note: {skipped:,} lines could not be parsed and were dropped",
              file=sys.stderr)


# --------------------------------------------------------------------------
# 1. Arrival profile and burstiness
# --------------------------------------------------------------------------

def arrival_profile(alerts):
    """Per-hour arrival counts, and how far from steady they are.

    The ranking half of the product assumes alerts cluster. If arrivals are
    close to steady, triage order matters much less and the premise weakens.
    """
    per_hour = Counter(a["epoch"] // 3600 for a in alerts)
    if not per_hour:
        return None

    lo, hi = min(per_hour), max(per_hour)
    # Include empty hours: silence is part of the arrival shape.
    counts = [per_hour.get(h, 0) for h in range(lo, hi + 1)]

    mean = statistics.fmean(counts)
    var = statistics.pvariance(counts) if len(counts) > 1 else 0.0
    peak = max(counts)
    busy = sorted(counts, reverse=True)

    total = sum(counts)
    top1pct = max(1, len(counts) // 100)
    share_top1pct = sum(busy[:top1pct]) / total if total else 0.0

    return {
        "hours_observed": len(counts),
        "hours_with_no_alerts": sum(1 for c in counts if c == 0),
        "alerts_per_hour_mean": mean,
        "alerts_per_hour_median": statistics.median(counts),
        "alerts_per_hour_p95": busy[max(0, int(len(busy) * 0.05) - 1)] if busy else 0,
        "alerts_per_hour_peak": peak,
        # Fano factor: variance/mean. 1.0 means Poisson (random, steady-ish).
        # Much greater than 1 means bursty — which is what the product needs.
        "fano_factor": (var / mean) if mean else 0.0,
        "coefficient_of_variation": (math.sqrt(var) / mean) if mean else 0.0,
        "peak_to_median_ratio": (peak / statistics.median(counts))
                                 if statistics.median(counts) else float("inf"),
        "share_of_alerts_in_busiest_1pct_of_hours": share_top1pct,
        "_counts": counts,
        "_start_hour": lo,
    }


def queue_pressure(alerts, service_rate_per_hour):
    """Simulate a backlog: alerts arrive in bursts, an engineer clears at a
    fixed rate. Reports how deep the queue gets and how long it stays deep.

    Crude on purpose — it is a pressure gauge, not a staffing model. What it
    answers is whether a human ever faces a queue long enough that ordering
    it is worth doing.
    """
    per_hour = Counter(a["epoch"] // 3600 for a in alerts)
    if not per_hour:
        return None
    lo, hi = min(per_hour), max(per_hour)

    depth = 0.0
    depths = []
    for h in range(lo, hi + 1):
        depth = max(0.0, depth + per_hour.get(h, 0) - service_rate_per_hour)
        depths.append(depth)

    nonzero = [d for d in depths if d > 0]
    return {
        "service_rate_per_hour": service_rate_per_hour,
        "max_queue_depth": max(depths),
        "mean_queue_depth": statistics.fmean(depths),
        "hours_with_backlog": len(nonzero),
        "share_of_hours_with_backlog": len(nonzero) / len(depths) if depths else 0.0,
        "hours_backlog_over_20": sum(1 for d in depths if d > 20),
    }


# --------------------------------------------------------------------------
# 2. Synthetic remediation history
# --------------------------------------------------------------------------
#
# SYNTHETIC. No public dataset records what an engineer did in response to an
# alert, and that is precisely the retrieval layer's training signal. This
# generates one from a deterministic rule set plus noise.
#
# It is labelled synthetic everywhere it surfaces. Any accuracy figure
# measured against it says the model learned THIS GENERATOR, not that it
# would help a real engineer. Treat it as a harness, never as evidence.
# --------------------------------------------------------------------------

ACTION_POOL = [
    "restart affected node",
    "drain and reboot node",
    "replace memory module",
    "reseat link cable",
    "clear and reinitialise cache",
    "fail over to standby",
    "reset service processor",
    "escalate to hardware vendor",
    "no action — transient, self-cleared",
    "patch kernel and reschedule job",
]

# Noise rates. Real remediation history is messy: engineers disagree, first
# attempts fail, and some recorded actions did not help.
P_WRONG_FIRST_ATTEMPT = 0.18
P_ENGINEER_DISAGREES = 0.22
P_ACTION_DID_NOT_HELP = 0.15


def _stable_hash(text, salt=0):
    """Deterministic hash so runs are reproducible without seeding global state."""
    h = 1469598103934665603 ^ salt
    for ch in text.encode("utf-8"):
        h = ((h ^ ch) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h


def _unit(text, salt):
    return (_stable_hash(text, salt) % 10_000) / 10_000.0


def synthesise_remediations(alerts):
    """Attach a synthetic remediation record to each alert.

    Deterministic given the input, so the evaluation is reproducible.
    """
    # Each signature gets a canonical "correct" action.
    canonical = {}
    for a in alerts:
        if a["sig"] not in canonical:
            idx = _stable_hash(a["sig"]) % len(ACTION_POOL)
            canonical[a["sig"]] = ACTION_POOL[idx]

    records = []
    for i, a in enumerate(alerts):
        key = f"{a['sig']}|{i}"
        correct = canonical[a["sig"]]
        action = correct

        if _unit(key, 1) < P_ENGINEER_DISAGREES:
            alt = _stable_hash(key, 2) % len(ACTION_POOL)
            action = ACTION_POOL[alt]

        first_attempt_failed = _unit(key, 3) < P_WRONG_FIRST_ATTEMPT
        helped = _unit(key, 4) >= P_ACTION_DID_NOT_HELP

        records.append({
            **a,
            "canonical_action": correct,
            "recorded_action": action,
            "first_attempt_failed": first_attempt_failed,
            "action_helped": helped,
            "synthetic": True,
        })
    return records, canonical


# --------------------------------------------------------------------------
# 3. Retrieval evaluation — time split, per signature
# --------------------------------------------------------------------------

def evaluate_retrieval(records, split=0.7):
    """Train on the past, test on the future. Never random split: a random
    split lets the model see later incidents when predicting earlier ones,
    which is not a situation the product is ever in.

    Baseline retrieval: exact signature match against the case base, and the
    most common recorded action for that signature wins. Deliberately the
    simplest thing that works, so later embedding-based retrieval has an
    honest floor to beat.
    """
    records = sorted(records, key=lambda r: r["epoch"])
    cut = int(len(records) * split)
    train, test = records[:cut], records[cut:]
    if not test:
        return None

    # Case base: signature -> recorded actions that helped.
    case_base = defaultdict(Counter)
    for r in train:
        if r["action_helped"]:
            case_base[r["sig"]][r["recorded_action"]] += 1

    per_sig = defaultdict(lambda: {"n": 0, "covered": 0, "top1_correct": 0,
                                   "weight": 0.0, "weighted_covered": 0.0})
    covered = top1_correct = 0
    weighted_total = weighted_covered = 0.0

    for r in test:
        w = SEVERITY_WEIGHT.get(r["level"], DEFAULT_WEIGHT)
        s = per_sig[r["sig"]]
        s["n"] += 1
        s["weight"] += w
        weighted_total += w

        candidates = case_base.get(r["sig"])
        if candidates:
            covered += 1
            weighted_covered += w
            s["covered"] += 1
            s["weighted_covered"] += w
            if candidates.most_common(1)[0][0] == r["canonical_action"]:
                top1_correct += 1
                s["top1_correct"] += 1

    n = len(test)
    return {
        "train_size": len(train),
        "test_size": n,
        "split_at": datetime.fromtimestamp(test[0]["epoch"], timezone.utc).isoformat(),
        "coverage": covered / n,
        "coverage_severity_weighted": (weighted_covered / weighted_total)
                                       if weighted_total else 0.0,
        "top1_accuracy_overall": top1_correct / n,
        "top1_accuracy_when_covered": (top1_correct / covered) if covered else 0.0,
        "cold_start_rate": 1 - (covered / n),
        "distinct_signatures_in_test": len(per_sig),
        "signatures_unseen_in_train": sum(1 for sig in per_sig
                                          if sig not in case_base),
        "_per_signature": per_sig,
    }


def concentration(records):
    """How much of the volume sits in the top few signatures.

    If two signatures are most of the traffic, an aggregate accuracy number
    mostly measures performance on those two. This is the check against
    fooling yourself.
    """
    counts = Counter(r["sig"] for r in records)
    total = sum(counts.values())
    ranked = counts.most_common()
    cum = 0
    marks = {}
    for i, (_, c) in enumerate(ranked, 1):
        cum += c
        if i in (1, 2, 5, 10, 20):
            marks[f"top_{i}_share"] = cum / total
    return {
        "distinct_signatures": len(counts),
        "total_events": total,
        "singletons": sum(1 for c in counts.values() if c == 1),
        **marks,
        "_ranked": ranked,
    }


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def hr(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def pct(x):
    return f"{x:.1%}"


def report(path, split, service_rate, outdir):
    print(f"Reading {path} ...", file=sys.stderr)
    rows = list(parse(path))
    if not rows:
        sys.exit("No parsable lines found — check the file format.")

    alerts = [r for r in rows if r["label"] != "-"]
    start = datetime.fromtimestamp(min(r["epoch"] for r in rows), timezone.utc)
    end = datetime.fromtimestamp(max(r["epoch"] for r in rows), timezone.utc)
    days = max((end - start).total_seconds() / 86400, 1e-9)

    hr("Corpus")
    print(f"  Lines parsed              {len(rows):>12,}")
    print(f"  Labelled alerts           {len(alerts):>12,}   ({pct(len(alerts)/len(rows))})")
    print(f"  Non-alert noise           {len(rows)-len(alerts):>12,}   ({pct(1-len(alerts)/len(rows))})")
    print(f"  Span                      {start:%Y-%m-%d} to {end:%Y-%m-%d}  ({days:.1f} days)")
    print(f"  Alerts per day (mean)     {len(alerts)/days:>12,.0f}")
    print()
    print("  Severity mix (all lines):")
    for lvl, c in Counter(r["level"] for r in rows).most_common():
        print(f"     {c:>10,}  {lvl}")

    hr("1. Arrival profile — is it bursty enough for ranking to matter?")
    prof = arrival_profile(alerts)
    if prof:
        print(f"  Hours observed            {prof['hours_observed']:>12,}")
        print(f"  Hours with zero alerts    {prof['hours_with_no_alerts']:>12,}   ({pct(prof['hours_with_no_alerts']/prof['hours_observed'])})")
        print(f"  Alerts/hour  median       {prof['alerts_per_hour_median']:>12,.0f}")
        print(f"               mean         {prof['alerts_per_hour_mean']:>12,.1f}")
        print(f"               p95          {prof['alerts_per_hour_p95']:>12,.0f}")
        print(f"               peak         {prof['alerts_per_hour_peak']:>12,.0f}")
        print()
        print(f"  Fano factor (var/mean)    {prof['fano_factor']:>12,.1f}")
        print(f"  Coefficient of variation  {prof['coefficient_of_variation']:>12,.2f}")
        print(f"  Peak / median ratio       {prof['peak_to_median_ratio']:>12,.0f}")
        print(f"  Alerts in busiest 1% hrs  {pct(prof['share_of_alerts_in_busiest_1pct_of_hours']):>12}")
        print()
        f = prof["fano_factor"]
        if f > 10:
            print("  READ: heavily bursty. Arrivals cluster hard, so queue ordering is")
            print("        a real problem and the ranking half is justified.")
        elif f > 2:
            print("  READ: moderately bursty. Ranking helps during peaks; the value")
            print("        case rests on the peaks, not the average hour.")
        else:
            print("  READ: close to steady (Fano near 1 is random/Poisson). Ranking is")
            print("        WEAKER than the brief assumes — revisit the premise before")
            print("        building it. Retrieval is unaffected by this.")

    hr(f"2. Queue pressure at {service_rate} alerts/hour cleared")
    qp = queue_pressure(alerts, service_rate)
    if qp:
        print(f"  Max backlog depth         {qp['max_queue_depth']:>12,.0f}")
        print(f"  Mean backlog depth        {qp['mean_queue_depth']:>12,.1f}")
        print(f"  Hours with any backlog    {qp['hours_with_backlog']:>12,}   ({pct(qp['share_of_hours_with_backlog'])})")
        print(f"  Hours with backlog > 20   {qp['hours_backlog_over_20']:>12,}")
        print()
        print("  NOTE: a fixed service rate is a crude model. It answers one question")
        print("        only — does a human ever face a queue worth ordering?")

    hr("3. Signature concentration — can the aggregate flatter us?")
    conc = concentration(alerts)
    print(f"  Distinct signatures       {conc['distinct_signatures']:>12,}")
    print(f"  Seen exactly once         {conc['singletons']:>12,}   ({pct(conc['singletons']/conc['distinct_signatures'])} of signatures)")
    for k in ("top_1_share", "top_2_share", "top_5_share", "top_10_share", "top_20_share"):
        if k in conc:
            print(f"  {k.replace('_', ' ').title():<25} {pct(conc[k]):>12}")
    print()
    print("  Largest signatures:")
    for sig, c in conc["_ranked"][:8]:
        print(f"     {c:>9,}  {sig[:58]}")
    if conc.get("top_2_share", 0) > 0.5:
        print()
        print("  READ: over half the volume sits in two signatures. Report per-signature")
        print("        performance; an aggregate number here is mostly measuring those two.")

    hr(f"4. Retrieval — time split at {split:.0%}, synthetic remediation history")
    print("  WARNING: remediation records are SYNTHETIC (no public dataset has them).")
    print("           Accuracy below measures the generator, not real usefulness.")
    print("           Coverage and cold-start rate come from the REAL data and are")
    print("           the numbers worth quoting.")
    print()
    records, _ = synthesise_remediations(alerts)
    ev = evaluate_retrieval(records, split)
    if ev:
        print(f"  Train / test              {ev['train_size']:,} / {ev['test_size']:,}")
        print(f"  Split at                  {ev['split_at']}")
        print()
        print(f"  Coverage (REAL)           {pct(ev['coverage']):>12}")
        print(f"  Coverage, severity-wtd    {pct(ev['coverage_severity_weighted']):>12}")
        print(f"  Cold-start rate (REAL)    {pct(ev['cold_start_rate']):>12}")
        print(f"  Signatures new in test    {ev['signatures_unseen_in_train']:>12,} of {ev['distinct_signatures_in_test']:,}")
        print()
        print(f"  Top-1 accuracy, overall   {pct(ev['top1_accuracy_overall']):>12}   (synthetic)")
        print(f"  Top-1 when covered        {pct(ev['top1_accuracy_when_covered']):>12}   (synthetic)")
        print()
        if ev["coverage_severity_weighted"] < ev["coverage"]:
            print("  READ: severity-weighted coverage is LOWER than raw coverage — the")
            print("        alerts that matter most are the ones with least precedent.")
            print("        This is the cold-start problem landing exactly where it hurts.")
        print()
        print("  Per-signature breakdown (test set, by volume):")
        per = sorted(ev["_per_signature"].items(), key=lambda kv: -kv[1]["n"])
        print(f"     {'n':>7}  {'cover':>6}  {'top1':>6}  signature")
        for sig, s in per[:12]:
            cov = s["covered"] / s["n"] if s["n"] else 0
            t1 = s["top1_correct"] / s["n"] if s["n"] else 0
            print(f"     {s['n']:>7,}  {cov:>6.0%}  {t1:>6.0%}  {sig[:44]}")

    # ---------------------------------------------------------------- output
    if outdir:
        os.makedirs(outdir, exist_ok=True)

        summary = {
            "corpus": {
                "lines": len(rows), "alerts": len(alerts),
                "span_days": days, "start": start.isoformat(), "end": end.isoformat(),
            },
            "arrival": {k: v for k, v in (prof or {}).items() if not k.startswith("_")},
            "queue_pressure": qp,
            "concentration": {k: v for k, v in conc.items() if not k.startswith("_")},
            "retrieval": {k: v for k, v in (ev or {}).items() if not k.startswith("_")},
            "caveats": [
                "Remediation history is synthetic; accuracy measures the generator.",
                "Coverage and cold-start rate derive from real BGL data.",
                "Severity weights are a judgement call, set in SEVERITY_WEIGHT.",
                "BGL is supercomputer RAS data, not network telemetry.",
            ],
        }
        with open(os.path.join(outdir, "summary.json"), "w") as fh:
            json.dump(summary, fh, indent=2, default=str)

        if prof:
            with open(os.path.join(outdir, "hourly_arrivals.csv"), "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["hour_index", "utc_hour_start", "alert_count"])
                for i, c in enumerate(prof["_counts"]):
                    ts = datetime.fromtimestamp((prof["_start_hour"] + i) * 3600, timezone.utc)
                    w.writerow([i, ts.isoformat(), c])

        if ev:
            with open(os.path.join(outdir, "per_signature.csv"), "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["signature", "test_n", "coverage", "top1_accuracy_synthetic"])
                for sig, s in sorted(ev["_per_signature"].items(), key=lambda kv: -kv[1]["n"]):
                    w.writerow([sig, s["n"],
                                round(s["covered"] / s["n"], 4) if s["n"] else 0,
                                round(s["top1_correct"] / s["n"], 4) if s["n"] else 0])

        print()
        print(f"Wrote summary.json, hourly_arrivals.csv, per_signature.csv to {outdir}/")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logfile", help="path to BGL.log (or any slice of it)")
    ap.add_argument("--split", type=float, default=0.7,
                    help="chronological train fraction (default 0.7)")
    ap.add_argument("--service-rate", type=float, default=12,
                    help="alerts an engineer clears per hour, for the queue model")
    ap.add_argument("--out", default="results",
                    help="directory for CSV/JSON output ('' to skip)")
    args = ap.parse_args()

    if not os.path.exists(args.logfile):
        sys.exit(f"File not found: {args.logfile}")

    report(args.logfile, args.split, args.service_rate, args.out or None)


if __name__ == "__main__":
    main()
