---
name: agent-return-contract
description: "Return contract for all spawned agents — required deliverable shape, timeout, and the re-prompt-once rule. Load when dispatching agents, when an agent goes idle without reporting, or when diagnosing empty returns. Enforced by hooks/agent-return-validator.py on SubagentStop."
user-invocable: false
---

# Agent Return Contract

> **Status:** v1.0, 2026-08-08. Enforced by `hooks/agent-return-validator.py`
> (`SubagentStop`) → `session/agent-returns.jsonl`.

## Why this exists

Forensics on 2026-08-08 measured the failure directly. During a single
investigation session, **six spawned agents went idle without delivering**:
`alignment-monitor`, `hr`, `routing-audit`, two built-in `Plan` debaters, and
`coo-proof`. Three never delivered at all, even after three explicit requests
each. Two were built-in agent types, so this is **not** a custom-roster defect.

Before this contract there was no `SubagentStop` hook registered anywhere in
`settings.json`, so **no agent return was ever observed**. A silently idle agent
looked exactly like an agent that had nothing to say, and both looked exactly
like success.

This is the hard prerequisite for department fan-out. At a 20% per-agent silent
idle rate, a 6-agent department has a **~74% chance** that at least one member
returns nothing — and without this contract, nobody would know which.

## The contract

**Every dispatched agent MUST end its run with a final message that is its
deliverable.** Not a status line, not "task complete", not a promise to report.
The final assistant message *is* the return value.

| Requirement | Rule |
|---|---|
| Non-empty | ≥ 40 characters of substantive final text |
| Self-contained | Readable without the dispatcher re-reading its transcript |
| Direct | The answer/finding itself, not a pointer to where it lives |
| Explicit on failure | If it could not complete, say so and say why |

**"I found nothing" is a valid return. Silence is not.** An agent that
determines there is no finding must say that explicitly — that is a result.

## Classification

`hooks/agent-return-validator.py` records one status per completion:

| Status | Meaning |
|---|---|
| `RETURNED` | ≥40 chars of final text — contract met |
| `THIN_RETURN` | 1-39 chars — acknowledgement, not a deliverable |
| `EMPTY_RETURN` | no final text at all — contract violated |

## Dispatcher rules

1. **Re-prompt once.** On `EMPTY_RETURN` or `THIN_RETURN`, send exactly one
   follow-up demanding the deliverable, stating "no further tool calls" and
   "do not return empty".
2. **Then mark it failed.** If the second attempt is still empty, record the
   agent as failed and surface that to the user. **Do not chase a third time** —
   three of the six failures above consumed three requests each and still
   produced nothing.
3. **Never silently substitute.** If an agent fails to return and the
   dispatcher does the work itself instead, say so plainly. Reporting an agent's
   contribution that did not happen is the exact failure mode this whole effort
   exists to eliminate.
4. **Never infer a result.** An agent that returned nothing produced nothing.
   Do not guess what it would have said.

## Prompt requirements when dispatching

Every spawn prompt must state:
- the deliverable shape ("return a table of X", "return PASS/FAIL plus reason"),
- that the final message is the return value,
- that returning empty is a failure and "nothing found" must be said explicitly.

## Known limitation

The `SubagentStop` payload shape is undocumented in this repo. The validator
captures `payload_keys` (key **names** only, never values) so the real schema can
be confirmed from live firings. Once confirmed, tighten `_extract_agent` /
`_extract_output` in `hooks/agent-return-validator.py` and drop that field.

Related: [[session-gate]] · [[fork-dispatch]] · [[position-zero-enforcement]]
