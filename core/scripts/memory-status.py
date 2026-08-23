#!/usr/bin/env python3
"""memory-status.py — the ONE sanctioned answer to "is the memory system healthy?"

Read-only by construction: audits receipts (logs/receipts/*.jsonl) plus ground-
truth artifacts directly. Liveness = artifact freshness + content evidence —
claims in registries, agent specs, or doctrine prose count for nothing.
Exit code nonzero if anything is red. (Memory-system overhaul Phase 2,
2026-08-21; anti-theater contract rules 1-5.)

Usage:
    python memory-status.py            # human table
    python memory-status.py --json     # machine output (for heartbeat/HUD)
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import memory_policy as policy

CLAUDE_DIR = Path(__file__).parent.parent
SESSION = CLAUDE_DIR / "session"
RECEIPTS = CLAUDE_DIR / "logs" / "receipts"
MEMORY = CLAUDE_DIR / "memory"
NOW = time.time()

GREEN, YELLOW, RED = "OK", "STALE", "DEAD"

def last_receipt(component: str):
    f = RECEIPTS / f"{component}.jsonl"
    if not f.exists():
        return None
    try:
        lines = f.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1]) if lines else None
    except Exception:
        return None

def age_hours(path: Path):
    try:
        return (NOW - path.stat().st_mtime) / 3600
    except OSError:
        return None

def check_all():
    rows = []

    # 1. Routing card sizes vs policy
    # Claude Code derives a per-project dir by flattening the path, so the name
    # differs on every machine. Glob for it rather than hardcoding one.
    cards = sorted((CLAUDE_DIR / "projects").glob("*/memory/MEMORY.md"))
    cards.append(MEMORY / "MEMORY.md")
    for card in cards:
        if not card.exists():
            continue
        size = card.stat().st_size
        state = GREEN if size < policy.WARN_AT else (YELLOW if size < policy.CRITICAL_AT else RED)
        if size >= policy.READ_LIMIT:
            state = RED
        rows.append(("card", str(card.relative_to(CLAUDE_DIR)), state,
                     f"{size:,} B (target {policy.TARGET:,}, warn {policy.WARN_AT:,}, critical {policy.CRITICAL_AT:,}, truncation {policy.READ_LIMIT:,})"))

    # 2. Recall indexes — freshness AND content evidence
    idx = SESSION / "memory-index.json"
    bm25 = SESSION / "bm25-index.json"
    ia, ba = age_hours(idx), age_hours(bm25)
    note_count = None
    if idx.exists():
        try:
            data = json.loads(idx.read_text(encoding="utf-8"))
            notes = data if isinstance(data, list) else data.get("notes", [])
            note_count = len(notes)
        except Exception:
            note_count = -1  # unparseable = red
    if ia is None or note_count in (None, -1):
        rows.append(("index", "memory-index.json", RED, "missing or unparseable"))
    else:
        state = GREEN if ia < 24 else (YELLOW if ia < 72 else RED)
        rows.append(("index", "memory-index.json", state, f"{note_count:,} notes, {ia:.0f}h old"))
    if ba is None:
        rows.append(("index", "bm25-index.json", RED, "missing"))
    else:
        state = GREEN if ba < 24 else (YELLOW if ba < 72 else RED)
        rows.append(("index", "bm25-index.json", state, f"{ba:.0f}h old"))

    # 3. Embeddings — formally disabled (Brian, 2026-08-21). Honest, not red.
    rows.append(("recall", "embeddings", GREEN, "DISABLED by decision — BM25+decay is the recall path"))

    # 4. Quarantine queue
    q = SESSION / "quarantine-notes.json"
    try:
        qdata = json.loads(q.read_text(encoding="utf-8")) if q.exists() else []
        qlen = len(qdata) if isinstance(qdata, list) else -1
    except Exception:
        qlen = -1
    state = GREEN if qlen == 0 else (YELLOW if 0 < qlen <= 50 else RED)
    rows.append(("quarantine", "queue", state, f"{qlen} notes pending"))

    # 5. Receipt-based liveness (schedule interval x2 rule)
    for comp, interval_h in (("scout-cron", 1.0),):  # 30-min schedule -> 1h allowance
        r = last_receipt(comp)
        if r is None:
            rows.append(("receipts", comp, RED, "no receipts ever written"))
            continue
        f = RECEIPTS / f"{comp}.jsonl"
        ah = age_hours(f)
        state = GREEN if ah is not None and ah < 2 * interval_h else RED
        rows.append(("receipts", comp, state,
                     f"last: {r.get('action')} {r.get('outcome')} ({ah:.1f}h ago)"))
        if r.get("outcome") == "FAILED":
            rows.append(("receipts", f"{comp}:last-outcome", RED, json.dumps(r.get("evidence"))[:120]))

    # 6. Staging / cold archive size (informational; red only if unparseable)
    cold = CLAUDE_DIR / "archive-cold"
    if cold.exists():
        try:
            size = sum(f.stat().st_size for f in cold.rglob("*") if f.is_file())
            rows.append(("staging", "archive-cold", GREEN,
                         f"{size/1e6:,.0f} MB held (30-day human purge policy)"))
        except OSError:
            rows.append(("staging", "archive-cold", YELLOW, "unreadable"))

    # 7. Corpus perimeter proof — excluded segments absent from the live index
    if note_count and note_count > 0:
        try:
            data = json.loads(idx.read_text(encoding="utf-8"))
            notes = data if isinstance(data, list) else data.get("notes", [])
            hits = [n.get("path", "") for n in notes
                    if any(part.lower() in policy.PERIMETER_SEGMENTS
                           for part in Path(n.get("path", "")).parts)]
            state = GREEN if not hits else RED
            rows.append(("perimeter", "excluded-segments-in-index", state,
                         f"{len(hits)} violations" + (f" e.g. {hits[0]}" if hits else "")))
        except Exception:
            rows.append(("perimeter", "excluded-segments-in-index", YELLOW, "check failed"))

    return rows

def main():
    rows = check_all()
    as_json = "--json" in sys.argv
    worst = GREEN
    for _, _, state, _ in rows:
        if state == RED:
            worst = RED
            break
        if state == YELLOW:
            worst = YELLOW
    if as_json:
        print(json.dumps({"overall": worst, "checks": [
            {"area": a, "item": i, "state": s, "detail": d} for a, i, s, d in rows]}, indent=1))
    else:
        print(f"MEMORY SYSTEM STATUS — overall: {worst}")
        for area, item, state, detail in rows:
            mark = {"OK": "+", "STALE": "!", "DEAD": "X"}[state]
            print(f"  [{mark}] {area:<10} {item:<40} {state:<6} {detail}")
    sys.exit(0 if worst == GREEN else (1 if worst == RED else 0))

if __name__ == "__main__":
    main()
