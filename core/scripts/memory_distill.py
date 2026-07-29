#!/usr/bin/env python3
"""
memory_distill.py v1.0 -- Slice 2 of the promotion loop: LLM distillation.

Sits between promote (raw capture -> pending-notes) and flush_pending
(pending -> vault). Rewrites each raw promoted capture into a clean, durable
memory note via `claude -p` (haiku tier = Max subscription, NO API billing),
and lets the LLM VETO a false-positive promotion (self-correcting the
deterministic scorer).

  promote_short_term()  -> session/pending-notes.json  (raw text, crude titles)
       memory_distill()  <- THIS: haiku rewrites each into {title,content,type,tags}
                            or {"skip": true} to drop a bad promotion
       flush_pending()   -> quality_gate -> vault  (clean notes)

Cost/bottleneck guarantees:
  - Runs in cron batch path only, never the hot path.
  - Only fires when undistilled notes are pending (promotion is strict -> rare).
  - Capped at MAX_PER_RUN LLM calls per cycle (bounds cost + wall time).
  - Haiku tier (cheapest); rides `dispatch()` so it also respects the Max
    force_local throttle if headroom is low.
  - Graceful: on LLM/parse failure, keeps the RAW note (never loses a promotion)
    after MAX_ATTEMPTS, so nothing gets stuck forever.
  - Idempotent: notes carry "distilled": true once handled.

Usage:
    python memory_distill.py --dry-run   # show what WOULD be distilled, no LLM calls
    python memory_distill.py             # distill for real (haiku)
"""

import json
import sys
import io
import importlib.util
from pathlib import Path

try:
    if not getattr(sys.stdout, "_utf8_wrapped", False):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stdout._utf8_wrapped = True
except Exception:
    pass

CLAUDE_DIR = Path(__file__).parent.parent
SESSION_DIR = CLAUDE_DIR / "session"
SCRIPTS_DIR = CLAUDE_DIR / "scripts"
PENDING_NOTES = SESSION_DIR / "pending-notes.json"

MAX_PER_RUN = 5      # cap LLM calls per cron cycle (cost + wall-time bound)
MAX_ATTEMPTS = 2     # after this many failed distills, keep the raw note and move on
VALID_TYPES = {"feedback", "decision", "project", "pattern", "reference"}

PROMPT_TEMPLATE = """You are a memory librarian. Rewrite the raw captured user statement below into a single clean, durable long-term memory note.

Return ONLY strict JSON, no prose, in exactly this shape:
{{"title": "<=8 words, no filler, states the durable fact", "content": "1-3 sentences stating the preference/decision/fact clearly and self-containedly", "type": "feedback|decision|project|pattern|reference", "tags": ["one","two"]}}

Rules:
- type: feedback = a preference/rule about how to work; decision = a chosen course; project = client/deal/build fact; pattern = a reusable technique; reference = a pointer/resource.
- Keep only what is genuinely durable. Strip conversational filler, profanity, and one-off task phrasing.
- If the statement contains NO durable knowledge worth keeping long-term, return exactly: {{"skip": true}}

Raw capture:
\"\"\"{raw}\"\"\"
"""


def _load_dispatch():
    """Import dispatch() from model_tier_router by path (cwd-independent)."""
    spec = importlib.util.spec_from_file_location(
        "model_tier_router", str(SCRIPTS_DIR / "model_tier_router.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.dispatch


def _extract_raw(note: dict) -> str:
    """Get the original captured text from a promoted note's content."""
    c = note.get("content", "")
    # promote.py appends a "\n\n**Source:** ..." provenance line -- strip it
    return c.split("\n\n**Source:**")[0].strip() or note.get("title", "")


def distill(dry_run: bool) -> str:
    try:
        pending = json.loads(PENDING_NOTES.read_text(encoding="utf-8")) if PENDING_NOTES.exists() else []
    except Exception:
        return "pending-notes unreadable -- skipped"
    if not isinstance(pending, list) or not pending:
        return "nothing pending to distill"

    todo = [n for n in pending if not n.get("distilled")]
    if not todo:
        return f"all {len(pending)} pending already distilled"

    if dry_run:
        titles = "; ".join(_extract_raw(n)[:40] for n in todo[:MAX_PER_RUN])
        return f"DRY-RUN: would distill {min(len(todo), MAX_PER_RUN)} of {len(todo)} pending ({titles})"

    dispatch = _load_dispatch()
    distilled = skipped = kept_raw = failed = 0
    llm_calls = 0  # true cost counter: every dispatch() attempt, success or fail
    survivors = []

    for note in pending:
        if note.get("distilled"):
            survivors.append(note)          # already handled -> carry forward
            continue
        if llm_calls >= MAX_PER_RUN:
            survivors.append(note)          # per-run cost cap hit -> defer to next cycle
            continue

        raw = _extract_raw(note)
        llm_calls += 1
        prompt = PROMPT_TEMPLATE.format(raw=raw[:800])
        try:
            res = dispatch(prompt, "haiku", max_tokens=400)
            if not res.get("ok"):
                raise RuntimeError(res.get("error") or "dispatch not ok")
            text = (res.get("response") or "").strip()
            # tolerate code fences / stray prose around the JSON
            start, end = text.find("{"), text.rfind("}")
            obj = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
        except Exception:
            note["distill_attempts"] = note.get("distill_attempts", 0) + 1
            if note["distill_attempts"] >= MAX_ATTEMPTS:
                note["distilled"] = True  # give up cleanly, keep raw note
                kept_raw += 1
            else:
                failed += 1
            survivors.append(note)
            continue

        if obj.get("skip") is True:
            skipped += 1  # LLM vetoed a false-positive promotion -> drop the note
            continue

        title = str(obj.get("title", "")).strip()
        content = str(obj.get("content", "")).strip()
        if not title or not content:
            note["distill_attempts"] = note.get("distill_attempts", 0) + 1
            if note["distill_attempts"] >= MAX_ATTEMPTS:
                note["distilled"] = True
                kept_raw += 1
            else:
                failed += 1
            survivors.append(note)
            continue

        ntype = obj.get("type") if obj.get("type") in VALID_TYPES else note.get("type", "pattern")
        tags = obj.get("tags") if isinstance(obj.get("tags"), list) and obj.get("tags") else note.get("tags", ["patterns"])
        note.update(
            title=title[:80],
            content=f"{content}\n\n**Source:** distilled from short-term capture by memory_distill.py (haiku).",
            type=ntype,
            tags=[str(t)[:30] for t in tags[:3]],
            distilled=True,
        )
        distilled += 1
        survivors.append(note)

    PENDING_NOTES.write_text(json.dumps(survivors, indent=2, ensure_ascii=False), encoding="utf-8")
    return (f"distilled {distilled}, vetoed/skipped {skipped}, kept-raw {kept_raw}, "
            f"retry-later {failed} (of {len(todo)} pending)")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print(distill(args.dry_run))


if __name__ == "__main__":
    main()
