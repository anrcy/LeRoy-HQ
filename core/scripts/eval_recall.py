#!/usr/bin/env python3
"""
eval_recall.py v1.0 -- retrieval QUALITY eval for the hybrid recall engine.

Why this exists:
  test-hybrid-recall.py answers "does the engine run?".
  This answers "does the engine return the RIGHT notes?" -- the number that was
  missing when we changed the vault (junk purge, promotion loop, reconcile).

Method:
  Reads memory/_eval/recall-goldset.jsonl -- hand-labeled {query -> expected paths}.
  Runs each query through hybrid_recall() (the SAME entry point production uses,
  imported in-process so the 55MB index + embedding model load ONCE, not per query).
  Reports recall@5, precision@1, MRR, and a per-difficulty split.

Metrics:
  recall@k     fraction of queries where ANY expected path landed in the top k
  precision@1  fraction of queries where the #1 hit was an expected path
  MRR          mean of 1/rank of the first expected hit (0 if not found at all)

Usage:
    python eval_recall.py                 # run, compare against previous baseline
    python eval_recall.py --baseline      # run and mark this result as the baseline
    python eval_recall.py --verbose       # per-query pass/fail detail
    python eval_recall.py --k 10          # change the recall@k cutoff (default 5)

Exit codes:
    0  ok (or improved)
    1  recall@5 regressed more than REGRESSION_TOLERANCE vs baseline
    2  harness error (goldset missing, engine import failed)

No external eval framework and no metered API calls -- stdlib + the local engine only.
"""

import argparse
import importlib.util
import io
import json
import statistics
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

# UTF-8 stdout: vault titles carry em-dashes and emoji; cp1252 would crash the run.
try:
    if not getattr(sys.stdout, "_utf8_wrapped", False):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stdout._utf8_wrapped = True
except Exception:
    pass

CLAUDE_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = CLAUDE_DIR / "scripts"
SESSION_DIR = CLAUDE_DIR / "session"
GOLDSET = CLAUDE_DIR / "memory" / "_eval" / "recall-goldset.jsonl"
BASELINE = SESSION_DIR / "recall-eval-baseline.json"

REGRESSION_TOLERANCE = 0.05   # >5% absolute drop in recall@k fails the gate
DEFAULT_K = 5


# ── Engine import ─────────────────────────────────────────────────────────────

def load_engine():
    """Import hybrid-recall-v7.py by path (the filename has dashes, so no plain
    import), then memoize its three loaders so the 55MB index is read ONCE
    instead of once per query."""
    path = SCRIPTS_DIR / "hybrid-recall-v7.py"
    if not path.exists():
        print(f"[ERROR] recall engine not found: {path}")
        return None
    spec = importlib.util.spec_from_file_location("hybrid_recall_v7", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        print(f"[ERROR] could not import recall engine: {e}")
        return None

    # The eval is an offline batch job: pay the encoder load once, in-process, so
    # every query gets true semantic scoring. The hot fallback path keeps it off.
    mod.ALLOW_MODEL_LOAD = True

    for fn_name in ("load_memory_index", "load_bm25_index", "load_memory_graph"):
        original = getattr(mod, fn_name, None)
        if original is None:
            continue
        setattr(mod, fn_name, _memoize(original))
    return mod


def _memoize(fn):
    cache = {}

    def wrapped(*a, **kw):
        if "v" not in cache:
            cache["v"] = fn(*a, **kw)
        return cache["v"]

    return wrapped


# ── Goldset ───────────────────────────────────────────────────────────────────

def load_goldset() -> list:
    if not GOLDSET.exists():
        print(f"[ERROR] goldset not found: {GOLDSET}")
        return []
    rows = []
    for i, line in enumerate(GOLDSET.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"[WARN] goldset line {i} is not valid JSON, skipped: {e}")
            continue
        if not row.get("query") or not row.get("expected"):
            print(f"[WARN] goldset line {i} missing query/expected, skipped")
            continue
        rows.append(row)
    return rows


def _norm(p: str) -> str:
    """Vault paths appear with either separator depending on writer; compare flat."""
    return (p or "").replace("\\", "/").strip().lower()


# ── Eval ──────────────────────────────────────────────────────────────────────

def evaluate(engine, goldset: list, k: int, verbose: bool) -> dict:
    results = []
    latencies = []

    for row in goldset:
        query = row["query"]
        expected = {_norm(p) for p in row["expected"]}

        t0 = time.time()
        try:
            out = engine.hybrid_recall(query, tier_1_count=max(k, 10))
            returned = [_norm(n.get("path", "")) for n in (out.get("notes") or [])]
            err = None
        except Exception as e:
            returned, err = [], str(e)
        latencies.append((time.time() - t0) * 1000)

        rank = 0
        for i, path in enumerate(returned, 1):
            if path in expected:
                rank = i
                break

        hit_at_k = 1 if (rank and rank <= k) else 0
        results.append({
            "id": row.get("id", ""),
            "query": query,
            "difficulty": row.get("difficulty", "unspecified"),
            "expected": sorted(expected),
            "rank": rank,
            "hit_at_k": hit_at_k,
            "top1": returned[0] if returned else None,
            "error": err,
        })

        if verbose:
            mark = "PASS" if hit_at_k else "FAIL"
            where = f"rank {rank}" if rank else "not in results"
            print(f"  [{mark}] {row.get('id','')}: {query[:52]:<52} {where}")
            if not hit_at_k:
                print(f"         expected {sorted(expected)[0]}")
                print(f"         got      {returned[0] if returned else '(nothing)'}")

    n = len(results) or 1
    recall_at_k = sum(r["hit_at_k"] for r in results) / n
    precision_at_1 = sum(1 for r in results if r["rank"] == 1) / n
    mrr = sum((1.0 / r["rank"]) if r["rank"] else 0.0 for r in results) / n

    by_difficulty = {}
    for r in results:
        d = r["difficulty"]
        by_difficulty.setdefault(d, []).append(r["hit_at_k"])
    by_difficulty = {
        d: {"n": len(v), f"recall_at_{k}": round(sum(v) / len(v), 4)}
        for d, v in sorted(by_difficulty.items())
    }

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "goldset_size": len(results),
        "k": k,
        f"recall_at_{k}": round(recall_at_k, 4),
        "precision_at_1": round(precision_at_1, 4),
        "mrr": round(mrr, 4),
        "median_latency_ms": round(statistics.median(latencies), 1) if latencies else None,
        "by_difficulty": by_difficulty,
        "errors": [r["id"] for r in results if r["error"]],
        "misses": [
            {"id": r["id"], "query": r["query"], "expected": r["expected"][0], "top1": r["top1"]}
            for r in results if not r["hit_at_k"]
        ],
    }


# ── Reporting ─────────────────────────────────────────────────────────────────

def report(current: dict, baseline: dict | None, k: int) -> int:
    key = f"recall_at_{k}"
    print()
    print("=" * 62)
    print(f"  RECALL EVAL — {current['goldset_size']} queries, k={k}")
    print("=" * 62)
    print(f"  recall@{k}      {current[key]:.1%}")
    print(f"  precision@1    {current['precision_at_1']:.1%}")
    print(f"  MRR            {current['mrr']:.3f}")
    print(f"  median latency {current['median_latency_ms']} ms")
    print()
    for d, v in current["by_difficulty"].items():
        print(f"    {d:<14} {v[f'recall_at_{k}']:.1%}  (n={v['n']})")

    if current["errors"]:
        print(f"\n  [WARN] {len(current['errors'])} queries errored: {current['errors']}")

    if current["misses"]:
        print(f"\n  MISSES ({len(current['misses'])}):")
        for m in current["misses"][:12]:
            print(f"    {m['id']}: {m['query'][:46]}")
            print(f"        want {m['expected']}")
            print(f"        got  {m['top1']}")
        if len(current["misses"]) > 12:
            print(f"    ... and {len(current['misses']) - 12} more")

    exit_code = 0
    if baseline and key in baseline:
        delta = current[key] - baseline[key]
        arrow = "improved" if delta > 0 else ("unchanged" if delta == 0 else "REGRESSED")
        print(f"\n  vs baseline ({baseline.get('ts','?')[:10]}): "
              f"{baseline[key]:.1%} -> {current[key]:.1%}  ({delta:+.1%}, {arrow})")
        if delta < -REGRESSION_TOLERANCE:
            print(f"  [FAIL] regression exceeds {REGRESSION_TOLERANCE:.0%} tolerance")
            exit_code = 1
    else:
        print("\n  (no baseline recorded — run with --baseline to set one)")

    print("=" * 62)
    return exit_code


def main() -> int:
    ap = argparse.ArgumentParser(description="Retrieval quality eval for hybrid recall")
    ap.add_argument("--baseline", action="store_true",
                    help="record this run as the comparison baseline")
    ap.add_argument("--verbose", action="store_true", help="per-query detail")
    ap.add_argument("--k", type=int, default=DEFAULT_K, help=f"recall@k cutoff (default {DEFAULT_K})")
    args = ap.parse_args()

    goldset = load_goldset()
    if not goldset:
        return 2

    engine = load_engine()
    if engine is None:
        return 2

    print(f"Running {len(goldset)} gold-set queries (loading index once)...")
    if args.verbose:
        print()

    current = evaluate(engine, goldset, args.k, args.verbose)

    baseline = None
    if BASELINE.exists():
        try:
            baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            baseline = None

    exit_code = report(current, baseline, args.k)

    out = SESSION_DIR / f"recall-eval-{date.today().isoformat()}.json"
    try:
        out.write_text(json.dumps(current, indent=2), encoding="utf-8")
        print(f"\nWrote {out}")
    except OSError as e:
        print(f"[WARN] could not write result file: {e}")

    if args.baseline:
        try:
            BASELINE.write_text(json.dumps(current, indent=2), encoding="utf-8")
            print(f"Baseline set: {BASELINE}")
        except OSError as e:
            print(f"[WARN] could not write baseline: {e}")
        exit_code = 0   # setting a baseline is never a regression

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
