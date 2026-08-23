"""memory_policy.py — single source of truth for memory-lifecycle constants.

Created 2026-08-21 (memory-system overhaul, Phase 1). Before this file, three
components carried three different threshold sets (doctrine 20k/23k, the
size-guard hook 12k/20k, agents/cko.md 25KB — above the Read truncation point).
Every consumer imports or cites THIS file; numbers stated anywhere else are
documentation, not policy.

Consumers: hooks/memory-size-guard.py, scripts/scout-cron.py,
scripts/memory-status.py, scripts/quality_gate.py (recalibration, Phase 4).
Doctrine mirrors: skills/meta/memory-lifecycle.md, agents/cko.md.
"""

# ── MEMORY.md (Tier B routing card) size budget, bytes ────────────────────────
READ_LIMIT = 24400   # Read-tool silent truncation point — the hard ceiling
TARGET     = 7000    # steady-state routing-card size
WARN_AT    = 12000   # guard warns (calibration of 2026-07-31, kept)
CRITICAL_AT = 20000  # guard escalates

# ── Card structure caps (earning test, memory-lifecycle.md) ───────────────────
OPEN_LOOPS_CAP  = 15
SAFETY_RAILS_CAP = 15

# ── Quality gate (/65 scale — quality_gate.py) ────────────────────────────────
# Admission runs on the 35-point core (frontmatter/tags/length); wikilink points
# are post-admission enrichment (links are added by consolidation AFTER approval,
# so they are structurally unearnable at admission time).
GATE_APPROVE    = 40   # legacy full-scale threshold (pre-recalibration)
GATE_QUARANTINE = 25
GATE_CORE_MAX   = 35   # max earnable at admission

# ── Corpus perimeter ─────────────────────────────────────────────────────────
# Path segments excluded from every recall corpus (build-memory-index.py
# VENDORED_SEGMENTS and vault-rag-sidecar _EXCLUDED_DIRS must include these).
PERIMETER_SEGMENTS = {"archive", "backup"}

# ── Lifecycle invariants ─────────────────────────────────────────────────────
# 1. Prune/demote/score automation may only touch paths classified `note` in
#    session/corpus-manifest.json; unclassified paths are untouchable.
# 2. Destructive actions stage (move + receipt), never delete; human-run purge
#    after STAGING_RETENTION_DAYS.
# 3. SAFETY RAILS card entries are never auto-demoted.
# 4. Index verification is by membership (path appears in the index), never by
#    mtime.
STAGING_RETENTION_DAYS = 30
RECEIPTS_DIR_NAME = "logs/receipts"
