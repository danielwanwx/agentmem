"""SessionManager: session lifecycle and message storage."""
import json
import re
import time
import uuid
import sqlite3

# Max chars of conversation context sent to LLM for topic extraction
_SESSION_SUMMARY_MAX_CHARS = 3000


class SessionManager:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    _MSG_RETENTION_DAYS = 30
    _SESSION_RETENTION_DAYS = 90  # ended sessions auto-pruned after 90 days

    def start(self, project: str = "", topic: str = "", source: str = "") -> str:
        """Create session record, return session_id.

        Side effects (non-blocking):
        - Closes any orphaned sessions for the same project (ended_at IS NULL).
        - Prunes messages older than MSG_RETENTION_DAYS.
        """
        sid = str(uuid.uuid4())[:8] + "-" + str(int(time.time()))

        # Close orphaned sessions for this project before starting a new one
        if project:
            self._close_orphaned_sessions(project)

        self._conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, project, topic, started_at, source) VALUES (?,?,?,?,?)",
            (sid, project, topic, time.time(), source or None),
        )
        self._conn.commit()

        # Clean up stale sessions across ALL projects (zombie cleanup)
        self._promote_all_stale_sessions(exclude_session_id=sid)

        # prune old messages and ended sessions (TTL rotation)
        self._prune_messages()
        self._prune_sessions()

        return sid

    def _promote_all_stale_sessions(self, exclude_session_id: str = None) -> None:
        """End and promote ALL stale open sessions across all projects.

        Stale = no messages for 1+ hours OR open for 7+ days.
        Called from start() to clean up zombies from crashed sessions.
        """
        now = time.time()
        one_hour_ago = now - 3600
        seven_days_ago = now - 7 * 86400

        orphans = self._conn.execute(
            """SELECT s.session_id, MAX(m.timestamp) as last_msg
               FROM sessions s
               LEFT JOIN messages m ON m.session_id = s.session_id
               WHERE s.ended_at IS NULL
               GROUP BY s.session_id
               HAVING last_msg IS NULL OR last_msg < ? OR s.started_at < ?""",
            (one_hour_ago, seven_days_ago),
        ).fetchall()

        for row in orphans:
            oid = row["session_id"]
            if oid == exclude_session_id:
                continue
            summary, key_facts, decisions, topic = self._extract_from_messages(oid)
            self._conn.execute(
                """UPDATE sessions SET ended_at=?, summary=?, key_facts=?, decisions=?, topic=?
                   WHERE session_id=?""",
                (now, summary, json.dumps(key_facts), json.dumps(decisions),
                 topic or None, oid),
            )
            self._conn.commit()
            self._maybe_promote(oid)
        if orphans:
            self._conn.commit()

    def _close_orphaned_sessions(self, project: str) -> None:
        """Close sessions with no ended_at for this project using rule-based extraction."""
        orphans = self._conn.execute(
            "SELECT session_id FROM sessions WHERE project=? AND ended_at IS NULL",
            (project,),
        ).fetchall()
        for row in orphans:
            oid = row["session_id"]
            summary, key_facts, decisions, topic = self._extract_from_messages(oid)
            self._conn.execute(
                """UPDATE sessions SET ended_at=?, summary=?, key_facts=?, decisions=?, topic=?
                   WHERE session_id=?""",
                (time.time(), summary, json.dumps(key_facts), json.dumps(decisions),
                 topic or None, oid),
            )
        if orphans:
            self._conn.commit()
        for row in orphans:
            self._maybe_promote(row["session_id"])

    def _prune_messages(self) -> None:
        """Delete messages older than retention window."""
        cutoff = time.time() - self._MSG_RETENTION_DAYS * 86400
        self._conn.execute("DELETE FROM messages WHERE timestamp < ?", (cutoff,))
        self._conn.commit()

    def _prune_sessions(self) -> None:
        """Promote and delete ended sessions older than SESSION_RETENTION_DAYS.

        Only prunes sessions with ended_at set (completed sessions).
        Orphaned sessions (ended_at IS NULL) are handled by _close_orphaned_sessions.
        This ensures the sessions table doesn't grow unbounded for CLI users
        who have no dashboard to manually delete sessions.
        """
        cutoff = time.time() - self._SESSION_RETENTION_DAYS * 86400
        old = self._conn.execute(
            "SELECT session_id FROM sessions WHERE ended_at IS NOT NULL AND ended_at < ?",
            (cutoff,),
        ).fetchall()
        for row in old:
            sid = row["session_id"]
            self._conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
            self._conn.execute("DELETE FROM sessions WHERE session_id=?", (sid,))
        if old:
            self._conn.commit()

    def get_latest_session_id(self, source: str = None, project: str = None) -> "str | None":
        """Return the most recent session_id matching source (exact) or project (fallback).
        Returns None if no match found.
        """
        if source:
            row = self._conn.execute(
                "SELECT session_id FROM sessions WHERE source=? ORDER BY started_at DESC LIMIT 1",
                (source,)
            ).fetchone()
            if row:
                return row[0]
        if project:
            row = self._conn.execute(
                "SELECT session_id FROM sessions WHERE project=? ORDER BY started_at DESC LIMIT 1",
                (project,)
            ).fetchone()
            if row:
                return row[0]
        return None

    def save_message(self, session_id: str, role: str, content: str,
                     project: str = "") -> None:
        """Append raw message. Never directly injected — source for session_end()."""
        self._conn.execute(
            """INSERT INTO messages (session_id, role, content, timestamp, project)
               VALUES (?,?,?,?,?)""",
            (session_id, role, content, time.time(), project),
        )
        self._conn.commit()

    def end(self, session_id: str, summary: str = None,
            key_facts: list = None, decisions: list = None,
            open_items: list = None) -> None:
        """Finalize session. If summary not provided, uses LLM extraction (rule fallback)."""
        topic = None
        if summary is None:
            summary, key_facts, decisions, topic = self._extract_from_messages(session_id)

        update_topic = topic is not None
        if update_topic:
            self._conn.execute(
                """UPDATE sessions SET ended_at=?, summary=?, key_facts=?,
                   decisions=?, open_items=?, topic=? WHERE session_id=?""",
                (
                    time.time(),
                    summary,
                    json.dumps(key_facts or []),
                    json.dumps(decisions or []),
                    json.dumps(open_items or []),
                    topic,
                    session_id,
                ),
            )
        else:
            self._conn.execute(
                """UPDATE sessions SET ended_at=?, summary=?, key_facts=?,
                   decisions=?, open_items=? WHERE session_id=?""",
                (
                    time.time(),
                    summary,
                    json.dumps(key_facts or []),
                    json.dumps(decisions or []),
                    json.dumps(open_items or []),
                    session_id,
                ),
            )
        self._create_session_relations(session_id)
        self._conn.commit()
        self._maybe_promote(session_id)

    def _maybe_promote(self, session_id: str) -> "int | None":
        """Promote session knowledge to documents table via three-tier quality gate.

        Returns doc_id if promoted, None if session too thin.
        Upserts by file_path='session:{session_id}' to prevent duplicates on re-promotion.
        """
        sess = self._conn.execute(
            "SELECT summary, key_facts, decisions, project, topic FROM sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if not sess or not sess["summary"]:
            return None

        summary = sess["summary"] or ""
        key_facts = json.loads(sess["key_facts"] or "[]")
        decisions = json.loads(sess["decisions"] or "[]")
        topic = sess["topic"] or ""
        project = sess["project"] or None

        # Three-tier quality gate
        if decisions or len(key_facts) >= 3:
            source = "session_extract"   # P1, 60d TTL
        elif len(summary) > 30:
            source = "session_note"      # P2, 30d TTL
        else:
            return None  # truly empty, not worth promoting

        title = topic or f"Session {session_id[:8]}"
        content_parts = [f"# {title}", summary]
        if key_facts:
            content_parts.append("Key facts:\n" + "\n".join(f"- {f}" for f in key_facts))
        if decisions:
            content_parts.append("Decisions:\n" + "\n".join(f"- {d}" for d in decisions))
        content = "\n\n".join(content_parts)

        file_path = f"session:{session_id}"

        # Upsert: check if already promoted
        existing = self._conn.execute(
            "SELECT doc_id FROM documents WHERE file_path=?", (file_path,)
        ).fetchone()

        from .store import _get_ttl_days, _effective_priority, _SOURCE_TTL_DAYS

        priority = _effective_priority(source, "P1")
        ttl_days = _get_ttl_days(source, priority)
        expires_at = time.time() + ttl_days * 86400 if ttl_days else None

        if existing:
            self._conn.execute(
                """UPDATE documents SET title=?, summary=?, key_facts=?, decisions=?,
                   raw_content=?, priority=?, source=?, expires_at=?, project=?
                   WHERE doc_id=?""",
                (title, summary, json.dumps(key_facts), json.dumps(decisions),
                 content, priority, source, expires_at, project, existing["doc_id"]),
            )
            self._conn.commit()
            return existing["doc_id"]
        else:
            cur = self._conn.execute(
                """INSERT INTO documents (title, summary, key_facts, decisions,
                   raw_content, priority, source, generator, file_path, expires_at, project)
                   VALUES (?,?,?,?,?,?,?,'rule',?,?,?)""",
                (title, summary, json.dumps(key_facts), json.dumps(decisions),
                 content, priority, source, file_path, expires_at, project),
            )
            self._conn.commit()
            return cur.lastrowid

    def checkpoint(self, session_id: str) -> dict:
        """Extract + promote without ending session. Safe to call repeatedly (upserts)."""
        summary, key_facts, decisions, topic = self._extract_from_messages(session_id)
        if summary:
            self._conn.execute(
                """UPDATE sessions SET summary=?, key_facts=?, decisions=?
                   WHERE session_id=?""",
                (summary, json.dumps(key_facts or []), json.dumps(decisions or []),
                 session_id),
            )
            if topic:
                self._conn.execute(
                    "UPDATE sessions SET topic=? WHERE session_id=?",
                    (topic, session_id),
                )
            self._conn.commit()
        doc_id = self._maybe_promote(session_id)
        return {"summary": summary, "key_facts": key_facts or [],
                "decisions": decisions or [], "doc_id": doc_id}

    def _create_session_relations(self, session_id: str) -> None:
        """Create 'related' relations between all docs produced in this session."""
        from .state import StateManager
        state = StateManager(self._conn)
        doc_ids = state.get(f"session_docs:{session_id}")
        if not doc_ids or not isinstance(doc_ids, list) or len(doc_ids) < 2:
            return
        for i in range(len(doc_ids)):
            for j in range(i + 1, len(doc_ids)):
                a, b = min(doc_ids[i], doc_ids[j]), max(doc_ids[i], doc_ids[j])
                try:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO doc_relations (doc_id_a, doc_id_b, relation_type) VALUES (?, ?, 'related')",
                        (a, b),
                    )
                except Exception:
                    pass
        self._conn.commit()

    def _extract_from_messages(self, session_id: str) -> tuple:
        """Extract session summary. Tries LLM first, falls back to rules.

        Returns (summary, key_facts, decisions, topic).
        topic is non-empty only when LLM extraction succeeds.
        """
        rows = self._conn.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
        if not rows:
            return ("", [], [], "")

        # Try LLM extraction first (graceful degradation — never raises)
        llm_result = self._llm_extract_session(rows)
        if llm_result:
            return llm_result

        # Rule-based fallback
        sess = self._conn.execute(
            "SELECT topic, project FROM sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        existing_topic = (sess["topic"] if sess else "") or ""
        summary = f"{existing_topic} — {len(rows)} messages" if existing_topic else f"{len(rows)} messages"

        key_facts = []
        decisions = []
        for row in rows:
            if row["role"] == "assistant":
                for line in row["content"].splitlines():
                    s = line.strip()
                    if re.match(r"[-*]\s+\*\*[^*]+\*\*[: ]", s):
                        key_facts.append(s.lstrip("-* ")[:150])
                    if re.match(r"(Decision:|Chose |→ Use |\*\*Decision)", s):
                        decisions.append(s[:150])

        return (summary, key_facts[:8], decisions[:6], "")

    def _llm_extract_session(self, rows: list) -> "tuple | None":
        """LLM-based session summary and topic extraction.

        Returns (summary, key_facts, decisions, topic) or None on failure.
        Uses first 10 messages (≤3000 chars) to keep latency low.
        """
        try:
            from .llm import chat

            # Build context: first 3 (setup) + last 7 (conclusions)
            if len(rows) > 10:
                sample = list(rows[:3]) + list(rows[-7:])
            else:
                sample = rows
            parts = []
            for row in sample:
                max_chars = 1500 if row["role"] == "assistant" else 200
                content = (row["content"] or "")[:max_chars].strip()
                if content:
                    parts.append(f"{row['role'].upper()}: {content}")
            context = "\n\n".join(parts)[:_SESSION_SUMMARY_MAX_CHARS]

            prompt = (
                "/no_think\n"
                "Summarize this conversation session. Return ONLY a JSON object:\n"
                '{"topic": "<one-line topic, max 60 chars>", '
                '"summary": "<2-3 sentences: what was accomplished>", '
                '"key_facts": ["<fact>"], "decisions": ["<decision>"]}\n\n'
                f"Conversation:\n{context}"
            )
            raw = chat([{"role": "user", "content": prompt}], timeout=20.0, think=False)
            if not raw:
                return None

            data = json.loads(raw)
            topic   = str(data.get("topic",   "") or "").strip()[:60]
            summary = str(data.get("summary", "") or "").strip()

            if not summary or len(summary) < 15:
                return None

            key_facts = [str(f).strip() for f in data.get("key_facts", []) if str(f).strip()]
            decisions = [str(d).strip() for d in data.get("decisions", []) if str(d).strip()]

            return (summary, key_facts[:8], decisions[:6], topic)
        except Exception:
            return None

    def delete(self, session_id: str) -> None:
        """Delete session and all its messages. Called from dashboard 'close chat'."""
        if not session_id:
            return
        self._conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        self._conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
        self._conn.commit()

    def list_for_dashboard(self, limit: int = 100,
                           include_cli: bool = False) -> list[dict]:
        """Return sessions for dashboard list view, with message counts.

        By default excludes cli:* sessions (Claude Code CLI) since those
        have no assistant messages. Pass include_cli=True to show them.
        """
        where = "" if include_cli else "WHERE s.source NOT LIKE 'cli:%'"
        rows = self._conn.execute(
            f"""SELECT s.session_id, s.topic, s.source, s.project,
                       s.started_at, s.ended_at,
                       COUNT(m.id) AS message_count
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.session_id
                {where}
                GROUP BY s.session_id
                ORDER BY s.started_at DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_messages(self, session_id: str) -> list[dict]:
        """Return all messages for a session (for dashboard full history view)."""
        rows = self._conn.execute(
            "SELECT role, content, timestamp FROM messages "
            "WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"],
                 "timestamp": r["timestamp"]} for r in rows]

    def get_resume_context(self, session_id: str,
                           max_tokens: int = 2000) -> dict:
        """Token-efficient session resume context.
        Returns summary L2 + last 10 messages (~1500 tok total).
        Falls back gracefully if session_end was never called (orphaned session).
        """
        sess = self._conn.execute(
            """SELECT summary, key_facts, decisions, open_items
               FROM sessions WHERE session_id=?""",
            (session_id,),
        ).fetchone()

        if sess and sess["summary"] is not None:
            summary = sess["summary"]
            key_facts = json.loads(sess["key_facts"] or "[]")
            decisions = json.loads(sess["decisions"] or "[]")
            open_items = json.loads(sess["open_items"] or "[]")
        else:
            summary, key_facts, decisions, _ = self._extract_from_messages(session_id)
            open_items = []

        msg_rows = self._conn.execute(
            """SELECT role, content FROM messages WHERE session_id=?
               ORDER BY id DESC LIMIT 10""",
            (session_id,),
        ).fetchall()
        recent = [{"role": r["role"], "content": r["content"]}
                  for r in reversed(msg_rows)]

        return {
            "session_id": session_id,
            "summary": summary,
            "key_facts": key_facts,
            "decisions": decisions,
            "open_items": open_items,
            "recent_messages": recent,
        }
