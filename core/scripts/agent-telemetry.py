#!/usr/bin/env python3
"""
agent-telemetry.py — transcript-based agent utilization metrics.

WHY TRANSCRIPT-BASED
--------------------
Every pre-existing counter in this repo is untrustworthy (audited 2026-08-08):

  * hooks/gate-enforcer.py:2269-2274 increments `total` and `gate_generated`
    together on every run, so `rate_pct` is structurally pinned at 100.0%.
  * session/metrics.json -> gate_compliance reports 0.0% (gates_present 0 of
    6391) because response-monitor.py:98 only scans response[:200], and the
    Position #0 box pushes the anchor past that window.
  * session/agent-spawn-log.jsonl held 3 records against 750 real spawns,
    because agent-spawn-logger.py only accepted tool_name == "Task" while the
    tool is now named "Agent". (Fixed 2026-08-08.)

Two of those numbers contradict each other and all three are wrong. This script
therefore derives everything from the raw session transcripts, which are the only
ground truth available.

USAGE
    python scripts/agent-telemetry.py                 # summary to stdout
    python scripts/agent-telemetry.py --json          # machine-readable
    python scripts/agent-telemetry.py --write-metrics # -> metrics.json
    python scripts/agent-telemetry.py --baseline      # freeze reference numbers
    python scripts/agent-telemetry.py --check-baseline# regression check, exit 1 on drift
"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_ROOT = Path.home() / ".claude"
PROJECTS = CLAUDE_ROOT / "projects"
SESSION = CLAUDE_ROOT / "session"
AGENTS_DIR = CLAUDE_ROOT / "agents"
METRICS = SESSION / "metrics.json"
BASELINE = SESSION / "agent-utilization-baseline.json"
RETURNS = SESSION / "agent-returns.jsonl"

SUBAGENT_RE = re.compile(r'"subagent_type"\s*:\s*"([A-Za-z0-9_\-]+)"')
GATE_RE = re.compile(r"\[GATE\]")

# Tolerance when comparing against the frozen baseline (relative).
DRIFT_TOLERANCE = 0.02


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------

def load_roster() -> set:
    """Spawnable agents = .md files with YAML frontmatter containing `name:`.

    Excludes `index.md` (loads as `agents-index` but self-declares
    non-executable) and the 4 pure docs that have no frontmatter.
    """
    roster = set()
    if not AGENTS_DIR.is_dir():
        return roster
    for fn in os.listdir(AGENTS_DIR):
        if not fn.endswith(".md") or fn == "index.md":
            continue
        try:
            txt = (AGENTS_DIR / fn).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if txt.startswith("---") and re.search(r"^name:", txt[:3000], re.M):
            roster.add(fn[:-3])
    return roster


# ---------------------------------------------------------------------------
# Lane classification
# ---------------------------------------------------------------------------

def classify_lane(first_prompt: str, human_turns: int) -> str:
    """Four lanes. Machine lanes must never trigger department fan-out."""
    if first_prompt.startswith("[LEROY-AUTO]"):
        return "leroy_auto"
    if re.match(r"^You are (a|an|the)\b", first_prompt):
        return "pipeline"
    if human_turns >= 3:
        return "human"
    return "machine_oneshot"


def real_human_turns(path: Path):
    """Human-authored turns only.

    Tool results are logged as type=user, and hook output is injected as user
    text — both must be excluded or every session looks interactive.
    """
    turns = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if '"type":"user"' not in line and '"type": "user"' not in line:
                    continue
                if "toolUseResult" in line or "tool_result" in line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                content = obj.get("message", {}).get("content")
                if isinstance(content, list):
                    content = " ".join(
                        b.get("text", "") for b in content if isinstance(b, dict)
                    )
                if not isinstance(content, str) or not content.strip():
                    continue
                text = content.strip()
                if text.startswith("<") or "system-reminder" in text[:200]:
                    continue
                turns.append(text)
    except OSError:
        pass
    return turns


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def scan() -> dict:
    roster = load_roster()

    sessions_by_lane = Counter()
    spawned_sessions_by_lane = Counter()
    spawns_by_lane = Counter()
    spawns_by_agent = Counter()
    agents_by_lane = defaultdict(Counter)
    gate_sessions = 0
    gate_without_spawn = 0
    total_sessions = 0

    if PROJECTS.is_dir():
        for dirpath, _dirnames, filenames in os.walk(PROJECTS):
            # Only top-level project dirs hold main-session transcripts.
            if dirpath[len(str(PROJECTS)):].count(os.sep) > 1:
                continue
            for fn in filenames:
                # agent-*.jsonl are subagent transcripts, not sessions.
                if not fn.endswith(".jsonl") or fn.startswith("agent-"):
                    continue
                fp = Path(dirpath) / fn
                total_sessions += 1

                found, has_gate = [], False
                try:
                    with fp.open("r", encoding="utf-8", errors="ignore") as fh:
                        for line in fh:
                            found.extend(SUBAGENT_RE.findall(line))
                            if not has_gate and GATE_RE.search(line):
                                has_gate = True
                except OSError:
                    continue

                turns = real_human_turns(fp)
                lane = classify_lane(turns[0] if turns else "", len(turns))

                sessions_by_lane[lane] += 1
                if found:
                    spawned_sessions_by_lane[lane] += 1
                    spawns_by_lane[lane] += len(found)
                    for a in found:
                        spawns_by_agent[a] += 1
                        agents_by_lane[lane][a] += 1
                if has_gate:
                    gate_sessions += 1
                    if not found:
                        gate_without_spawn += 1

    # Nested spawns live in agent-*.jsonl (subagent transcripts), NOT in main
    # session transcripts. Scanning only main sessions makes every COO-deployed
    # department invisible — which is precisely what Phase 6 needs to measure.
    nested_by_agent = Counter()
    nested_sessions = 0
    if PROJECTS.is_dir():
        for dirpath, _dn, filenames in os.walk(PROJECTS):
            for fn in filenames:
                if not (fn.startswith("agent-") and fn.endswith(".jsonl")):
                    continue
                fp = Path(dirpath) / fn
                hits = []
                try:
                    with fp.open("r", encoding="utf-8", errors="ignore") as fh:
                        for line in fh:
                            hits.extend(SUBAGENT_RE.findall(line))
                except OSError:
                    continue
                if hits:
                    nested_sessions += 1
                    for a in hits:
                        nested_by_agent[a] += 1

    used = set(spawns_by_agent) | set(nested_by_agent)
    roster_used = roster & used
    spawn_rate = {
        lane: round(100.0 * spawned_sessions_by_lane.get(lane, 0) / n, 1)
        for lane, n in sessions_by_lane.items() if n
    }

    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "total_sessions": total_sessions,
        "sessions_by_lane": dict(sessions_by_lane),
        "sessions_with_spawns_by_lane": dict(spawned_sessions_by_lane),
        "spawn_rate_pct_by_lane": spawn_rate,
        "total_spawns": sum(spawns_by_agent.values()),
        "spawns_by_lane": dict(spawns_by_lane),
        "spawns_by_agent": dict(spawns_by_agent.most_common()),
        # Delegation depth: spawns issued BY a subagent (e.g. a COO deploying a
        # department). Counted separately so top-level and delegated fan-out
        # are never conflated.
        "nested_spawns": sum(nested_by_agent.values()),
        "nested_spawn_sessions": nested_sessions,
        "nested_spawns_by_agent": dict(nested_by_agent.most_common()),
        "agents_by_lane": {k: dict(v.most_common(10)) for k, v in agents_by_lane.items()},
        "roster_size": len(roster),
        "roster_used": len(roster_used),
        "roster_unused": len(roster - used),
        "roster_coverage_pct": round(100.0 * len(roster_used) / len(roster), 1) if roster else 0.0,
        "unused_agents": sorted(roster - used),
        "gate_sessions": gate_sessions,
        "gate_without_spawn": gate_without_spawn,
        "gate_truth_rate_pct": round(
            100.0 * (gate_sessions - gate_without_spawn) / gate_sessions, 1
        ) if gate_sessions else 0.0,
        "returns": return_stats(),
    }


def return_stats() -> dict:
    """Return-contract stats from the SubagentStop hook log."""
    if not RETURNS.exists():
        return {"observed": 0, "note": "no SubagentStop firings recorded yet"}
    counts, by_agent = Counter(), defaultdict(Counter)
    try:
        with RETURNS.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                counts[o.get("status", "UNKNOWN")] += 1
                by_agent[str(o.get("agent", "unknown"))][o.get("status", "UNKNOWN")] += 1
    except OSError:
        pass
    total = sum(counts.values())
    good = counts.get("RETURNED", 0)
    return {
        "observed": total,
        "by_status": dict(counts),
        "return_rate_pct": round(100.0 * good / total, 1) if total else 0.0,
        "empty_return_rate_pct": round(
            100.0 * counts.get("EMPTY_RETURN", 0) / total, 1
        ) if total else 0.0,
        "by_agent": {k: dict(v) for k, v in by_agent.items()},
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def atomic_write(path: Path, payload: dict) -> None:
    """Same technique as response-monitor.py's MetricsUpdater (mkstemp+move)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        shutil.move(tmp, str(path))
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_metrics(data: dict) -> None:
    existing = {}
    if METRICS.exists():
        for enc in ("utf-8", "utf-16", "utf-8-sig"):
            try:
                existing = json.loads(METRICS.read_text(encoding=enc))
                break
            except Exception:
                continue
    if not isinstance(existing, dict):
        existing = {}
    existing["agent_utilization"] = data
    atomic_write(METRICS, existing)


def summary(d: dict) -> str:
    L = []
    L.append("AGENT UTILIZATION — transcript-derived")
    L.append(f"  sessions scanned      : {d['total_sessions']}")
    L.append("  by lane (sessions | spawn rate):")
    for lane in ("human", "leroy_auto", "pipeline", "machine_oneshot"):
        n = d["sessions_by_lane"].get(lane, 0)
        r = d["spawn_rate_pct_by_lane"].get(lane, 0.0)
        L.append(f"      {lane:17} {n:6}   {r:5.1f}%")
    L.append(f"  total spawns          : {d['total_spawns']}  (top-level)")
    L.append(
        f"  nested spawns         : {d['nested_spawns']} "
        f"across {d['nested_spawn_sessions']} subagent runs  <- delegation depth"
    )
    L.append(
        f"  roster coverage       : {d['roster_used']}/{d['roster_size']} "
        f"({d['roster_coverage_pct']}%)  unused={d['roster_unused']}"
    )
    L.append(
        f"  gate truth rate       : {d['gate_truth_rate_pct']}%  "
        f"({d['gate_without_spawn']} of {d['gate_sessions']} gates spawned nothing)"
    )
    r = d["returns"]
    if r.get("observed"):
        L.append(
            f"  return contract       : {r['return_rate_pct']}% returned, "
            f"{r['empty_return_rate_pct']}% empty  (n={r['observed']})"
        )
    else:
        L.append("  return contract       : no SubagentStop firings recorded yet")
    top = list(d["spawns_by_agent"].items())[:8]
    if top:
        L.append("  top agents            : " + ", ".join(f"{k}={v}" for k, v in top))
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write-metrics", action="store_true")
    ap.add_argument("--baseline", action="store_true", help="freeze current numbers")
    ap.add_argument("--check-baseline", action="store_true", help="exit 1 on drift")
    args = ap.parse_args()

    data = scan()

    if args.baseline:
        atomic_write(BASELINE, data)
        print(f"baseline frozen -> {BASELINE}")

    if args.check_baseline:
        if not BASELINE.exists():
            print("no baseline to check against; run --baseline first")
            return 2
        base = json.loads(BASELINE.read_text(encoding="utf-8"))
        drifted = []
        for key in ("total_sessions", "total_spawns", "roster_used"):
            b, c = base.get(key, 0), data.get(key, 0)
            if b and abs(c - b) / b > DRIFT_TOLERANCE:
                drifted.append(f"{key}: baseline={b} current={c}")
        if drifted:
            print("BASELINE DRIFT:")
            for d_ in drifted:
                print("  " + d_)
            return 1
        print("baseline OK (within tolerance)")

    if args.write_metrics:
        write_metrics(data)
        print(f"agent_utilization written -> {METRICS}")

    print(json.dumps(data, indent=2) if args.json else summary(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
