---
user-invocable: false
---

# Memory Lifecycle Protocol v1.0 — tiers, deprecation, RAG

> **Created:** 2026-07-29 (triggered by MEMORY.md hitting 85% of the read limit)
> **Owns:** what memory lives where, what moves between tiers, what ages out, what NEVER ages out.
> **Companions:** `memory-recall.md` (read path) · `memory-consolidation.md` (write path) · `memory-system-overview.md` (v6.0 vault reference)

---

## The four tiers

| Tier | Store | Load | Size | Reachable by |
|------|-------|------|------|--------------|
| **A — Identity** | `CLAUDE.md`, `SOUL.md`, `USER.md` | every session, always | small, hand-managed | always in context |
| **B — Hot index** | `projects/<your-project-dir>/memory/MEMORY.md` | every session, always | 🔒 **hard cap 24,400 B** | always in context |
| **C — Warm notes** | `projects/<your-project-dir>/memory/*.md` (a few hundred notes) | on demand | unbounded | ⚠️ **Tier-B links ONLY** |
| **D — Cold vault** | `memory/` — Patterns 491, Decisions 105, Chat 70, Boardroom 46, … | on demand | unbounded | BM25 + vector RAG |

**Short-term = A+B** (always paid for, every single turn).
**Long-term = C+D** (paid for only when retrieved).

The whole game is keeping B small while keeping C and D *findable*.

---

## 🔴 The structural gap (open as of 2026-07-29)

All three indexers — `build-memory-index.py`, `bm25-indexer.py`, `embed-memory-vault.py` — root at
`MEMORY_VAULT = ~/.claude/memory`. **Tier C is not in that tree, so it is not indexed and RAG cannot see it.**

Consequence: a Tier-C note's *only* access path is its line in MEMORY.md. Removing a line does not
demote that memory — it **orphans** it. 218 curated notes are one edit away from being unreachable.

**Therefore the standing precondition:**

> ⛔ **No line may be dropped from MEMORY.md until Tier C is RAG-reachable.**
> Until the bridge exists, compaction means *tightening hooks*, never *removing entries*.

Fix (not yet applied — recall engine was only just repaired 2.2%→68.9%, so it gets tested standalone
before wiring): index Tier C into the RAG, either by adding a second root to the three indexers or by
exposing `projects/*/memory/` under the vault tree. Verify with `scripts/eval_recall.py` **before**
and **after** — a regression in vault recall means back it out.

---

## Deprecation ladder

Deprecation is **de-indexing, never deletion**. A note leaving Tier B stays on disk forever.

```
  OPEN ──(loop closes)──► CLOSED ──(90d cold AND RAG-reachable)──► de-indexed
                                                                   (file stays)
  DOCTRINE ──(contradicted / superseded)──► deleted outright
```

**OPEN** — unfinished action. Full hooks; these are what a session actually needs.
Leaves OPEN only when the loop closes, never on age. An 8-month-old open loop stays open.

**DOCTRINE** — standing rules. **Never ages out.** Terse hooks (the rule is the hook).
The only exit is contradiction: when you reverse a rule, the old note is *deleted*, not archived —
a stale doctrine note that survives is worse than no note, because recall will surface it as current.

**CLOSED** — shipped, resolved, provenance only. Minimal hooks. First to demote, and the only tier
the 90-day age rule touches.

---

## Budgets and cadence

| Threshold | Action |
|-----------|--------|
| 16,000 B (65%) | soft cap — schedule a compaction |
| 20,000 B (82%) | `memory-size-guard.py` WARN fires at SessionStart + on every MEMORY.md Write/Edit |
| 23,000 B (94%) | CRITICAL — compact this session, truncation imminent |
| 24,400 B | Read tool truncates **silently**; bottom entries vanish with no error |

The guard is deliberately **warn-only** — it never blocks a write. Enforcement is this protocol, not the hook.

**Cadence**
- **Every session start** — guard measures, warns if over threshold.
- **Weekly (Monday, cko)** — tier re-scan: move finished loops OPEN→CLOSED; delete contradicted doctrine.
- **Monthly** — demote cold CLOSED lines *(blocked until the RAG bridge lands)*.
- **Continuous (scout-cron)** — Tier D lifecycle already runs: `memory_promote.py` → `memory_distill.py`
  → `memory_reconcile.py`. Append-only, precision-biased, off the hot path. Do not put it on the hot path.

---

## Write path — where a new fact goes

1. **Is it a standing rule?** → Tier B `DOCTRINE` + a `feedback_*` note. Never let it age.
2. **Is it live project state?** → Tier B `OPEN` + a `project_*` note. Demote when the loop closes.
3. **Is it session chatter?** → leave it in `memory/Chat/`. scout-cron promotes what earns it;
   the rest ages out naturally. Chat *is* the archive.

Never hand-write into Tier D. Never let Tier B grow a line for something Tier D already holds —
duplicate memory is the cheapest way to hit the cap.

---

## Non-negotiables

- **Truncation must never happen silently.** The owner always gets told.
- **Preserve every link path and title exactly** when compacting. Rewrite hooks, never identifiers.
- **Deprecation ≠ deletion.** Only contradicted doctrine is deleted.
- **Verify recall before and after** any indexer change (`scripts/eval_recall.py`) — the engine was at
  2.2% accuracy once and nobody noticed until it was measured.
