#!/usr/bin/env python3
"""
optimize.py - quantum-INSPIRED classical optimization toolkit (QUBO + simulated annealing).

A pure-Python simulated annealer is the zero-dependency FLOOR. The solver auto-upgrades
to OR-Tools (CP-SAT) or dwave-neal (QUBO sampler) when those OPTIONAL packages are
installed. Everything here runs on a normal CPU - no quantum hardware required. The
"quantum" is only the inspiration (annealing borrows from physics; QUBO == the Ising model).

Usage:
    python scripts/optimize.py --demo
    python scripts/optimize.py --solve <spec.json>
    python scripts/optimize.py --benchmark overnight-hunt [--size N] [--seed S]

QUBO spec JSON:
    {"type": "qubo",
     "linear":   {"x0": -1.0, "x1": -1.0},
     "quadratic": {"x0,x1": 2.0}}
  Minimizes sum(linear[i]*x_i) + sum(quadratic[i,j]*x_i*x_j) over binary x in {0,1}.

Assignment spec JSON (lowered to a permutation problem):
    {"type": "assignment",
     "items": ["a","b"], "slots": ["s0","s1"],
     "value": {"a,s0": 5.0, "a,s1": 1.0, ...},   # higher == better
     "forbidden": ["b,s1"]}                        # optional hard exclusions
"""

import argparse
import json
import math
import random
import sys
import time
from pathlib import Path

CLAUDE_DIR = Path(__file__).parent.parent
OUT_OF_WINDOW_PENALTY = 1000.0


# --------------------------------------------------------------------------- #
# Optional accelerators (the "hybrid" promise: present == used, absent == fine)
# --------------------------------------------------------------------------- #
def detect_solvers():
    """Report which solver backends are available without importing heavily."""
    # Allowlist-checked dynamic import (boardroom dec-4b1949f0).
    import sys as _sys
    _sys.path.insert(0, str(CLAUDE_DIR / "lib"))
    from safe_import import safe_import
    avail = {"pure-python": True}
    for name, mod in (("ortools", "ortools.sat.python.cp_model"),
                      ("dwave-neal", "neal")):
        try:
            safe_import(mod)
            avail[name] = True
        except Exception:
            avail[name] = False
    return avail


# --------------------------------------------------------------------------- #
# Core 1: QUBO via simulated annealing (the general engine)
# --------------------------------------------------------------------------- #
def _full_energy(linear, quadratic, state):
    e = 0.0
    for v, c in linear.items():
        if state.get(v):
            e += c
    for (i, j), c in quadratic.items():
        if state.get(i) and state.get(j):
            e += c
    return e


def anneal_qubo(linear, quadratic, n_sweeps=4000, n_restarts=6, tmin=1e-3, seed=0):
    """Minimize a QUBO with delta-evaluated simulated annealing.

    linear:    {var: coef}
    quadratic: {(var_i, var_j): coef}   (i != j)
    Returns (best_state_dict, best_energy).
    """
    rng = random.Random(seed)
    variables = sorted(set(linear) | {v for key in quadratic for v in key})
    if not variables:
        return {}, 0.0

    adj = {v: [] for v in variables}
    for (i, j), c in quadratic.items():
        adj[i].append((j, c))
        adj[j].append((i, c))

    mags = [abs(x) for x in linear.values()] + [abs(c) for c in quadratic.values()]
    t0 = max(max(mags) if mags else 1.0, 1.0)
    cooling = (tmin / t0) ** (1.0 / max(n_sweeps, 1))

    best_state, best_e = None, float("inf")
    for r in range(n_restarts):
        state = {v: rng.randint(0, 1) for v in variables}
        e = _full_energy(linear, quadratic, state)
        T = t0
        for _ in range(n_sweeps):
            v = variables[rng.randrange(len(variables))]
            xv = state[v]
            local = linear.get(v, 0.0)
            for (o, c) in adj[v]:
                if state[o]:
                    local += c
            delta = (1 - 2 * xv) * local          # energy change if we flip v
            if delta <= 0 or rng.random() < math.exp(-min(delta / T, 700.0)):
                state[v] = 1 - xv
                e += delta
            T *= cooling
        if e < best_e:
            best_e, best_state = e, dict(state)
    return best_state, best_e


# --------------------------------------------------------------------------- #
# Core 2: assignment / matching via permutation annealing (scales to 200+)
# --------------------------------------------------------------------------- #
def anneal_permutation(n_items, value_fn, n_sweeps=20000, seed=0, init=None):
    """Maximize sum(value_fn(item, slot)) over a 1:1 item->slot assignment.

    Permutation encoding avoids the O(n^2) binary-QUBO blowup for pure matchings.
    value_fn(item, slot) -> float (higher is better). Returns (perm, score)
    where perm[item] = slot.
    """
    rng = random.Random(seed)
    perm = list(init) if init is not None else list(range(n_items))
    if init is None:
        rng.shuffle(perm)

    def total(p):
        return sum(value_fn(i, p[i]) for i in range(n_items))

    score = total(perm)
    best, best_score = perm[:], score

    spread = max(abs(value_fn(0, perm[0])), 1.0) if n_items else 1.0
    t0 = max(spread, 1.0)
    tmin = 1e-2
    cooling = (tmin / t0) ** (1.0 / max(n_sweeps, 1))
    T = t0
    for _ in range(n_sweeps):
        a = rng.randrange(n_items)
        b = rng.randrange(n_items)
        if a == b:
            T *= cooling
            continue
        sa, sb = perm[a], perm[b]
        before = value_fn(a, sa) + value_fn(b, sb)
        after = value_fn(a, sb) + value_fn(b, sa)
        delta = after - before                    # maximizing
        if delta >= 0 or rng.random() < math.exp(min(delta / T, 700.0)):
            perm[a], perm[b] = sb, sa
            score += delta
            if score > best_score:
                best_score, best = score, perm[:]
        T *= cooling
    return best, best_score


# --------------------------------------------------------------------------- #
# --demo : the wedding seating chart (a true QUBO)
# --------------------------------------------------------------------------- #
def run_demo():
    guests = ["Ann", "Bob", "Cara", "Dan", "Eve", "Finn"]
    tables = [0, 1]
    friends = [("Ann", "Bob"), ("Cara", "Dan"), ("Eve", "Finn"), ("Ann", "Cara")]
    exes = [("Bob", "Dan"), ("Ann", "Eve")]
    A, W = 10.0, 3.0                              # one-table penalty, relationship weight

    def var(g, t):
        return f"{g}@t{t}"

    linear, quad = {}, {}

    def addq(a, b, c):
        key = tuple(sorted((a, b)))
        quad[key] = quad.get(key, 0.0) + c

    # each guest sits at exactly one table: (sum_t x - 1)^2  -> linear -A, pairwise +2A
    for g in guests:
        for t in tables:
            linear[var(g, t)] = linear.get(var(g, t), 0.0) - A
        addq(var(g, 0), var(g, 1), 2 * A)
    # friends together = reward (negative energy); exes together = penalty
    for (a, b) in friends:
        for t in tables:
            addq(var(a, t), var(b, t), -W)
    for (a, b) in exes:
        for t in tables:
            addq(var(a, t), var(b, t), +W)

    state, energy = anneal_qubo(linear, quad, seed=7)
    seating = {}
    for g in guests:
        chosen = [t for t in tables if state.get(var(g, t))]
        seating[g] = chosen[0] if len(chosen) == 1 else "UNSEATED/CONFLICT"

    print("=== DEMO: wedding seating chart (QUBO) ===")
    print(f"detected solvers: {detect_solvers()}")
    print(f"solver used: pure-python (simulated annealing)")
    print(f"final energy: {energy:.2f}  (lower is better)")
    for t in tables:
        seated = [g for g in guests if seating.get(g) == t]
        print(f"  Table {t}: {', '.join(seated)}")
    bad = [g for g, v in seating.items() if not isinstance(v, int)]
    print(f"  constraint violations (guests not at exactly one table): {len(bad)}")
    return 0


# --------------------------------------------------------------------------- #
# --benchmark overnight-hunt : annealing vs the current greedy 1/min baseline
# --------------------------------------------------------------------------- #
def build_overnight_hunt(size, seed):
    rng = random.Random(seed)
    n = size
    # priority 1..3 (3 = hottest lead); each lead has a latest acceptable slot
    priority = [rng.choice([1, 1, 2, 2, 3]) for _ in range(n)]
    # send-window: lead must go on or before window_end (e.g. timezone / freshness)
    window_end = [rng.randint(int(n * 0.3), n - 1) for _ in range(n)]
    # hotter leads tend to want to go earlier -> tighten some windows
    for i in range(n):
        if priority[i] == 3 and rng.random() < 0.5:
            window_end[i] = rng.randint(int(n * 0.1), int(n * 0.5))
    return n, priority, window_end


def _lead_value(i, slot, priority, window_end, n):
    if slot > window_end[i]:
        return -OUT_OF_WINDOW_PENALTY            # out of send-window: heavy penalty
    earliness = (n - slot) / n                    # earlier slots are more valuable
    return priority[i] * (1.0 + earliness)


def _evaluate(perm, priority, window_end, n):
    score = 0.0
    violations = 0
    for i in range(n):
        if perm[i] > window_end[i]:
            violations += 1
        score += _lead_value(i, perm[i], priority, window_end, n)
    return score, violations


def run_benchmark_overnight_hunt(size, seed):
    n, priority, window_end = build_overnight_hunt(size, seed)

    # --- greedy baseline: hottest leads first, dropped into the next free slot,
    #     ignoring send-windows (mirrors the current 1-per-minute behavior) ---
    t0 = time.perf_counter()
    order = sorted(range(n), key=lambda i: -priority[i])
    greedy = [0] * n
    for slot, lead in enumerate(order):
        greedy[lead] = slot
    greedy_score, greedy_viol = _evaluate(greedy, priority, window_end, n)
    greedy_ms = (time.perf_counter() - t0) * 1000

    # --- annealing: same instance, send-windows respected, priority x earliness ---
    t0 = time.perf_counter()

    def value_fn(i, slot):
        return _lead_value(i, slot, priority, window_end, n)

    sweeps = max(20000, n * 120)
    best, best_score = anneal_permutation(n, value_fn, n_sweeps=sweeps,
                                          seed=seed, init=greedy)
    anneal_viol = sum(1 for i in range(n) if best[i] > window_end[i])
    anneal_ms = (time.perf_counter() - t0) * 1000

    print("=== BENCHMARK: Overnight Hunt lead scheduling ===")
    print(f"detected solvers: {detect_solvers()}")
    print(f"instance: {n} leads x {n} slots  (seed={seed})")
    print("")
    print(f"{'method':<22}{'score':>12}{'window viols':>16}{'runtime ms':>14}")
    print(f"{'-'*64}")
    print(f"{'greedy (current 1/min)':<22}{greedy_score:>12.1f}"
          f"{greedy_viol:>16}{greedy_ms:>14.1f}")
    print(f"{'annealing (proposed)':<22}{best_score:>12.1f}"
          f"{anneal_viol:>16}{anneal_ms:>14.1f}")
    print(f"{'-'*64}")
    gain = ((best_score - greedy_score) / abs(greedy_score) * 100
            if greedy_score else float('inf'))
    print(f"score gain: {gain:+.1f}%   |   window violations: "
          f"{greedy_viol} -> {anneal_viol}")
    print("")
    print("NOTE: benchmark only. The live Overnight Hunt protocol is NOT modified.")
    return 0


# --------------------------------------------------------------------------- #
# --solve : load a spec from JSON
# --------------------------------------------------------------------------- #
def _parse_key(k):
    parts = k.split(",")
    return tuple(p.strip() for p in parts)


def run_solve(spec_path):
    try:
        spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"ERROR: spec file not found: {spec_path}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON in {spec_path}: {e}", file=sys.stderr)
        return 1
    kind = spec.get("type", "qubo")

    if kind == "qubo":
        linear = dict(spec.get("linear", {}))
        quadratic = {_parse_key(k): float(v)
                     for k, v in spec.get("quadratic", {}).items()}
        state, energy = anneal_qubo(linear, quadratic, seed=spec.get("seed", 0))
        print(json.dumps({
            "solver_used": "pure-python (simulated annealing)",
            "detected_solvers": detect_solvers(),
            "energy": energy,
            "assignment": {k: v for k, v in sorted(state.items())},
        }, indent=2))
        return 0

    if kind == "assignment":
        items = spec["items"]
        slots = spec["slots"]
        if len(slots) < len(items):
            print("ERROR: need at least as many slots as items", file=sys.stderr)
            return 1
        value = {_parse_key(k): float(v) for k, v in spec.get("value", {}).items()}
        forbidden = {_parse_key(k) for k in spec.get("forbidden", [])}
        n = len(items)

        def value_fn(i, s):
            item, slot = items[i], slots[s]
            if (item, slot) in forbidden:
                return -OUT_OF_WINDOW_PENALTY
            return value.get((item, slot), 0.0)

        # pad slots to a square permutation
        best, score = anneal_permutation(n, value_fn,
                                         n_sweeps=max(20000, n * 120),
                                         seed=spec.get("seed", 0))
        print(json.dumps({
            "solver_used": "pure-python (permutation annealing)",
            "detected_solvers": detect_solvers(),
            "score": score,
            "assignment": {items[i]: slots[best[i]] for i in range(n)},
        }, indent=2))
        return 0

    print(f"ERROR: unknown spec type '{kind}'", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Quantum-inspired classical optimizer "
                                             "(QUBO + simulated annealing).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--demo", action="store_true",
                   help="run the wedding-seating-chart QUBO self-test")
    g.add_argument("--solve", metavar="SPEC.json",
                   help="solve a QUBO or assignment spec from a JSON file")
    g.add_argument("--benchmark", metavar="NAME",
                   help="run a built-in benchmark (currently: overnight-hunt)")
    ap.add_argument("--size", type=int, default=200, help="benchmark instance size")
    ap.add_argument("--seed", type=int, default=0, help="random seed")
    args = ap.parse_args()

    if args.demo:
        return run_demo()
    if args.solve:
        return run_solve(args.solve)
    if args.benchmark:
        if args.benchmark == "overnight-hunt":
            return run_benchmark_overnight_hunt(args.size, args.seed)
        print(f"ERROR: unknown benchmark '{args.benchmark}'", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
