#!/usr/bin/env python3
"""
bead_manager.py v1.0 -- durable work-item ledger ("beads").

Ported concept from Steve Yegge's Gas Town (open-sourced 2026-01-01). LeRoy already
tracks three horizons; beads fill the gap between them:

  session/state.json   a SESSION            dies on /reset
  TodoWrite            intra-session tasks  dies with the conversation
  session/goals.json   a multi-day GOAL     20-50 steps, overseer-driven
  session/beads.jsonl  a WORK ITEM  <- THIS  one unit of work with an owner and an
                                             artifact; survives handoff + compaction

A bead is the thing you would otherwise have to re-explain after a context wipe:
"the reconcile stage is written but not wired into cron yet, and it needs a dry-run
before it can be trusted." Goals are too coarse for that; todos evaporate.

Conventions mirror scripts/goal_manager.py deliberately: atomic mutations through this
module only (never touch the file from a caller), a markdown mirror under memory/Beads/
so a bead survives /reset, and archive-on-close rather than delete.

CLI:
    python bead_manager.py create "Wire reconcile into scout-cron" --goal G-V2AZWY --owner builder
    python bead_manager.py list [--status open|blocked|done|all]
    python bead_manager.py show B-XXXXXX
    python bead_manager.py update B-XXXXXX --status blocked --blocked-by "needs admin restart"
    python bead_manager.py artifact B-XXXXXX scripts/memory_reconcile.py
    python bead_manager.py close B-XXXXXX [--resolution done|dropped]
    python bead_manager.py count          # open-bead count for the gate display
"""

import argparse
import contextlib
import json
import os
import secrets
import sys
import io
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    if not getattr(sys.stdout, "_utf8_wrapped", False):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stdout._utf8_wrapped = True
except Exception:
    pass

CLAUDE_DIR = Path(__file__).resolve().parent.parent
SESSION_DIR = CLAUDE_DIR / "session"
BEADS_JSONL = SESSION_DIR / "beads.jsonl"
BEADS_MIRROR = CLAUDE_DIR / "memory" / "Beads"
BASE32_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ0123456789"   # no I/L/O/U — unambiguous when read aloud

VALID_STATUS = {"open", "in_progress", "blocked", "done", "dropped"}
CLOSED_STATUS = {"done", "dropped"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_id() -> str:
    return "B-" + "".join(secrets.choice(BASE32_ALPHABET) for _ in range(6))


# ── Storage ───────────────────────────────────────────────────────────────────

LOCK_FILE = SESSION_DIR / "beads.lock"
LOCK_TIMEOUT_S = 10.0
LOCK_STALE_S = 30.0     # a crashed holder's lock self-clears after this


@contextlib.contextmanager
def _ledger_lock():
    """Cross-process mutual exclusion for the read-modify-write cycle.

    v1.1 fix (2026-07-24, caught by stress test): every operation is load →
    mutate → rewrite-whole-file, so two concurrent creates were last-writer-wins —
    measured 7 of 8 parallel creates LOST. parallel-fleet.md explicitly plans
    concurrent agents touching beads, so this must hold under contention.
    O_CREAT|O_EXCL lock with stale-steal, same pattern as scout-cron.py.
    """
    deadline = time.time() + LOCK_TIMEOUT_S
    while True:
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - LOCK_FILE.stat().st_mtime > LOCK_STALE_S:
                    LOCK_FILE.unlink(missing_ok=True)   # stale holder — steal
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                raise TimeoutError("bead ledger lock timeout")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass


def _load() -> list:
    """JSONL so a torn write corrupts at most one line, and the ledger stays
    greppable. Cross-process safety comes from _ledger_lock(), NOT the format —
    every mutation rewrites the file, so writers must hold the lock."""
    try:
        if not BEADS_JSONL.exists():
            return []
        out = []
        for line in BEADS_JSONL.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue          # one corrupt line must not lose the ledger
        return out
    except OSError:
        return []


def _save(beads: list) -> bool:
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        tmp = BEADS_JSONL.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(json.dumps(b, default=str) + "\n" for b in beads),
                       encoding="utf-8")
        tmp.replace(BEADS_JSONL)      # atomic on Windows and POSIX
        return True
    except OSError:
        return False


def _unique_id(existing: set) -> str:
    for _ in range(100):
        c = _generate_id()
        if c not in existing:
            return c
    raise RuntimeError("bead_manager: ID space exhausted after 100 attempts")


# ── Memory mirror (survives /reset) ───────────────────────────────────────────

def _mirror(bead: dict) -> bool:
    try:
        BEADS_MIRROR.mkdir(parents=True, exist_ok=True)
        arts = "\n".join(f"- `{a}`" for a in bead.get("artifacts", [])) or "_none yet_"
        blocked = "\n".join(f"- {b}" for b in bead.get("blocked_by", [])) or "_none_"
        body = f"""---
created: {bead['created'][:10]}
modified: {bead['updated'][:10]}
tags: [beads, work-item]
type: project
bead_id: {bead['id']}
status: {bead['status']}
---

# {bead['id']} — {bead['title']}

| Field | Value |
|-------|-------|
| Status | {bead['status']} |
| Owner | {bead.get('owner') or '—'} |
| Goal | {bead.get('goal_id') or '—'} |
| Created | {bead['created'][:16]} |
| Updated | {bead['updated'][:16]} |

## Notes

{bead.get('notes') or '_none_'}

## Artifacts

{arts}

## Blocked By

{blocked}
"""
        (BEADS_MIRROR / f"{bead['id']}.md").write_text(body, encoding="utf-8")
        return True
    except OSError:
        return False


# ── Operations ────────────────────────────────────────────────────────────────

def create_bead(title: str, goal_id: str = None, owner: str = None,
                notes: str = None) -> dict:
    with _ledger_lock():
        beads = _load()
        bead = {
            "id": _unique_id({b.get("id") for b in beads}),
            "title": title,
            "status": "open",
            "owner": owner,
            "goal_id": goal_id,
            "notes": notes,
            "artifacts": [],
            "blocked_by": [],
            "created": _now_iso(),
            "updated": _now_iso(),
        }
        beads.append(bead)
        _save(beads)
    _mirror(bead)
    return bead


def _mutate(bead_id: str, fn) -> dict | None:
    with _ledger_lock():
        beads = _load()
        target = None
        for b in beads:
            if b.get("id") == bead_id:
                fn(b)
                b["updated"] = _now_iso()
                target = b
                break
        if target is None:
            return None
        _save(beads)
    _mirror(target)
    return target


def update_bead(bead_id: str, status: str = None, owner: str = None,
                notes: str = None, blocked_by: str = None) -> dict | None:
    def _fn(b):
        if status and status in VALID_STATUS:
            b["status"] = status
        if owner:
            b["owner"] = owner
        if notes:
            b["notes"] = notes
        if blocked_by:
            b.setdefault("blocked_by", []).append(blocked_by)
            b["status"] = "blocked"
    return _mutate(bead_id, _fn)


def add_artifact(bead_id: str, path: str) -> dict | None:
    def _fn(b):
        if path not in b.setdefault("artifacts", []):
            b["artifacts"].append(path)
    return _mutate(bead_id, _fn)


def close_bead(bead_id: str, resolution: str = "done") -> dict | None:
    """Closing keeps the bead in the ledger and its mirror on disk — the record of
    what was done is the point. Never deletes."""
    if resolution not in CLOSED_STATUS:
        resolution = "done"
    return _mutate(bead_id, lambda b: b.update({"status": resolution}))


def list_beads(status: str = "open") -> list:
    beads = _load()
    if status == "all":
        return beads
    if status == "open":       # "open" means anything still live
        return [b for b in beads if b.get("status") not in CLOSED_STATUS]
    return [b for b in beads if b.get("status") == status]


def get_bead(bead_id: str) -> dict | None:
    return next((b for b in _load() if b.get("id") == bead_id), None)


def open_count() -> int:
    """Used by the Position #0 gate display."""
    return len(list_beads("open"))


# ── CLI ───────────────────────────────────────────────────────────────────────

def _print(b: dict) -> None:
    print(f"{b['id']}  [{b['status']:<11}] {b['title']}")
    if b.get("goal_id"):
        print(f"           goal: {b['goal_id']}")
    if b.get("owner"):
        print(f"           owner: {b['owner']}")
    for a in b.get("artifacts", []):
        print(f"           artifact: {a}")
    for x in b.get("blocked_by", []):
        print(f"           blocked: {x}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Durable work-item ledger")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create"); c.add_argument("title")
    c.add_argument("--goal"); c.add_argument("--owner"); c.add_argument("--notes")

    l = sub.add_parser("list"); l.add_argument("--status", default="open")
    s = sub.add_parser("show"); s.add_argument("bead_id")

    u = sub.add_parser("update"); u.add_argument("bead_id")
    u.add_argument("--status"); u.add_argument("--owner")
    u.add_argument("--notes"); u.add_argument("--blocked-by", dest="blocked_by")

    a = sub.add_parser("artifact"); a.add_argument("bead_id"); a.add_argument("path")
    cl = sub.add_parser("close"); cl.add_argument("bead_id")
    cl.add_argument("--resolution", default="done")
    sub.add_parser("count")

    args = ap.parse_args()

    if args.cmd == "create":
        _print(create_bead(args.title, args.goal, args.owner, args.notes)); return 0
    if args.cmd == "list":
        beads = list_beads(args.status)
        if not beads:
            print(f"no {args.status} beads"); return 0
        for b in beads:
            _print(b)
        return 0
    if args.cmd == "show":
        b = get_bead(args.bead_id)
        if not b:
            print(f"no such bead: {args.bead_id}"); return 1
        _print(b); return 0
    if args.cmd == "update":
        b = update_bead(args.bead_id, args.status, args.owner, args.notes, args.blocked_by)
        if not b:
            print(f"no such bead: {args.bead_id}"); return 1
        _print(b); return 0
    if args.cmd == "artifact":
        b = add_artifact(args.bead_id, args.path)
        if not b:
            print(f"no such bead: {args.bead_id}"); return 1
        _print(b); return 0
    if args.cmd == "close":
        b = close_bead(args.bead_id, args.resolution)
        if not b:
            print(f"no such bead: {args.bead_id}"); return 1
        _print(b); return 0
    if args.cmd == "count":
        print(open_count()); return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
