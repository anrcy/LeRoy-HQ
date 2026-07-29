#!/usr/bin/env python3
"""
Claim Verifier v1.0
Stop hook: flags completion claims that were never backed by a verification action.

Why this exists
---------------
LeRoy's enforcement is almost entirely PRE-action — Position #0, the gate, routing,
PreToolUse guards. Nothing checks, after the fact, whether a claim was true.
`memory/Patterns/verification-before-claims.md` states the doctrine but nothing
enforces it, and the failure mode is documented and expensive:
project_destructive_command_guard_false_notify_2026_07_13 — a "the guard is live"
email sent for a guard that had never been wired.

This is Karpathy's fourth rule ("define verifiable success criteria, loop until met")
applied at session end: if the session SAID it shipped something, there should be a
tool call showing it checked.

Design constraints
------------------
- ADVISORY, never blocking. A Stop hook that blocks would break the fast lane and
  every trivial exchange. It warns and logs; the log makes the pattern visible.
- Fail-safe: every path exits 0. A hook that crashes the session is worse than a
  missed claim, so all logic sits inside a bare except.
- Cheap: reads the tail of the transcript only. No LLM call, no network.

Output: appends `claim_unverified` to session/leroy.log and an entry to
session/interrupts.queue, which the Position #0 gate already renders.
"""

import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

if sys.stdin.encoding != "utf-8":
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")

CLAUDE_DIR = Path.home() / ".claude"
SESSION_DIR = CLAUDE_DIR / "session"
LEROY_LOG = SESSION_DIR / "leroy.log"
INTERRUPTS = SESSION_DIR / "interrupts.queue"

# Read only the tail — a long session's transcript can be many MB.
TAIL_BYTES = 400_000

# Phrases asserting that something is now true in the world. Deliberately narrow:
# "I'll fix it" or "this should work" are not claims, they're intent. Only past-tense
# / state assertions qualify, or the pattern generates constant false positives.
CLAIM_PATTERNS = [
    r"\bit'?s (?:now )?live\b",
    r"\bis (?:now )?live\b",
    r"\bshipped\b",
    r"\bdeployed\b",
    r"\bverified\b",
    r"\bconfirmed working\b",
    r"\btests? (?:now )?pass(?:es|ing)?\b",
    r"\bfixed (?:it|the|that)\b",
    r"\bworks now\b",
    r"\bup and running\b",
    r"\bsuccessfully (?:sent|created|deployed|installed|restarted)\b",
]

# Tools whose use constitutes evidence. Running a command, reading a file back, or
# probing a service all count; writing a file does NOT — writing is the claim, not
# the check.
VERIFY_TOOLS = {
    "Bash", "PowerShell", "Read", "Grep", "Glob", "WebFetch", "Monitor",
    "BashOutput", "TaskOutput", "NotebookRead",
}
VERIFY_TOOL_PREFIXES = ("mcp__",)      # MCP status/get/list calls are real probes


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_input() -> dict:
    try:
        return json.loads(sys.stdin.read())
    except Exception:
        return {}


def read_tail(path: Path) -> list:
    """Return the transcript's trailing JSONL entries."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()           # discard the partial line
            raw = f.read().decode("utf-8", errors="replace")
        out = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
    except Exception:
        return []


def _text_of(entry: dict) -> str:
    """Flatten an assistant message's text content."""
    msg = entry.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            c.get("text", "") for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
    return ""


def _tools_of(entry: dict) -> list:
    msg = entry.get("message") or {}
    content = msg.get("content")
    names = []
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                names.append(c.get("name", ""))
    return names


def analyze(entries: list) -> list:
    """Find claims with no verification tool call at or after their position.

    Ordering matters: evidence must come BEFORE the claim in the transcript (you
    verify, then assert). We scan forward tracking the most recent verification
    index, and flag a claim only when nothing verified preceded it in this session.
    """
    findings = []
    last_verify_idx = -1

    for i, entry in enumerate(entries):
        if entry.get("type") != "assistant":
            continue

        for name in _tools_of(entry):
            if name in VERIFY_TOOLS or name.startswith(VERIFY_TOOL_PREFIXES):
                last_verify_idx = i

        text = _text_of(entry)
        if not text:
            continue

        for pat in CLAIM_PATTERNS:
            m = re.search(pat, text, re.IGNORECASE)
            if not m:
                continue
            if last_verify_idx == -1:
                snippet = text[max(0, m.start() - 60):m.end() + 60].replace("\n", " ")
                findings.append({"claim": m.group(0), "context": snippet.strip()})
            break        # one finding per message is enough

    return findings


def emit(findings: list, session_id: str) -> None:
    try:
        with LEROY_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": _now(), "level": "warn", "trace_id": None,
                "session_id": session_id, "agent": None, "skill": None,
                "tool": None, "event": "claim_unverified", "ref": None,
                "payload": {"count": len(findings), "claims": findings[:5]},
            }) + "\n")
    except Exception:
        pass

    try:
        with INTERRUPTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": _now(), "priority": "MEDIUM", "acked": False, "surfaced": False,
                "source": "claim-verifier",
                "message": (f"{len(findings)} completion claim(s) made with no "
                            f"verification step: "
                            + "; ".join(x["claim"] for x in findings[:3])),
            }) + "\n")
    except Exception:
        pass


def main() -> int:
    try:
        data = parse_input()
        tpath = data.get("transcript_path", "")
        if not tpath:
            return 0
        p = Path(tpath)
        if not p.exists():
            return 0

        findings = analyze(read_tail(p))
        if findings:
            emit(findings, data.get("session_id", "") or "")
    except Exception:
        # Never let this hook disrupt a session.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
