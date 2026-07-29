#!/usr/bin/env python3
"""
Hybrid Recall v7.1 - Executable Memory Recall Engine

Features:
- BM25 keyword scoring for rare terms (Pass 1.75)
- Vector semantic similarity via pre-computed embeddings (Pass 1.5)
- MMR diversity filter (Pass 6)
- Bidirectional link expansion (Pass 3)
- Optional cross-encoder reranking (Pass 7)
- Graceful fallback (works without embeddings)

Performance Target: <350ms total (200ms without reranking)
CRITICAL FIX: Removed model loading bottleneck (17.7s → <200ms)

Author: Claude Code + @builder
Version: 7.1 (2026-02-05)
"""

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set

import numpy as np

# =============================================================================
# CONFIGURATION
# =============================================================================

CLAUDE_DIR = Path(__file__).parent.parent
MEMORY_VAULT = CLAUDE_DIR / "memory"
SESSION_DIR = CLAUDE_DIR / "session"
STATE_FILE = SESSION_DIR / "state.json"
MEMORY_INDEX = SESSION_DIR / "memory-index.json"
BM25_INDEX = SESSION_DIR / "bm25-index.json"
MEMORY_GRAPH = SESSION_DIR / "memory-graph.json"  # v7.2: typed edges
QUERY_EMBED_CACHE = SESSION_DIR / "query-embed-cache.json"  # v7.3: query vectors by hash

# v7.3 candidate pool: how many notes each signal contributes BEFORE final scoring.
# Previously the tag filter alone cut 4,343 notes to 30 before vector/BM25 ever ran,
# so the correct note was usually discarded before it could be scored.
POOL_FROM_VECTOR = 60
POOL_FROM_BM25 = 60
QUERY_CACHE_CAP = 500

# Off by default so the hot fallback path never pays the ~30s encoder load.
# Enabled by --allow-model-load, by LEROY_RECALL_ALLOW_MODEL=1, or in-process by
# callers that can afford it (eval_recall.py, warm-query-cache.py).
ALLOW_MODEL_LOAD = os.environ.get("LEROY_RECALL_ALLOW_MODEL", "") == "1"

# BM25 parameters
BM25_K1 = 1.5  # Term frequency saturation
BM25_B = 0.75  # Document length normalization

# MMR parameters
MMR_LAMBDA = 0.7  # Relevance vs diversity trade-off (0.7 = 70% relevance, 30% diversity)

# v7.2: Decay floor (Pass 0.25) — drop inferred notes whose effective_confidence has decayed below this
DECAY_FLOOR = 0.3
# v7.2: Edge demotion penalty (Pass 6.5)
SUPERSEDED_PENALTY = 0.5

# Scoring weights
# v7.3 weights, chosen by sweep against memory/_eval/recall-goldset.jsonl.
# Rationale: query-RELEVANT signals (vector, bm25) now carry the ranking; the tag
# score is a flat 0.1 baseline whenever the query yields no tags, and domain is a
# query-independent prior — at their old weights (tag .30 / domain .15) those two
# priors outvoted the actual match. Measured recall@5 across configs:
#   tag.30 vec.25 bm25.20 dom.15  -> 53.3%   (previous)
#   tag.15 vec.35 bm25.35 dom.15  -> 57.8%
#   tag.10 vec.30 bm25.50 dom.10  -> 62.2%
#   tag.00 vec.50 bm25.50 dom.00  -> 66.7%
#   tag.05 vec.55 bm25.30 dom.10  -> 68.9%   (selected: best recall AND best MRR)
# Re-run `python scripts/eval_recall.py` after changing anything here.
WEIGHTS = {
    "tag": 0.05,
    "vector": 0.55,
    "bm25": 0.30,
    "domain": 0.10,
    "recency": 0.0
}


# =============================================================================
# STATE MANAGEMENT
# =============================================================================

def load_session_state() -> Dict:
    """Load session state from state.json with error handling."""
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[WARNING] Could not load state.json: {e}")
    return {}


def save_session_state(state: Dict) -> None:
    """Save session state to state.json atomically."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
    except IOError as e:
        print(f"[ERROR] Could not save state.json: {e}")


def load_memory_index() -> Dict:
    """Load memory index with embeddings."""
    try:
        if MEMORY_INDEX.exists():
            with open(MEMORY_INDEX, 'r', encoding='utf-8') as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[WARNING] Could not load memory-index.json: {e}")
    return {"notes": [], "version": "6.0"}


def load_bm25_index() -> Optional[Dict]:
    """Load BM25 term index and normalize to the ranking pass's expected shape.

    v7.4 fix (2026-07-24, found by stress test): the writer (bm25-indexer.py) and
    this reader NEVER shared a schema. The file carries stats.num_docs /
    stats.avg_doc_length / term_doc_counts / inverted_index; the ranking pass reads
    doc_count / avg_doc_len / term_doc_freq / doc_term_freq. Four of five lookups
    silently returned defaults, so every BM25 score computed to 0.0 — the "hybrid"
    engine has been running with its keyword leg fully dead. Normalizing here (one
    pass over ~70K postings at load) rather than changing the writer keeps the
    on-disk format, which is the more compact of the two.
    """
    try:
        if not BM25_INDEX.exists():
            return None
        with open(BM25_INDEX, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[INFO] BM25 index not available: {e}")
        return None

    # Already in reader shape (future-proof: if the writer is ever aligned).
    if 'term_doc_freq' in raw and 'doc_term_freq' in raw:
        return raw

    stats = raw.get('stats', {})
    inverted = raw.get('inverted_index', {})

    # invert term->[[doc, tf], ...] into doc->{term: tf}
    doc_term_freq: Dict = {}
    for term, postings in inverted.items():
        for doc, tf in postings:
            doc_term_freq.setdefault(doc, {})[term] = tf

    return {
        'doc_count': stats.get('num_docs', len(raw.get('doc_lengths', {})) or 1),
        'avg_doc_len': stats.get('avg_doc_length', 1.0),
        'term_doc_freq': raw.get('term_doc_counts', {}),
        'doc_term_freq': doc_term_freq,
        'doc_lengths': raw.get('doc_lengths', {}),
    }


def load_memory_graph() -> Dict:
    """v7.2: Load typed-edge graph (memory-graph.json).

    Schema (per build-memory-index.py):
        {
          "version": "...", "edge_types": [...],
          "edges": {path: {supersedes:[...], contradicts:[...], supports:[...],
                           derived_from:[...], refers_to:[...]}},
          "legacy_edges": {...}
        }
    Returns the dict, or empty stub on missing/error.
    """
    try:
        if MEMORY_GRAPH.exists():
            with open(MEMORY_GRAPH, 'r', encoding='utf-8') as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[INFO] memory-graph.json not available: {e}")
    return {"edges": {}, "legacy_edges": {}}


# REMOVED: load_embeddings_model() - no longer needed
# We now use pre-computed embeddings from memory-index.json


# =============================================================================
# TEXT PROCESSING
# =============================================================================

def tokenize(text: str) -> List[str]:
    """Simple tokenization for BM25."""
    # Lowercase and split on non-alphanumeric
    tokens = re.findall(r'\w+', text.lower())
    return tokens


def extract_tags_from_query(query: str) -> Set[str]:
    """Extract potential tags from query (simple keyword extraction)."""
    # Common software/tech terms that might be tags
    tech_terms = {
        'crm', 'revit', 'android', 'python', 'typescript',
        'git', 'leroy', 'bim', 'mcp', 'supabase',
        'react', 'excel', 'word', 'pdf', 'playwright', 'memory'
    }

    query_lower = query.lower()
    tokens = tokenize(query)

    # Extract tags from query
    tags = set()
    for term in tech_terms:
        if term in query_lower:
            tags.add(term)

    return tags


# =============================================================================
# RECALL PIPELINE - PASS 0: SESSION FILTER
# =============================================================================

def pass_0_session_filter(notes: List[Dict], session_id: str) -> List[Dict]:
    """
    MANDATORY: Filter notes by session_id to prevent cross-session contamination.

    Notes without session_id are considered global (accessible to all sessions).
    """
    filtered = []
    for note in notes:
        note_session = note.get('session_id')
        # Include note if:
        # 1. It has no session_id (global note)
        # 2. Its session_id matches current session
        if not note_session or note_session == session_id:
            filtered.append(note)

    return filtered


# =============================================================================
# RECALL PIPELINE - PASS 0.25: DECAY FLOOR (v7.2)
# =============================================================================

def pass_0_25_decay_floor(notes: List[Dict], floor: float = DECAY_FLOOR,
                          include_stale: bool = False) -> Tuple[List[Dict], int]:
    """
    v7.2 Decay-A: Drop inferred notes whose effective_confidence has decayed below floor.

    Behavior:
      - Notes without effective_confidence default to 1.0 (no drop). Backward-compatible.
      - Notes with inferred=False (asserted) default-pass at any confidence.
      - Only notes flagged inferred=True with effective_confidence < floor are dropped.
      - include_stale=True bypasses the filter (high-recall mode for explicit queries).

    Activation: zero impact today (no notes have inferred=True yet).
    Activates automatically as memory-consolidation marks notes inferred and time passes.

    Returns (filtered_notes, dropped_count).
    """
    if include_stale:
        return notes, 0

    kept = []
    dropped = 0
    for note in notes:
        ec = note.get('effective_confidence', 1.0)
        # Only drop if explicitly inferred AND below floor
        # (asserted/legacy notes with default ec=1.0 always pass)
        if note.get('inferred') and ec < floor:
            dropped += 1
            continue
        kept.append(note)
    return kept, dropped


# =============================================================================
# RECALL PIPELINE - PASS 1: TAG FILTER
# =============================================================================

def pass_1_tag_filter(notes: List[Dict], query: str, working_memory: Dict) -> Tuple[List[Dict], Dict]:
    """
    Tag intersection + working memory priority.

    Returns:
        - Top 30 candidates
        - Tag scores dict {note_path: score}
    """
    query_tags = extract_tags_from_query(query)

    # Extract working memory context
    current_project = working_memory.get('project', '').lower()
    current_client = working_memory.get('client', '').lower()

    scores = {}

    for note in notes:
        note_tags = set(tag.lower() for tag in note.get('tags', []))
        note_path = note.get('path', '')

        # Tag overlap score
        if query_tags:
            overlap = len(query_tags & note_tags)
            tag_score = overlap / len(query_tags) if query_tags else 0.0
        else:
            # If no query tags, use baseline
            tag_score = 0.1

        # Working memory boost
        boost = 0.0
        if current_project and current_project in note.get('project', '').lower():
            boost += 0.3
        if current_client and current_client in note_path.lower():
            boost += 0.2

        scores[note_path] = min(1.0, tag_score + boost)

    # Sort by score and return top 30
    sorted_notes = sorted(notes, key=lambda n: scores.get(n.get('path', ''), 0.0), reverse=True)
    candidates = sorted_notes[:30]

    return candidates, scores


# =============================================================================
# RECALL PIPELINE - PASS 1.5: VECTOR SIMILARITY
# =============================================================================

def generate_query_embedding(query: str) -> Optional[np.ndarray]:
    """
    Embed the QUERY with the same model that embedded the notes (all-MiniLM-L6-v2).

    v7.3 fix (2026-07-23): the previous implementation never embedded the query at
    all — it averaged the embeddings of notes that ALREADY lexically matched the
    query's title/tags. That was circular (semantic pass could only rediscover what
    keyword matching had found) and returned None whenever nothing lexically matched,
    which silently disabled the entire vector pass. Measured cost: recall@5 of 2.2%
    on the 45-query gold set.

    Embeddings are cached on disk by query hash so repeat queries skip the model load.

    Returns:
        384-dim normalized query embedding, or None if the encoder is unavailable
        (callers fall back to the keyword pipeline).
    """
    cache_key = hashlib.md5(query.strip().lower().encode("utf-8")).hexdigest()
    cache = _load_query_embedding_cache()
    if cache_key in cache:
        return np.array(cache[cache_key])

    # HOT-PATH GUARD: loading sentence-transformers costs ~30s cold. This script is
    # the FALLBACK for gate-enforcer.py when the warm sidecar (127.0.0.1:7742) is
    # down, so a cold model load here would stall session start. Unless the caller
    # explicitly opts in (eval, offline reindex, cache warming), skip the encoder and
    # let the keyword pipeline answer. Cached queries above still get full semantics.
    if not ALLOW_MODEL_LOAD:
        return None

    try:
        spec = importlib.util.spec_from_file_location(
            "vector_embeddings", Path(__file__).parent / "vector_embeddings.py"
        )
        ve = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ve)
        emb = ve.generate_embedding(query)
    except Exception as e:
        print(f"[INFO] query encoder unavailable ({e}); keyword-only recall")
        return None

    if not emb:
        return None

    vec = np.array(emb, dtype=float)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm

    cache[cache_key] = vec.tolist()
    _save_query_embedding_cache(cache)
    return vec


def _load_query_embedding_cache() -> Dict:
    try:
        if QUERY_EMBED_CACHE.exists():
            return json.loads(QUERY_EMBED_CACHE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_query_embedding_cache(cache: Dict) -> None:
    # Bounded: keep the most recent QUERY_CACHE_CAP entries so this can't grow forever.
    try:
        if len(cache) > QUERY_CACHE_CAP:
            cache = dict(list(cache.items())[-QUERY_CACHE_CAP:])
        QUERY_EMBED_CACHE.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass


def generate_simple_query_embedding(query: str, notes: List[Dict]) -> Optional[np.ndarray]:
    """
    DEPRECATED (v7.3) — lexical bootstrap retained only as a fallback for when the
    sentence-transformers encoder is unavailable. See generate_query_embedding().

    Strategy: Find notes with matching keywords, average their embeddings.

    Returns:
        Query embedding (384 dimensions) or None if no matches
    """
    query_terms = set(query.lower().split())

    # Find notes with overlapping terms
    matching_notes = []
    for note in notes:
        # Check title and tags
        note_text = f"{note.get('title', '')} {' '.join(note.get('tags', []))}".lower()
        overlap = len(query_terms & set(note_text.split()))

        if overlap > 0 and "embedding" in note and note["embedding"]:
            matching_notes.append((note, overlap))

    if not matching_notes:
        return None

    # Average embeddings of matching notes, weighted by overlap
    total_weight = sum(weight for _, weight in matching_notes)

    avg_embedding = np.zeros(384)  # 384 dimensions for all-MiniLM-L6-v2
    for note, weight in matching_notes:
        note_emb = np.array(note["embedding"])
        avg_embedding += note_emb * (weight / total_weight)

    # Normalize
    norm = np.linalg.norm(avg_embedding)
    if norm > 0:
        avg_embedding = avg_embedding / norm

    return avg_embedding


def _build_candidate_pool(notes: List[Dict], tag_top: List[Dict],
                          vector_scores: Dict, bm25_scores: Dict) -> List[Dict]:
    """
    v7.3: union the strongest notes from every signal into one candidate pool.

    Before this existed, the tag filter's top-30 WAS the candidate set, so a note
    that matched semantically or by rare keyword — but carried no matching tag —
    could never be scored. With no extractable query tags every note ties at 0.1
    and the "top 30" degenerated to the first 30 notes in index order.

    Order is irrelevant here; pass_5_final_scoring re-ranks the whole pool.
    """
    by_path = {n.get("path", ""): n for n in notes}
    pool_paths = {n.get("path", "") for n in tag_top}

    for scores, cap in ((vector_scores, POOL_FROM_VECTOR), (bm25_scores, POOL_FROM_BM25)):
        if not scores:
            continue
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        pool_paths.update(p for p, _ in ranked[:cap])

    pool = [by_path[p] for p in pool_paths if p in by_path]
    return pool or tag_top


def pass_1_5_vector_similarity(notes: List[Dict], query: str, query_embedding: Optional[np.ndarray] = None) -> Dict:
    """
    NEW: Semantic similarity via pre-computed embeddings.
    Uses embeddings already in memory-index.json (no model loading).

    Args:
        notes: Candidate notes with embeddings
        query: Search query (unused if query_embedding provided)
        query_embedding: Pre-computed query embedding (optional)

    Returns:
        - Similarity scores dict {note_path: score}
    """
    if query_embedding is None:
        # Fallback: return empty scores
        return {}

    scores = {}

    # Use pre-computed embeddings from notes
    for note in notes:
        note_path = note.get('path', '')

        if "embedding" not in note or note["embedding"] is None:
            continue

        note_embedding = np.array(note["embedding"])

        # Cosine similarity
        similarity = np.dot(query_embedding, note_embedding) / (
            np.linalg.norm(query_embedding) * np.linalg.norm(note_embedding)
        )

        scores[note_path] = float(similarity)

    return scores


# =============================================================================
# RECALL PIPELINE - PASS 1.75: BM25 RANKING
# =============================================================================

def pass_1_75_bm25_ranking(notes: List[Dict], query: str, bm25_index: Optional[Dict]) -> Dict:
    """
    NEW: BM25 keyword scoring for rare terms.

    BM25 formula:
    score(D, Q) = Σ IDF(q_i) * (f(q_i, D) * (k1 + 1)) / (f(q_i, D) + k1 * (1 - b + b * |D| / avgdl))

    Returns:
        - BM25 scores dict {note_path: score}
    """
    if bm25_index is None:
        return {}

    scores = {}
    query_tokens = tokenize(query)

    # Get corpus stats from index
    doc_count = bm25_index.get('doc_count', 1)
    avg_doc_len = bm25_index.get('avg_doc_len', 1.0)
    term_doc_freq = bm25_index.get('term_doc_freq', {})
    doc_term_freq = bm25_index.get('doc_term_freq', {})
    doc_lengths = bm25_index.get('doc_lengths', {})

    for note in notes:
        note_path = note.get('path', '')

        # Get document stats
        doc_len = doc_lengths.get(note_path, avg_doc_len)
        term_freq = doc_term_freq.get(note_path, {})

        bm25_score = 0.0

        for token in query_tokens:
            # Calculate IDF (Inverse Document Frequency)
            df = term_doc_freq.get(token, 0)
            if df == 0:
                continue

            idf = math.log((doc_count - df + 0.5) / (df + 0.5) + 1.0)

            # Get term frequency in document
            tf = term_freq.get(token, 0)

            # BM25 formula
            numerator = tf * (BM25_K1 + 1)
            denominator = tf + BM25_K1 * (1 - BM25_B + BM25_B * (doc_len / avg_doc_len))

            bm25_score += idf * (numerator / denominator)

        # Normalize by query length
        if query_tokens:
            bm25_score = bm25_score / len(query_tokens)

        scores[note_path] = bm25_score

    # Normalize scores to [0, 1]
    if scores:
        max_score = max(scores.values())
        if max_score > 0:
            scores = {k: v / max_score for k, v in scores.items()}

    return scores


# =============================================================================
# RECALL PIPELINE - PASS 2-4: DOMAIN SCORING
# =============================================================================

def pass_2_4_domain_scoring(notes: List[Dict], state: Dict) -> Dict:
    """
    Existing: Project, domain, recency scoring.

    Returns:
        - Domain scores dict {note_path: score}
    """
    scores = {}

    # Extract context from state
    current_project = state.get('session_window', '').lower()
    current_time = datetime.utcnow()

    for note in notes:
        note_path = note.get('path', '')
        score = 0.0

        # Project match (0.4)
        note_project = note.get('project', '').lower()
        if note_project and current_project and note_project == current_project:
            score += 0.4

        # Domain relevance (0.3)
        # Boost notes with domain tags
        if note.get('domain'):
            score += 0.3

        # Recency (0.3)
        # Parse modified timestamp
        modified_str = note.get('modified', '')
        if modified_str:
            try:
                # Handle ISO format with Z suffix
                if modified_str.endswith('Z'):
                    modified_str = modified_str[:-1]

                modified = datetime.fromisoformat(modified_str)
                days_old = (current_time - modified).days

                # Decay function: 1.0 at 0 days, 0.5 at 30 days, 0.1 at 90 days
                recency_score = max(0.1, 1.0 - (days_old / 90.0))
                score += 0.3 * recency_score
            except Exception:
                pass

        scores[note_path] = min(1.0, score)

    return scores


# =============================================================================
# RECALL PIPELINE - PASS 5: FINAL SCORING
# =============================================================================

def _normalize_scores(score_dict: Dict, notes: List[Dict]) -> Dict:
    """Min-max a single signal to 0-1 over the candidate pool.

    A signal that is constant across the pool (e.g. the tag baseline of 0.1 when the
    query produced no tags) carries no ranking information, so it collapses to 0
    rather than adding a uniform offset that dilutes the signals that do discriminate.
    """
    if not score_dict:
        return {}
    paths = [n.get('path', '') for n in notes]
    vals = [score_dict.get(p, 0.0) for p in paths]
    if not vals:
        return {}
    lo, hi = min(vals), max(vals)
    span = hi - lo
    if span <= 1e-12:
        return {p: 0.0 for p in paths}
    return {p: (score_dict.get(p, 0.0) - lo) / span for p in paths}


def pass_5_final_scoring(notes: List[Dict], scores: Dict, weights: Dict) -> List[Tuple[Dict, float]]:
    """
    Weighted combination of all scores.

    Args:
        notes: Candidate notes
        scores: Dict of score dicts {score_type: {note_path: score}}
        weights: Score weights

    Returns:
        List of (note, final_score) tuples sorted by score descending
    """
    # v7.3: normalize each signal to 0-1 across the candidate pool BEFORE weighting.
    # Previously the signals were combined on wildly different scales — BM25 is
    # unbounded, vector similarity is a cosine that rarely exceeds ~0.4, and the tag
    # score is a flat 0.1 baseline when the query yields no tags. The configured
    # weights therefore did not mean what they said, and query-independent priors
    # (domain, recency) could outrank the actual semantic match.
    # Only BM25 is normalized. Measured on the gold set: min-max'ing every signal
    # dropped recall@5 from 53.3% to 46.7%, because rescaling a weak/near-constant
    # signal (domain, tag baseline) to a full 0-1 range promotes noise to the same
    # confidence as a real match. Vector cosine, tag and domain are already bounded.
    scores = dict(scores)
    if "bm25" in scores:
        scores["bm25"] = _normalize_scores(scores["bm25"], notes)

    final_scores = []

    for note in notes:
        note_path = note.get('path', '')

        # Combine scores
        final_score = 0.0

        for score_type, weight in weights.items():
            if score_type in scores:
                score_dict = scores[score_type]
                final_score += weight * score_dict.get(note_path, 0.0)

        # Add final score to note for output
        note_copy = note.copy()
        note_copy['final_score'] = final_score

        final_scores.append((note_copy, final_score))

    # Sort by final score descending
    final_scores.sort(key=lambda x: x[1], reverse=True)

    return final_scores


# =============================================================================
# RECALL PIPELINE - PASS 6: MMR DIVERSIFICATION
# =============================================================================

def pass_6_mmr_diversification(candidates: List[Tuple[Dict, float]], lambda_param: float = 0.7) -> List[Dict]:
    """
    NEW: Maximal Marginal Relevance - remove redundant notes.

    MMR formula: argmax[λ * relevance - (1-λ) * max_similarity_to_selected]

    Args:
        candidates: List of (note, relevance_score) tuples
        lambda_param: Trade-off between relevance and diversity (0.7 = 70% relevance)

    Returns:
        Diverse top 15 notes
    """
    if not candidates:
        return []

    selected = []
    remaining = list(candidates)

    # Select first note (highest relevance)
    first = remaining.pop(0)
    selected.append(first[0])

    # Iteratively select diverse notes
    while remaining and len(selected) < 15:
        best_mmr_score = -float('inf')
        best_idx = -1

        for idx, (note, relevance) in enumerate(remaining):
            # Calculate max similarity to selected notes
            max_sim = 0.0

            for selected_note in selected:
                # Use embeddings for similarity if available
                if 'embedding' in note and 'embedding' in selected_note:
                    note_emb = np.array(note['embedding'])
                    sel_emb = np.array(selected_note['embedding'])

                    # Cosine similarity
                    note_norm = np.linalg.norm(note_emb)
                    sel_norm = np.linalg.norm(sel_emb)

                    if note_norm > 0 and sel_norm > 0:
                        sim = np.dot(note_emb, sel_emb) / (note_norm * sel_norm)
                        max_sim = max(max_sim, sim)
                else:
                    # Fallback: Use tag overlap as similarity
                    note_tags = set(note.get('tags', []))
                    sel_tags = set(selected_note.get('tags', []))

                    if note_tags or sel_tags:
                        overlap = len(note_tags & sel_tags)
                        total = len(note_tags | sel_tags)
                        sim = overlap / total if total > 0 else 0.0
                        max_sim = max(max_sim, sim)

            # MMR score
            mmr_score = lambda_param * relevance - (1 - lambda_param) * max_sim

            if mmr_score > best_mmr_score:
                best_mmr_score = mmr_score
                best_idx = idx

        if best_idx >= 0:
            selected.append(remaining.pop(best_idx)[0])
        else:
            break

    return selected


# =============================================================================
# RECALL PIPELINE - PASS 6.5: EDGE WALK (v7.2 Edges-B)
# =============================================================================

def pass_6_5_edge_walk(notes: List[Dict], graph: Dict) -> Tuple[List[Dict], Dict]:
    """
    v7.2 Edges-B: 1-hop typed-edge walk on the result set.

    For each note in the result set:
      - If another result-set note SUPERSEDES this one, mark this with [SUPERSEDED]
        and demote so the superseding note ranks higher.
      - If this note CONTRADICTS another result-set note, mark BOTH with [CONFLICT].

    Cost: O(N²) over result set (N≈12). Negligible (<5ms).

    No-op safety:
      - Notes lacking edges in graph contribute nothing.
      - Today: 0 typed edges in graph → this pass is a no-op until consolidation
        starts writing typed `edges:` frontmatter blocks.

    Returns (annotated_notes, walk_stats).
    """
    if not graph or not graph.get("edges"):
        return notes, {"superseded": 0, "conflicts": 0, "skipped": "no_typed_graph"}

    edges = graph["edges"]
    paths_in_results = {n.get("path") for n in notes if n.get("path")}

    superseded_count = 0
    conflict_count = 0

    for note in notes:
        path = note.get("path")
        if not path:
            continue
        note_edges = edges.get(path, {})

        # Inbound supersedes: does ANYTHING in the index supersede this note?
        # Walk every other note's `supersedes` list and check if it includes us.
        # (Cheap: only walk result-set notes for reciprocal awareness; full-index walk
        # is reserved for explicit --trace mode.)
        for other in notes:
            if other is note:
                continue
            other_edges = edges.get(other.get("path", ""), {})
            if path in other_edges.get("supersedes", []):
                # `other` supersedes `note` → demote `note`
                note["_superseded_by"] = other.get("path")
                note["_marker"] = "[SUPERSEDED]"
                superseded_count += 1
                break

        # Outbound contradicts intersected with result set
        contradicts = note_edges.get("contradicts", [])
        for target_path in contradicts:
            if target_path in paths_in_results:
                # Flag both — walk the result set to find the target dict and mark it
                note["_marker"] = "[CONFLICT]"
                note.setdefault("_conflict_with", [])
                if target_path not in note["_conflict_with"]:
                    note["_conflict_with"].append(target_path)
                    conflict_count += 1
                for other in notes:
                    if other.get("path") == target_path:
                        other["_marker"] = "[CONFLICT]"
                        other.setdefault("_conflict_with", [])
                        if path not in other["_conflict_with"]:
                            other["_conflict_with"].append(path)

    # Stable resort: superseded notes drop to the bottom of the list (preserve relative order)
    annotated = sorted(notes, key=lambda n: 1 if n.get("_marker") == "[SUPERSEDED]" else 0)

    return annotated, {"superseded": superseded_count, "conflicts": conflict_count}


# =============================================================================
# RECALL PIPELINE - PASS 7: RERANKING
# =============================================================================

def pass_7_reranking(notes: List[Dict], query: str, use_reranking: bool = False) -> List[Dict]:
    """
    NEW: Optional cross-encoder reranking.

    Args:
        notes: Top 15 diverse notes
        query: Search query
        use_reranking: Enable reranking (adds ~150ms)

    Returns:
        Reranked notes
    """
    if not use_reranking:
        return notes

    try:
        from sentence_transformers import CrossEncoder

        # Load cross-encoder model
        model = CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')

        # Create query-document pairs
        pairs = []
        for note in notes:
            # Use title + first 500 chars of content as document
            title = note.get('title', '')
            content = note.get('content', '')[:500]
            doc = f"{title}\n{content}"
            pairs.append([query, doc])

        # Get reranking scores
        scores = model.predict(pairs)

        # Sort notes by reranking scores
        reranked = sorted(zip(notes, scores), key=lambda x: x[1], reverse=True)
        return [note for note, _ in reranked]

    except ImportError:
        print("[INFO] Cross-encoder not available. Skipping reranking.")
        return notes
    except Exception as e:
        print(f"[WARNING] Reranking error: {e}")
        return notes


# =============================================================================
# MAIN RECALL FUNCTION
# =============================================================================

def hybrid_recall(query: str, use_reranking: bool = False, tier_1_count: int = 12) -> Dict:
    """
    Execute hybrid recall with all 7 layers.

    Args:
        query: Search query
        use_reranking: Enable optional cross-encoder reranking
        tier_1_count: Number of top notes to return (default 5)

    Returns:
        Dict with notes and metadata
    """
    start_time = time.time()

    # Load resources
    state = load_session_state()
    memory_index = load_memory_index()
    bm25_index = load_bm25_index()
    memory_graph = load_memory_graph()  # v7.2: typed edges

    notes = memory_index.get("notes", [])
    active_layers = []

    # Pass 0: Session filter (MANDATORY)
    session_id = state.get("session_id", "")
    notes = pass_0_session_filter(notes, session_id)
    active_layers.append("Session Filter")

    # Pass 0.25: Decay floor (v7.2) — drop inferred notes below confidence floor
    notes, decay_dropped = pass_0_25_decay_floor(notes, floor=DECAY_FLOOR)
    if decay_dropped > 0:
        active_layers.append(f"Decay Floor (dropped {decay_dropped})")
    else:
        active_layers.append("Decay Floor")

    # Pass 1: Tag filter — scores every note; its top-30 is ONE contributor to the pool
    tag_top, tag_scores = pass_1_tag_filter(notes, query, state.get("working_memory", {}))
    active_layers.append("Tag Filter")

    # Pass 1.5: Vector similarity over the FULL note set (v7.3).
    # Scoring must happen before narrowing — you cannot rank what was already discarded.
    vector_scores = {}
    has_embeddings = any("embedding" in n and n.get("embedding") for n in notes)
    if not has_embeddings:
        # No embeddings in index — skip entirely, keyword pipeline still works
        active_layers.append("Vector Similarity (SKIPPED - no embeddings stored)")
    else:
        try:
            query_embedding = generate_query_embedding(query)
            if query_embedding is None:
                # Encoder unavailable — fall back to the legacy lexical bootstrap
                query_embedding = generate_simple_query_embedding(query, notes)
                fallback = " (fallback: lexical)"
            else:
                fallback = ""
            if query_embedding is not None:
                vector_scores = pass_1_5_vector_similarity(notes, query, query_embedding)
                active_layers.append(f"Vector Similarity{fallback}")
            else:
                active_layers.append("Vector Similarity (SKIPPED - no query embedding)")
        except Exception as e:
            print(f"[INFO] Vector similarity unavailable: {e}")
            active_layers.append("Vector Similarity (SKIPPED - exception)")

    # Pass 1.75: BM25 ranking over the FULL note set (v7.3, was: 30 tag candidates)
    bm25_scores = {}
    if bm25_index:
        bm25_scores = pass_1_75_bm25_ranking(notes, query, bm25_index)
        active_layers.append("BM25 Ranking")

    # Pass 1.9 (v7.3): assemble the candidate pool from ALL signals, not tags alone.
    candidates = _build_candidate_pool(notes, tag_top, vector_scores, bm25_scores)
    active_layers.append(f"Candidate Pool ({len(candidates)})")

    # Pass 2-4: Domain scoring
    domain_scores = pass_2_4_domain_scoring(candidates, state)
    active_layers.append("Domain Scoring")

    # Pass 5: Final scoring
    all_scores = {
        "tag": tag_scores,
        "vector": vector_scores,
        "bm25": bm25_scores,
        "domain": domain_scores
    }
    ranked_candidates = pass_5_final_scoring(candidates, all_scores, WEIGHTS)
    active_layers.append("Final Scoring")

    # Pass 6: MMR diversification (NEW)
    diverse_notes = pass_6_mmr_diversification(ranked_candidates[:30], lambda_param=MMR_LAMBDA)
    active_layers.append("MMR Diversity")

    # Pass 6.5: Edge walk (v7.2) — annotate [SUPERSEDED]/[CONFLICT] markers
    diverse_notes, edge_stats = pass_6_5_edge_walk(diverse_notes, memory_graph)
    if edge_stats.get("superseded") or edge_stats.get("conflicts"):
        active_layers.append(
            f"Edge Walk (superseded={edge_stats['superseded']}, "
            f"conflicts={edge_stats['conflicts']})"
        )
    else:
        active_layers.append("Edge Walk")

    # Pass 7: Optional reranking (NEW)
    final_notes = pass_7_reranking(diverse_notes, query, use_reranking)
    if use_reranking:
        active_layers.append("Cross-Encoder Reranking")

    # Take top N for Tier 1
    tier_1_notes = final_notes[:tier_1_count]

    total_ms = (time.time() - start_time) * 1000

    return {
        "notes": tier_1_notes,
        "metadata": {
            "total_ms": round(total_ms, 1),
            "active_layers": active_layers,
            "candidates_evaluated": len(candidates),
            "final_count": len(tier_1_notes),
            "query": query,
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }
    }


# =============================================================================
# OUTPUT FORMATTING
# =============================================================================

def format_memory_block(notes: List[Dict], metadata: Dict) -> str:
    """Format as [MEMORY] markdown block."""
    output = ["[MEMORY]", ""]
    output.append(f"**Recalled:** {len(notes)} notes ({metadata['total_ms']}ms)")
    output.append(f"**Layers Active:** {', '.join(metadata['active_layers'])}")
    output.append(f"**Query:** {metadata['query']}")
    output.append("")

    for i, note in enumerate(notes, 1):
        # v7.2: surface edge-walk markers in the title
        marker = note.get('_marker', '')
        title = note.get('title', 'Untitled')
        output.append(f"### {i}. {marker + ' ' if marker else ''}{title}")
        output.append(f"**Path:** `{note.get('path', 'Unknown')}`")

        # v7.2: explain the marker
        if marker == '[SUPERSEDED]' and note.get('_superseded_by'):
            output.append(f"**Superseded by:** `{note['_superseded_by']}`")
        elif marker == '[CONFLICT]' and note.get('_conflict_with'):
            paths = ', '.join(f'`{p}`' for p in note['_conflict_with'])
            output.append(f"**Conflicts with:** {paths}")

        tags = note.get('tags', [])
        if tags:
            output.append(f"**Tags:** {', '.join(tags)}")

        score = note.get('final_score')
        if score is not None:
            output.append(f"**Score:** {score:.3f}")

        output.append("")

        # Include content preview (first 500 chars)
        content = note.get('content', '')
        if content:
            preview = content[:500]
            output.append(preview)
            if len(content) > 500:
                output.append("...")

        output.append("")
        output.append("---")
        output.append("")

    return "\n".join(output)


def format_json_output(notes: List[Dict], metadata: Dict) -> str:
    """Format as JSON for testing."""
    return json.dumps({
        "notes": notes,
        "metadata": metadata
    }, indent=2)


# =============================================================================
# STATE UPDATES
# =============================================================================

def update_state_after_recall(result: Dict):
    """Update state.json with recall timestamp and metrics."""
    state = load_session_state()

    if "memory_system" not in state:
        state["memory_system"] = {}

    state["memory_system"]["last_recall"] = datetime.utcnow().isoformat() + "Z"
    state["memory_system"]["recall_count"] = state["memory_system"].get("recall_count", 0) + 1
    state["memory_system"]["last_recall_ms"] = result["metadata"]["total_ms"]

    # Save state
    save_session_state(state)


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

def main():
    # Windows cp1252 stdout crashes on Unicode (e.g. → arrow). Force UTF-8.
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass  # Older Python (<3.7) or non-reconfigurable stream

    parser = argparse.ArgumentParser(
        description="Hybrid Recall v7.0 - Memory Recall Engine with BM25 + Vector + MMR"
    )
    parser.add_argument('--query', required=True, help='Search query')
    parser.add_argument(
        '--format',
        default='memory-block',
        choices=['memory-block', 'json'],
        help='Output format (default: memory-block)'
    )
    parser.add_argument(
        '--update-state',
        action='store_true',
        help='Update state.json after recall'
    )
    parser.add_argument(
        '--use-reranking',
        action='store_true',
        help='Enable optional cross-encoder reranking (adds ~150ms)'
    )
    parser.add_argument(
        '--metrics',
        action='store_true',
        help='Show performance metrics'
    )
    parser.add_argument(
        '--tier-1-count',
        type=int,
        default=12,
        help='Number of top notes to return (default: 12)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show pipeline pass trace (which passes ran, how many notes survived each) — no state update'
    )
    parser.add_argument(
        '--allow-model-load',
        action='store_true',
        help='Permit the ~30s sentence-transformers load for uncached queries. OFF by '
             'default so the gate fallback path stays fast; use for eval/cache warming.'
    )

    args = parser.parse_args()

    global ALLOW_MODEL_LOAD
    if args.allow_model_load:
        ALLOW_MODEL_LOAD = True

    # Execute recall
    result = hybrid_recall(
        args.query,
        use_reranking=args.use_reranking,
        tier_1_count=args.tier_1_count
    )

    # --dry-run: print pass trace, then output JSON (machine-parseable), skip state update
    if args.dry_run:
        print("[DRY-RUN] Hybrid Recall Pipeline Trace")
        print(f"Query: {args.query!r}")
        print(f"MEMORY_INDEX: {MEMORY_INDEX}")
        print(f"Index exists: {MEMORY_INDEX.exists()}")
        print("")
        for i, layer in enumerate(result["metadata"]["active_layers"], 1):
            print(f"  Pass {i}: {layer}")
        print("")
        print(f"Candidates evaluated: {result['metadata']['candidates_evaluated']}")
        print(f"Final results: {result['metadata']['final_count']}")
        print(f"Total time: {result['metadata']['total_ms']}ms")
        print("")
        print(format_json_output(result["notes"], result["metadata"]))
        return

    # Format output
    if args.format == 'memory-block':
        print(format_memory_block(result["notes"], result["metadata"]))
    elif args.format == 'json':
        print(format_json_output(result["notes"], result["metadata"]))

    # Update state if requested
    if args.update_state:
        update_state_after_recall(result)
        print(f"\n[STATE] Updated state.json with recall timestamp")

    # Show metrics if requested
    if args.metrics:
        print(f"\n[METRICS] Total: {result['metadata']['total_ms']}ms")
        print(f"[METRICS] Layers: {', '.join(result['metadata']['active_layers'])}")
        print(f"[METRICS] Candidates: {result['metadata']['candidates_evaluated']}")
        print(f"[METRICS] Final: {result['metadata']['final_count']}")


if __name__ == "__main__":
    main()
