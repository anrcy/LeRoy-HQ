#!/usr/bin/env python3
"""
memory_promote.py v1.0 -- Slice 1 of the short-term -> long-term promotion loop.

Role in the pipeline (see project_memory_promotion_loop_design_2026_07_20):
  short-term stream (memory/Chat/YYYY-MM-DD.md)  <- captured by scout-cron
    score_and_promote()  <- THIS module (deterministic, precision-biased):
      HIGH   -> append to session/pending-notes.json (existing flush_pending ->
                quality_gate -> write_note_to_vault = LONG-TERM, embedded)
      LOW    -> leave in Chat (ages out naturally; Chat IS the archive)
      MIDDLE -> counted only, deferred to the phase-2 LLM adjudicator

Design guarantees (the anti-bottleneck / anti-repollution contract):
  - Runs in the cron batch path ONLY -- never on the hot UserPromptSubmit path.
  - NEVER deletes anything. Promotion is append-only; low-signal simply isn't touched.
  - Idempotent: a content-hash seen-set (session/promote-seen.json) means a line is
    never promoted twice, so re-running every minute is safe and cheap.
  - PRECISION-BIASED: heavily rejects tool-activity noise ([BASH]/[WRITE]/[EDIT]/paths/
    commands). We just cleaned 65% junk out of the vault -- do not put it back.
  - Fail-safe: any error returns a string, never throws, never blocks the cron.

Standalone usage:
    python memory_promote.py --dry-run     # preview scoring, write nothing
    python memory_promote.py               # promote HIGH candidates for real
    python memory_promote.py --dry-run --verbose   # show per-candidate scores
"""

import json
import re
import sys
import io
import hashlib

# Force UTF-8 stdout so non-ASCII in captured content can't crash the cron (cp1252 default)
try:
    if not getattr(sys.stdout, "_utf8_wrapped", False):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stdout._utf8_wrapped = True
except Exception:
    pass
from datetime import date, datetime, timezone
from pathlib import Path

CLAUDE_DIR = Path(__file__).parent.parent
MEMORY_DIR = CLAUDE_DIR / "memory"
SESSION_DIR = CLAUDE_DIR / "session"
CHAT_DIR = MEMORY_DIR / "Chat"
PENDING_NOTES = SESSION_DIR / "pending-notes.json"
SEEN_FILE = SESSION_DIR / "promote-seen.json"

# --- Thresholds (tunable). Precision-biased: HIGH bar so only clear signal promotes. ---
PROMOTE_HIGH = 5     # >= this -> promote to pending-notes (calibrated: genuine
                     # decisions/preferences cluster at 5; tool-noise is <=0, gap is wide)
AGE_LOW = 2          # <= this -> ignore (ages out in Chat)
                     # between (3-4) = MIDDLE, deferred to phase-2 LLM adjudicator
SEEN_CAP = 4000      # cap the seen-set so it can't grow unbounded
MAX_PROMOTE_PER_RUN = 12   # clamp a pathological signal-flood; excess stays UNSEEN and
                           # promotes on later runs (drains over time, nothing lost)
PENDING_BACKPRESSURE = 40  # if this many notes already await distillation, stop promoting
                           # so the queue can't outrun the distiller (hard bound on pending)

# Activity-log line types that are NEVER durable knowledge (hard reject).
ACTIVITY_TYPES = {"BASH", "WRITE", "EDIT", "READ", "GLOB", "GREP", "TASK", "AGENT"}

# Strong durable-signal keywords, weighted. Decisions/commitments/money/identity
# are worth more than generic project chatter.
SIGNAL_WEIGHTS = [
    (3, r'\b(decid\w+|decision|we will|going with|chose|agreed)\b'),
    (3, r'\b(always|never|must|do not|don\'t ever|from now on)\b'),
    (3, r'\b(prefer|preference|rule|policy|doctrine|law)\b'),
    (2, r'\b(contract|retainer|invoice|price|pricing|\$[\d,]+|quote)\b'),
    (2, r'\b(client|deal|partner|vendor)\b'),
    (2, r'\b(remember|important|critical|key|note that)\b'),
    (2, r'\b(deadline|due|schedule|meeting|call on)\b'),
    (1, r'\b(project|build|ship|deploy|fix|feature)\b'),
]
SIGNAL_RES = [(w, re.compile(p, re.IGNORECASE)) for w, p in SIGNAL_WEIGHTS]

# Noise markers that subtract (paths, extensions, code, URLs, shell).
NOISE_RES = [
    re.compile(r'[A-Za-z]:\\|/[A-Za-z]+/|\.\w{2,4}\b'),   # paths / file extensions
    re.compile(r'https?://'),                              # URLs
    re.compile(r'\b(cp|rm|cd|mkdir|gh|git|python|npm|schtasks)\b'),  # commands
    re.compile(r'[{}<>|;`]'),                              # code punctuation
]

PREFIX_RE = re.compile(r'^\s*-?\s*\[[^\]]+\]\s*\[[^\]]*\]\s*\[([A-Z]+)\]\s*(.*)$')
PROMPT_PREFIX_RE = re.compile(r'^\s*-?\s*\[[^\]]+\]\s*\[[^\]]*\]\s*\[PROMPT\]\s*(.*)$', re.IGNORECASE)


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def content_hash(text: str) -> str:
    return hashlib.sha1(text.strip().lower().encode("utf-8", "replace")).hexdigest()[:16]


def parse_line(line: str):
    """Return (activity_type, body) for a Chat capture line, or (None, raw)."""
    m = PREFIX_RE.match(line)
    if m:
        return m.group(1).upper(), m.group(2).strip()
    return None, line.strip().lstrip("- ").strip()


def score(body: str, activity_type) -> int:
    """Deterministic durability score. Higher = more worth long-term promotion."""
    if activity_type in ACTIVITY_TYPES:
        return -99  # hard reject: tool-activity log, never knowledge
    if len(body) < 25:
        return 0
    s = 0
    for w, rx in SIGNAL_RES:
        if rx.search(body):
            s += w
    # explicit pin gets an extra bump
    if re.search(r'\b(remember|from now on|always|never)\b', body, re.IGNORECASE):
        s += 2
    # noise penalties
    for rx in NOISE_RES:
        if rx.search(body):
            s -= 2
    # very long raw dumps are usually logs, not distilled knowledge
    if len(body) > 400:
        s -= 1
    return s


def infer_type_and_tags(body: str):
    b = body.lower()
    if re.search(r'\b(always|never|prefer|from now on|rule|policy|doctrine)\b', b):
        return "feedback", ["preference"]
    if re.search(r'\b(decid|decision|we will|chose|agreed|going with)\b', b):
        return "decision", ["decision"]
    if re.search(r'\b(client|deal|contract|price|retainer|invoice)\b', b):
        return "project", ["project"]
    return "pattern", ["patterns"]


def make_title(body: str) -> str:
    words = re.sub(r'\s+', ' ', body).strip().split(" ")
    return " ".join(words[:9])[:80] or "Untitled promotion"


def score_and_promote(dry_run: bool, verbose: bool = False) -> str:
    """Score today's Chat capture stream; promote HIGH candidates to pending-notes."""
    target = CHAT_DIR / f"{date.today().isoformat()}.md"
    if not target.exists():
        return "no Chat file today -- nothing to score"

    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        return f"could not read Chat file: {e}"

    seen = set(load_json(SEEN_FILE, []) or [])
    pending = load_json(PENDING_NOTES, []) or []
    if not isinstance(pending, list):
        pending = []

    promoted = deferred = aged = skipped_seen = held = 0
    new_seen = []
    details = []

    # Backpressure: how many notes already await distillation? If the queue is deep,
    # stop promoting so it can't outrun the distiller (hard bound on pending size).
    undistilled = sum(1 for n in pending if isinstance(n, dict) and not n.get("distilled"))
    promo_open = undistilled < PENDING_BACKPRESSURE

    for line in lines:
        if not line.strip() or line.startswith("#") or line.startswith("##"):
            continue
        atype, body = parse_line(line)
        if not body:
            continue
        h = content_hash(body)
        if h in seen:
            skipped_seen += 1
            continue
        sc = score(body, atype)
        if sc >= PROMOTE_HIGH and (not promo_open or promoted >= MAX_PROMOTE_PER_RUN):
            # would promote, but per-run cap or backpressure is in effect -> HOLD it
            # (do NOT mark seen; it will promote on a later run once the queue drains)
            held += 1
            continue
        if sc >= PROMOTE_HIGH:
            ntype, tags = infer_type_and_tags(body)
            note = {
                "title": make_title(body),
                "content": (f"{body}\n\n**Source:** promoted from short-term Chat capture "
                            f"{date.today().isoformat()} by memory_promote.py (score {sc})."),
                "type": ntype,
                "domain": "general",
                "project": "meta",
                "tags": tags,
            }
            pending.append(note)
            promoted += 1
            new_seen.append(h)
            if verbose:
                details.append(f"  PROMOTE [{sc}] {note['title']}")
        elif sc <= AGE_LOW:
            aged += 1
            new_seen.append(h)  # mark seen so we don't re-evaluate log noise forever
        else:
            deferred += 1  # MIDDLE band -- leave for phase-2 LLM; do NOT mark seen
            if verbose:
                details.append(f"  defer   [{sc}] {body[:60]}")

    if not dry_run:
        # Persist promotions
        if promoted:
            PENDING_NOTES.parent.mkdir(parents=True, exist_ok=True)
            PENDING_NOTES.write_text(json.dumps(pending, indent=2, ensure_ascii=False),
                                     encoding="utf-8")
        # Update seen-set (append + cap)
        merged = list(seen) + new_seen
        if len(merged) > SEEN_CAP:
            merged = merged[-SEEN_CAP:]
        SEEN_FILE.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")

    summary = (f"{'DRY-RUN ' if dry_run else ''}scored Chat/{date.today().isoformat()}: "
               f"promoted {promoted} -> pending, held {held} (cap/backpressure), "
               f"deferred {deferred} (middle band), aged {aged} low-signal, "
               f"skipped {skipped_seen} already-seen")
    if verbose and details:
        summary += "\n" + "\n".join(details[:30])
    return summary


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    print(f"memory_promote [{now_ts()}]")
    print(score_and_promote(args.dry_run, args.verbose))


if __name__ == "__main__":
    main()
