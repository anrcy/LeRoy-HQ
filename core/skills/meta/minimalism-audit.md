---
name: minimalism-audit
description: Ponytail-style minimalism ruleset — challenge whether code/files should exist at all before they are written, audit the harness for accumulated bloat, and record deliberate shortcuts in a debt ledger.
context: load-on-trigger
tags: [quality, minimalism, audit, debt, harness, guardian]
trigger: "audit harness" | "minimalism audit" | "ponytail audit" | "what can we delete"
version: 1.0
created: 2026-07-23
---

# Minimalism Audit — The Best Code Is the Code You Never Wrote

> Ported (not installed) from Ponytail, MIT-licensed, ~74K stars. Ported rather than
> vendored per `feedback_no_paid_saas_default` and so the rules can reference LeRoy's
> own structures. No upstream dependency, nothing to drift.

Guardian audits changes for scope, security, and hygiene at commit time. It never asks
the prior question: **should this exist at all?** That gap is why the harness accumulates
— `skills/meta/skill-shopping.md` still claims "Total: 32 skills across 8 categories"
while `skills/meta/` alone holds ~95 files.

---

## The Four Questions (Pre-Write Gate)

Before creating any new file, script, skill, agent, or abstraction, answer all four.
An unanswered question is a WARN finding, not a formality.

1. **Is this needed at all?** What breaks if it simply doesn't exist? If the answer is
   "nothing measurable", stop here.
2. **Does something already do it?** Search `skills/`, `scripts/`, `agents/`, and
   `session/skill-index.json` first. LeRoy has ~200 skill files and ~300 scripts —
   the odds that a capability is genuinely absent are lower than they feel.
3. **Is there a native mechanism?** The harness already provides hooks, `Skill`,
   `Agent` with `isolation: "worktree"`, TodoWrite, CronCreate, MCP servers. Reimplementing
   any of these in Python is the most common form of waste here.
4. **Can an existing file be extended?** A new section in a live file beats a new file
   that must be discovered, indexed, routed, and maintained.

**Escape hatch:** answering "yes, build it anyway" is legitimate — but it goes in the
debt ledger with a reason, not silently.

---

## Anti-Patterns This Catches

| Pattern | Why it's waste | Instead |
|---|---|---|
| Speculative parameter/flag | No caller today; the shape is a guess | Add when the second caller appears |
| Single-use abstraction | Indirection with one implementation | Inline it |
| Parallel script for a variant | Two files drift; one gets stale | One script, one flag |
| New skill duplicating a section | Splits routing; both rot | Extend the existing skill |
| Re-implementing a native mechanism | Maintained forever, worse than built-in | Use the harness |
| Defensive layer for an impossible state | Untested code that hides real bugs | Let it fail loudly |
| Doc describing intent, not behavior | Goes stale the first time code changes | Describe behavior, or delete |

---

## Harness Audit (Primary Target)

The first target is LeRoy itself, not application code — that is where the measured
bloat is. Run report-only; triage before deleting anything.

**Dimensions:**

1. **Never-loaded skills** — cross-reference `scripts/skill-usage-tracker.py` output and
   `session/agent-last-used.json`. A skill never loaded in 90 days is a deletion candidate.
2. **Orphans and dead references** — already owned by `agents/alignment-monitor.md`;
   cite its findings rather than recomputing them.
3. **Overlapping-content clusters** — skills whose trigger sets or content substantially
   duplicate. These split routing so neither reliably wins.
4. **Stale inventories** — self-reported counts that no longer match reality
   (the skill-shopping "32 skills" claim is the canonical example).
5. **Superseded scripts** — `.bak`, `.broken`, `-v2`, `.py.bak-*` siblings of live files.
6. **Backup/copy trees inside the vault** — packaged-app copies (`win-unpacked/`,
   `release/`) and derived rewrites (`caveman-versions/`). These are not just dead weight:
   they were actively poisoning retrieval until excluded on 2026-07-23.

**Order of operations — non-negotiable:**
1. Report only. Produce findings with paths and evidence.
2. Triage into the debt ledger.
3. Delete in a separate, reviewable pass — never in the same run that found them.

**Never auto-delete.** Vault doctrine is never-deletes (see `memory_reconcile.py`,
`archive_quarantine()`); archive or mark superseded instead.

---

## Debt Ledger

Deliberate shortcuts get recorded when taken, not remembered. `session/debt-ledger.jsonl`,
one JSON object per line:

```json
{"ts":"2026-07-23T22:10:00Z","file":"scripts/memory_reconcile.py","shortcut":"UPDATE appends a dated line rather than merging prose","rationale":"merge needs a diff model; append is auditable and reversible","revisit_by":"2026-09-01"}
```

| Field | Meaning |
|---|---|
| `ts` | When the shortcut was taken |
| `file` | Where it lives |
| `shortcut` | What was deferred, in one line |
| `rationale` | Why it was the right call *then* |
| `revisit_by` | Date this should be re-examined (or `null` for accepted-forever) |

A shortcut with no ledger entry is not a shortcut — it is a bug waiting to be
rediscovered. Surface overdue entries during the Monday cleanup.

---

## Guardian Integration

Guardian runs this as **Phase 5b** inside its existing Phase 5 Standard Audit
(`agents/guardian.md`). Applies to new-file creation and net-LOC growth:

- Four questions unanswered on a new file → **WARN**, with `file:line` evidence
  (consistent with guardian's existing evidence contract — findings without
  `file:line` are INVALID).
- New file duplicating an existing skill/script → **WARN**, name the existing path.
- Deliberate shortcut with no ledger entry → **WARN**.

This never blocks a commit on its own. Minimalism is a judgment call; guardian's BLOCK
severities stay reserved for security, scope, and naming conflicts.

---

## Reporting

```yaml
minimalism_audit:
  target: harness            # harness | code
  mode: report-only
  scanned: 312
  findings:
    - kind: never_loaded
      path: skills/domains/example.md
      evidence: "no load event in skill-usage-tracker since 2026-03-11"
      recommend: archive
    - kind: stale_inventory
      path: skills/meta/skill-shopping.md
      evidence: "claims 32 skills; skills/meta alone has 95"
      recommend: regenerate from session/skill-index.json
  ledger_entries_added: 3
  deletions_performed: 0     # ALWAYS 0 in a report-only pass
```

## Related

[[skill-shopping]] | [[orphan-detection]] | [[quick-trigger-maintenance]] |
[[grill-me]] — grill-me closes the spec; this asks whether to build it at all.
