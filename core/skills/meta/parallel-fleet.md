---
name: parallel-fleet
description: When and how to fan agents out across isolated git worktrees, how beads map to worktrees, and the merge protocol. Policy only — the mechanism is already native to the harness.
context: load-on-trigger
tags: [orchestration, worktree, parallel, beads, fleet]
trigger: "fan out" | "parallel agents" | "worktree fleet" | "run these in parallel"
version: 1.0
created: 2026-07-23
---

# Parallel Fleet — Worktree Isolation Policy

> Gas Town's contribution, minus its machinery. Yegge's system coordinates 20-30 agents
> on one codebase via Mayor/Rigs/Crew/Polecats. LeRoy already has that coordination
> layer — COO routing, `skills/meta/goal-engine.md`, `agents/goal-overseer.md`. What it
> lacked was a rule for **when parallelism is worth its cost** and **how the work merges
> back**. That is all this file provides.

**Build no orchestration code.** The mechanism is native:
- `Agent` tool with `isolation: "worktree"` — fresh git worktree per agent
- `EnterWorktree` / `ExitWorktree` tools
- `Workflow` tool for deterministic fan-out (opt-in only, see below)

---

## When to Fan Out

All three must hold:

1. **≥3 genuinely independent work items** — each a bead with its own artifact.
   Two items are faster sequentially than the worktree setup cost.
2. **They touch the same repo** — otherwise plain background agents suffice; worktrees
   solve *write collisions*, nothing else.
3. **No shared ordering dependency** — if B needs A's output, that is a pipeline, not
   a fleet. Use `pipeline()`, not `parallel()`.

**Do NOT fan out for:**
- Read-only work (searching, auditing, reviewing) — no writes, no collisions, no need
  for isolation. Use plain background agents.
- Anything touching `~/.claude` itself. The harness is the running system; parallel
  agents editing live hooks and skills is how you get a 3 AM outage.
- Fewer than 3 items. Worktree setup is ~200-500ms plus disk per agent.

**Cost is real:** worktrees are the most expensive isolation mode available. The
`Agent` tool's own guidance says use it ONLY when agents mutate files in parallel and
would otherwise conflict.

---

## Beads Map 1:1 to Worktrees

One bead → one worktree → one agent → one merge.

```
B-A1B2C3  "Migrate auth module"     -> worktree 1 -> agent -> diff -> guardian -> merge
B-D4E5F6  "Migrate billing module"  -> worktree 2 -> agent -> diff -> guardian -> merge
B-G7H8J9  "Migrate report module"   -> worktree 3 -> agent -> diff -> guardian -> merge
```

Before spawning, each bead must have:
- A **single, stated artifact** (the file or set of files it owns)
- **No artifact overlap** with any other bead in the fleet — overlapping artifacts mean
  the beads are not independent and the fan-out is wrong
- An **owner agent** recorded via `python scripts/bead_manager.py update <id> --owner <agent>`

Record progress on the bead, not in chat. That is the entire point: a bead survives
compaction and handoff, so an interrupted fleet can be resumed rather than re-derived.

---

## Merge Protocol (Gas Town's "refinery", mapped to LeRoy's gate)

Yegge's refinery merges agent output. LeRoy already has a stricter version of that
component — guardian. Use it rather than inventing a second one.

1. **Agent completes** → marks its bead `done` and attaches artifact paths.
2. **Guardian audits each worktree diff separately**, before any merge. Per-worktree,
   never on the combined result — a combined diff hides which agent introduced what,
   which defeats guardian's `file:line` evidence contract.
3. **Merge only guardian-APPROVED diffs.** A BLOCKED diff sends its bead back to
   `blocked` with the finding recorded in `blocked_by`.
4. **Merge sequentially, re-checking between merges.** Three diffs that are each
   individually clean can still conflict pairwise. Sequential merge surfaces that;
   simultaneous merge buries it.
5. **Unchanged worktrees are auto-removed** by the harness — no cleanup step needed.

---

## Failure Modes

| Symptom | Cause | Fix |
|---|---|---|
| Agents produce conflicting edits | Beads shared an artifact | Re-split beads by file ownership |
| Fleet slower than sequential | Fewer than 3 items, or setup dominates | Don't fan out |
| Merge conflicts at step 5 | Beads were not independent | Sequential merge; re-scope the split |
| Lost work after compaction | Progress tracked in chat, not beads | Record on the bead as you go |
| Harness instability | Fleet edited `~/.claude` | Never fan out on the harness itself |

---

## Relationship to the Workflow Tool

`Workflow` also fans out, deterministically, and supports `isolation: "worktree"`
per agent. It requires **explicit user opt-in** — it can spawn dozens of agents and
consume a large token budget, so it is never inferred from a task that merely looks
parallel.

- **This skill** = the policy for when parallelism is justified and how it merges.
- **Workflow** = one mechanism for executing it, when you have explicitly asked.

## Related

[[goal-engine]] | [[minimalism-audit]] | [[hil-gate]] |
`scripts/bead_manager.py` — the ledger the fleet coordinates through.
