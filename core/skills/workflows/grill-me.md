---
name: grill-me
description: Adversarial requirements interrogation before an autonomous goal runs. Asks one focused question at a time, each with a recommended answer, until the spec is closed. Pre-flight for High Effort /goal ONLY.
context: load-on-trigger
tags: [planning, requirements, goal, high-effort, spec]
trigger: "grill me" | High Effort /goal pre-flight (automatic)
version: 1.0
created: 2026-07-23
---

# Grill Me — Spec Closure Before Autonomous Execution

> Ported from Matt Pocock's `grill-me` (2026). Premise: most agent failures are not
> bad code generation — they are underspecified requirements. The cheapest moment to
> catch that is before execution, not during.

---

## When This Fires

**Automatically:** exactly one place — the High Effort branch of `/goal`, between goal
creation and manifest approval, **before** `goal-overseer` spawns. See
`skills/meta/goal-engine.md` (auto-tier rule, line ~35).

**Manually:** the user says "grill me".

**Never fires for:** Standard-tier goals, fast lane, or ordinary chat. This bound is
deliberate — it protects `feedback_max_autonomy_no_goal_prompts`. Grilling *replaces*
questions you would otherwise answer at manifest approval; it does not add a new
interrogation to normal work.

**Rationale for the bound:** a High Effort goal is 20–50 steps, overnight-capable, and
driven by an autonomous overseer off a single line of user input. A wrong assumption at
step 2 costs the whole run. Standard goals are <15 min and correctable in flight.

---

## The Loop

Repeat until no HIGH-uncertainty items remain (hard cap: 7 questions).

1. **Identify the largest unresolved uncertainty** in the goal as stated. Rank by
   blast radius — what would cost the most to get wrong at step 40?
2. **Ask exactly ONE question.** Never batch. Batched questions get batched,
   low-information answers.
3. **Supply a recommended answer**, drawn from vault context (recall the relevant
   notes first) and labeled with why. You should be able to reply "yes" and move on.
4. **Record the answer** into the goal's manifest as an explicit constraint.
5. **Re-derive dependent uncertainties.** Answers collapse some questions and open
   others — recompute the list rather than working a fixed queue.

Stop when remaining uncertainties are all LOW, or at 7 questions, whichever comes first.
Hitting the cap is a signal the goal is too big — recommend splitting it.

---

## What Counts As a Real Question

Ask about things that **change what gets built**:

| Ask about | Example |
|---|---|
| Ambiguous scope boundary | "Does 'all clients' include the archived CRM records, or active only?" |
| Unstated success criteria | "What makes this done — the file exists, or the email actually sent?" |
| Irreversible actions | "Should step 12 send live, or draft for review?" |
| Contested defaults | "Vault says never send from personal gmail. Confirm the work identity for all sends?" |
| Resource/authority limits | "Can this spend tokens overnight, or stop at the daily cap?" |

Do **not** ask about:
- Anything answerable from the vault, code, or git history — **look it up instead**
- Style preferences with an established doctrine (check `memory/Feedback/` first)
- Implementation detail the overseer can decide in flight
- Confirmation of things already stated in the goal text

**A question you could have answered by reading is a failure of this skill.**
Recall first, ask second.

---

## Output Format

Per question:

```
[GRILL 2/7] Scope boundary
Q: Does the overnight sweep include the paused account?
   Recommended: NO — memory/…/outreach-stopped excludes them from all
   target lists.
   Impact if wrong: 40 outbound emails to an excluded account.
```

On completion, append the closed spec to the goal manifest:

```
SPEC CLOSED (7 questions, 3 assumptions corrected)
- Scope: active CRM clients only, paused accounts excluded
- Sends: draft-only, no live send without HiL gate
- Done: recipient confirmed in the CRM, not just file written
```

---

## Integration

| Piece | Role |
|---|---|
| `skills/meta/goal-engine.md` | Calls this in the High Effort branch before overseer spawn |
| `scripts/goal_manager.py` | Closed spec is stored on the goal via the manifest |
| `agents/goal-overseer.md` | Reads the closed spec as binding constraints |
| `skills/meta/memory-recall.md` | Recall runs BEFORE questioning, to source recommendations |
| `skills/meta/hil-gate.md` | Irreversible actions surfaced here still pass the HiL gate at execution |

## Related

[[goal-engine]] | [[hil-gate]] | [[minimalism-audit]] — grill-me closes the spec,
minimalism-audit then asks whether the thing needs building at all.
