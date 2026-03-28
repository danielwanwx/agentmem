"""MemoryStore: main API surface for agent-memory SDK."""
import json
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


def _get_ttl_days(source: str, priority: str) -> "int | None":
    """Return TTL in days. Source wins over priority; None = never expires."""
    if source in _SOURCE_TTL_DAYS:
        return _SOURCE_TTL_DAYS[source]
    return _PRIORITY_TTL_DAYS.get(priority)


def _effective_priority(source: str, priority: str) -> str:
    """Return the priority used for search ranking. Source wins over caller-supplied priority."""
    return _SOURCE_PRIORITY.get(source, priority)


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
        self._conn = init_db(self._db_path)
        self.state = StateManager(self._conn)
        self.session = SessionManager(self._conn)
        self._pruned_this_session = False

    def _track_session_doc(self, doc_id: int) -> None:
        """Track a doc_id for the current session (used for auto-relations on session end)."""
        current_sid = self.state.get("current_session_id")
        if current_sid:
            existing_docs = self.state.get(f"session_docs:{current_sid}") or []
            if doc_id not in existing_docs:
                existing_docs.append(doc_id)
                self.state.set(f"session_docs:{current_sid}", existing_docs)

    def _find_duplicate(self, title: str, source: str, project: str = None) -> "int | None":
        """Check if a document with similar title and same source exists.
        Uses FTS5 MATCH on title. Returns doc_id if duplicate found, else None.
        Scoped to same project (or both global).
        """
        if not title or len(title) < 3:
            return None
        try:
            if project:
                rows = self._conn.execute(
                    """SELECT d.doc_id, d.title, d.source, fts.rank
                       FROM documents_fts fts
                       JOIN documents d ON d.doc_id = fts.rowid
                       WHERE documents_fts MATCH ?
                         AND d.source = ?
                         AND (d.project = ? OR d.project IS NULL)
                       ORDER BY fts.rank
                       LIMIT 1""",
                    (title, source, project),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """SELECT d.doc_id, d.title, d.source, fts.rank
                       FROM documents_fts fts
                       JOIN documents d ON d.doc_id = fts.rowid
                       WHERE documents_fts MATCH ?
                         AND d.source = ?
                       ORDER BY fts.rank
                       LIMIT 1""",
                    (title, source),
                ).fetchall()
            if rows and rows[0]["rank"] > -5.0:
                return rows[0]["doc_id"]
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
    ) -> int:
        """Extract fields from content and persist to documents table.
        When file_path is provided, upserts by file_path (prevents duplicate docs
        for the same file across multiple edits). Returns doc_id.
        """
        fields = extract_fields(content, title_hint=title)
        actual_title = title or fields.get("title", "Untitled")

        effective_prio = _effective_priority(source, priority)
        ttl_days = _get_ttl_days(source, priority)
        expires_at = time.time() + ttl_days * 86400 if ttl_days else None

        # architectural_decision is always global (project=NULL)
        effective_project = None if source == "architectural_decision" else project

        # Dedup: check for existing doc with same title + source + project
        # file_path upsert takes precedence (skip dedup if file_path provided)
        if not file_path:
            dup_id = self._find_duplicate(actual_title, source, effective_project)
            if dup_id is not None:
                existing = self._conn.execute(
                    "SELECT key_facts, decisions, code_sigs, metrics FROM documents WHERE doc_id=?",
                    (dup_id,),
                ).fetchone()
                merged_kf = _merge_json_arrays(existing["key_facts"], json.dumps(fields["key_facts"]))
                merged_dec = _merge_json_arrays(existing["decisions"], json.dumps(fields["decisions"]))
                merged_cs = _merge_json_arrays(existing["code_sigs"], json.dumps(fields["code_sigs"]))
                merged_met = _merge_json_arrays(existing["metrics"], json.dumps(fields["metrics"]))
                self._conn.execute(
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
                self._conn.commit()
                self._embed_async(dup_id, actual_title, fields, raw_content or content)
                self._track_session_doc(dup_id)
                return dup_id

        if file_path:
            # Upsert: update existing doc for this file_path, or insert new
            existing = self._conn.execute(
                "SELECT doc_id FROM documents WHERE file_path=?", (file_path,)
            ).fetchone()
            if existing:
                self._conn.execute(
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
                self._conn.commit()
                self._embed_async(existing["doc_id"], actual_title, fields, raw_content or content)
                self._track_session_doc(existing["doc_id"])
                return existing["doc_id"]

        cur = self._conn.execute(
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
        self._conn.commit()
        doc_id = cur.lastrowid
        self._embed_async(doc_id, actual_title, fields, raw_content or content)
        self._track_session_doc(doc_id)
        return doc_id

    def _embed_async(self, doc_id: int, title: str, rule_fields: dict, content: str = "") -> None:
        """Background thread: LLM extraction → UPDATE fields → embedding → UPDATE embedding.

        Flow:
          1. Try LLM extraction from raw content (better quality than rules)
          2. If LLM succeeds: UPDATE summary/key_facts/decisions/generator in DB
          3. Compute embedding from best available fields
          4. UPDATE embedding

        daemon=False so the thread completes even if the main process exits
        (important for scripts like kb_import.py that exit after save()).
        Opens its own DB connection to avoid racing the caller's connection.
        """
        db_path = self._db_path

        def _run():
            use_fields = rule_fields
            try:
                conn = sqlite3.connect(db_path, check_same_thread=False)

                # Step 1: LLM extraction (if content provided)
                if content:
                    llm_fields = llm_extract(content, title_hint=title)
                    if llm_fields:
                        # Preserve rule-extracted code_sigs/metrics
                        llm_fields["code_sigs"] = rule_fields.get("code_sigs", [])
                        llm_fields["metrics"]   = rule_fields.get("metrics", [])
                        # Update DB with LLM-quality fields
                        conn.execute(
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
                        conn.commit()
                        use_fields = llm_fields

                # Step 2: Compute embedding from best available fields
                vec = embed_doc(
                    use_fields.get("title") or title,
                    use_fields.get("summary") or "",
                    use_fields.get("key_facts") or [],
                )
                if vec is not None:
                    conn.execute(
                        "UPDATE documents SET embedding=? WHERE doc_id=?",
                        (vec_to_blob(vec), doc_id),
                    )
                    conn.commit()
                    upsert_vec(conn, doc_id, vec)
                    conn.commit()

                conn.close()
            except Exception:
                pass

        threading.Thread(target=_run, daemon=False).start()

    def search(self, query: str, max_results: int = 5, project: str = None) -> list[SearchResult]:
        """Search documents. Updates last_accessed_at on hits to extend TTL.
        Prunes expired documents once per session (background, non-blocking).
        """
        if not self._pruned_this_session:
            self._pruned_this_session = True
            self._prune_expired_async()
        results = search_documents(self._conn, query, max_results=max_results, project=project)
        if results:
            self._touch_accessed_async([r.id for r in results])
        return results

    def prune_expired(self) -> int:
        """Delete documents whose expires_at has passed. Returns count deleted.

        P0 documents (expires_at IS NULL) are never deleted.
        Also removes orphaned rows from vec_documents (no FK cascade in SQLite).
        Called automatically on each search() call via _prune_expired_async().
        Can also be called manually: am doc prune.
        """
        expired_ids = [
            row[0] for row in self._conn.execute(
                "SELECT doc_id FROM documents WHERE expires_at IS NOT NULL AND expires_at < ?",
                (time.time(),),
            ).fetchall()
        ]
        if not expired_ids:
            return 0
        placeholders = ",".join("?" * len(expired_ids))
        self._conn.execute(
            f"DELETE FROM documents WHERE doc_id IN ({placeholders})", expired_ids
        )
        try:
            self._conn.execute(
                f"DELETE FROM vec_documents WHERE document_id IN ({placeholders})",
                expired_ids,
            )
        except Exception:
            pass  # vec_documents may not exist (sqlite-vec not loaded)
        self._conn.commit()
        return len(expired_ids)

    def _prune_expired_async(self) -> None:
        """Fire-and-forget background prune. Called from search() once per session."""
        db_path = self._db_path

        def _run():
            try:
                conn = sqlite3.connect(db_path, check_same_thread=False)
                expired_ids = [
                    row[0] for row in conn.execute(
                        "SELECT doc_id FROM documents "
                        "WHERE expires_at IS NOT NULL AND expires_at < ?",
                        (time.time(),),
                    ).fetchall()
                ]
                if expired_ids:
                    placeholders = ",".join("?" * len(expired_ids))
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
                    conn.commit()
                conn.close()
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()

    def _touch_accessed_async(self, doc_ids: list[int]) -> None:
        """Background: reset expires_at from now for each accessed doc (LRU TTL extension)."""
        db_path = self._db_path

        def _run():
            try:
                conn = sqlite3.connect(db_path, check_same_thread=False)
                now = time.time()
                for doc_id in doc_ids:
                    row = conn.execute(
                        "SELECT source, priority FROM documents WHERE doc_id=?",
                        (doc_id,),
                    ).fetchone()
                    if not row:
                        continue
                    ttl = _get_ttl_days(row[0], row[1])
                    if ttl:
                        conn.execute(
                            """UPDATE documents
                               SET last_accessed_at=?, expires_at=?
                               WHERE doc_id=?""",
                            (now, now + ttl * 86400, doc_id),
                        )
                conn.commit()
                conn.close()
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()

    def _get_related_docs(self, doc_id: int, exclude_ids: set) -> list[dict]:
        """Fetch related documents for inject expansion."""
        try:
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

        return "# [agent-memory] Relevant context\n" + "\n\n---\n".join(blocks)

    def close(self):
        self._conn.close()
