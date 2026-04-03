"""MemoryStore: main API surface for am-memory SDK."""
import json
import logging
import sqlite3
import threading
import time

from .db import init_db, DB_PATH
from .extract import extract_fields
from .llm_extract import llm_extract
from .models import SearchResult
from .search import search_documents
from .state import StateManager
from .session import SessionManager
from .vector import embed_doc, vec_to_blob, upsert_vec
from .write_queue import WriteQueue

logger = logging.getLogger(__name__)

# Source-aware TTL (days). Takes precedence over priority-based TTL.
_SOURCE_TTL_DAYS: dict[str, int | None] = {
    # Claude-classified sources (via am_save source enum)
    "architectural_decision": None,   # never expires — core design choices
    "debug_solution": 90,             # non-obvious fixes worth remembering
    "technical_insight": 90,          # analysis, trade-offs, lessons learned
    "session_note": 30,               # ephemeral session context
    "routine": 30,                    # standard ops, low signal
    # Internal/pipeline sources
    "hook": 14,                       # file snapshots (code changes daily)
    "session_extract": 60,            # distilled conversation knowledge
}

# Priority-based TTL fallback (for source='explicit' and unknown sources)
_PRIORITY_TTL_DAYS: dict[str, int | None] = {"P0": None, "P1": 90, "P2": 30}


# Source → effective priority for search ranking weight.
# architectural_decision gets P0 boost; high-value sources get P1; ephemeral get P2.
_SOURCE_PRIORITY: dict[str, str] = {
    "architectural_decision": "P0",
    "debug_solution":         "P1",
    "technical_insight":      "P1",
    "session_extract":        "P1",
    "hook":                   "P2",
    "session_note":           "P2",
    "routine":                "P2",
}


def _get_ttl_days(source: str, priority: str) -> int | None:
    """Return TTL in days. Source wins over priority; None = never expires."""
    if source in _SOURCE_TTL_DAYS:
        return _SOURCE_TTL_DAYS[source]
    return _PRIORITY_TTL_DAYS.get(priority)


def _effective_priority(source: str, priority: str) -> str:
    """Return the priority used for search ranking. Source wins over caller-supplied priority."""
    return _SOURCE_PRIORITY.get(source, priority)


def _normalize_fact(fact: str) -> str:
    """Normalize a fact string for comparison."""
    return fact.strip().lower()


def _facts_similar(a: str, b: str) -> bool:
    """Check if two facts are semantically similar using SequenceMatcher."""
    from difflib import SequenceMatcher
    return SequenceMatcher(None, _normalize_fact(a), _normalize_fact(b)).ratio() > 0.85


def _key_facts_jaccard(new_facts: list[str], existing_facts: list[str]) -> float:
    """Compute Jaccard overlap between two key_facts lists.

    Uses fuzzy matching (>0.85 SequenceMatcher ratio) for fact comparison.
    Returns 0.0-1.0. Returns 1.0 if both lists are empty.
    """
    if not new_facts and not existing_facts:
        return 1.0
    if not new_facts or not existing_facts:
        return 0.0

    new_normalized = [_normalize_fact(f) for f in new_facts]
    existing_normalized = [_normalize_fact(f) for f in existing_facts]

    # Count matches (fuzzy)
    matched = 0
    used = set()
    for nf in new_normalized:
        for j, ef in enumerate(existing_normalized):
            if j not in used and _facts_similar(nf, ef):
                matched += 1
                used.add(j)
                break

    union_size = len(new_normalized) + len(existing_normalized) - matched
    if union_size == 0:
        return 1.0
    return matched / union_size


def _merge_facts_fuzzy(old_facts: list, new_facts: list) -> list:
    """Merge two fact lists, deduplicating fuzzy-similar facts.

    Keeps all unique facts from both lists. When a new fact is similar
    to an existing one (>0.85 ratio), keeps the newer version.
    """
    merged = list(new_facts)  # new facts take priority
    for old_f in old_facts:
        is_dup = False
        for new_f in merged:
            if _facts_similar(str(old_f), str(new_f)):
                is_dup = True
                break
        if not is_dup:
            merged.append(old_f)
    return merged


def _merge_json_arrays(old_json: str, new_json: str) -> str:
    """Union-dedup two JSON arrays, preserving order (new items first)."""
    old = json.loads(old_json or "[]")
    new = json.loads(new_json or "[]")
    seen = set()
    merged = []
    for item in new + old:
        key = item.strip() if isinstance(item, str) else str(item)
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return json.dumps(merged)


class MemoryStore:
    def __init__(self, db_path: str = None):
        self._db_path = db_path or str(DB_PATH)
        # Initialize schema via init_db (creates tables, runs migrations)
        init_conn = init_db(self._db_path)
        init_conn.close()  # Close the init connection

        # WriteQueue owns the single write connection
        self._wq = WriteQueue(self._db_path)
        # Read connection for queries (separate from write conn, WAL allows concurrent reads)
        self._conn = self._wq.read_conn()
        # Lock for serializing reads on the shared read connection.
        # SQLite connections are not thread-safe for concurrent cursor operations.
        self._read_lock = threading.Lock()
        self.state = StateManager(self._conn, self._wq, self._read_lock)
        self.session = SessionManager(self._conn, self._wq, self._read_lock)
        self._pruned_this_session = False

    def _track_session_doc(self, doc_id: int) -> None:
        """Track a doc_id for the current session (used for auto-relations on session end)."""
        current_sid = self.state.get("current_session_id")
        if current_sid:
            existing_docs = self.state.get(f"session_docs:{current_sid}") or []
            if doc_id not in existing_docs:
                existing_docs.append(doc_id)
                self.state.set(f"session_docs:{current_sid}", existing_docs)

    def _find_duplicate(self, title: str, source: str, new_key_facts: list[str],
                        project: str = None,
                        conn: sqlite3.Connection = None) -> int | None:
        """Check if a document with similar title and same source exists.

        Two-stage detection:
        1. FTS5 MATCH on title for candidate retrieval (fast)
        2. key_facts Jaccard overlap >= 0.5 for precision check

        Returns doc_id if duplicate confirmed, else None.
        """
        c = conn or self._conn
        if not title or len(title) < 3:
            return None
        try:
            if project:
                rows = c.execute(
                    """SELECT d.doc_id, d.title, d.source, d.key_facts, fts.rank
                       FROM documents_fts fts
                       JOIN documents d ON d.doc_id = fts.rowid
                       WHERE documents_fts MATCH ?
                         AND d.source = ?
                         AND (d.project = ? OR d.project IS NULL)
                       ORDER BY fts.rank
                       LIMIT 3""",
                    (title, source, project),
                ).fetchall()
            else:
                rows = c.execute(
                    """SELECT d.doc_id, d.title, d.source, d.key_facts, fts.rank
                       FROM documents_fts fts
                       JOIN documents d ON d.doc_id = fts.rowid
                       WHERE documents_fts MATCH ?
                         AND d.source = ?
                       ORDER BY fts.rank
                       LIMIT 3""",
                    (title, source),
                ).fetchall()
            if not rows:
                return None
            # Stage 1: BM25 rank threshold
            candidates = [r for r in rows if r["rank"] > -5.0]
            if not candidates:
                return None
            # Stage 2: key_facts Jaccard overlap for precision
            best = candidates[0]
            existing_facts = json.loads(best["key_facts"] or "[]")
            overlap = _key_facts_jaccard(new_key_facts, existing_facts)

            # High overlap (>= 0.3): confirmed duplicate with fact similarity
            # Both empty facts: fall back to title match only
            # Any shared facts at all with good BM25: likely same topic
            if overlap >= 0.3:
                logger.info("Dedup: '%s' matches doc %d (Jaccard=%.2f)",
                            title[:40], best["doc_id"], overlap)
                return best["doc_id"]
            # Both have no facts — title match alone is sufficient
            if not new_key_facts and not existing_facts:
                return best["doc_id"]
            # One or both have facts but low overlap — not a duplicate
            return None
        except Exception:
            pass
        return None

    def save(
        self,
        title: str,
        content: str,
        priority: str = "P1",
        source: str = "explicit",
        raw_content: str = None,
        file_path: str = None,
        project: str = None,
        force_new: bool = False,
    ) -> int:
        """Extract fields from content and persist to documents table.
        When file_path is provided, upserts by file_path (prevents duplicate docs
        for the same file across multiple edits).
        When force_new=True, always creates a new document (skip dedup).
        Returns doc_id.
        """
        fields = extract_fields(content, title_hint=title)
        actual_title = title or fields.get("title", "Untitled")

        effective_prio = _effective_priority(source, priority)
        ttl_days = _get_ttl_days(source, priority)
        expires_at = time.time() + ttl_days * 86400 if ttl_days else None

        # architectural_decision is always global (project=NULL)
        effective_project = None if source == "architectural_decision" else project

        # Dedup: check for existing doc with same title + source + project + key_facts overlap
        # file_path upsert takes precedence (skip dedup if file_path provided)
        # force_new bypasses dedup entirely
        # All reads from self._conn are protected by _read_lock for thread safety.
        dup_id = None
        existing = None
        if not file_path and not force_new:
            with self._read_lock:
                dup_id = self._find_duplicate(
                    actual_title, source, fields.get("key_facts", []), effective_project
                )
                if dup_id is not None:
                    existing = self._conn.execute(
                        "SELECT key_facts, decisions, code_sigs, metrics FROM documents WHERE doc_id=?",
                        (dup_id,),
                    ).fetchone()
            if dup_id is not None and existing:
                # Fuzzy merge for key_facts (dedup near-identical facts)
                old_kf = json.loads(existing["key_facts"] or "[]")
                merged_kf = json.dumps(_merge_facts_fuzzy(old_kf, fields["key_facts"]))
                # Exact dedup for decisions, code_sigs, metrics
                merged_dec = _merge_json_arrays(existing["decisions"], json.dumps(fields["decisions"]))
                merged_cs = _merge_json_arrays(existing["code_sigs"], json.dumps(fields["code_sigs"]))
                merged_met = _merge_json_arrays(existing["metrics"], json.dumps(fields["metrics"]))
                logger.info("Merged doc '%s' into doc_id=%d, %d facts",
                            actual_title[:40], dup_id, len(json.loads(merged_kf)))
                self._wq.execute(
                    """UPDATE documents SET title=?, summary=?, key_facts=?, decisions=?,
                       code_sigs=?, metrics=?, raw_content=?, priority=?, source=?,
                       generator='rule', expires_at=?, project=?
                       WHERE doc_id=?""",
                    (
                        actual_title,
                        fields["summary"],
                        merged_kf,
                        merged_dec,
                        merged_cs,
                        merged_met,
                        raw_content or content,
                        effective_prio,
                        source,
                        expires_at,
                        effective_project,
                        dup_id,
                    ),
                )
                self._embed_async(dup_id, actual_title, fields, raw_content or content)
                self._track_session_doc(dup_id)
                return dup_id

        if file_path:
            # Upsert: update existing doc for this file_path, or insert new
            with self._read_lock:
                existing = self._conn.execute(
                    "SELECT doc_id FROM documents WHERE file_path=?", (file_path,)
                ).fetchone()
            if existing:
                self._wq.execute(
                    """UPDATE documents SET title=?, summary=?, key_facts=?, decisions=?,
                       code_sigs=?, metrics=?, raw_content=?, priority=?, source=?,
                       generator='rule', expires_at=?, project=?
                       WHERE file_path=?""",
                    (
                        actual_title,
                        fields["summary"],
                        json.dumps(fields["key_facts"]),
                        json.dumps(fields["decisions"]),
                        json.dumps(fields["code_sigs"]),
                        json.dumps(fields["metrics"]),
                        raw_content or content,
                        effective_prio,
                        source,
                        expires_at,
                        effective_project,
                        file_path,
                    ),
                )
                self._embed_async(existing["doc_id"], actual_title, fields, raw_content or content)
                self._track_session_doc(existing["doc_id"])
                return existing["doc_id"]

        doc_id = self._wq.execute(
            """INSERT INTO documents
               (title, summary, key_facts, decisions, code_sigs, metrics,
                raw_content, priority, source, generator, file_path, expires_at, project)
               VALUES (?,?,?,?,?,?,?,?,?,'rule',?,?,?)""",
            (
                actual_title,
                fields["summary"],
                json.dumps(fields["key_facts"]),
                json.dumps(fields["decisions"]),
                json.dumps(fields["code_sigs"]),
                json.dumps(fields["metrics"]),
                raw_content or content,
                effective_prio,
                source,
                file_path,
                expires_at,
                effective_project,
            ),
        )
        self._embed_async(doc_id, actual_title, fields, raw_content or content)
        self._track_session_doc(doc_id)
        return doc_id

    def _embed_async(self, doc_id: int, title: str, rule_fields: dict, content: str = "") -> None:
        """Background thread: LLM extraction → UPDATE fields → embedding → UPDATE embedding.

        Uses WriteQueue for all DB writes (thread-safe).
        daemon=False so the thread completes even if the main process exits.
        """
        wq = self._wq

        def _run():
            use_fields = rule_fields
            try:
                # Step 1: LLM extraction (if content provided)
                if content:
                    llm_fields = llm_extract(content, title_hint=title)
                    if llm_fields:
                        # Preserve rule-extracted code_sigs/metrics
                        llm_fields["code_sigs"] = rule_fields.get("code_sigs", [])
                        llm_fields["metrics"]   = rule_fields.get("metrics", [])
                        # Update DB with LLM-quality fields via WriteQueue
                        wq.execute(
                            """UPDATE documents
                               SET title=?, summary=?, key_facts=?, decisions=?,
                                   generator='llm'
                               WHERE doc_id=?""",
                            (
                                llm_fields["title"] or title,
                                llm_fields["summary"],
                                json.dumps(llm_fields["key_facts"]),
                                json.dumps(llm_fields["decisions"]),
                                doc_id,
                            ),
                        )
                        use_fields = llm_fields

                # Step 2: Compute embedding from best available fields
                vec = embed_doc(
                    use_fields.get("title") or title,
                    use_fields.get("summary") or "",
                    use_fields.get("key_facts") or [],
                )
                if vec is not None:
                    wq.execute(
                        "UPDATE documents SET embedding=? WHERE doc_id=?",
                        (vec_to_blob(vec), doc_id),
                    )
                    # upsert_vec needs a connection — use a transaction for atomicity
                    with wq.transaction() as conn:
                        upsert_vec(conn, doc_id, vec)

            except Exception:
                logger.debug("_embed_async failed for doc_id=%d", doc_id, exc_info=True)

        threading.Thread(target=_run, daemon=False).start()

    def search(self, query: str, max_results: int = 5, project: str = None) -> list[SearchResult]:
        """Search documents. Updates last_accessed_at on hits to extend TTL.
        Prunes expired documents once per session (background, non-blocking).
        """
        if not self._pruned_this_session:
            self._pruned_this_session = True
            self._prune_expired_async()
        with self._read_lock:
            results = search_documents(self._conn, query, max_results=max_results, project=project)
        if results:
            self._touch_accessed_async([r.id for r in results])
        return results

    def delete_documents(self, doc_ids: list[int]) -> int:
        """Delete documents and cascade to vec_documents + doc_relations.

        Central delete method — all document deletion should go through here
        to ensure vec_documents and doc_relations are cleaned up.
        Returns count deleted.
        """
        if not doc_ids:
            return 0
        placeholders = ",".join("?" * len(doc_ids))
        with self._wq.transaction() as conn:
            conn.execute(
                f"DELETE FROM documents WHERE doc_id IN ({placeholders})", doc_ids
            )
            try:
                conn.execute(
                    f"DELETE FROM vec_documents WHERE document_id IN ({placeholders})",
                    doc_ids,
                )
            except Exception:
                pass  # vec_documents may not exist (sqlite-vec not loaded)
            try:
                conn.execute(
                    f"DELETE FROM doc_relations WHERE doc_id_a IN ({placeholders}) OR doc_id_b IN ({placeholders})",
                    doc_ids + doc_ids,
                )
            except Exception:
                pass  # doc_relations may not exist
        return len(doc_ids)

    def cleanup_orphaned_vectors(self) -> int:
        """Remove vec_documents rows that have no matching documents row.

        One-time migration cleanup for historical orphans.
        Returns count of orphans removed.
        """
        try:
            with self._read_lock:
                orphan_ids = [
                    row[0] for row in self._conn.execute(
                        """SELECT v.document_id FROM vec_documents v
                           LEFT JOIN documents d ON d.doc_id = v.document_id
                           WHERE d.doc_id IS NULL"""
                    ).fetchall()
                ]
            if not orphan_ids:
                return 0
            placeholders = ",".join("?" * len(orphan_ids))
            self._wq.execute(
                f"DELETE FROM vec_documents WHERE document_id IN ({placeholders})",
                orphan_ids,
            )
            return len(orphan_ids)
        except Exception:
            return 0  # vec_documents may not exist

    def prune_expired(self) -> int:
        """Delete documents whose expires_at has passed. Returns count deleted.

        P0 documents (expires_at IS NULL) are never deleted.
        Cascades to vec_documents and doc_relations via delete_documents().
        Called automatically on each search() call via _prune_expired_async().
        Can also be called manually: am doc prune.
        """
        with self._read_lock:
            expired_ids = [
                row[0] for row in self._conn.execute(
                    "SELECT doc_id FROM documents WHERE expires_at IS NOT NULL AND expires_at < ?",
                    (time.time(),),
                ).fetchall()
            ]
        return self.delete_documents(expired_ids)

    def _prune_expired_async(self) -> None:
        """Fire-and-forget background prune. Called from search() once per session.

        Uses WriteQueue for thread-safe writes.
        """
        wq = self._wq
        db_path = self._db_path

        def _run():
            try:
                # Use a separate read connection for the SELECT
                read_conn = sqlite3.connect(db_path, check_same_thread=False)
                read_conn.row_factory = sqlite3.Row
                expired_ids = [
                    row[0] for row in read_conn.execute(
                        "SELECT doc_id FROM documents "
                        "WHERE expires_at IS NOT NULL AND expires_at < ?",
                        (time.time(),),
                    ).fetchall()
                ]
                read_conn.close()
                if expired_ids:
                    placeholders = ",".join("?" * len(expired_ids))
                    with wq.transaction() as conn:
                        conn.execute(
                            f"DELETE FROM documents WHERE doc_id IN ({placeholders})",
                            expired_ids,
                        )
                        try:
                            conn.execute(
                                f"DELETE FROM vec_documents WHERE document_id IN ({placeholders})",
                                expired_ids,
                            )
                        except Exception:
                            pass  # vec_documents may not exist
                        try:
                            conn.execute(
                                f"DELETE FROM doc_relations WHERE doc_id_a IN ({placeholders}) OR doc_id_b IN ({placeholders})",
                                expired_ids + expired_ids,
                            )
                        except Exception:
                            pass
            except Exception:
                logger.debug("_prune_expired_async failed", exc_info=True)

        threading.Thread(target=_run, daemon=True).start()

    def _touch_accessed_async(self, doc_ids: list[int]) -> None:
        """Background: reset expires_at from now for each accessed doc (LRU TTL extension).

        Uses WriteQueue for thread-safe writes.
        """
        wq = self._wq
        db_path = self._db_path

        def _run():
            try:
                # Read source/priority from a separate read connection
                read_conn = sqlite3.connect(db_path, check_same_thread=False)
                read_conn.row_factory = sqlite3.Row
                now = time.time()
                updates = []
                for doc_id in doc_ids:
                    row = read_conn.execute(
                        "SELECT source, priority FROM documents WHERE doc_id=?",
                        (doc_id,),
                    ).fetchone()
                    if not row:
                        continue
                    ttl = _get_ttl_days(row[0], row[1])
                    if ttl:
                        updates.append((now, now + ttl * 86400, doc_id))
                read_conn.close()
                if updates:
                    wq.executemany(
                        "UPDATE documents SET last_accessed_at=?, expires_at=? WHERE doc_id=?",
                        updates,
                    )
            except Exception:
                logger.debug("_touch_accessed_async failed", exc_info=True)

        threading.Thread(target=_run, daemon=True).start()

    def _get_related_docs(self, doc_id: int, exclude_ids: set) -> list[dict]:
        """Fetch related documents for inject expansion."""
        try:
            with self._read_lock:
                rows = self._conn.execute(
                    """SELECT d.doc_id, d.title, d.summary, d.key_facts, r.relation_type
                       FROM doc_relations r
                       JOIN documents d ON d.doc_id = CASE
                           WHEN r.doc_id_a = ? THEN r.doc_id_b
                           ELSE r.doc_id_a END
                       WHERE (r.doc_id_a = ? OR r.doc_id_b = ?)
                         AND (d.expires_at IS NULL OR d.expires_at > strftime('%s','now'))
                       ORDER BY d.priority, d.created_at DESC
                       LIMIT 3""",
                    (doc_id, doc_id, doc_id),
                ).fetchall()
        except Exception:
            return []
        results = []
        for row in rows:
            if row["doc_id"] not in exclude_ids:
                results.append({
                    "doc_id": row["doc_id"],
                    "title": row["title"] or "",
                    "summary": row["summary"] or "",
                    "relation_type": row["relation_type"],
                })
        return results

    def inject(self, results: list, max_tokens: int = 3000,
               min_score: float = 0.0) -> str:
        """Format SearchResults into system-reminder injection block.
        Top result at l2 tier, remainder at l1, all within max_tokens budget.
        Results below min_score are dropped to avoid injecting noise.
        BM25 scores are ~1e-6 to 1e-1; Phase0 scores are ~1 to 50.
        Use min_score=1.0 to filter weak Phase0-only matches.
        """
        if not results:
            return ""

        # Filter low-relevance results (only meaningful for Phase0 scores)
        results = [r for r in results if r.score >= min_score]
        if not results:
            return ""

        token_budget = max_tokens
        blocks = []

        result_ids = {r.id for r in results}

        for i, r in enumerate(results):
            tier = "l2" if i == 0 else "l1"
            content = r.l2 if tier == "l2" else r.l1
            estimated_tokens = len(content) // 4

            if estimated_tokens > token_budget:
                content = r.l1
                estimated_tokens = len(content) // 4
                if estimated_tokens > token_budget:
                    break

            blocks.append(r.to_inject_block(tier=tier))
            token_budget -= estimated_tokens

            # Relation expansion
            try:
                related = self._get_related_docs(r.id, result_ids)
                if not related:
                    continue
                if token_budget > 300:
                    for rel in related[:2]:
                        rel_l1 = f"### {rel['title']}\n{rel['summary']}"
                        rel_tokens = len(rel_l1) // 4
                        if rel_tokens > token_budget:
                            break
                        blocks.append(f"  [{rel['relation_type']}]\n{rel_l1}")
                        token_budget -= rel_tokens
                        result_ids.add(rel["doc_id"])
                else:
                    hints = ", ".join(f"#{rel['doc_id']} '{rel['title'][:30]}'" for rel in related[:3])
                    blocks.append(f"Related: {hints}")
                    token_budget -= 8
            except Exception:
                pass

        if not blocks:
            return ""

        return "# [am-memory] Relevant context\n" + "\n\n---\n".join(blocks)

    def namespace_list(self) -> list[dict]:
        """Return all known namespaces (projects) with document counts and last updated.

        Returns list of dicts: [{project, doc_count, last_updated}].
        project=None is returned as '(global)'.
        """
        with self._read_lock:
            rows = self._conn.execute(
                """SELECT
                       COALESCE(project, '(global)') as project,
                       COUNT(*) as doc_count,
                       MAX(created_at) as last_updated
                   FROM documents
                   GROUP BY project
                   ORDER BY doc_count DESC"""
            ).fetchall()
        return [{"project": r["project"], "doc_count": r["doc_count"],
                 "last_updated": r["last_updated"]} for r in rows]

    def namespace_stats(self, project: str) -> dict:
        """Return detailed stats for a specific namespace/project."""
        is_global = project in ("(global)", "global", "")
        where = "project IS NULL" if is_global else "project = ?"
        params = () if is_global else (project,)
        with self._read_lock:
            row = self._conn.execute(
                f"""SELECT COUNT(*) as doc_count,
                           SUM(CASE WHEN priority='P0' THEN 1 ELSE 0 END) as p0_count,
                           SUM(CASE WHEN priority='P1' THEN 1 ELSE 0 END) as p1_count,
                           SUM(CASE WHEN priority='P2' THEN 1 ELSE 0 END) as p2_count,
                           MAX(created_at) as last_updated
                    FROM documents WHERE {where}""",
                params,
            ).fetchone()
        return {
            "project": project,
            "doc_count": row["doc_count"],
            "p0_count": row["p0_count"] or 0,
            "p1_count": row["p1_count"] or 0,
            "p2_count": row["p2_count"] or 0,
            "last_updated": row["last_updated"],
        }

    def close(self):
        self._conn.close()
        self._wq.close()
