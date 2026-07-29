#!/usr/bin/env python3
"""
memory-size-guard.py v1.0 — MEMORY.md truncation early-warning protocol.

Why this exists: the auto-memory index MEMORY.md is read into context each
session. The Read tool truncates any file over ~24.4 KB. If MEMORY.md crosses
that line it gets SILENTLY clipped — entries at the bottom vanish from recall
with no error. Standing rule: truncation must NEVER happen without the owner
being told.

Wired as TWO hooks in settings.json, both invoking this one script:
  - SessionStart          → checks at READ time (the moment truncation bites)
  - PostToolUse Write|Edit → immediate feedback when MEMORY.md is grown

Behavior: reads hook JSON on stdin, measures MEMORY.md byte size, and if it
crosses a threshold emits a systemMessage (visible to you) plus
additionalContext (so the model proactively compacts). ALWAYS exits 0 — this
warns, it never blocks an edit or a session start.
"""

import json
import sys
import io
import os
import glob

# Force UTF-8 stdin (Windows console defaults to cp1252)
if sys.stdin.encoding and sys.stdin.encoding.lower() != "utf-8":
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")

# --- Thresholds (bytes). Read limit is ~24.4 KB = 24400. ---
READ_LIMIT = 24400
WARN_AT = 20000      # ~82% — start nudging; plenty of runway to compact calmly
CRITICAL_AT = 23000  # ~94% — loud; compact NOW, truncation is imminent


def find_memory_files(explicit_path=None):
    """Return the MEMORY.md file(s) to check.

    PostToolUse gives us the exact edited path — only act if it's a MEMORY.md.
    SessionStart gives us nothing — glob every project's auto-memory index.
    """
    if explicit_path:
        if os.path.basename(explicit_path).upper() == "MEMORY.MD":
            return [explicit_path] if os.path.isfile(explicit_path) else []
        return []  # some other file was edited — not our concern
    # SessionStart: derive .claude root from this script's location, then glob.
    claude_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    hits = glob.glob(os.path.join(claude_root, "projects", "*", "memory", "MEMORY.md"))
    return [p for p in hits if os.path.isfile(p)]


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)  # unparseable input — never block

    tool_input = data.get("tool_input", {}) or {}
    explicit = tool_input.get("file_path") or tool_input.get("path") or data.get("file_path")

    worst = None  # (size, path)
    for path in find_memory_files(explicit):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size >= WARN_AT and (worst is None or size > worst[0]):
            worst = (size, path)

    if worst is None:
        sys.exit(0)  # everything comfortably under WARN — silent, no noise

    size, path = worst
    pct = round(size / READ_LIMIT * 100)
    headroom = READ_LIMIT - size

    if size >= CRITICAL_AT:
        icon = "\U0001F6A8"  # 🚨
        sev = "CRITICAL"
        nudge = ("Compact MEMORY.md NOW — it is about to exceed the read limit and "
                 "entries at the bottom will be silently dropped from recall.")
    else:
        icon = "⚠️"  # ⚠️
        sev = "WARNING"
        nudge = ("Compact MEMORY.md soon (tighten hooks / move detail into topic "
                 "files). It is approaching the read limit where it truncates silently.")

    msg = (f"{icon} MEMORY.md size {sev}: {size:,} bytes = {pct}% of the "
           f"{READ_LIMIT:,}-byte read limit ({headroom:,} bytes headroom). {nudge}")

    out = {
        "systemMessage": msg,
        "hookSpecificOutput": {
            "hookEventName": data.get("hook_event_name", "SessionStart"),
            "additionalContext": (
                f"[memory-size-guard] {path} is at {size:,} bytes ({pct}% of the "
                f"{READ_LIMIT:,}-byte Read-tool truncation limit). {nudge} "
                "Preserve every link path and title exactly when compacting."
            ),
        },
    }
    print(json.dumps(out))
    sys.exit(0)


if __name__ == "__main__":
    main()
