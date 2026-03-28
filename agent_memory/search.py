"""Search against SQLite structured fields.

Search strategy (tried in order):
  1. Hybrid BM25 + Vector (if ollama available)
  2. BM25-only via FTS5 (if ollama unavailable)
  3. Phase 0 keyword fallback (if FTS5 unavailable)
"""
import json
import re
import sqlite3
from .models import SearchResult
from .extract import build_l1, build_l2
from .vector import embed, knn_search


def _tokenize(query: str) -> list[str]:
    """Extract searchable tokens from query (handles mixed Chinese/English)."""
    tokens = re.findall(r'[a-zA-Z0-9_\-\.]+', query.lower())
    tokens += re.findall(r'[\u4e00-\u9fff]+', query)
    return [t for t in tokens if len(t) > 1]


def _score_doc(doc: sqlite3.Row, tokens: list[str]) -> float:
    """Score a document row against query tokens."""
    if not tokens:
        return 0.0

    haystack = {
        "title":     (doc["title"] or "").lower(),
        "summary":   (doc["summary"] or "").lower(),
        "key_facts": (doc["key_facts"] or "").lower(),
        "decisions": (doc["decisions"] or "").lower(),
    }
    weights = {"title": 3.0, "summary": 2.0, "key_facts": 1.5, "decisions": 1.0}

    score = 0.0
    matched = 0
    for tok in tokens:
        for field, text in haystack.items():
            if tok in text:
                score += len(tok) * weights[field]
                matched += 1
                break  # count token once

    coverage = matched / len(tokens)
    score *= (1.0 + coverage)

    title_words = re.findall(r'[a-z]+', haystack["title"])
    if title_words:
        token_set = set(tokens)
        title_hit = sum(1 for w in title_words if w in token_set)
        if title_hit / len(title_words) > 0.6:
            score *= 2.5

    return score


_PRIORITY_WEIGHT = {"P0": 2.0, "P1": 1.0, "P2": 0.75}


def _clean_fts_tokens(query: str) -> list[str]:
    """Shared token extraction: strip FTS5 special chars, filter <3-char tokens."""
    clean = re.sub(r'[\"()*^]', ' ', query)
    clean = re.sub(r'\b(AND|OR|NOT)\b', ' ', clean, flags=re.IGNORECASE)
    tokens = re.findall(r'\S+', clean)
    return [t for t in tokens if len(t) >= 3]


def _to_fts5_query_and(query: str) -> str:
    """Convert query to FTS5 AND expression (high precision, may return 0 results)."""
    tokens = _clean_fts_tokens(query)
    if not tokens:
        return query
    return " AND ".join(tokens)


def _to_fts5_query_or(query: str) -> str:
    """Convert query to FTS5 OR expression (high recall, lower precision)."""
    tokens = _clean_fts_tokens(query)
    if not tokens:
        return query
    return " OR ".join(tokens)


def _run_fts5_query(conn: sqlite3.Connection, fts_query: str,
                    max_results: int, project: str = None) -> list[sqlite3.Row]:
    """Execute a single FTS5 MATCH query and return raw rows."""
    if project:
        return conn.execute(
            """SELECT d.doc_id, d.title, d.summary, d.key_facts, d.decisions,
                      d.code_sigs, d.metrics, d.raw_content, d.priority, d.source,
                      fts.rank
               FROM documents_fts fts
               JOIN documents d ON d.doc_id = fts.rowid
               WHERE documents_fts MATCH ?
                 AND (d.expires_at IS NULL OR d.expires_at > strftime('%s','now'))
                 AND (d.project = ? OR d.project IS NULL)
               ORDER BY fts.rank
               LIMIT ?""",
            (fts_query, project, max_results)
        ).fetchall()
    return conn.execute(
        """SELECT d.doc_id, d.title, d.summary, d.key_facts, d.decisions,
                  d.code_sigs, d.metrics, d.raw_content, d.priority, d.source,
                  fts.rank
           FROM documents_fts fts
           JOIN documents d ON d.doc_id = fts.rowid
           WHERE documents_fts MATCH ?
             AND (d.expires_at IS NULL OR d.expires_at > strftime('%s','now'))
           ORDER BY fts.rank
           LIMIT ?""",
        (fts_query, max_results)
    ).fetchall()


def _rows_to_results(rows: list[sqlite3.Row]) -> list[SearchResult]:
    """Convert raw DB rows to SearchResult objects with priority-weighted scores."""
    results = []
    for row in rows:
        fields = {
            "title":     row["title"] or "",
            "summary":   row["summary"] or "",
            "sections":  [],
            "key_facts": json.loads(row["key_facts"] or "[]"),
            "decisions": json.loads(row["decisions"] or "[]"),
            "code_sigs": json.loads(row["code_sigs"] or "[]"),
            "metrics":   json.loads(row["metrics"] or "[]"),
        }
        priority = row["priority"] or "P1"
        base_score = -row["rank"]   # negate: FTS5 rank is negative, we want positive
        weighted_score = base_score * _PRIORITY_WEIGHT.get(priority, 1.0)
        results.append(SearchResult(
            id=row["doc_id"],
            type="document",
            l1=build_l1(fields),
            l2=build_l2(fields),
            raw=row["raw_content"] or "",
            score=weighted_score,
            priority=priority,
            source=row["source"] or "explicit",
        ))
    return results


def search_documents_bm25(conn: sqlite3.Connection, query: str,
                           max_results: int = 5, project: str = None) -> list[SearchResult]:
    """BM25 search via SQLite FTS5 with AND→OR fallback.

    Tries AND first (high precision). If empty, retries with OR (high recall).
    Priority weight applied after BM25 scoring so P0 docs rank above equal-score P2 docs.
    """
    # Try AND first for precision
    and_query = _to_fts5_query_and(query)
    rows = _run_fts5_query(conn, and_query, max_results, project=project)
    if rows:
        return _rows_to_results(rows)

    # AND returned nothing — fall back to OR for recall
    or_query = _to_fts5_query_or(query)
    if or_query == and_query:
        return []  # single-token query, no point retrying
    rows = _run_fts5_query(conn, or_query, max_results, project=project)
    return _rows_to_results(rows)


def search_documents(conn: sqlite3.Connection, query: str,
                     max_results: int = 5,
                     min_score: float = 0.0,
                     project: str = None) -> list[SearchResult]:
    """Search documents. Tries hybrid BM25+Vector, falls back to BM25, then Phase 0.

    min_score: filter out results below this score (0.0 = no filter).
    Hybrid RRF scores range ~0.008–0.033; Phase0 scores vary by token match.
    project: when set, only return docs matching this project or global (NULL) docs.
    """
    # Attempt hybrid (requires ollama to be running)
    try:
        results = search_hybrid(conn, query, max_results, project=project)
        if results:
            if min_score > 0:
                results = [r for r in results if r.score >= min_score]
            return results
    except Exception:
        pass

    # BM25-only fallback
    try:
        results = search_documents_bm25(conn, query, max_results, project=project)
        if results:
            if min_score > 0:
                results = [r for r in results if r.score >= min_score]
            return results
    except Exception:
        pass

    # Phase 0 fallback: weighted keyword scoring
    results = _search_phase0(conn, query, max_results, project=project)
    if min_score > 0:
        results = [r for r in results if r.score >= min_score]
    return results


# ── Vector search ─────────────────────────────────────────────────────────────

def search_vector(conn: sqlite3.Connection, query: str,
                  max_results: int = 20,
                  min_cosine: float = 0.5) -> list[tuple[int, float]]:
    """Vector search via HNSW (sqlite-vec) with cosine re-scoring.

    Falls back to brute-force if sqlite-vec is unavailable.
    Returns [] if ollama is unavailable or no doc exceeds min_cosine.
    """
    query_vecs = embed([query])
    if not query_vecs:
        return []
    query_vec = query_vecs[0]
    return knn_search(conn, query_vec, k=max_results, min_cosine=min_cosine)


def search_hybrid(conn: sqlite3.Connection, query: str,
                  max_results: int = 5, project: str = None) -> list[SearchResult]:
    """BM25 + Vector → Reciprocal Rank Fusion (k=60).

    RRF rewards documents that rank well in *both* systems.
    Falls back to empty list if ollama is unavailable (caller then uses BM25).
    """
    # Vector ranks — if ollama is down this returns [], triggering fallback
    # min_cosine=0.5: qwen3-embedding:8b scores gibberish at ~0.41, real queries at 0.6+
    vec_ranks_list = search_vector(conn, query, max_results=20, min_cosine=0.5)
    if not vec_ranks_list:
        return []
    vec_ranks = {doc_id: i for i, (doc_id, _) in enumerate(vec_ranks_list)}

    # BM25 ranks
    bm25_rows = []
    try:
        and_q = _to_fts5_query_and(query)
        bm25_rows = _run_fts5_query(conn, and_q, max_results=20, project=project)
        if not bm25_rows:
            or_q = _to_fts5_query_or(query)
            bm25_rows = _run_fts5_query(conn, or_q, max_results=20, project=project)
    except Exception:
        pass
    bm25_ranks = {row["doc_id"]: i for i, row in enumerate(bm25_rows)}

    # RRF merge over union of candidates
    k = 60
    all_ids = set(vec_ranks) | set(bm25_ranks)
    rrf_scores: dict[int, float] = {}
    for doc_id in all_ids:
        score = 0.0
        if doc_id in bm25_ranks:
            score += 1.0 / (k + bm25_ranks[doc_id])
        if doc_id in vec_ranks:
            score += 1.0 / (k + vec_ranks[doc_id])
        rrf_scores[doc_id] = score

    # Filter: require doc to appear meaningfully in at least one ranking list.
    # Single-list rank-0 score = 1/60 ≈ 0.0167. Threshold 0.010 removes truly random hits.
    MIN_RRF = 0.010
    rrf_scores = {k: v for k, v in rrf_scores.items() if v >= MIN_RRF}
    if not rrf_scores:
        return []

    top_ids = sorted(rrf_scores, key=lambda x: rrf_scores[x], reverse=True)[:max_results]

    # Fetch full doc rows for the winners — exclude expired docs
    placeholders = ",".join("?" * len(top_ids))
    if project:
        rows = conn.execute(
            f"""SELECT d.doc_id, d.title, d.summary, d.key_facts, d.decisions,
                       d.code_sigs, d.metrics, d.raw_content, d.priority, d.source
                FROM documents d
                WHERE d.doc_id IN ({placeholders})
                  AND (d.expires_at IS NULL OR d.expires_at > strftime('%s','now'))
                  AND (d.project = ? OR d.project IS NULL)""",
            top_ids + [project],
        ).fetchall()
    else:
        rows = conn.execute(
            f"""SELECT d.doc_id, d.title, d.summary, d.key_facts, d.decisions,
                       d.code_sigs, d.metrics, d.raw_content, d.priority, d.source
                FROM documents d
                WHERE d.doc_id IN ({placeholders})
                  AND (d.expires_at IS NULL OR d.expires_at > strftime('%s','now'))""",
            top_ids,
        ).fetchall()
    row_map = {row["doc_id"]: row for row in rows}

    results = []
    for doc_id in top_ids:
        row = row_map.get(doc_id)
        if not row:
            continue
        fields = {
            "title":     row["title"] or "",
            "summary":   row["summary"] or "",
            "sections":  [],
            "key_facts": json.loads(row["key_facts"] or "[]"),
            "decisions": json.loads(row["decisions"] or "[]"),
            "code_sigs": json.loads(row["code_sigs"] or "[]"),
            "metrics":   json.loads(row["metrics"] or "[]"),
        }
        priority = row["priority"] or "P1"
        weighted_score = rrf_scores[doc_id] * _PRIORITY_WEIGHT.get(priority, 1.0)
        results.append(SearchResult(
            id=doc_id,
            type="document",
            l1=build_l1(fields),
            l2=build_l2(fields),
            raw=row["raw_content"] or "",
            score=weighted_score,
            priority=priority,
            source=row["source"] or "explicit",
        ))
    return results


def _search_phase0(conn: sqlite3.Connection, query: str,
                   max_results: int = 5, project: str = None) -> list[SearchResult]:
    """Phase 0 fallback: weighted keyword scoring against structured fields."""
    tokens = _tokenize(query)
    if not tokens:
        return []

    if project:
        rows = conn.execute(
            """SELECT doc_id, title, summary, key_facts, decisions,
                      code_sigs, metrics, raw_content, priority, source
               FROM documents
               WHERE (expires_at IS NULL OR expires_at > strftime('%s','now'))
                 AND (project = ? OR project IS NULL)
               ORDER BY created_at DESC
               LIMIT 200""",
            (project,)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT doc_id, title, summary, key_facts, decisions,
                      code_sigs, metrics, raw_content, priority, source
               FROM documents
               WHERE expires_at IS NULL OR expires_at > strftime('%s','now')
               ORDER BY created_at DESC
               LIMIT 200"""
        ).fetchall()

    # MIN_PHASE0: a 3-char token matching summary (weight 2.0) → score 6.0.
    # Requires at least one meaningful token match to avoid noise.
    MIN_PHASE0 = 4.0
    scored = []
    for row in rows:
        score = _score_doc(row, tokens)
        if score >= MIN_PHASE0:
            scored.append((score, row))

    scored.sort(key=lambda x: x[0], reverse=True)
    scored = scored[:max_results]

    results = []
    for score, row in scored:
        fields = {
            "title":     row["title"] or "",
            "summary":   row["summary"] or "",
            "sections":  [],
            "key_facts": json.loads(row["key_facts"] or "[]"),
            "decisions": json.loads(row["decisions"] or "[]"),
            "code_sigs": json.loads(row["code_sigs"] or "[]"),
            "metrics":   json.loads(row["metrics"] or "[]"),
        }
        results.append(SearchResult(
            id=row["doc_id"],
            type="document",
            l1=build_l1(fields),
            l2=build_l2(fields),
            raw=row["raw_content"] or "",
            score=score,
            priority=row["priority"] or "P1",
            source=row["source"] or "explicit",
        ))
    return results
