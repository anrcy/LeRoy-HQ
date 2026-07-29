#!/usr/bin/env python3
"""
destructive-command-guard.py -- PreToolUse process-level block on destructive
git/shell commands (boardroom dec-2dd29317, red-tier, owner: vera).

Closes the gap where a cron job or background agent shells out directly and
never passes through the prompt-level git-safety-protocol check. This guard
runs on EVERY Bash/PowerShell tool call, whatever spawned it.

Matcher (settings.json): Bash|PowerShell

Blocked patterns
----------------
  HARD BLOCK (no override -- these are never legitimate here):
    - rm -rf / rd /s on filesystem roots, ~, $HOME, %USERPROFILE%
    - rm -rf on the .claude root, memory root, or hooks root themselves
    - Remove-Item -Recurse -Force on the same roots
    - Format-Volume / format <drive>: / mkfs
  FLAG-TIER (single-use override via session/dcg-approved.flag):
    - git push --force / -f  (--force-with-lease is ALLOWED)
    - git reset --hard
    - git clean -f...
    - git branch -D
    - any other rm -rf / Remove-Item -Recurse -Force target NOT under a
      sanctioned scratch root (temp, scratchpad, node_modules, __pycache__,
      venv, dist, build)

Kill switch (Quinn's rollback requirement): create session/dcg-disabled.flag
and the guard allows everything (still logs, marked KILLSWITCH). Delete the
flag to re-arm. Instant, no restart needed -- the hook re-reads it every call.

Override for a deliberate, approved destructive command:
    echo "why" > session/dcg-approved.flag     (single-use, consumed on allow)

Log: session/dcg-blocked.jsonl (one JSON object per decision)
SAFETY: whole body try/except -> NEVER blocks on parse error (fail open).
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_ROOT = Path.home() / ".claude"
SESSION_DIR = CLAUDE_ROOT / "session"
LOG_FILE = SESSION_DIR / "dcg-blocked.jsonl"
KILL_SWITCH = SESSION_DIR / "dcg-disabled.flag"
APPROVE_FLAG = SESSION_DIR / "dcg-approved.flag"

# ---- HARD BLOCK: catastrophic targets, no override ----
HARD_PATTERNS = [
    # rm -rf (any flag order/combination containing r and f) on roots / home
    (re.compile(r"\brm\s+(-[a-z]*[rf][a-z]*\s+)*-[a-z]*[rf][a-z]*\s+(--\s+)?"
                r"(/|~|\$HOME|%USERPROFILE%|[A-Za-z]:[\\/]?)\s*($|[;&|])", re.IGNORECASE),
     "rm -rf on a filesystem root or home directory"),
    (re.compile(r"\brm\s+(-[a-z]*[rf][a-z]*\s+)+[\"']?(~?/|[A-Za-z]:[\\/])?"
                r"(Users[\\/]\w+[\\/])?\.claude[\"']?\s*($|[;&|])", re.IGNORECASE),
     "rm -rf on the .claude root"),
    (re.compile(r"\brm\s+(-[a-z]*[rf][a-z]*\s+)+\S*\.claude[\\/](memory|hooks|session)[\"']?\s*($|[;&|])",
                re.IGNORECASE),
     "rm -rf on the memory/hooks/session root"),
    (re.compile(r"\bRemove-Item\b(?=[^;|&]*-Recurse)(?=[^;|&]*-Force)[^;|&]*"
                r"([\"']?([A-Za-z]:[\\/]|\$env:USERPROFILE|~)[\\/]?[\"']?\s*($|[;&|])"
                r"|\S*\.claude[\\/]?(memory|hooks|session)?[\\/]?[\"']?\s*($|[;&|]))", re.IGNORECASE),
     "Remove-Item -Recurse -Force on a root / .claude tree"),
    (re.compile(r"\b(Format-Volume|mkfs(\.\w+)?)\b|\bformat\s+[A-Za-z]:", re.IGNORECASE),
     "disk format command"),
    (re.compile(r"\b(rd|rmdir)\s+/s\s+(/q\s+)?[\"']?([A-Za-z]:[\\/]?|%USERPROFILE%)[\"']?\s*($|[;&|])",
                re.IGNORECASE),
     "rd /s on a drive root or home"),
]

# ---- FLAG-TIER: destructive, needs the single-use approval flag ----
FLAG_PATTERNS = [
    (re.compile(r"\bgit\s+push\b(?![^;|&]*--force-with-lease)[^;|&]*(\s--force\b|\s-f\b)",
                re.IGNORECASE),
     "git force-push (use --force-with-lease, or approve via flag)"),
    (re.compile(r"\bgit\s+reset\s+[^;|&]*--hard", re.IGNORECASE), "git reset --hard"),
    (re.compile(r"\bgit\s+clean\b[^;|&]*\s-[a-z]*f", re.IGNORECASE), "git clean -f"),
    (re.compile(r"\bgit\s+branch\s+[^;|&]*\s-D\b"), "git branch -D (force delete)"),
    (re.compile(r"\brm\s+(-[a-z]*[rf][a-z]*\s+)*-[a-z]*r[a-z]*\s", re.IGNORECASE),
     "recursive rm outside a sanctioned scratch root"),
    (re.compile(r"\bRemove-Item\b(?=[^;|&]*-Recurse)(?=[^;|&]*-Force)", re.IGNORECASE),
     "Remove-Item -Recurse -Force outside a sanctioned scratch root"),
]

# Recursive deletes whose every path argument lives under one of these are fine.
SCRATCH_HINTS = [
    "temp\\claude", "temp/claude", "scratchpad", "node_modules", "__pycache__",
    "venv", "/tmp/", "dist", "build", ".pytest_cache", "worktrees",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(event: dict) -> None:
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        event["ts"] = _now()
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _scratch_only(cmd: str, pattern: re.Pattern) -> bool:
    """True if this recursive-delete command only touches sanctioned scratch paths."""
    if "rm" not in cmd.lower() and "remove-item" not in cmd.lower():
        return False
    lower = cmd.lower()
    # Every path-looking token after the delete verb must contain a scratch hint.
    tokens = re.findall(r"[\"']?((?:[A-Za-z]:)?[\\/~][^\s\"';|&]*|~[^\s\"';|&]*)[\"']?", cmd)
    if not tokens:
        return False
    return all(any(h in t.lower() for h in SCRATCH_HINTS) for t in tokens)


def _block(cmd: str, reason: str, tier: str) -> None:
    _log({"action": "BLOCK", "tier": tier, "reason": reason, "command": cmd[:400]})
    sys.stderr.write(
        "[DESTRUCTIVE-COMMAND-GUARD] BLOCKED\n"
        f"Reason: {reason}\n"
        f"Command: {cmd[:200]}\n\n"
        "This guard (boardroom dec-2dd29317) blocks destructive commands at the\n"
        "process level, including from background tasks and crons.\n\n"
    )
    if tier == "flag":
        sys.stderr.write(
            "If this is deliberate and safe:\n"
            "  echo \"why\" > ~/.claude/session/dcg-approved.flag   (single-use)\n"
            "then re-run. For force-push, prefer --force-with-lease (always allowed).\n"
        )
    else:
        sys.stderr.write("This target is never deletable via this path. No override exists.\n")
    sys.exit(2)


def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit(0)
        payload = json.loads(raw)
        ti = payload.get("tool_input", {}) or {}
        cmd = ti.get("command", "") or ti.get("script", "") or ""
        if not isinstance(cmd, str) or not cmd:
            sys.exit(0)

        hard_hit = next(((p, r) for p, r in HARD_PATTERNS if p.search(cmd)), None)
        flag_hit = None
        if not hard_hit:
            for p, r in FLAG_PATTERNS:
                if p.search(cmd):
                    if "rm" in r or "Remove-Item" in r:
                        if _scratch_only(cmd, p):
                            continue
                    flag_hit = (p, r)
                    break
        if not hard_hit and not flag_hit:
            sys.exit(0)

        # Kill switch: guard disarmed, allow but log loudly.
        if KILL_SWITCH.exists():
            _log({"action": "ALLOW-KILLSWITCH", "command": cmd[:400],
                  "reason": (hard_hit or flag_hit)[1]})
            sys.exit(0)

        if hard_hit:
            _block(cmd, hard_hit[1], "hard")

        # Flag-tier: consume single-use approval if present.
        if APPROVE_FLAG.exists():
            try:
                why = APPROVE_FLAG.read_text(encoding="utf-8").strip()
            except Exception:
                why = "?"
            _log({"action": "ALLOW-APPROVED", "command": cmd[:400],
                  "reason": flag_hit[1], "approval": why[:200]})
            try:
                APPROVE_FLAG.unlink()
            except Exception:
                pass
            sys.exit(0)
        _block(cmd, flag_hit[1], "flag")

    except SystemExit:
        raise
    except Exception as e:
        _log({"action": "ERROR-FAILOPEN", "error": f"{type(e).__name__}: {e}"})
        sys.exit(0)  # fail open -- never break the session on a guard bug


if __name__ == "__main__":
    main()
