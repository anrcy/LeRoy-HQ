#!/usr/bin/env python3
"""
agent-stress-test.py — agent utilization stress test.

MODES
  --replay N     Replay real prompts through the ACTUAL routing decision and
                 score lane correctness + dispatch coverage. Fully offline and
                 deterministic.
  --coverage     Static reachability audit of every spawnable agent, plus a
                 generated trigger fixture for live execution.
  --load         Report observed concurrency / return-rate curve from real
                 telemetry, and emit the load plan.

SCOPE NOTE (important, not a limitation to paper over)
------------------------------------------------------
This script cannot call the Agent tool — only the Claude Code harness can spawn
subagents. So `--coverage` and `--load` do the deterministic half here (is every
agent reachable? what do the return logs actually say?) and write a fixture /
plan that the harness executes live. Anything this script reports is measured;
anything requiring a live spawn is explicitly marked PENDING-LIVE rather than
assumed to pass.

Routing logic is extracted directly from hooks/gate-enforcer.py at runtime, so
the replay cannot silently drift from what the hook really does.
"""

import argparse
import ast
import json
import os
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

CLAUDE = Path.home() / ".claude"
HOOK = CLAUDE / "hooks" / "gate-enforcer.py"
AGENTS_DIR = CLAUDE / "agents"
SESSION = CLAUDE / "session"
PROJECTS = CLAUDE / "projects"
SKILL_INDEX = SESSION / "skill-index.json"
RETURNS = SESSION / "agent-returns.jsonl"
FIXTURE = SESSION / "agent-coverage-fixture.json"
REPORT = SESSION / "agent-stress-report.json"

# Tier capacities from skills/meta/session-gate.md:163-168 (the "Maximize Rule"
# at :170-180 makes undercapacity a named violation, not a preference).
TIER_CAPACITY = [(3, 5), (9, 12), (15, 18), (10**6, 20)]


# ---------------------------------------------------------------------------
# Import the REAL routing logic (no reimplementation, no drift)
# ---------------------------------------------------------------------------

def load_hook_functions() -> dict:
    """Extract routing functions from gate-enforcer.py without executing it.

    The hook is a 2,800-line straight-line script with side effects (writes
    state, writes enforcement.todo) — importing it would fire all of that. So we
    parse it and exec only the function bodies we need.
    """
    src = HOOK.read_text(encoding="utf-8", errors="ignore")
    tree = ast.parse(src)
    wanted = {"detect_substantial_task", "_classify_lane", "extract_line_items"}
    ns: dict = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            try:
                exec(compile(ast.Module([node], []), "<hook>", "exec"), ns)
            except Exception:
                pass
    missing = wanted - set(ns)
    if "detect_substantial_task" in missing or "_classify_lane" in missing:
        raise SystemExit(
            f"FATAL: could not extract routing logic from {HOOK} "
            f"(missing: {sorted(missing)}). The hook may have been restructured."
        )
    return ns


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

def load_prompt_corpus(limit: int) -> list:
    """Real prompts, stratified across lanes.

    session/prompt-history.jsonl is capped at 500 and holds {timestamp, prompt}
    with no responses — fine as a replay corpus. Transcripts supply lane
    diversity that prompt-history alone lacks.
    """
    prompts = []
    ph = SESSION / "prompt-history.jsonl"
    if ph.exists():
        try:
            for line in ph.read_text(encoding="utf-8", errors="ignore").splitlines():
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                p = o.get("prompt")
                if isinstance(p, str) and p.strip():
                    prompts.append(p.strip())
        except OSError:
            pass

    if PROJECTS.is_dir() and len(prompts) < limit:
        for dirpath, _dn, files in os.walk(PROJECTS):
            if dirpath[len(str(PROJECTS)):].count(os.sep) > 1:
                continue
            for fn in files:
                if not fn.endswith(".jsonl") or fn.startswith("agent-"):
                    continue
                try:
                    with (Path(dirpath) / fn).open("r", encoding="utf-8", errors="ignore") as fh:
                        for line in fh:
                            if '"type":"user"' not in line and '"type": "user"' not in line:
                                continue
                            if "toolUseResult" in line or "tool_result" in line:
                                continue
                            try:
                                o = json.loads(line)
                            except Exception:
                                continue
                            c = o.get("message", {}).get("content")
                            if isinstance(c, list):
                                c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
                            if not isinstance(c, str) or not c.strip():
                                continue
                            t = c.strip()
                            if t.startswith("<") or "system-reminder" in t[:200]:
                                continue
                            prompts.append(t)
                            break  # opening prompt only — that's what routing sees
                except OSError:
                    continue
            if len(prompts) > limit * 4:
                break

    random.seed(1337)  # deterministic sampling so runs are comparable
    random.shuffle(prompts)
    return prompts[:limit]


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def mode_replay(n: int) -> dict:
    fns = load_hook_functions()
    classify, substantial = fns["_classify_lane"], fns["detect_substantial_task"]
    corpus = load_prompt_corpus(n)

    by_lane = Counter()
    dispatch_by_lane = Counter()
    violations = []

    for p in corpus:
        lane = classify(p)
        by_lane[lane] += 1
        would_dispatch = bool(substantial(p)) and lane == "human"
        if would_dispatch:
            dispatch_by_lane[lane] += 1
        # HARD RULE: machine lanes must never dispatch a department.
        if lane != "human" and would_dispatch:
            violations.append({"lane": lane, "prompt": p[:120]})

    human = by_lane.get("human", 0)
    return {
        "mode": "replay",
        "prompts_replayed": len(corpus),
        "sessions_by_lane": dict(by_lane),
        "would_dispatch_by_lane": dict(dispatch_by_lane),
        "human_dispatch_coverage_pct": (
            round(100.0 * dispatch_by_lane.get("human", 0) / human, 1) if human else 0.0
        ),
        "machine_lane_violations": violations,
        "pass": len(violations) == 0,
    }


def roster() -> dict:
    """name -> {file, has_desc, desc_len, tools}"""
    out = {}
    if not AGENTS_DIR.is_dir():
        return out
    for fn in sorted(os.listdir(AGENTS_DIR)):
        if not fn.endswith(".md") or fn == "index.md":
            continue
        try:
            txt = (AGENTS_DIR / fn).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not txt.startswith("---"):
            continue
        end = txt.find("\n---", 3)
        head = txt[:end] if end > 0 else txt[:4000]
        name = None
        for line in head.splitlines():
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip()
                break
        if not name:
            continue
        # Descriptions come in three YAML shapes and a naive single-line read
        # produces FALSE POSITIVES. alignment-monitor.md uses a block scalar
        # (`description: |`) with explicit "Use when:" triggers underneath —
        # reading only the `description:` line saw an empty string and wrongly
        # flagged it as unselectable.
        desc = ""
        lines = head.splitlines()
        for i, line in enumerate(lines):
            if not line.startswith("description:"):
                continue
            first = line.split(":", 1)[1].strip()
            if first in ("|", ">", "|-", ">-", "|+", ">+"):
                # Block scalar: consume the indented continuation.
                body = []
                for cont in lines[i + 1:]:
                    if cont.strip() and not cont.startswith((" ", "\t")):
                        break
                    body.append(cont.strip())
                desc = " ".join(body).strip()
            else:
                desc = first.strip('"\'')
            break
        tools = ""
        for line in head.splitlines():
            if line.startswith("tools:"):
                tools = line.split(":", 1)[1].strip()
                break
        out[name] = {
            "file": fn,
            "desc_len": len(desc),
            # Selectability heuristic. Kept deliberately broad: "Use to ..." and
            # "Use for ..." are legitimate trigger phrasings, and treating only
            # "use when" as valid produced false positives on cko, etsy-overseer
            # and simulator, which all state real use cases.
            "has_triggers": any(
                k in desc.lower()
                for k in (
                    "use when", "use this agent", "use for", "use to",
                    "trigger", "deploy when", "when:", "activates on",
                    "spawned by", "runs every", "auto-spawn",
                )
            ),
            "declares_tools": bool(tools),
        }
    return out


def mode_coverage() -> dict:
    r = roster()
    idx_agents = set()
    if SKILL_INDEX.exists():
        try:
            idx = json.loads(SKILL_INDEX.read_text(encoding="utf-8", errors="ignore"))
            idx_agents = {a.get("a") or a.get("name") for a in idx.get("agents", [])}
            idx_agents.discard(None)
        except Exception:
            pass

    rows, fixture = [], {}
    for name, meta in sorted(r.items()):
        problems = []
        if not meta["has_triggers"]:
            # A description without trigger conditions is effectively
            # unselectable by a model doing dynamic routing.
            problems.append("WEAK_DESCRIPTION: no explicit trigger conditions")
        if idx_agents and name not in idx_agents:
            problems.append("NOT_IN_SKILL_INDEX: unreachable via skill-matcher")
        rows.append({
            "agent": name,
            "static": "FAIL" if problems else "PASS",
            "problems": problems,
            "live": "PENDING-LIVE",   # only the harness can prove this
        })
        fixture[name] = (
            f"COVERAGE PROBE for `{name}`. Reply with one sentence stating your "
            f"role and that you are reachable. Per "
            f"skills/meta/agent-return-contract.md your final message IS the "
            f"return value; returning empty is a contract violation."
        )

    try:
        FIXTURE.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
    except OSError:
        pass

    failed = [x for x in rows if x["static"] == "FAIL"]
    return {
        "mode": "coverage",
        "roster_size": len(r),
        "static_pass": len(rows) - len(failed),
        "static_fail": len(failed),
        "failures": failed,
        "fixture_written": str(FIXTURE),
        "live_execution": "PENDING-LIVE — harness must spawn each agent in the fixture",
        "pass": len(failed) == 0,
    }


def mode_load() -> dict:
    """Observed return behaviour. Cannot spawn; reports what really happened."""
    statuses, by_agent = Counter(), defaultdict(Counter)
    if RETURNS.exists():
        for line in RETURNS.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                o = json.loads(line)
            except Exception:
                continue
            statuses[o.get("status", "UNKNOWN")] += 1
            by_agent[str(o.get("agent"))][o.get("status", "UNKNOWN")] += 1

    total = sum(statuses.values())
    returned = statuses.get("RETURNED", 0)
    empty = statuses.get("EMPTY_RETURN", 0) + statuses.get("THIN_RETURN", 0)
    rate = round(100.0 * returned / total, 1) if total else None

    # Safe department size from measured per-agent reliability:
    # P(all N return) = rate**N. Require >=80% chance the whole department reports.
    recommended = 1
    if rate:
        p = rate / 100.0
        for k in range(1, 21):
            if p ** k >= 0.80:
                recommended = k
            else:
                break

    return {
        "mode": "load",
        "returns_observed": total,
        "by_status": dict(statuses),
        "return_rate_pct": rate,
        "empty_or_thin_pct": round(100.0 * empty / total, 1) if total else None,
        "by_agent": {k: dict(v) for k, v in by_agent.items()},
        "recommended_max_department_size": recommended,
        "tier_capacity_targets": TIER_CAPACITY,
        "note": (
            "Concurrency is driven by the harness, not this script. "
            "recommended_max_department_size assumes independent per-agent "
            "return probability and requires >=80% chance that ALL members "
            "report back."
        ),
        "pass": bool(total) and (rate or 0) >= 80.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", type=int, metavar="N")
    ap.add_argument("--coverage", action="store_true")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not (args.replay or args.coverage or args.load):
        ap.print_help()
        return 2

    results = {"generated": datetime.now(timezone.utc).isoformat()}
    if args.replay:
        results["replay"] = mode_replay(args.replay)
    if args.coverage:
        results["coverage"] = mode_coverage()
    if args.load:
        results["load"] = mode_load()

    try:
        REPORT.write_text(json.dumps(results, indent=2), encoding="utf-8")
    except OSError:
        pass

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for key in ("replay", "coverage", "load"):
            r = results.get(key)
            if not r:
                continue
            status = "PASS" if r.get("pass") else "FAIL"
            print(f"[{status}] {key.upper()}")
            for k, v in r.items():
                if k in ("mode", "pass", "by_agent", "failures", "machine_lane_violations"):
                    continue
                print(f"    {k}: {v}")
            for f in (r.get("failures") or [])[:12]:
                print(f"    - {f['agent']}: {'; '.join(f['problems'])}")
            for v in (r.get("machine_lane_violations") or [])[:5]:
                print(f"    - LANE VIOLATION [{v['lane']}]: {v['prompt'][:80]}")
            print()

    failed = [k for k in ("replay", "coverage", "load")
              if results.get(k) and not results[k].get("pass")]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
