#!/usr/bin/env python3
"""
agent-return-validator.py — SubagentStop hook. Enforces the agent return contract.

WHY THIS EXISTS
---------------
Forensics on 2026-08-08 found that spawned agents routinely go idle and return
nothing, and that this is indistinguishable from an agent that simply had nothing
to say. Five agents did it during a single investigation session, including
built-in `Plan` agents. Before that was measurable there was no `SubagentStop`
hook registered anywhere in settings.json, so no return was ever observed.

This hook records, for every subagent completion:
    RETURNED      — non-empty final output
    EMPTY_RETURN  — agent stopped having produced no usable final text
It never blocks. Fail-open, always exit 0 — a telemetry hook must never be able
to break a spawn.

SCHEMA DISCOVERY
----------------
The SubagentStop payload shape is not documented in this repo, so the first
records intentionally capture `payload_keys` (key names only, never values).
Once real firings confirm the shape, tighten `_extract_agent` / `_extract_output`
and drop the discovery field.

Output: session/agent-returns.jsonl
"""

import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "buffer"):
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")

CLAUDE_ROOT = Path.home() / ".claude"
SESSION_DIR = CLAUDE_ROOT / "session"
RETURN_LOG = SESSION_DIR / "agent-returns.jsonl"

# A return shorter than this is treated as no real deliverable. Idle pings and
# bare acknowledgements land well under it.
MIN_MEANINGFUL_CHARS = 40


def _parse_stdin() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_agent(payload: dict) -> str:
    """Agent identity. Confirmed SubagentStop schema uses `agent_type`, which is
    empty/None for forks (spawned with subagent_type omitted) — fall back to
    `agent_id` so forks are still attributable rather than all collapsing to
    'unknown'."""
    for key in (
        "agent_type", "subagent_type", "agent", "agent_name",
        "name", "subagent_name",
    ):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    nested = payload.get("tool_input") or payload.get("subagent") or {}
    if isinstance(nested, dict):
        for key in ("subagent_type", "agent_type", "name"):
            val = nested.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    # Fork spawns carry no agent_type; agent_id still identifies the run.
    aid = payload.get("agent_id")
    if isinstance(aid, str) and aid.strip():
        return f"fork:{aid.strip()}"
    return "unknown"


def _text_from_content(content) -> str:
    """Flatten Claude message content (str, or list of blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    return ""


def _last_assistant_text(transcript_path: str) -> str:
    """Read the subagent transcript and return its final assistant text."""
    try:
        p = Path(transcript_path)
        if not p.exists():
            return ""
        last = ""
        with p.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if '"assistant"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") != "assistant":
                    continue
                text = _text_from_content(
                    obj.get("message", {}).get("content")
                ).strip()
                if text:
                    last = text
        return last
    except Exception:
        return ""


def _extract_output(payload: dict) -> str:
    """Final text the SUBAGENT produced.

    Confirmed SubagentStop schema supplies `last_assistant_message` directly —
    prefer it. For the transcript fallback, `agent_transcript_path` is the
    subagent's own transcript; plain `transcript_path` is the PARENT session's.
    Reading the latter measures the parent's output and silently reports every
    return as healthy, so it is deliberately NOT used as a fallback here.
    """
    for key in (
        "last_assistant_message", "output", "result",
        "final_output", "response", "text",
    ):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, (list, dict)):
            flat = _text_from_content(val).strip()
            if flat:
                return flat
    tpath = payload.get("agent_transcript_path")
    if isinstance(tpath, str) and tpath.strip():
        return _last_assistant_text(tpath.strip())
    return ""


def main() -> None:
    payload = _parse_stdin()

    agent = _extract_agent(payload)
    output = _extract_output(payload)
    chars = len(output)

    if chars >= MIN_MEANINGFUL_CHARS:
        status = "RETURNED"
    elif chars > 0:
        status = "THIN_RETURN"
    else:
        status = "EMPTY_RETURN"

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent": agent,
        "status": status,
        "chars": chars,
        "preview": output[:160],
        "session_id": payload.get("session_id"),
    }

    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        with RETURN_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        # Fail-open: telemetry must never break a spawn.
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
