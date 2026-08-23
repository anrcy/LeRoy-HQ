#!/usr/bin/env python3
"""
Agent Spawn Validator v2.0
PreToolUse hook — fires on Task, WebFetch, WebSearch, Write, Edit tool calls.

v1.0: Governance tier validation for agent spawning
v2.0: + Network policy enforcement (WebFetch) [NemoClaw-inspired]
      + Filesystem zone policy (Write/Edit) [NemoClaw-inspired]
      Soft enforcement throughout: log + warn, hard block on explicit blocklist only.

Performance target: <50ms
"""

import json
import sys
import io
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

# Force UTF-8 encoding for stdin
if sys.stdin.encoding != 'utf-8':
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8', errors='replace')

# Paths
CLAUDE_ROOT = Path.home() / ".claude"
AGENTS_DIR = CLAUDE_ROOT / "agents"
SESSION_DIR = CLAUDE_ROOT / "session"
SPAWN_LOG = SESSION_DIR / "agent-spawn-log.jsonl"
NETWORK_LOG = SESSION_DIR / "network-policy-log.jsonl"
FS_LOG = SESSION_DIR / "filesystem-policy-log.jsonl"
POLICY_FILE = SESSION_DIR / "network-policy.yaml"

# Allowed filesystem zones (soft enforcement — warn only, never block)
ALLOWED_FS_ZONES = [
    str(Path.home() / ".claude"),
    str(Path.home() / "Desktop" / "Projects"),
]

# Governance rule map — keyed by agent filename (without .md)
#
# Coverage: all 37 spawnable agents. Previously only 10 were listed, so the other
# 27 silently defaulted to `tier = 4` at handle_task():270 and were never checked.
#
# Enforcement is SOFT — handle_task() always returns {"decision": "allow"} and only
# attaches a warning. Entries here therefore affect reporting and warnings, never
# execution.
#
# KNOWN DOCTRINE CONFLICT (surfaced, not silently encoded):
#   * org-governance.md:65-138 declares Tier 5 "read-only", but scout and planner
#     legitimately write session/growth-output.md and session/planner-output.md.
#     Encoding read-only literally would fire a false violation on every scout
#     spawn, so Tier 5 forbids only NotebookEdit here. Resolve the doctrine, then
#     tighten this.
#   * legal is Tier 1 in this map (pre-existing) and in conductor.md:18, but Tier 4
#     Specialist in org-governance.md:119. Left at Tier 1 to preserve behaviour.
#   * auditor is the documented read-only exception at org-governance.md (Tier 4).
GOVERNANCE = {
    # Tier 1 C-Suite: NO Edit/Write/NotebookEdit
    "conductor":         {"tier": 1, "forbidden": ["Edit", "Write", "NotebookEdit"]},
    "cto":               {"tier": 1, "forbidden": ["Edit", "Write", "NotebookEdit"]},
    "cfo":               {"tier": 1, "forbidden": ["Edit", "Write", "NotebookEdit"]},
    "cko":               {"tier": 1, "forbidden": ["Edit", "Write", "NotebookEdit"]},
    "legal":             {"tier": 1, "forbidden": ["Edit", "Write", "NotebookEdit"]},
    # Tier 2 VP: NO Edit/Write/NotebookEdit
    "vp-engineering":    {"tier": 2, "forbidden": ["Edit", "Write", "NotebookEdit"]},
    # Tier 3 Management: NotebookEdit forbidden only
    "chief-of-staff":    {"tier": 3, "forbidden": ["NotebookEdit"]},
    "secretary":         {"tier": 3, "forbidden": ["NotebookEdit"]},
    "scrum-leader":      {"tier": 3, "forbidden": ["NotebookEdit"]},
    "hr":                {"tier": 3, "forbidden": ["NotebookEdit"]},
    "tech-lead":         {"tier": 3, "forbidden": ["NotebookEdit"]},
    # Tier 4 Specialists: full write access (auditor is the read-only exception)
    "builder":           {"tier": 4, "forbidden": []},
    "designer":          {"tier": 4, "forbidden": []},
    "forge":             {"tier": 4, "forbidden": []},
    "professor":         {"tier": 4, "forbidden": []},
    "guardian":          {"tier": 4, "forbidden": []},
    "janitor":           {"tier": 4, "forbidden": []},
    "proposal-writer":   {"tier": 4, "forbidden": []},
    "cyber-operator":    {"tier": 4, "forbidden": []},
    "integratorOS-agent":{"tier": 4, "forbidden": []},
    "goal-overseer":     {"tier": 4, "forbidden": []},
    "etsy-overseer":     {"tier": 4, "forbidden": []},
    "etsy-rd":           {"tier": 4, "forbidden": []},
    "etsy-manager":      {"tier": 4, "forbidden": []},
    "etsy-designer":     {"tier": 4, "forbidden": []},
    "auditor":           {"tier": 4, "forbidden": ["Edit", "Write", "NotebookEdit"]},
    # Tier 5 Support: advisory/read-mostly (see doctrine conflict note above)
    "scout":             {"tier": 5, "forbidden": ["NotebookEdit"]},
    "planner":           {"tier": 5, "forbidden": ["NotebookEdit"]},
    "analyst":           {"tier": 5, "forbidden": ["NotebookEdit"]},
    "quick":             {"tier": 5, "forbidden": ["NotebookEdit"]},
    "scraper":           {"tier": 5, "forbidden": ["NotebookEdit"]},
    "alignment-monitor": {"tier": 5, "forbidden": ["NotebookEdit"]},
    "recon-agent":       {"tier": 5, "forbidden": ["NotebookEdit"]},
    "ai-sec-agent":      {"tier": 5, "forbidden": ["NotebookEdit"]},
    "simulator":         {"tier": 5, "forbidden": ["NotebookEdit"]},
    "skill-matcher":     {"tier": 5, "forbidden": ["NotebookEdit"]},
    "mesh-wrapper":      {"tier": 5, "forbidden": ["NotebookEdit"]},
}


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_stdin() -> dict:
    try:
        return json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        return {}


def extract_frontmatter_tools(agent_file: Path) -> list:
    """Parse YAML frontmatter from agent .md file. Returns tool list."""
    if not agent_file.exists():
        return []
    try:
        content = agent_file.read_text(encoding='utf-8', errors='replace')
    except Exception:
        return []

    fm_match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
    if not fm_match:
        return []

    frontmatter = fm_match.group(1)
    tools_match = re.search(r'^tools:\s*(.+)$', frontmatter, re.MULTILINE)
    if not tools_match:
        return []

    tools_raw = tools_match.group(1).strip()
    return [t.strip().strip('"\'') for t in tools_raw.split(',') if t.strip()]


# ---------------------------------------------------------------------------
# Network policy
# ---------------------------------------------------------------------------

def _parse_simple_policy(content: str, default: dict) -> dict:
    """Minimal YAML list parser for network-policy.yaml without PyYAML."""
    result = dict(default)
    current_key = None
    for line in content.split('\n'):
        stripped = line.rstrip()
        if not stripped or stripped.startswith('#'):
            continue
        if stripped.lstrip().startswith('- '):
            value = stripped.lstrip('- ').strip().strip('"').strip("'")
            if value and value != '[]' and current_key:
                if not isinstance(result.get(current_key), list):
                    result[current_key] = []
                result[current_key].append(value)
        elif ':' in stripped:
            key, _, value = stripped.partition(':')
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            current_key = key
            if value:
                result[key] = value
            else:
                if key not in result or not isinstance(result[key], list):
                    result[key] = []
    return result


def load_network_policy() -> dict:
    """Load network policy YAML. Returns permissive defaults on any error."""
    default = {
        "version": "1.0",
        "default": "allow",
        "allowlist": [],
        "blocklist": [],
        "high_risk": [],
    }
    if not POLICY_FILE.exists():
        return default
    try:
        content = POLICY_FILE.read_text(encoding='utf-8')
        try:
            import yaml
            parsed = yaml.safe_load(content) or {}
            return {**default, **parsed}
        except ImportError:
            return _parse_simple_policy(content, default)
    except Exception:
        return default  # Never fail — return permissive defaults


def extract_domain(url: str) -> str:
    """Extract hostname from URL string."""
    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        return urlparse(url).netloc.lower()
    except Exception:
        return url.lower()


def domain_matches(domain: str, pattern: str) -> bool:
    """Check if domain matches a pattern. Supports wildcard prefix *.example.com."""
    pattern = pattern.lower().strip()
    domain = domain.lower().strip()
    if pattern.startswith('*.'):
        suffix = pattern[2:]
        return domain == suffix or domain.endswith('.' + suffix)
    return domain == pattern or domain.endswith('.' + pattern)


def check_domain_against_policy(domain: str, policy: dict) -> tuple:
    """
    Returns (status, reason) where status is one of:
      'blocked'       — explicit blocklist match → hard block
      'high_risk'     — matches high_risk pattern → warn + allow
      'allowed'       — explicit allowlist match → silent pass
      'unknown_block' — not listed, default=block → warn + allow (soft)
      'unknown_allow' — not listed, default=allow → silent pass
    """
    for pattern in (policy.get("blocklist") or []):
        if pattern and domain_matches(domain, pattern):
            return "blocked", f"Domain '{domain}' matches blocklist pattern '{pattern}'"

    for pattern in (policy.get("allowlist") or []):
        if pattern and domain_matches(domain, pattern):
            return "allowed", f"Domain '{domain}' is allowlisted"

    for pattern in (policy.get("high_risk") or []):
        if pattern and domain_matches(domain, pattern):
            return "high_risk", f"Domain '{domain}' flagged high-risk (pattern: '{pattern}')"

    if (policy.get("default") or "allow") == "block":
        return "unknown_block", f"Domain '{domain}' not in allowlist — default policy is BLOCK"
    return "unknown_allow", f"Domain '{domain}' not in any policy list — default ALLOW"


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def log_spawn_event(agent: str, tier: int, tools: list,
                    violations: list, task_preview: str) -> None:
    """Append agent spawn event to JSONL log. Non-blocking."""
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            # Timezone-AWARE UTC. datetime.utcnow() produced a NAIVE UTC string
            # while agent-spawn-logger.py writes aware UTC and gate-enforcer
            # compares against naive LOCAL — three conventions in one log, which
            # skewed dispatch-rate comparisons by the UTC offset. Also silences
            # the utcnow() DeprecationWarning.
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": agent,
            "tier": tier,
            "tools": tools,
            "violations": violations,
            "compliant": len(violations) == 0,
            "task_preview": task_preview[:80],
        }
        with SPAWN_LOG.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(entry) + '\n')
    except Exception:
        pass


def log_network_event(tool: str, url: str, domain: str,
                      status: str, reason: str) -> None:
    """Append network policy event to JSONL log. Non-blocking."""
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            # Timezone-AWARE UTC. datetime.utcnow() produced a NAIVE UTC string
            # while agent-spawn-logger.py writes aware UTC and gate-enforcer
            # compares against naive LOCAL — three conventions in one log, which
            # skewed dispatch-rate comparisons by the UTC offset. Also silences
            # the utcnow() DeprecationWarning.
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "url": url[:200],
            "domain": domain,
            "status": status,
            "reason": reason,
        }
        with NETWORK_LOG.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(entry) + '\n')
    except Exception:
        pass


def log_fs_event(tool: str, path: str, zone_ok: bool,
                 zone_matched: Optional[str]) -> None:
    """Append filesystem zone event to JSONL log. Non-blocking."""
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            # Timezone-AWARE UTC. datetime.utcnow() produced a NAIVE UTC string
            # while agent-spawn-logger.py writes aware UTC and gate-enforcer
            # compares against naive LOCAL — three conventions in one log, which
            # skewed dispatch-rate comparisons by the UTC offset. Also silences
            # the utcnow() DeprecationWarning.
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "path": path[:300],
            "zone_ok": zone_ok,
            "zone_matched": zone_matched,
        }
        with FS_LOG.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(entry) + '\n')
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def handle_task(tool_input: dict) -> None:
    """Validate agent spawn governance (v1.0 logic, unchanged)."""
    subagent_type = tool_input.get("subagent_type", "")
    if not subagent_type:
        print(json.dumps({"decision": "allow"}))
        return

    agent_name = subagent_type.lstrip('@')
    if agent_name.startswith("agent-"):
        agent_name = agent_name[len("agent-"):]

    governance_rule = GOVERNANCE.get(agent_name)
    tier = governance_rule["tier"] if governance_rule else 4
    agent_file = AGENTS_DIR / f"{agent_name}.md"
    declared_tools = extract_frontmatter_tools(agent_file)

    task_desc = tool_input.get("description", tool_input.get("prompt", ""))
    task_preview = str(task_desc)[:80] if task_desc else ""

    violations = []
    if governance_rule and declared_tools:
        forbidden = governance_rule["forbidden"]
        for tool in declared_tools:
            if tool in forbidden:
                violations.append(tool)

    log_spawn_event(agent_name, tier, declared_tools, violations, task_preview)

    if violations:
        warning = (
            f"GOVERNANCE WARNING: Agent '{agent_name}' (Tier {tier}) "
            f"declares forbidden tools: {violations}. "
            f"Per org-governance.md, these tools are not permitted at this tier. "
            f"Spawn allowed (soft mode) — escalate to @agent-conductor for remediation."
        )
        print(json.dumps({"decision": "allow", "reason": warning}))
    else:
        print(json.dumps({"decision": "allow"}))


def handle_webfetch(tool_input: dict) -> None:
    """Network policy check for WebFetch calls."""
    url = tool_input.get("url", "")
    if not url:
        print(json.dumps({"decision": "allow"}))
        return

    domain = extract_domain(url)
    policy = load_network_policy()
    status, reason = check_domain_against_policy(domain, policy)
    log_network_event("WebFetch", url, domain, status, reason)

    if status == "blocked":
        print(json.dumps({
            "decision": "block",
            "reason": (
                f"NETWORK POLICY: {reason}. "
                f"To allow, add '{domain}' to session/network-policy.yaml allowlist."
            ),
        }))
    elif status in ("high_risk", "unknown_block"):
        # Soft enforcement: warn but allow
        print(json.dumps({
            "decision": "allow",
            "reason": f"NETWORK POLICY WARNING: {reason}. See session/network-policy-log.jsonl.",
        }))
    else:
        # allowed or unknown_allow — silent pass
        print(json.dumps({"decision": "allow"}))


def handle_websearch(tool_input: dict) -> None:
    """Log WebSearch calls (search engine domain is implicitly trusted)."""
    log_network_event(
        "WebSearch",
        tool_input.get("query", "")[:100],
        "search-engine",
        "allowed",
        "WebSearch always allowed — routes through configured search engine",
    )
    print(json.dumps({"decision": "allow"}))


def handle_write_edit(tool_name: str, tool_input: dict) -> None:
    """Filesystem zone check for Write/Edit calls (soft enforcement — warn only)."""
    path = tool_input.get("file_path", "")
    if not path:
        print(json.dumps({"decision": "allow"}))
        return

    # Normalize for comparison (Path.resolve() works even for non-existent paths)
    try:
        norm_path = str(Path(path).resolve())
    except Exception:
        norm_path = path

    zone_matched = None
    for zone in ALLOWED_FS_ZONES:
        try:
            zone_norm = str(Path(zone).resolve())
        except Exception:
            zone_norm = zone
        if norm_path.startswith(zone_norm):
            zone_matched = zone
            break

    zone_ok = zone_matched is not None
    log_fs_event(tool_name, path, zone_ok, zone_matched)

    if not zone_ok:
        warning = (
            f"FILESYSTEM ZONE WARNING: {tool_name} targeting '{path}' "
            f"is outside allowed zones. "
            f"Expected: ~/.claude/ or ~/Desktop/Projects/. "
            f"Write allowed (soft mode). See session/filesystem-policy-log.jsonl."
        )
        print(json.dumps({"decision": "allow", "reason": warning}))
    else:
        print(json.dumps({"decision": "allow"}))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    hook_data = parse_stdin()
    tool_name = hook_data.get("tool_name", "")
    tool_input = hook_data.get("tool_input", {})

    # The spawn tool is named "Agent" in current Claude Code and "Task" in older
    # builds. settings.json matches "Task|Agent"; accepting only "Task" meant
    # tier governance silently never ran on any modern spawn.
    if tool_name in ("Task", "Agent"):
        handle_task(tool_input)
    elif tool_name == "WebFetch":
        handle_webfetch(tool_input)
    elif tool_name == "WebSearch":
        handle_websearch(tool_input)
    elif tool_name in ("Write", "Edit"):
        handle_write_edit(tool_name, tool_input)
    else:
        # All other tools (Read, Glob, Grep, Bash, etc.) — allow immediately
        print(json.dumps({"decision": "allow"}))

    sys.exit(0)


if __name__ == "__main__":
    main()
