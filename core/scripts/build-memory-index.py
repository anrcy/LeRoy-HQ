#!/usr/bin/env python3
"""
Build Memory Index v6.0 - Predictive Client Context System

Scans ENTIRE memory vault and creates:
- session/memory-index.json (master index with all metadata)
- session/shards/ folder with domain-specific indexes (v5.0)
- session/memory-graph.json (wikilink graph, v5.2)

NEW in v6.0:
- Entity index for client matching (Predictive Client Context Phase 1)
- Extracts client names from titles and frontmatter
- Indexes CRM IDs (contact, company, deal)
- O(1) client name -> note path lookup

v5.0 Features (preserved):
- Tag intersection index (O(1) multi-tag queries)
- Pre-computed scoring (recency, type, project)
- Domain sharding (load only relevant shard)
- Performance targeting <200ms at 10K notes

v4.0 Features (preserved):
- Tracks wikilinks in each note
- Reports graph connectivity statistics
- Identifies isolated nodes (no wikilinks)
- Tracks hub note connections

STRICT TAG RULES v2.0:
For Claude/ subfolder:
- Tag 1 (REQUIRED): Folder tag (decisions, patterns, preferences, skills-learned, projects)
- Tags 2-4 (OPTIONAL): Software/integration tags only

For integrations/ subfolder:
- Tag 1: 'integrations'
- Tag 2: Software name (your integrations, e.g. crm, db, docs)

For config/ subfolder:
- Tag 1: 'config'
- Tags 2-4: Related software names

MAX 4 tags total for all files.
"""

import os
import sys
import io
import json
import re
import yaml
from datetime import datetime, timezone
import pathlib
from pathlib import Path

# v6.1 Decay-A: Per-type half-life defaults (council vote, 2026-04-29).
# Days until inferred-fact confidence halves. None = never decays.
# Vault note types: decision, pattern, skill-learned, preference, project-note, general
HALF_LIFE_VAULT = {
    "decision":     None,  # asserted by definition
    "pattern":      90,    # patterns are facts about how the system works
    "skill-learned": 90,
    "preference":   180,
    "project-note": 90,    # facts about a specific project
    "general":      30,    # default for misc notes
}
CONFIDENCE_FLOOR = 0.3

# Force UTF-8 encoding for stdout (Windows compatibility)
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Paths - FULL VAULT (v3.0) + SHARDS (v5.0)
# Resolved relative to the install root so this works on any machine/OS.
# Override the root with the LEROY_HOME env var if your install isn't at ~/.claude.
CLAUDE_DIR = Path(os.environ.get("LEROY_HOME") or (Path.home() / ".claude"))
MEMORY_VAULT = CLAUDE_DIR / "memory"
OUTPUT_FILE = CLAUDE_DIR / "session" / "memory-index.json"
SHARDS_DIR = CLAUDE_DIR / "session" / "shards"

# v5.3: vendored / build-output path segments that are never memories.
# Matched case-insensitively against the note's path parts.
VENDORED_SEGMENTS = {
    "node_modules", "site-packages", ".venv", "venv", "env",
    "__pycache__", ".git", ".pytest_cache", ".mypy_cache",
    "dist", "build", "win-unpacked", "release", "out",
    "vendor", "third_party", "bower_components", ".next", ".nuxt",
    "coverage", "htmlcov", ".tox", "egg-info",
    # 2026-07-24 (stress test): derived rewrites of live skills — byte-similar to
    # the originals, so they outscore them on their own queries. Matches the
    # sidecar's exclusion set so both recall paths see the same corpus.
    "caveman-versions",
}


def _is_vendored(filepath) -> bool:
    """True when any path segment is a dependency or build-output directory.

    Segment matching (not substring) on purpose: a note legitimately named
    'release-notes.md' or a folder like 'Projects/Distribution' must NOT be
    excluded just because it contains 'release' or 'dist'.
    """
    for part in filepath.parts:
        p = part.lower()
        if p in VENDORED_SEGMENTS or p.endswith(".egg-info"):
            return True
    return False

# PROJECT SHARDS (v7.0 - Multi-Project Architecture)
# Each project has its own shard for isolated loading
# CUSTOMIZE THIS for your own work. Each key is a shard name (usually a
# project/client/context), each value is the list of tags that route a note into
# that shard. Notes that match nothing land in 'meta'. Folder names under
# memory/Projects/ are matched against these keys automatically (see infer_project).
PROJECT_SHARDS = {
    # 'acme':     ['crm', 'sales'],          # e.g. a client
    # 'teaching': ['course', 'grading'],     # e.g. a side context
    'meta': ['memory-system', 'enforcement', 'git', 'python', 'playwright', 'supabase', 'gas', 'netlify', 'general', 'system-design']
}

# Optional exclusions. Add a shard name here to keep its notes out of the index
# entirely (e.g. a context you never want surfaced in recall).
BLACKLISTED_PROJECTS = []  # No blacklisting - all projects active
BLACKLISTED_DOMAINS = []  # No domain-level blacklisting

# DOMAIN SHARDS (derived from PROJECT_SHARDS for backward compatibility)
DOMAIN_SHARDS = PROJECT_SHARDS

# Type relevance weights for pre-computed scoring (v5.0)
TYPE_RELEVANCE = {
    'decision': 1.0,       # Highest - architectural choices
    'pattern': 0.9,        # High - reusable solutions
    'skill-learned': 0.8,  # High - valuable insights
    'preference': 0.7,     # Medium - user preferences
    'project-note': 0.6,   # Medium - project-specific
    'general': 0.4         # Lower - general notes
}

# STRICT TAG RULES v2.0
VALID_FOLDER_TAGS = [
    # Claude/ subfolder tags
    'decisions', 'patterns', 'preferences', 'skills-learned', 'projects',
    # Top-level folder tags (v2.0)
    'integrations', 'config', 'meta'
]
VALID_SOFTWARE_TAGS = [
    'crm', 'revit', 'android',
    'git', 'netlify', 'playwright', 'supabase', 'gas',
    'python', 'memory-system', 'enforcement', 'leroy',
    # Additional integration software (v2.0)
    'google', 'canvas', 'api-reference',
    # v4.0: Hub note tag
    'hub',
    # Common framework/vendor tags
    'nextjs', 'openai', 'excalidraw',
    # Additional common tags
    'multi-agent', 'collaboration', 'debate', 'real-time'
]

def extract_title(content):
    """Extract H1 title from markdown"""
    match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
    return match.group(1).strip() if match else "Untitled"

def extract_metadata_field(content, field):
    """Extract metadata field like **Date:** value"""
    pattern = rf'\*\*{field}:\*\*\s+(.+?)(?:\n|$)'
    match = re.search(pattern, content)
    return match.group(1).strip() if match else None

def extract_frontmatter(content):
    """Extract YAML frontmatter from markdown file"""
    if content.startswith('---'):
        end = content.find('---', 3)
        if end != -1:
            try:
                frontmatter = yaml.safe_load(content[3:end])
                return frontmatter if frontmatter else {}
            except yaml.YAMLError as e:
                # YAML parsing failed - try to extract tags manually as fallback
                # Common issue: unquoted values with colons (e.g., "session: text with : in it")
                yaml_text = content[3:end]

                # Try to extract tags field manually
                tags_match = re.search(r'^tags:\s*\[(.*?)\]', yaml_text, re.MULTILINE)
                if tags_match:
                    tags_str = tags_match.group(1)
                    # Split by comma and clean up
                    tags = [t.strip().strip('"').strip("'") for t in tags_str.split(',')]
                    return {'tags': tags}

                # If manual extraction also fails, return empty
                return {}
    return {}

def get_folder_tag(filepath):
    """Get folder tag from file path (v2.0 - full vault)"""
    path_str = str(filepath)

    # FIXED: Removed Claude/ prefix - vault structure is memory/Decisions/, NOT memory/Claude/Decisions/
    # Direct folder tags (memory/Decisions/, memory/Patterns/, etc.)
    if '\\Decisions\\' in path_str or '/Decisions/' in path_str:
        return 'decisions'
    elif '\\Patterns\\' in path_str or '/Patterns/' in path_str:
        return 'patterns'
    elif '\\Preferences\\' in path_str or '/Preferences/' in path_str:
        return 'preferences'
    elif '\\Skills-Learned\\' in path_str or '/Skills-Learned/' in path_str:
        return 'skills-learned'
    elif '\\Projects\\' in path_str or '/Projects/' in path_str:
        return 'projects'

    # Top-level folder tags (v2.0)
    elif '\\integrations\\' in path_str or '/integrations/' in path_str:
        return 'integrations'
    elif '\\config\\' in path_str or '/config/' in path_str:
        return 'config'
    elif '\\System\\' in path_str or '/System/' in path_str:
        return 'meta'

    # Root-level files and unknown folders
    return 'meta'

def validate_and_normalize_tags(tags, filepath):
    """
    Validate and normalize tags according to STRICT TAG RULES v1.0
    Returns: (normalized_tags, is_valid, issues)
    """
    issues = []
    normalized = []

    # Get folder tag from path (always required as first tag)
    folder_tag = get_folder_tag(filepath)
    if folder_tag not in VALID_FOLDER_TAGS:
        folder_tag = 'decisions'  # Default fallback
        issues.append(f"Unknown folder, defaulted to 'decisions'")

    normalized.append(folder_tag)

    # Process existing tags (skip any that duplicate folder tag)
    if tags:
        for tag in tags:
            tag_lower = tag.lower().strip()
            # Skip if it's the folder tag (already added)
            if tag_lower in VALID_FOLDER_TAGS:
                continue
            # Only keep valid software tags
            if tag_lower in VALID_SOFTWARE_TAGS:
                if tag_lower not in normalized:
                    normalized.append(tag_lower)
            else:
                issues.append(f"Removed invalid tag: '{tag}'")

    # Limit to 4 tags max
    if len(normalized) > 4:
        issues.append(f"Truncated from {len(normalized)} to 4 tags")
        normalized = normalized[:4]

    is_valid = len(issues) == 0
    return normalized, is_valid, issues

def extract_tags_from_frontmatter(content, filepath):
    """Extract and validate tags from YAML frontmatter"""
    frontmatter = extract_frontmatter(content)
    raw_tags = frontmatter.get('tags', [])

    # Handle string tags (shouldn't happen but be safe)
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]
    elif raw_tags is None:
        raw_tags = []

    # Validate and normalize
    normalized_tags, is_valid, issues = validate_and_normalize_tags(raw_tags, filepath)

    return normalized_tags, is_valid, issues

def extract_keywords(content, max_words=30):
    """
    Extract keywords from title and first 200 chars.

    NOTE: This function is DEPRECATED in v5.1+ to reduce index bloat.
    Keywords caused 70% of index size (1.5MB → 450KB without them).
    Tag-based filtering is now primary search mechanism (O(1) lookups).

    Set INCLUDE_KEYWORDS = False to skip keyword extraction.
    """
    title = extract_title(content)
    # Get first 200 chars after title
    content_no_title = re.sub(r'^#.+?\n+', '', content, count=1)
    preview = content_no_title[:200]

    # Combine and extract words
    combined = f"{title} {preview}"
    words = re.findall(r'\b[a-z]{3,}\b', combined.lower())

    # Remove common words
    common = {'the', 'and', 'for', 'with', 'that', 'this', 'from', 'was', 'were', 'are'}
    keywords = [w for w in words if w not in common]

    return keywords[:max_words]

# Global flag to control keyword extraction (v5.1+)
INCLUDE_KEYWORDS = False  # Set to False to reduce index bloat

def extract_wikilinks(content, frontmatter=None):
    """Extract [[wikilinks]] from content and frontmatter related: field (v4.0)

    BACKWARD COMPAT shim: returns flat list (used by stats and lean index).
    For typed-edge extraction (v6.1 Edges-A), call extract_typed_edges().
    """
    # Extract from content body
    content_links = re.findall(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]', content)

    # Extract from frontmatter related: field (v4.0)
    frontmatter_links = []
    if frontmatter:
        related = frontmatter.get('related', [])
        if related:
            for link in related:
                # Extract link name from "[[Name]]" format
                match = re.search(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]', str(link))
                if match:
                    frontmatter_links.append(match.group(1))

    # Combine and dedupe
    all_links = list(set(content_links + frontmatter_links))
    return all_links


# v6.1 Edges-A: 5 typed edge categories
EDGE_TYPES = ("supersedes", "contradicts", "supports", "derived_from", "refers_to")


def _normalize_link(s):
    """Pull the target name out of any of: '[[Foo]]', '[[Foo|alias]]', or 'Foo'."""
    if s is None:
        return None
    s = str(s).strip()
    m = re.search(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]', s)
    if m:
        return m.group(1).strip()
    return s if s else None


def extract_typed_edges(content, frontmatter=None):
    """v6.1 Edges-A: extract typed memory edges.

    Schema (in frontmatter):
        edges:
          supersedes:    ["[[OldNote]]"]   # this note replaces these
          contradicts:   ["[[OtherNote]]"] # this note disagrees with these
          supports:      ["[[OtherNote]]"] # this note reinforces these
          derived_from:  ["[[Source]]"]    # this note was extracted from these
          refers_to:     ["[[Person]]"]    # generic reference (legacy default)

    Returns:
        Dict[str, List[str]] keyed by EDGE_TYPES. Empty lists allowed but suppressed
        in the final graph file. Legacy notes (no `edges:` block) get all body wikilinks +
        frontmatter `related:` entries collapsed into `refers_to` for backward compat.
    """
    edges = {t: [] for t in EDGE_TYPES}

    # Typed edges from frontmatter.edges block
    if frontmatter and isinstance(frontmatter.get("edges"), dict):
        raw = frontmatter["edges"]
        for edge_type in EDGE_TYPES:
            vals = raw.get(edge_type, []) or []
            if isinstance(vals, str):
                vals = [vals]
            for v in vals:
                norm = _normalize_link(v)
                if norm and norm not in edges[edge_type]:
                    edges[edge_type].append(norm)

    # Legacy wikilinks in body → refers_to (only if not already typed elsewhere)
    body_links = re.findall(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]', content)
    typed_targets = {tgt for lst in edges.values() for tgt in lst}
    for link in body_links:
        link = link.strip()
        if link and link not in typed_targets and link not in edges["refers_to"]:
            edges["refers_to"].append(link)

    # Legacy frontmatter.related → refers_to
    if frontmatter:
        related = frontmatter.get("related", []) or []
        if isinstance(related, str):
            related = [related]
        for v in related:
            norm = _normalize_link(v)
            if norm and norm not in typed_targets and norm not in edges["refers_to"]:
                edges["refers_to"].append(norm)

    return edges


def is_hub_note(title):
    """Check if note is a hub note"""
    return title.startswith('HUB - ') or title.startswith('_HUB - ')

def infer_project(path):
    """Infer the shard for a note from its folder structure.

    Any note under memory/Projects/<Name>/ is attributed to shard '<name>'
    (lowercased) when that shard is declared in PROJECT_SHARDS; everything else
    falls back to 'meta'. Deriving this from the folder name means adding a new
    project needs no code change — just a PROJECT_SHARDS entry."""
    parts = [q.lower() for q in pathlib.Path(str(path)).parts]
    if 'projects' in parts:
        i = parts.index('projects')
        if i + 1 < len(parts):
            candidate = parts[i + 1]
            if candidate in PROJECT_SHARDS:
                return candidate
    return 'meta'

def is_blacklisted_note(project, domain):
    """Check if note should be excluded from index (v6.0)"""
    if project.lower() in BLACKLISTED_PROJECTS:
        return True, f"Project '{project}' is blacklisted"
    if domain.lower() in BLACKLISTED_DOMAINS:
        return True, f"Domain '{domain}' is blacklisted"
    return False, ""

def infer_domain(path, content, keywords):
    """Infer domain from filename, path, and keywords"""
    path_str = str(path).lower()
    content_lower = content.lower()

    # Check path first
    domain_keywords = {
        'crm': ['crm', 'deals', 'pipeline'],
        'revit': ['revit', 'bim'],
        'memory-system': ['memory', 'recall', 'consolidation'],
        'enforcement': ['enforcement', 'protocol', 'hook'],
        'leroy': ['leroy', 'report', 'factory'],
        'android': ['android', 'quickquote'],
        'system-design': ['system', 'architecture', 'design']
    }

    for domain, kws in domain_keywords.items():
        if any(kw in path_str or kw in content_lower for kw in kws):
            return domain

    return 'general'

def get_note_type(path):
    """Get note type from folder structure"""
    path_str = str(path)
    if '\\Decisions\\' in path_str or '/Decisions/' in path_str:
        return 'decision'
    elif '\\Patterns\\' in path_str or '/Patterns/' in path_str:
        return 'pattern'
    elif '\\Preferences\\' in path_str or '/Preferences/' in path_str:
        return 'preference'
    elif '\\Skills-Learned\\' in path_str or '/Skills-Learned/' in path_str:
        return 'skill-learned'
    elif '\\Projects\\' in path_str or '/Projects/' in path_str:
        return 'project-note'
    else:
        return 'general'

def get_file_times(filepath):
    """Get creation and modification times"""
    stat = filepath.stat()
    created = datetime.fromtimestamp(stat.st_ctime).isoformat() + 'Z'
    modified = datetime.fromtimestamp(stat.st_mtime).isoformat() + 'Z'
    return created, modified


def compute_effective_confidence(frontmatter, note_type, modified_iso, now=None):
    """v6.1 Decay-A: Compute decay-adjusted confidence for a note.

    Inputs (all from frontmatter, all optional):
      confidence: float 0-1 (default 1.0)
      inferred: bool (default False — legacy notes are treated as asserted)
      last_verified: ISO date (falls back to file modified time)
      half_life_days: int override (else uses HALF_LIFE_VAULT[type])

    Asserted notes (inferred:false) never decay.
    Inferred notes decay exponentially: c * 0.5^(days_since_verified / half_life).
    Half-life None → no decay even if marked inferred.
    """
    confidence = frontmatter.get('confidence', 1.0)
    try:
        confidence = float(confidence)
    except (ValueError, TypeError):
        confidence = 1.0

    inferred = bool(frontmatter.get('inferred', False))
    if not inferred:
        return round(confidence, 4)

    # Per-note override, else type default
    half_life = frontmatter.get('half_life_days')
    if half_life is None:
        half_life = HALF_LIFE_VAULT.get(note_type, 30)
    if half_life is None:
        return round(confidence, 4)
    try:
        half_life = float(half_life)
    except (ValueError, TypeError):
        half_life = 30

    # last_verified — frontmatter override, else file mtime
    lv_str = frontmatter.get('last_verified')
    if not lv_str:
        lv_str = modified_iso

    if hasattr(lv_str, 'isoformat'):
        lv_str = lv_str.isoformat()

    try:
        lv_clean = str(lv_str).replace('Z', '').split('.')[0]
        lv = datetime.fromisoformat(lv_clean)
        if now is None:
            now = datetime.now()
        days = (now - lv).total_seconds() / 86400.0
        if days < 0:
            days = 0
        decay = 0.5 ** (days / half_life)
        return round(confidence * decay, 4)
    except (ValueError, TypeError):
        return round(confidence, 4)

def process_note(filepath):
    """Process a single note and extract metadata with STRICT TAG validation"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        # Get relative path from vault root
        rel_path = filepath.relative_to(MEMORY_VAULT)

        # Extract metadata from frontmatter
        frontmatter = extract_frontmatter(content)

        # Extract and validate tags (STRICT RULES v1.0)
        tags, tags_valid, tag_issues = extract_tags_from_frontmatter(content, filepath)

        # Log tag issues
        if tag_issues:
            print(f"  TAG ISSUES in {filepath.name}:")
            for issue in tag_issues:
                print(f"    - {issue}")

        # Extract other metadata
        title = extract_title(content)

        # v5.1: Conditionally extract keywords (set INCLUDE_KEYWORDS=False to reduce index bloat)
        keywords = extract_keywords(content) if INCLUDE_KEYWORDS else []

        wikilinks = extract_wikilinks(content, frontmatter)  # v4.0: flat list, used for stats
        typed_edges = extract_typed_edges(content, frontmatter)  # v6.1 Edges-A: typed dict
        created, modified = get_file_times(filepath)

        # Get project/domain/type from frontmatter or infer
        project = frontmatter.get('project', infer_project(filepath))
        domain = frontmatter.get('domain', infer_domain(filepath, content, keywords))
        note_type = frontmatter.get('type', get_note_type(filepath))

        # v6.0: Apply blacklist filter
        is_blacklisted, blacklist_reason = is_blacklisted_note(project, domain)
        if is_blacklisted:
            print(f"  BLACKLISTED: {filepath.name} - {blacklist_reason}")
            return None  # Skip this note

        # v4.0: Check if this is a hub note
        is_hub = is_hub_note(title)

        # Ensure dates are strings (YAML may parse as datetime)
        fm_created = frontmatter.get('created', created)
        fm_modified = frontmatter.get('modified', modified)
        if hasattr(fm_created, 'isoformat'):
            fm_created = fm_created.isoformat() + 'Z'
        if hasattr(fm_modified, 'isoformat'):
            fm_modified = fm_modified.isoformat() + 'Z'

        # v6.1 Decay-A: compute effective_confidence at index time (cheap, lazy-on-write)
        effective_conf = compute_effective_confidence(frontmatter, note_type, str(fm_modified))
        is_inferred = bool(frontmatter.get('inferred', False))

        # v5.2: Lean index mode - exclude wikilinks from per-note storage
        # Wikilinks are stored in separate graph file to reduce bloat
        # Full wikilinks still available via graph-index.json if needed
        note_data = {
            "path": str(rel_path).replace('\\', '/'),
            "title": title,
            "created": str(fm_created),
            "modified": str(fm_modified),
            "project": project,
            "domain": domain,
            "type": note_type,
            "tags": tags,  # Normalized tags following STRICT RULES
            "effective_confidence": effective_conf,  # v6.1 Decay-A
        }

        # Only include optional fields if they have meaningful data
        if not tags_valid:
            note_data["tags_valid"] = False  # Only store if invalid
        if keywords:
            note_data["keywords"] = keywords  # Only if INCLUDE_KEYWORDS=True
        if is_hub:
            note_data["is_hub"] = True  # Only store for hub notes
        if len(wikilinks) > 0:
            note_data["wikilink_count"] = len(wikilinks)  # Just the count, not full list
        if is_inferred:
            note_data["inferred"] = True  # v6.1 — only store for inferred notes (legacy default false)

        # Store full wikilinks in separate structure for graph operations
        note_data["_wikilinks"] = wikilinks  # Temporary, stripped before JSON output
        note_data["_typed_edges"] = typed_edges  # v6.1 Edges-A: stripped before JSON output

        return note_data
    except Exception as e:
        print(f"Error processing {filepath}: {e}")
        return None

def compute_precomputed_scores(note, now=None):
    """
    Compute pre-computed scoring components for a note (v5.0).

    Pre-computes:
    - recency_score: Based on days since modification (0.0 to 1.0)
    - type_score: Based on note type relevance
    - project_base: Project identifier for quick filtering

    Leaves keyword_match for runtime (context-dependent).

    Args:
        note: Note metadata dict
        now: Current datetime (optional, for testing)

    Returns:
        Dict with pre-computed score components
    """
    if now is None:
        now = datetime.now()

    # Recency score: 1.0 - (days_old / 90), min 0.1
    modified_str = note.get('modified', '')
    try:
        # Handle ISO format with or without Z suffix
        if modified_str.endswith('Z'):
            modified_str = modified_str[:-1]
        modified = datetime.fromisoformat(modified_str.replace('Z', ''))
        days_old = (now - modified).days
        recency_score = max(0.1, 1.0 - (days_old / 90))
    except (ValueError, TypeError):
        recency_score = 0.5  # Default if date parsing fails

    # Type relevance score
    note_type = note.get('type', 'general')
    type_score = TYPE_RELEVANCE.get(note_type, 0.4)

    # Project base (for quick project filtering)
    project = note.get('project', 'meta')

    return {
        'recency': round(recency_score, 3),
        'type_relevance': round(type_score, 3),
        'project_base': project
    }


def get_shard_for_domain(domain):
    """
    Determine which shard a domain belongs to (v5.0).

    Args:
        domain: Domain name (crm, revit, etc.)

    Returns:
        Shard name — any key of PROJECT_SHARDS, or 'meta' as the fallback
    """
    for shard, domains in DOMAIN_SHARDS.items():
        if domain in domains:
            return shard
    return 'meta'  # Default to meta shard


def build_tag_index(notes):
    """
    Build tag intersection index for O(1) multi-tag queries (v5.0).

    Creates:
    - single_tag_index: tag -> [note paths]
    - combo_2_index: "tag1+tag2" -> [note paths]
    - combo_3_index: "tag1+tag2+tag3" -> [note paths]

    Args:
        notes: List of note metadata dicts

    Returns:
        Dict with tag indexes
    """
    single_index = {}
    combo_2_index = {}
    combo_3_index = {}

    for note in notes:
        note_path = note.get('path', '')
        tags = note.get('tags', [])

        if not note_path or not tags:
            continue

        # Single tag index
        for tag in tags:
            if tag not in single_index:
                single_index[tag] = []
            single_index[tag].append(note_path)

        # 2-tag combinations
        if len(tags) >= 2:
            for i in range(len(tags)):
                for j in range(i + 1, len(tags)):
                    combo = tuple(sorted([tags[i], tags[j]]))
                    combo_key = f"{combo[0]}+{combo[1]}"
                    if combo_key not in combo_2_index:
                        combo_2_index[combo_key] = []
                    combo_2_index[combo_key].append(note_path)

        # 3-tag combinations
        if len(tags) >= 3:
            for i in range(len(tags)):
                for j in range(i + 1, len(tags)):
                    for k in range(j + 1, len(tags)):
                        combo = tuple(sorted([tags[i], tags[j], tags[k]]))
                        combo_key = f"{combo[0]}+{combo[1]}+{combo[2]}"
                        if combo_key not in combo_3_index:
                            combo_3_index[combo_key] = []
                        combo_3_index[combo_key].append(note_path)

    return {
        'single': single_index,
        'combo_2': combo_2_index,
        'combo_3': combo_3_index,
        'stats': {
            'single_tags': len(single_index),
            'two_tag_combos': len(combo_2_index),
            'three_tag_combos': len(combo_3_index)
        }
    }


def extract_client_name_from_title(title):
    """
    Extract client name from note title (v6.0 - Predictive Client Context).

    Examples:
    - "Jane Doe" -> "jane doe"
    - "Client-Acme-Shops" -> "acme shops"
    - "Contact-Jane-Doe" -> "jane doe"
    - "Acme - Rental Management System" -> "acme"
    """
    # Remove common prefixes
    name = title
    prefixes_to_remove = [
        'Client-', 'Contact-', 'Project-', 'Deal-'
    ]
    for prefix in prefixes_to_remove:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break

    # Handle "Name - Description" format (take first part)
    if ' - ' in name:
        name = name.split(' - ')[0].strip()

    # Convert dashes/underscores to spaces
    name = name.replace('-', ' ').replace('_', ' ')

    # Clean up and lowercase
    name = ' '.join(name.split()).strip().lower()

    return name if name else None


def build_entity_index(notes):
    """
    Build entity lookup index for O(1) client matching (v6.0 - Predictive Client Context).

    Creates:
    - clients: {client_name: [note paths]} - from titles and frontmatter
    - deals: {deal_id: [note paths]} - from crm_deal_id frontmatter
    - crm_ids: {crm_entity_id: [note paths]} - from crm_id, crm_company_id

    Args:
        notes: List of note metadata dicts

    Returns:
        Dict with entity indexes and stats
    """
    entity_index = {
        'clients': {},
        'deals': {},
        'crm_ids': {},
        'companies': {}  # Map company name -> client note path
    }

    # Track for stats
    clients_indexed = 0
    deals_indexed = 0
    crm_ids_indexed = 0

    for note in notes:
        path = note.get('path', '')
        title = note.get('title', '')
        note_type = note.get('type', '')

        # Skip notes without path
        if not path:
            continue

        # Read frontmatter from note data (already extracted during processing)
        # We need to re-read the file for frontmatter fields not in the note dict
        # For efficiency, check if this is a client/contact/project type note

        # Extract client name from title for contact, client, and project notes
        if note_type in ['contact', 'client', 'project']:
            client_name = extract_client_name_from_title(title)
            if client_name and len(client_name) >= 2:
                if client_name not in entity_index['clients']:
                    entity_index['clients'][client_name] = []
                if path not in entity_index['clients'][client_name]:
                    entity_index['clients'][client_name].append(path)
                    clients_indexed += 1

        # For client notes, also extract frontmatter fields
        if note_type == 'client':
            # Read frontmatter for additional IDs
            try:
                full_path = MEMORY_VAULT / path
                if full_path.exists():
                    with open(full_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    frontmatter = extract_frontmatter(content)

                    # Extract client_name from frontmatter (canonical name)
                    fm_client_name = frontmatter.get('client_name', '')
                    if fm_client_name:
                        name_lower = fm_client_name.lower()
                        if name_lower not in entity_index['clients']:
                            entity_index['clients'][name_lower] = []
                        if path not in entity_index['clients'][name_lower]:
                            entity_index['clients'][name_lower].append(path)
                        # Also add to companies index
                        if name_lower not in entity_index['companies']:
                            entity_index['companies'][name_lower] = []
                        if path not in entity_index['companies'][name_lower]:
                            entity_index['companies'][name_lower].append(path)

                    # Extract CRM company ID
                    crm_company = frontmatter.get('crm_company_id', '')
                    if crm_company:
                        crm_company = str(crm_company)
                        if crm_company not in entity_index['crm_ids']:
                            entity_index['crm_ids'][crm_company] = []
                        if path not in entity_index['crm_ids'][crm_company]:
                            entity_index['crm_ids'][crm_company].append(path)
                            crm_ids_indexed += 1

                    # Extract CRM deal ID
                    crm_deal = frontmatter.get('crm_deal_id', '')
                    if crm_deal:
                        crm_deal = str(crm_deal)
                        if crm_deal not in entity_index['deals']:
                            entity_index['deals'][crm_deal] = []
                        if path not in entity_index['deals'][crm_deal]:
                            entity_index['deals'][crm_deal].append(path)
                            deals_indexed += 1
            except Exception as e:
                print(f"  Warning: Could not read frontmatter for {path}: {e}")

        # For contact notes, extract crm_id
        if note_type == 'contact':
            try:
                full_path = MEMORY_VAULT / path
                if full_path.exists():
                    with open(full_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    frontmatter = extract_frontmatter(content)

                    # Extract CRM contact ID
                    crm_entity_id = frontmatter.get('crm_id', '')
                    if crm_entity_id:
                        crm_entity_id = str(crm_entity_id)
                        if crm_entity_id not in entity_index['crm_ids']:
                            entity_index['crm_ids'][crm_entity_id] = []
                        if path not in entity_index['crm_ids'][crm_entity_id]:
                            entity_index['crm_ids'][crm_entity_id].append(path)
                            crm_ids_indexed += 1

                    # Extract CRM company ID from contacts too
                    crm_company = frontmatter.get('crm_company_id', '')
                    if crm_company:
                        crm_company = str(crm_company)
                        if crm_company not in entity_index['crm_ids']:
                            entity_index['crm_ids'][crm_company] = []
                        if path not in entity_index['crm_ids'][crm_company]:
                            entity_index['crm_ids'][crm_company].append(path)
                            crm_ids_indexed += 1
            except Exception as e:
                print(f"  Warning: Could not read frontmatter for {path}: {e}")

    # Add stats
    entity_index['stats'] = {
        'unique_clients': len(entity_index['clients']),
        'unique_deals': len(entity_index['deals']),
        'unique_crm_ids': len(entity_index['crm_ids']),
        'total_client_mappings': clients_indexed,
        'total_deal_mappings': deals_indexed,
        'total_crm_mappings': crm_ids_indexed
    }

    return entity_index


def build_domain_shards(notes):
    """
    Build domain shards for selective loading.

    Creates one index file per shard declared in PROJECT_SHARDS, plus 'meta'.
    Shards listed in BLACKLISTED_PROJECTS are skipped.

    Args:
        notes: List of note metadata dicts

    Returns:
        Dict with shard metadata (paths and note counts)
    """
    # Derived from PROJECT_SHARDS so adding a project needs no code change here.
    shards = {name: [] for name in PROJECT_SHARDS}
    shards.setdefault('meta', [])

    for note in notes:
        # Determine shard by project first, then domain
        project = note.get('project', 'meta')
        domain = note.get('domain', 'general')

        if project in shards:
            shards[project].append(note)
        else:
            # Fall back to domain-based shard assignment
            shard = get_shard_for_domain(domain)
            shards[shard].append(note)

    # Write shard files
    SHARDS_DIR.mkdir(parents=True, exist_ok=True)
    shard_meta = {}

    for shard_name, shard_notes in shards.items():
        shard_file = SHARDS_DIR / f"{shard_name}-shard.json"

        shard_data = {
            'shard': shard_name,
            'notes': shard_notes,
            'count': len(shard_notes),
            'created': datetime.now().isoformat() + 'Z'
        }

        with open(shard_file, 'w', encoding='utf-8') as f:
            json.dump(shard_data, f, indent=2)

        shard_meta[shard_name] = {
            'path': str(shard_file),
            'count': len(shard_notes)
        }

        print(f"  Shard {shard_name}: {len(shard_notes)} notes")

    return shard_meta


def build_index():
    """Scan vault and build index with STRICT TAG validation + wikilink tracking (v4.0) + scaling (v5.0)"""
    import time
    start_time = time.time()

    print(f"Scanning {MEMORY_VAULT}...")
    print(f"Using STRICT TAG RULES v2.0 + WIKILINK TRACKING v4.0 + SCALING v5.0 + ENTITY INDEX v6.0")
    print("-" * 50)

    # Find all markdown files
    notes = []
    valid_count = 0
    invalid_count = 0
    skipped_count = 0

    for filepath in MEMORY_VAULT.rglob('*.md'):
        # Skip temp files
        if "tmpclaude" in str(filepath):
            skipped_count += 1
            continue

        # Skip .obsidian folder
        if ".obsidian" in str(filepath):
            skipped_count += 1
            continue

        # v5.3 (2026-07-23): skip vendored/build artifacts. These are third-party
        # READMEs and license files that live under project folders — they are not
        # memories, and at ~2,800 of 4,343 indexed notes they were crowding real
        # notes out of recall results (measured: a site-packages/onnxruntime README
        # outranked the actual bounty-filing rule for its own query).
        if _is_vendored(filepath):
            skipped_count += 1
            continue

        # v5.2: Skip reference data folders (not memories, causes index bloat)
        # - vendor/catalog caches: bulk reference snapshots
        # - integrations: API documentation
        if "Vendor Cache" in str(filepath) or "Catalog Cache" in str(filepath):
            skipped_count += 1
            continue
        if "\\integrations\\" in str(filepath) or "/integrations/" in str(filepath):
            skipped_count += 1
            continue

        print(f"Processing: {filepath.name}")
        note_meta = process_note(filepath)
        if note_meta:
            notes.append(note_meta)
            if note_meta.get('tags_valid', False):
                valid_count += 1
            else:
                invalid_count += 1

    # Sort by modification time (most recent first)
    notes.sort(key=lambda x: x['modified'], reverse=True)

    # Collect tag statistics
    all_tags = []
    folder_tags = {}
    software_tags = {}

    for note in notes:
        tags = note.get('tags', [])
        if tags:
            # First tag is folder tag
            folder_tag = tags[0]
            folder_tags[folder_tag] = folder_tags.get(folder_tag, 0) + 1

            # Rest are software tags
            for tag in tags[1:]:
                software_tags[tag] = software_tags.get(tag, 0) + 1
                all_tags.append(tag)

    # v4.0: Collect graph connectivity statistics
    total_wikilinks = 0
    notes_with_links = 0
    notes_without_links = []
    hub_notes = []
    wikilink_distribution = {
        '0': 0,
        '1-2': 0,
        '3-5': 0,
        '6+': 0
    }

    for note in notes:
        wikilink_count = note.get('wikilink_count', 0)
        total_wikilinks += wikilink_count

        if wikilink_count == 0:
            wikilink_distribution['0'] += 1
            notes_without_links.append(note.get('title', 'Untitled'))
        elif wikilink_count <= 2:
            wikilink_distribution['1-2'] += 1
            notes_with_links += 1
        elif wikilink_count <= 5:
            wikilink_distribution['3-5'] += 1
            notes_with_links += 1
        else:
            wikilink_distribution['6+'] += 1
            notes_with_links += 1

        if note.get('is_hub', False):
            hub_notes.append(note.get('title', 'Untitled'))

    # v6.1 Edges-A: Build typed edge graph (replaces flat wikilink list)
    print("\n[v6.1 Edges-A] Building typed memory graph...")
    typed_graph = {}            # path -> {edge_type: [targets]}
    typed_edge_counts = {t: 0 for t in EDGE_TYPES}
    legacy_wikilink_graph = {}  # backup compat — flat list per note
    for note in notes:
        note_path = note.get('path', '')
        wikilinks = note.pop('_wikilinks', [])  # legacy flat list
        typed = note.pop('_typed_edges', None)  # v6.1 typed dict

        if wikilinks:
            legacy_wikilink_graph[note_path] = wikilinks

        if typed:
            # Strip empty edge-type lists for compactness
            non_empty = {t: v for t, v in typed.items() if v}
            if non_empty:
                typed_graph[note_path] = non_empty
                for t, v in non_empty.items():
                    typed_edge_counts[t] = typed_edge_counts.get(t, 0) + len(v)

    # v6.1: Back up the previous flat-format graph file before overwrite (one-shot migration)
    GRAPH_FILE = OUTPUT_FILE.parent / "memory-graph.json"
    if GRAPH_FILE.exists():
        backup = GRAPH_FILE.with_suffix(".json.pre-typed.bak")
        if not backup.exists():  # only back up the FIRST time we migrate
            try:
                backup.write_bytes(GRAPH_FILE.read_bytes())
                print(f"  Pre-typed backup: {backup.name}")
            except OSError as e:
                print(f"  WARN: backup failed ({e}) — proceeding anyway")

    with open(GRAPH_FILE, 'w', encoding='utf-8') as f:
        json.dump({
            "version": "2.0",  # v6.1 Edges-A — typed schema
            "description": "Typed memory edges (5 types). Legacy flat wikilinks under legacy_edges for back-compat.",
            "edge_types": list(EDGE_TYPES),
            "edges": typed_graph,           # path -> {edge_type: [targets]}
            "legacy_edges": legacy_wikilink_graph,  # path -> [flat list] (read by tools that haven't migrated)
            "stats": {
                "notes_with_typed_edges": len(typed_graph),
                "notes_with_any_edges": len(legacy_wikilink_graph),
                "total_typed_edges": sum(typed_edge_counts.values()),
                "by_type": typed_edge_counts,
            },
            "created": datetime.now().isoformat() + 'Z'
        }, f, indent=2)
    graph_size = GRAPH_FILE.stat().st_size
    print(f"  Graph file: {graph_size / 1024:.1f}KB ({len(typed_graph)} notes with typed edges, {len(legacy_wikilink_graph)} notes with any links)")
    for t, c in typed_edge_counts.items():
        if c:
            print(f"    {t}: {c} edge(s)")

    # v5.0: Add pre-computed scores to each note
    print("\n[v5.0] Computing pre-computed scores...")
    now = datetime.now()
    for note in notes:
        note['precomputed_scores'] = compute_precomputed_scores(note, now)

    # v5.0: Build tag intersection index
    print("[v5.0] Building tag intersection index...")
    tag_index = build_tag_index(notes)

    # v6.0: Build entity index for client matching
    print("[v6.0] Building entity index for client matching...")
    entity_index = build_entity_index(notes)

    # v5.2: Strip _wikilinks and _typed_edges from notes before building shards
    for note in notes:
        note.pop('_wikilinks', None)
        note.pop('_typed_edges', None)  # v6.1 — already consumed above

    # v5.0: Build domain shards
    print("[v5.0] Building domain shards...")
    shard_meta = build_domain_shards(notes)

    # Build index structure
    index = {
        "version": "6.0",  # v6.0: Predictive Client Context + Entity Index
        "notes": notes,
        "last_updated": datetime.now().isoformat() + 'Z',
        "total_notes": len(notes),
        "tag_stats": {
            "folder_tags": folder_tags,
            "software_tags": software_tags,
            "unique_software_tags": len(set(all_tags)),
            "valid_tag_count": valid_count,
            "invalid_tag_count": invalid_count
        },
        # v4.0: Graph connectivity stats
        "graph_stats": {
            "total_wikilinks": total_wikilinks,
            "notes_with_links": notes_with_links,
            "isolated_nodes": len(notes_without_links),
            "hub_notes": len(hub_notes),
            "avg_links_per_note": round(total_wikilinks / len(notes), 2) if notes else 0,
            "wikilink_distribution": wikilink_distribution,
            "connectivity_score": round((notes_with_links / len(notes)) * 100, 1) if notes else 0
        },
        # v5.0: Tag intersection index for O(1) lookups
        "tag_index": tag_index,
        # v5.0: Shard references for selective loading
        "shards": shard_meta,
        # v6.0: Entity index for client matching (Predictive Client Context)
        "entity_index": entity_index
    }

    # Write to file
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(index, f, indent=2)

    elapsed_time = time.time() - start_time

    print("-" * 50)
    print(f"\n[MEMORY INDEX v6.0 - Predictive Client Context + Scaling]")
    print(f"Index created: {OUTPUT_FILE}")
    print(f"Total notes: {len(notes)}")
    print(f"Build time: {elapsed_time:.2f}s")
    print(f"Skipped (temp/system files): {skipped_count}")
    print(f"Tags valid: {valid_count}/{len(notes)} ({100*valid_count//len(notes) if notes else 0}%)")
    print(f"Tags need fix: {invalid_count}")
    print(f"\nFolder tag distribution:")
    for tag, count in sorted(folder_tags.items()):
        print(f"  {tag}: {count}")
    print(f"\nSoftware tag distribution:")
    for tag, count in sorted(software_tags.items(), key=lambda x: -x[1]):
        print(f"  {tag}: {count}")

    # v5.0: Scaling stats
    print(f"\n" + "=" * 50)
    print(f"SCALING ENHANCEMENTS (v5.0)")
    print(f"=" * 50)
    print(f"Tag intersection index:")
    print(f"  Single tags indexed: {tag_index['stats']['single_tags']}")
    print(f"  2-tag combos: {tag_index['stats']['two_tag_combos']}")
    print(f"  3-tag combos: {tag_index['stats']['three_tag_combos']}")
    print(f"\nDomain shards:")
    for shard_name, shard_info in shard_meta.items():
        print(f"  {shard_name}: {shard_info['count']} notes")
    print(f"\nPre-computed scores: {len(notes)} notes scored")

    # v4.0: Graph connectivity report
    print(f"\n" + "=" * 50)
    print(f"GRAPH CONNECTIVITY REPORT (v4.0)")
    print(f"=" * 50)
    print(f"Total wikilinks: {total_wikilinks}")
    print(f"Average links per note: {index['graph_stats']['avg_links_per_note']}")
    print(f"Connectivity score: {index['graph_stats']['connectivity_score']}%")
    print(f"\nWikilink distribution:")
    print(f"  0 links (isolated): {wikilink_distribution['0']} notes")
    print(f"  1-2 links (sparse): {wikilink_distribution['1-2']} notes")
    print(f"  3-5 links (connected): {wikilink_distribution['3-5']} notes")
    print(f"  6+ links (hub-like): {wikilink_distribution['6+']} notes")
    print(f"\nHub notes found: {len(hub_notes)}")
    for hub in hub_notes:
        print(f"  - {hub}")
    if notes_without_links and len(notes_without_links) <= 20:
        print(f"\nIsolated nodes (no wikilinks):")
        for title in notes_without_links[:20]:
            print(f"  - {title}")
        if len(notes_without_links) > 20:
            print(f"  ... and {len(notes_without_links) - 20} more")
    elif notes_without_links:
        print(f"\nIsolated nodes: {len(notes_without_links)} (run migration to add wikilinks)")

    # v6.1 Decay-A: confidence/decay distribution
    print(f"\n" + "=" * 50)
    print(f"DECAY DISTRIBUTION (v6.1 Decay-A)")
    print(f"=" * 50)
    inferred_count = sum(1 for n in notes if n.get('inferred'))
    below_floor = sum(1 for n in notes if n.get('effective_confidence', 1.0) < CONFIDENCE_FLOOR)
    asserted_count = len(notes) - inferred_count
    print(f"Asserted notes (inferred:false, no decay): {asserted_count}")
    print(f"Inferred notes (decay-eligible): {inferred_count}")
    print(f"Below confidence floor ({CONFIDENCE_FLOOR}): {below_floor}")
    if inferred_count and below_floor:
        print(f"  → Recall Pass 0.25 will drop these unless --include-stale")

    # v6.0: Entity index report (Predictive Client Context)
    print(f"\n" + "=" * 50)
    print(f"ENTITY INDEX (v6.0 - Predictive Client Context)")
    print(f"=" * 50)
    print(f"Unique clients indexed: {entity_index['stats']['unique_clients']}")
    print(f"Unique deals indexed: {entity_index['stats']['unique_deals']}")
    print(f"Unique CRM IDs indexed: {entity_index['stats']['unique_crm_ids']}")
    print(f"\nClient names in index:")
    for client_name in sorted(entity_index['clients'].keys())[:10]:
        paths = entity_index['clients'][client_name]
        print(f"  '{client_name}' -> {len(paths)} note(s)")
    if len(entity_index['clients']) > 10:
        print(f"  ... and {len(entity_index['clients']) - 10} more clients")
    print(f"\nDeal IDs in index:")
    for deal_id in sorted(entity_index['deals'].keys()):
        paths = entity_index['deals'][deal_id]
        print(f"  {deal_id} -> {len(paths)} note(s)")
    print(f"\nCRM IDs in index:")
    for crm_entity_id in sorted(entity_index['crm_ids'].keys())[:5]:
        paths = entity_index['crm_ids'][crm_entity_id]
        print(f"  {crm_entity_id} -> {len(paths)} note(s)")
    if len(entity_index['crm_ids']) > 5:
        print(f"  ... and {len(entity_index['crm_ids']) - 5} more IDs")

if __name__ == '__main__':
    build_index()
