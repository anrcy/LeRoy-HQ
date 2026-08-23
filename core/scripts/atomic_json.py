#!/usr/bin/env python3
"""
atomic_json.py v1.0 — corruption-proof JSON writes for the memory index.

Why this exists (2026-07-31 incident):
  `session/memory-index.json` was written by five scripts, several cron-driven, all using
  the same non-atomic pattern:

      with open(INDEX, 'w', encoding='utf-8') as f:
          json.dump(index, f, indent=2)

  `open(...,'w')` truncates immediately, then the dump streams out over seconds (the file is
  ~27 MB). Any second writer overlapping that window interleaves with it. The observed result
  was a file that was valid JSON for 26,116,208 chars followed by 141 bytes of a *second*
  `embedding_info` block — and because nothing validates the index on read, hybrid recall
  simply returned None for every query. recall@5 went to 0.0% silently.

The fix is write-to-temp + atomic rename:
  - The temp file is in the SAME directory, so os.replace() is a same-filesystem atomic rename.
  - The temp name carries the PID, so two concurrent writers never share a temp file.
  - We fsync before replacing, so a power loss can't leave a half-written temp winning.
  - We parse the temp back BEFORE replacing, so a serialization bug can never overwrite a
    good index with a broken one.

Net effect: concurrent writes degrade to last-writer-wins (a complete, valid index either way)
instead of producing a corrupt file. Readers never observe a partially-written index, because
os.replace swaps the inode in one step — a reader holds either the old file or the new one.

Usage:
    from atomic_json import write_json_atomic, read_json_repaired

    write_json_atomic(INDEX_PATH, index, indent=2)
    data, repaired = read_json_repaired(INDEX_PATH)   # tolerates legacy trailing garbage
"""

import json
import os
import sys
from pathlib import Path

__all__ = ["write_json_atomic", "read_json_repaired", "verify_json_file"]


STALE_TEMP_AGE_S = 900  # 15 min — far longer than any legitimate write of a ~27 MB index


def _sweep_stale_temps(path, max_age_s=STALE_TEMP_AGE_S):
    """Delete abandoned `<name>.tmpNNN` files left by writers that died mid-write.

    Age-based on purpose: a temp younger than max_age_s may belong to a writer that is
    still running, and deleting it would sabotage a healthy process.
    """
    import time
    path = Path(path)
    now = time.time()
    try:
        for t in path.parent.glob(f"{path.name}.tmp*"):
            try:
                if now - t.stat().st_mtime > max_age_s:
                    t.unlink()
            except OSError:
                pass  # someone else cleaned it, or it's locked — either is fine
    except OSError:
        pass


def write_json_atomic(path, obj, *, indent=2, ensure_ascii=True, verify=True):
    """Serialize `obj` to `path` atomically. Returns the Path written.

    Raises on failure WITHOUT touching the existing file — a failed write leaves the previous
    good index in place, which is the whole point.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # A writer killed mid-write leaves its temp behind (confirmed by the crash-safety case in
    # stress-test-index-writes.py). Harmless to the live file, but 27 MB of litter per crash
    # adds up. Sweep abandoned temps opportunistically — age-based, so we never race a live
    # writer and never need cross-platform PID liveness checks.
    _sweep_stale_temps(path)

    # PID in the name: two concurrent writers must not share a temp file.
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")

    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=indent, ensure_ascii=ensure_ascii)
            f.flush()
            os.fsync(f.fileno())   # survive power loss, not just process exit

        if verify:
            # Parse it back before it becomes the live index. A serialization bug should
            # never be allowed to replace a working file.
            with open(tmp, "r", encoding="utf-8") as f:
                json.load(f)

        os.replace(tmp, path)      # atomic on Windows and POSIX (same filesystem)
        return path

    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def read_json_repaired(path):
    """Load JSON, tolerating trailing garbage from a legacy non-atomic write.

    Returns (obj, repaired: bool). `repaired` is True when trailing bytes were discarded —
    callers should log that loudly, because it means something wrote non-atomically.
    Raises if even the leading prefix is not valid JSON.
    """
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    try:
        return json.loads(raw), False
    except json.JSONDecodeError:
        # Recover the valid prefix — this is exactly the 2026-07-31 corruption shape.
        obj, end = json.JSONDecoder().raw_decode(raw)
        return obj, True


def verify_json_file(path):
    """Return (ok: bool, detail: str). Never raises — safe for health checks and hooks."""
    path = Path(path)
    if not path.exists():
        return False, "missing"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        return False, f"unreadable: {e}"
    try:
        json.loads(raw)
        return True, "ok"
    except json.JSONDecodeError as e:
        try:
            _, end = json.JSONDecoder().raw_decode(raw)
            return False, f"trailing garbage: {len(raw) - end} bytes after valid prefix (recoverable)"
        except Exception:
            return False, f"unparseable: {e}"


if __name__ == "__main__":
    # Standalone check: python atomic_json.py [path ...]
    targets = sys.argv[1:] or [
        Path(__file__).resolve().parent.parent / "session" / "memory-index.json",
        Path(__file__).resolve().parent.parent / "session" / "bm25-index.json",
    ]
    bad = 0
    for t in targets:
        ok, detail = verify_json_file(t)
        print(f"{'OK  ' if ok else 'FAIL'}  {t}  — {detail}")
        bad += 0 if ok else 1
    sys.exit(1 if bad else 0)
