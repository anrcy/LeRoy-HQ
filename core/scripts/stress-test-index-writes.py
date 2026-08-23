#!/usr/bin/env python3
"""
stress-test-index-writes.py — prove the atomic-write fix actually holds under load.

Written 2026-07-31 after a concurrent non-atomic write corrupted session/memory-index.json
and drove recall@5 to 0.0% silently.

The bar is NOT "the new code didn't crash." The bar is:
  1. the OLD pattern demonstrably corrupts under this exact load  (control)
  2. the NEW pattern demonstrably does not                        (treatment)
  3. concurrent READERS never observe a partial file              (the failure mode that killed recall)
  4. a killed writer never damages the live file                  (crash safety)

Runs entirely in a temp directory. Never touches the real indexes.

Usage:
    python stress-test-index-writes.py                # full suite
    python stress-test-index-writes.py --workers 12   # crank concurrency
"""

import argparse
import json
import multiprocessing as mp
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atomic_json import write_json_atomic, verify_json_file


def make_payload(worker_id, size_kb):
    """Payload big enough that a write takes real wall-clock time (the race window)."""
    filler = "x" * 1000
    return {
        "writer": worker_id,
        "marker_start": f"WORKER-{worker_id}",
        "notes": {f"note-{i}": {"w": worker_id, "blob": filler} for i in range(size_kb)},
        "marker_end": f"WORKER-{worker_id}",
    }


def legacy_write(path, obj):
    """The pre-fix pattern: truncate-then-stream. This is what corrupted the index."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)


def writer_proc(mode, path, worker_id, iterations, size_kb):
    for _ in range(iterations):
        payload = make_payload(worker_id, size_kb)
        try:
            if mode == "legacy":
                legacy_write(path, payload)
            else:
                write_json_atomic(path, payload, indent=2)
        except Exception:
            pass  # writer-side errors are counted by the reader's view of the file


def reader_proc(path, duration_s, result_q):
    """Continuously parse the live file. Any failure = a reader saw a broken index."""
    parse_fail = torn = reads = 0
    deadline = time.time() + duration_s
    while time.time() < deadline:
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        if not raw:
            continue
        reads += 1
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            parse_fail += 1
            continue
        # Structural tear: markers from two different writers in one file.
        if obj.get("marker_start") != obj.get("marker_end"):
            torn += 1
    result_q.put({"reads": reads, "parse_fail": parse_fail, "torn": torn})


def run_case(mode, workers, iterations, size_kb, tmpdir):
    path = str(Path(tmpdir) / f"index-{mode}.json")
    write_json_atomic(path, make_payload(0, size_kb), indent=2)  # seed a valid file

    q = mp.Queue()
    reader = mp.Process(target=reader_proc, args=(path, 6, q))
    reader.start()

    procs = [
        mp.Process(target=writer_proc, args=(mode, path, wid, iterations, size_kb))
        for wid in range(1, workers + 1)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    reader.join()

    stats = q.get() if not q.empty() else {"reads": 0, "parse_fail": -1, "torn": -1}
    ok, detail = verify_json_file(path)
    stats["final_ok"] = ok
    stats["final_detail"] = detail

    # leftover temp files = the writer leaked scratch state
    stats["leftover_tmp"] = len(list(Path(tmpdir).glob(f"index-{mode}.json.tmp*")))
    return stats


def test_crash_safety(tmpdir):
    """SIGKILL a writer mid-write; the live file must remain valid and complete."""
    path = Path(tmpdir) / "crash.json"
    write_json_atomic(path, make_payload(99, 400), indent=2)
    before = path.read_text(encoding="utf-8")

    code = (
        "import sys,os,time;"
        f"sys.path.insert(0,r'{os.path.dirname(os.path.abspath(__file__))}');"
        "from atomic_json import write_json_atomic;"
        "big={'notes':{str(i):'y'*2000 for i in range(20000)}};"
        f"write_json_atomic(r'{path}', big)"
    )
    p = subprocess.Popen([sys.executable, "-c", code])
    time.sleep(0.35)          # land inside the write window
    p.kill()
    p.wait()

    ok, detail = verify_json_file(path)
    unchanged = path.read_text(encoding="utf-8") == before
    leftover = len(list(Path(tmpdir).glob("crash.json.tmp*")))
    for f in Path(tmpdir).glob("crash.json.tmp*"):
        f.unlink(missing_ok=True)
    return {"valid_after_kill": ok, "detail": detail,
            "unchanged": unchanged, "leftover_tmp": leftover}


def test_corruption_detection(tmpdir):
    """The exact 2026-07-31 shape: valid JSON + trailing fragment. Must be caught."""
    path = Path(tmpdir) / "corrupt.json"
    write_json_atomic(path, {"notes": {"a": 1}, "embedding_info": {"n": 1}}, indent=2)
    with open(path, "a", encoding="utf-8") as f:
        f.write('LM-L6-v2",\n    "dimension": 384,\n    "embedded_count": 2126')
    ok, detail = verify_json_file(path)
    return {"detected": (not ok), "detail": detail}


def test_temp_sweep(tmpdir):
    """An abandoned temp from a dead writer must be reaped; a LIVE one must survive."""
    path = Path(tmpdir) / "sweep.json"
    write_json_atomic(path, {"a": 1})

    stale = path.with_name(path.name + ".tmp999999")
    stale.write_text("garbage from a writer that died", encoding="utf-8")
    old = time.time() - 3600
    os.utime(stale, (old, old))          # backdate it an hour

    fresh = path.with_name(path.name + ".tmp888888")
    fresh.write_text("a writer that is still going", encoding="utf-8")

    write_json_atomic(path, {"a": 2})    # triggers the sweep

    result = {"stale_reaped": not stale.exists(), "live_temp_preserved": fresh.exists()}
    for f in (stale, fresh):
        f.unlink(missing_ok=True)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--iterations", type=int, default=6)
    ap.add_argument("--size-kb", type=int, default=1200)
    args = ap.parse_args()

    tmpdir = tempfile.mkdtemp(prefix="idxstress-")
    print("=" * 66)
    print("  INDEX WRITE STRESS TEST")
    print(f"  {args.workers} concurrent writers x {args.iterations} iterations, "
          f"~{args.size_kb}KB payload, 1 concurrent reader")
    print("=" * 66)

    try:
        print("\n[1] CONTROL — legacy truncate-then-stream (expected to CORRUPT)")
        legacy = run_case("legacy", args.workers, args.iterations, args.size_kb, tmpdir)
        print(f"    reads={legacy['reads']}  parse_failures={legacy['parse_fail']}  "
              f"torn_files={legacy['torn']}")
        print(f"    final file: {'OK' if legacy['final_ok'] else 'CORRUPT'} — {legacy['final_detail']}")

        print("\n[2] TREATMENT — write_json_atomic (expected CLEAN)")
        atomic = run_case("atomic", args.workers, args.iterations, args.size_kb, tmpdir)
        print(f"    reads={atomic['reads']}  parse_failures={atomic['parse_fail']}  "
              f"torn_files={atomic['torn']}")
        print(f"    final file: {'OK' if atomic['final_ok'] else 'CORRUPT'} — {atomic['final_detail']}")
        print(f"    leftover temp files: {atomic['leftover_tmp']}")

        print("\n[3] CRASH SAFETY — kill a writer mid-write")
        crash = test_crash_safety(tmpdir)
        print(f"    live file valid after kill: {crash['valid_after_kill']}")
        print(f"    live file unchanged:        {crash['unchanged']}")
        print(f"    leftover temp files:        {crash['leftover_tmp']}")

        print("\n[4] CORRUPTION DETECTION — 2026-07-31 trailing-garbage shape")
        det = test_corruption_detection(tmpdir)
        print(f"    detected: {det['detected']} — {det['detail']}")

        print("\n[5] TEMP SWEEP — reap dead writers' scratch, spare live ones")
        sweep = test_temp_sweep(tmpdir)
        print(f"    stale temp reaped:     {sweep['stale_reaped']}")
        print(f"    live temp preserved:   {sweep['live_temp_preserved']}")

        print("\n" + "=" * 66)
        checks = {
            "sweep: stale temp reaped":            sweep["stale_reaped"],
            "sweep: live temp NOT touched":        sweep["live_temp_preserved"],
            "control corrupts (proves the test has teeth)":
                (legacy["parse_fail"] > 0 or legacy["torn"] > 0 or not legacy["final_ok"]),
            "atomic: zero reader parse failures":  atomic["parse_fail"] == 0,
            "atomic: zero torn reads":             atomic["torn"] == 0,
            "atomic: final file valid":            atomic["final_ok"],
            "atomic: no leaked temp files":        atomic["leftover_tmp"] == 0,
            "crash: live file still valid":        crash["valid_after_kill"],
            "crash: live file unchanged":          crash["unchanged"],
            "corruption is detected":              det["detected"],
        }
        for name, passed in checks.items():
            print(f"  [{'PASS' if passed else 'FAIL'}]  {name}")
        failed = [n for n, p in checks.items() if not p]
        print("=" * 66)
        if failed:
            print(f"RESULT: {len(failed)} CHECK(S) FAILED")
            return 1
        print("RESULT: ALL CHECKS PASSED")
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
