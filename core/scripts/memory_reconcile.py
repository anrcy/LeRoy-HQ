#!/usr/bin/env python3
"""
memory_reconcile.py v1.0 -- Slice 3 of the promotion loop: reconcile against what
the vault ALREADY knows before writing anything new.

Pipeline position (see project_memory_promotion_loop_design_2026_07_20):

  promote_short_term()  -> session/pending-notes.json   (raw capture, crude titles)
  memory_distill()      -> clean {title, content, type, tags}, or veto
  memory_reconcile()    <- THIS: ADD / UPDATE / NOOP / SUPERSEDE vs existing notes
  flush_pending()       -> quality_gate -> vault

Why it exists:
  The loop was append-only. Distillation made each note CLEAN but still asked
  "is this worth keeping?" and never "do we already know this?". Near-duplicates
  therefore accumulated -- the same standing rule re-learned under five titles.
  This is the mem0 insight ported to a markdown vault: memory writes are a
  reconciliation against prior state, not an append.

Verdicts:
  ADD        genuinely new            -> stays in pending, flush writes it
  UPDATE     refines an existing note -> append a dated line to that note, drop pending
  NOOP       already fully covered    -> drop from pending (nothing written)
  SUPERSEDE  contradicts an existing  -> mark the old note, then ADD the new one

NEVER DELETES. SUPERSEDE annotates and cross-links; the old note stays on disk and
in git history. This preserves the never-deletes contract held by memory_promote.py
and archive_quarantine().

Cost/bottleneck guarantees (same contract as the other stages):
  - Cron batch path only, never the hot UserPromptSubmit path.
  - Retrieval via the warm RAG sidecar (<100ms); degrades to keyword recall if down.
  - Capped at MAX_PER_RUN LLM calls per cycle; haiku tier (Max subscription, no API).
  - Idempotent: notes carry "reconciled": true once handled.
  - Fail-safe: every error path returns a string, never raises, never blocks cron.

Usage:
    python memory_reconcile.py --dry-run             # verdicts only, writes nothing
    python memory_reconcile.py --dry-run --verbose   # + the candidates considered
    python memory_reconcile.py                       # apply for real
"""

import argparse
import importlib.util
import io
import json
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

try:
    if not getattr(sys.stdout, "_utf8_wrapped", False):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stdout._utf8_wrapped = True
except Exception:
    pass

CLAUDE_DIR = Path(__file__).resolve().parent.parent
SESSION_DIR = CLAUDE_DIR / "session"
SCRIPTS_DIR = CLAUDE_DIR / "scripts"
MEMORY_DIR = CLAUDE_DIR / "memory"
PENDING_NOTES = SESSION_DIR / "pending-notes.json"
RECONCILE_LOG = SESSION_DIR / "reconcile-log.jsonl"

SIDECAR_URL = "http://127.0.0.1:7742/query"
SIDECAR_TIMEOUT = 2.0

MAX_PER_RUN = 5          # LLM calls per cycle -- cost + wall-time bound
CANDIDATE_K = 5          # existing notes shown to the adjudicator
SIM_FLOOR = 0.30         # below this, nothing is close enough to be a duplicate
VALID_VERDICTS = {"ADD", "UPDATE", "NOOP", "SUPERSEDE"}

PROMPT_TEMPLATE = """You are a memory librarian reconciling a NEW note against notes the vault already holds.

NEW NOTE:
title: {title}
content: {content}

EXISTING NOTES:
{candidates}

Decide ONE verdict. Return ONLY strict JSON, no prose:
{{"verdict": "ADD|UPDATE|NOOP|SUPERSEDE", "target": "<exact path of the existing note, or null for ADD>", "reason": "<12 words max>"}}

Rules:
- ADD: the new note states something none of the existing notes cover.
- UPDATE: the new note sharpens, extends, or adds detail to ONE existing note that remains correct.
- NOOP: an existing note already states this. Nothing would be gained by writing it.
- SUPERSEDE: the new note CONTRADICTS an existing note -- the old one is now wrong.
- Prefer NOOP over ADD when an existing note already carries the fact. Duplicate memories are the failure mode we are preventing.
- "target" is REQUIRED and must be copied exactly for UPDATE, NOOP and SUPERSEDE.
"""


def _load_dispatch():
    """Import dispatch() from model_tier_router by path (cwd-independent)."""
    spec = importlib.util.spec_from_file_location(
        "model_tier_router", str(SCRIPTS_DIR / "model_tier_router.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.dispatch


# ── Retrieval ─────────────────────────────────────────────────────────────────

def _retrieve_sidecar(query: str) -> list | None:
    """Warm RAG sidecar. Returns None (not []) when unreachable, so the caller can
    tell 'sidecar down' apart from 'sidecar found nothing'."""
    try:
        payload = json.dumps({"q": query[:500], "top_k": CANDIDATE_K}).encode("utf-8")
        req = urllib.request.Request(
            SIDECAR_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=SIDECAR_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return [
            {"path": r.get("path") or r.get("file", ""),
             "score": float(r.get("score", 0.0)),
             "excerpt": (r.get("content") or "")[:400]}
            for r in body.get("results", [])
        ]
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None


def _retrieve_fallback(query: str) -> list:
    """Keyword recall when the sidecar is down. Deliberately does NOT enable the
    encoder -- a 30s model load has no place in a cron stage."""
    try:
        spec = importlib.util.spec_from_file_location(
            "hybrid_recall_v7", str(SCRIPTS_DIR / "hybrid-recall-v7.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = mod.hybrid_recall(query, tier_1_count=CANDIDATE_K)
        return [
            {"path": n.get("path", ""), "score": float(n.get("final_score", 0.0)),
             "excerpt": n.get("title", "")}
            for n in out.get("notes", [])
        ]
    except Exception:
        return []


def retrieve_candidates(title: str, content: str) -> tuple:
    """Returns (candidates, source)."""
    query = f"{title} {content}"[:500]
    hits = _retrieve_sidecar(query)
    if hits is None:
        return _retrieve_fallback(query), "fallback"
    return hits, "sidecar"


# ── Applying verdicts ─────────────────────────────────────────────────────────

def _resolve(target: str) -> Path | None:
    """Map an adjudicator-returned path onto a real vault file. Returns None rather
    than guessing when it cannot be resolved -- a wrong target would edit the wrong
    memory, which is worse than skipping the reconcile."""
    if not target:
        return None
    t = str(target).strip().replace("\\", "/").lstrip("/")
    direct = MEMORY_DIR / t
    if direct.is_file():
        return direct
    matches = list(MEMORY_DIR.rglob(Path(t).name))
    return matches[0] if len(matches) == 1 else None


def apply_update(path: Path, note: dict) -> bool:
    """Append a dated refinement to an existing note. Append-only: the prior text is
    never rewritten, so the note's history stays readable in git."""
    try:
        line = (f"\n\n**Refined {date.today().isoformat()}:** "
                f"{note.get('content', '').strip()}\n")
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
        return True
    except OSError:
        return False


def apply_supersede(path: Path, note: dict) -> bool:
    """Mark the contradicted note. It is NOT deleted -- it keeps its content and
    gains a pointer, so the contradiction is auditable instead of silent."""
    try:
        marker = (f"\n\n> **[SUPERSEDED {date.today().isoformat()}]** "
                  f"Contradicted by newer memory: {note.get('title', '').strip()}\n"
                  f"> {note.get('content', '').strip()}\n")
        with path.open("a", encoding="utf-8") as f:
            f.write(marker)
        return True
    except OSError:
        return False


def _log(entry: dict) -> None:
    try:
        with RECONCILE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


# ── Main ──────────────────────────────────────────────────────────────────────

def reconcile(dry_run: bool, verbose: bool) -> str:
    try:
        pending = json.loads(PENDING_NOTES.read_text(encoding="utf-8")) if PENDING_NOTES.exists() else []
    except Exception:
        return "pending-notes unreadable -- skipped"
    if not isinstance(pending, list) or not pending:
        return "nothing pending to reconcile"

    todo = [n for n in pending if n.get("distilled") and not n.get("reconciled")]
    if not todo:
        return f"all {len(pending)} pending already reconciled"

    counts = {"ADD": 0, "UPDATE": 0, "NOOP": 0, "SUPERSEDE": 0, "unresolved": 0}
    survivors = []
    llm_calls = 0
    dispatch = None if dry_run else _load_dispatch()

    for note in pending:
        if not note.get("distilled") or note.get("reconciled"):
            survivors.append(note)
            continue
        if llm_calls >= MAX_PER_RUN:
            survivors.append(note)          # cost cap -> defer to the next cycle
            continue

        title = note.get("title", "")
        content = note.get("content", "")
        cands, source = retrieve_candidates(title, content)
        cands = [c for c in cands if c.get("score", 0) >= SIM_FLOOR]

        if verbose:
            print(f"  [{title[:44]}] {len(cands)} candidates via {source}")
            for c in cands:
                print(f"      {c['score']:.2f} {c['path']}")

        # Nothing close enough to duplicate -> unambiguously new.
        if not cands:
            counts["ADD"] += 1
            if not dry_run:
                note["reconciled"] = True
            survivors.append(note)
            continue

        if dry_run:
            print(f"  DRY-RUN would adjudicate: {title[:50]} vs {len(cands)} candidates")
            survivors.append(note)
            continue

        rendered = "\n".join(
            f"- path: {c['path']}\n  excerpt: {c['excerpt'][:200]}" for c in cands)
        prompt = PROMPT_TEMPLATE.format(title=title, content=content, candidates=rendered)

        llm_calls += 1
        try:
            res = dispatch(prompt, "haiku", max_tokens=300)
            if not res.get("ok"):
                raise RuntimeError(res.get("error") or "dispatch not ok")
            text = (res.get("response") or "").strip()
            s, e = text.find("{"), text.rfind("}")
            obj = json.loads(text[s:e + 1]) if s >= 0 and e > s else {}
        except Exception as exc:
            # Adjudication failed -> keep the note and let it ADD. Biased toward
            # keeping knowledge: a duplicate is recoverable, a lost memory is not.
            counts["unresolved"] += 1
            note["reconciled"] = True
            survivors.append(note)
            _log({"ts": date.today().isoformat(), "title": title,
                  "verdict": "ERROR", "error": str(exc)[:120]})
            continue

        verdict = str(obj.get("verdict", "")).strip().upper()
        target = obj.get("target")
        if verdict not in VALID_VERDICTS:
            verdict = "ADD"

        resolved = _resolve(target) if verdict != "ADD" else None
        if verdict != "ADD" and resolved is None:
            # Could not identify the target note -> fail toward ADD, never guess.
            counts["unresolved"] += 1
            verdict = "ADD"

        _log({"ts": date.today().isoformat(), "title": title, "verdict": verdict,
              "target": str(resolved.relative_to(MEMORY_DIR)) if resolved else None,
              "reason": str(obj.get("reason", ""))[:100], "source": source})

        if verdict == "ADD":
            counts["ADD"] += 1
            note["reconciled"] = True
            survivors.append(note)
        elif verdict == "NOOP":
            counts["NOOP"] += 1          # drop: the vault already knows this
        elif verdict == "UPDATE":
            if apply_update(resolved, note):
                counts["UPDATE"] += 1    # folded into the existing note
            else:
                note["reconciled"] = True
                survivors.append(note)   # write failed -> fall back to ADD
        elif verdict == "SUPERSEDE":
            apply_supersede(resolved, note)
            counts["SUPERSEDE"] += 1
            note["reconciled"] = True
            survivors.append(note)       # old note marked, new one still gets written

    if dry_run:
        return (f"DRY-RUN: {len(todo)} to reconcile "
                f"(would ADD {counts['ADD']} outright, adjudicate the rest)")

    try:
        PENDING_NOTES.write_text(json.dumps(survivors, indent=2), encoding="utf-8")
    except OSError as e:
        return f"reconcile computed but pending-notes write failed: {e}"

    return (f"reconciled {llm_calls} via LLM: ADD {counts['ADD']}, UPDATE {counts['UPDATE']}, "
            f"NOOP {counts['NOOP']}, SUPERSEDE {counts['SUPERSEDE']}, "
            f"unresolved {counts['unresolved']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Reconcile pending notes against the vault")
    ap.add_argument("--dry-run", action="store_true", help="decide nothing, write nothing")
    ap.add_argument("--verbose", action="store_true", help="show retrieved candidates")
    args = ap.parse_args()
    try:
        print(reconcile(args.dry_run, args.verbose))
    except Exception as e:                      # belt and braces: cron must never break
        print(f"reconcile error (non-fatal): {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
