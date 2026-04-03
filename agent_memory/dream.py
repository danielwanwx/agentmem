"""Dream — background memory consolidation.

Reviews recent sessions, finds cross-session patterns, resolves
contradictions, and merges knowledge into higher-quality documents.

Unlike _maybe_promote() which operates on single sessions, dream
reviews ALL recent sessions together with cross-session awareness.

4 phases: Orient → Gather → Consolidate → Prune
Gate: min_hours since last dream + min_sessions since last dream
Lock: file-based (.dream-lock) with PID + stale detection
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .db import DB_PATH

logger = logging.getLogger(__name__)

# --- Gate & Lock Configuration ---

DEFAULT_MIN_HOURS = 24
DEFAULT_MIN_SESSIONS = 5
LOCK_STALE_SECONDS = 3600  # 60 minutes


@dataclass
class DreamResult:
    """Result of a dream consolidation run."""
    success: bool
    phase: str  # orient | gather | consolidate | prune | complete | gate_failed
    sessions_reviewed: int = 0
    patterns_found: int = 0
    contradictions_resolved: int = 0
    documents_created: int = 0
    documents_updated: int = 0
    documents_pruned: int = 0
    duration_ms: int = 0
    reason: str | None = None
    errors: list[str] = field(default_factory=list)
    # Dry-run planned actions
    planned_actions: list[dict] = field(default_factory=list)
    # Health check results
    stale_detected: int = 0
    cross_contradictions_resolved: int = 0
    redundant_merged: int = 0


class DreamLock:
    """File-based lock preventing concurrent dream runs."""

    def __init__(self, lock_path: Path | None = None):
        self._path = lock_path or (DB_PATH.parent / ".dream-lock")

    @property
    def path(self) -> Path:
        return self._path

    def last_dream_at(self) -> float:
        if not self._path.exists():
            return 0.0
        return self._path.stat().st_mtime

    def hours_since_last(self) -> float:
        last = self.last_dream_at()
        if last == 0:
            return float("inf")
        return (time.time() - last) / 3600

    def try_acquire(self) -> bool:
        if self._path.exists():
            try:
                pid = int(self._path.read_text().strip())
                age = time.time() - self._path.stat().st_mtime
                # Block only if lock is fresh AND held by a *different* live process
                if age < LOCK_STALE_SECONDS and pid != os.getpid() and _pid_alive(pid):
                    return False
            except (ValueError, OSError):
                pass
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(str(os.getpid()))
        return True

    def release(self) -> None:
        if self._path.exists():
            self._path.touch()  # mtime = last dream time

    def rollback(self, prior_mtime: float) -> None:
        if self._path.exists() and prior_mtime > 0:
            os.utime(self._path, (prior_mtime, prior_mtime))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _fuzzy_match(a: str, b: str) -> float:
    """Fuzzy string similarity (0-1)."""
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _deduplicate_strings(items: list[str], threshold: float = 0.85) -> list[str]:
    """Deduplicate a list of strings using fuzzy matching."""
    unique = []
    for item in items:
        is_dup = False
        for existing in unique:
            if _fuzzy_match(item, existing) > threshold:
                is_dup = True
                break
        if not is_dup:
            unique.append(item)
    return unique


def _detect_contradictions(facts: list[str]) -> list[tuple[str, str]]:
    """Detect potentially contradictory facts.

    Simple heuristic: facts about the same subject with different values.
    Returns list of (fact_a, fact_b) pairs that may contradict.
    """
    contradictions = []
    for i in range(len(facts)):
        for j in range(i + 1, len(facts)):
            a, b = facts[i].lower(), facts[j].lower()
            # Check if they share a key (e.g., "timeout: 30s" vs "timeout: 600s")
            # by looking at text before the colon
            if ":" in a and ":" in b:
                key_a = a.split(":")[0].strip()
                key_b = b.split(":")[0].strip()
                if _fuzzy_match(key_a, key_b) > 0.8:
                    val_a = a.split(":", 1)[1].strip()
                    val_b = b.split(":", 1)[1].strip()
                    if val_a != val_b:
                        contradictions.append((facts[i], facts[j]))
    return contradictions


class Dreamer:
    """4-phase memory consolidation engine operating on AgentMem's SQLite DB."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        min_hours: float = DEFAULT_MIN_HOURS,
        min_sessions: int = DEFAULT_MIN_SESSIONS,
        lock: DreamLock | None = None,
        write_queue: Any = None,
    ):
        self._conn = conn
        self._min_hours = min_hours
        self._min_sessions = min_sessions
        self._lock = lock or DreamLock()
        self._wq = write_queue

    def _execute_write(self, sql: str, params: tuple | list = ()) -> int:
        """Route writes through WriteQueue if available."""
        if self._wq:
            return self._wq.execute(sql, params)
        else:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid

    def check_gate(self) -> tuple[bool, str]:
        """Check if dream should run."""
        hours = self._lock.hours_since_last()
        if hours < self._min_hours:
            return False, f"Only {hours:.1f}h since last dream (need {self._min_hours}h)"

        last_ts = self._lock.last_dream_at()
        count = self._count_sessions_since(last_ts)
        if count < self._min_sessions:
            return False, f"Only {count} sessions since last dream (need {self._min_sessions})"

        return True, "Gate passed"

    def run(self, force: bool = False, dry_run: bool = False) -> DreamResult:
        """Execute the full 4-phase dream consolidation.

        If dry_run=True, returns planned actions without writing to DB.
        """
        start = time.monotonic()

        if not force:
            should, reason = self.check_gate()
            if not should:
                return DreamResult(
                    success=False, phase="gate_failed", reason=reason,
                    duration_ms=int((time.monotonic() - start) * 1000),
                )

        # Capture prior mtime BEFORE acquiring lock (which overwrites the file)
        prior_mtime = self._lock.last_dream_at()

        if not dry_run and not self._lock.try_acquire():
            return DreamResult(
                success=False, phase="gate_failed",
                reason="Lock held by another process",
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        try:
            # Phase 1: Orient — understand current state
            existing_docs = self._orient()

            # Phase 2: Gather — collect recent session knowledge
            sessions = self._gather(prior_mtime)
            if not sessions:
                if not dry_run:
                    self._lock.release()
                return DreamResult(
                    success=True, phase="gather",
                    sessions_reviewed=0,
                    duration_ms=int((time.monotonic() - start) * 1000),
                )

            # Phase 3: Consolidate — find patterns, merge, resolve contradictions
            consolidation = self._consolidate(sessions, existing_docs, dry_run=dry_run)

            # Phase 3.5: Health Check — staleness, cross-doc contradictions, redundancy
            health = self._health_check(existing_docs, dry_run=dry_run)

            # Phase 4: Prune — remove expired docs (now catches stale docs from health check)
            if dry_run:
                pruned = 0
            else:
                pruned = self._prune()

            if not dry_run:
                self._lock.release()

            all_actions = consolidation.get("planned_actions", []) + health.get("planned_actions", [])

            return DreamResult(
                success=True,
                phase="complete",
                sessions_reviewed=len(sessions),
                patterns_found=consolidation["patterns_found"],
                contradictions_resolved=consolidation.get("contradictions_resolved", 0),
                documents_created=consolidation["created"],
                documents_updated=consolidation["updated"],
                documents_pruned=pruned,
                duration_ms=int((time.monotonic() - start) * 1000),
                planned_actions=all_actions,
                stale_detected=health.get("stale_detected", 0),
                cross_contradictions_resolved=health.get("cross_contradictions_resolved", 0),
                redundant_merged=health.get("redundant_merged", 0),
            )

        except Exception as e:
            if not dry_run:
                self._lock.rollback(prior_mtime)
            return DreamResult(
                success=False, phase="consolidate",
                errors=[str(e)],
                duration_ms=int((time.monotonic() - start) * 1000),
            )

    # --- Phase 1: Orient ---

    def _orient(self) -> list[dict]:
        """Read existing documents to understand current knowledge state."""
        rows = self._conn.execute(
            """SELECT doc_id, title, summary, key_facts, decisions, source, priority
               FROM documents ORDER BY created_at DESC LIMIT 100"""
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Phase 2: Gather ---

    def _gather(self, since_ts: float) -> list[dict]:
        """Collect ended sessions since last dream with their knowledge."""
        rows = self._conn.execute(
            """SELECT session_id, topic, summary, key_facts, decisions, project,
                      started_at, ended_at
               FROM sessions
               WHERE ended_at IS NOT NULL AND ended_at > ?
               ORDER BY ended_at ASC""",
            (since_ts,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _count_sessions_since(self, since_ts: float) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE ended_at IS NOT NULL AND ended_at > ?",
            (since_ts,),
        ).fetchone()
        return row[0] if row else 0

    # --- Phase 3: Consolidate ---

    def _consolidate(self, sessions: list[dict], existing_docs: list[dict],
                     dry_run: bool = False) -> dict:
        """Find cross-session patterns and merge knowledge.

        Tries LLM consolidation first, falls back to rule-based merging.
        """
        result = {"patterns_found": 0, "created": 0, "updated": 0,
                  "contradictions_resolved": 0, "planned_actions": []}

        # Try LLM-based consolidation (skip in dry-run — LLM can't be "undone")
        if not dry_run:
            llm_result = self._llm_consolidate(sessions, existing_docs)
            if llm_result:
                return llm_result

        # Rule-based consolidation: group sessions by project, merge with fuzzy dedup
        by_project: dict[str, list[dict]] = {}
        for sess in sessions:
            proj = sess.get("project") or "_default"
            by_project.setdefault(proj, []).append(sess)

        for project, project_sessions in by_project.items():
            all_facts: list[str] = []
            all_decisions: list[str] = []
            topics: list[str] = []

            for sess in project_sessions:
                facts = json.loads(sess.get("key_facts") or "[]")
                decisions = json.loads(sess.get("decisions") or "[]")
                all_facts.extend(facts)
                all_decisions.extend(decisions)
                if sess.get("topic"):
                    topics.append(sess["topic"])

            if not all_facts and not all_decisions:
                continue

            # Step 1: Detect contradictions BEFORE dedup (exact strings from sessions)
            contradictions = _detect_contradictions(all_facts)
            if contradictions:
                result["contradictions_resolved"] += len(contradictions)
                # Resolve: remove the earlier fact (first in list = older session)
                for old_fact, new_fact in contradictions:
                    logger.info("Contradiction resolved: '%s' → '%s'",
                                old_fact[:50], new_fact[:50])
                    # Remove the old fact (first occurrence) if it still exists
                    if old_fact in all_facts:
                        all_facts.remove(old_fact)

            # Step 2: Fuzzy deduplicate remaining facts and decisions
            unique_facts = _deduplicate_strings(all_facts, threshold=0.90)
            unique_decisions = _deduplicate_strings(all_decisions, threshold=0.90)

            title = f"Dream: {project}" if project != "_default" else "Dream: cross-session consolidation"
            summary = f"Consolidated from {len(project_sessions)} sessions."
            if topics:
                summary += f" Topics: {', '.join(topics[:5])}"

            content_parts = [f"# {title}", summary]
            if unique_facts:
                content_parts.append("## Key Facts\n" + "\n".join(f"- {f}" for f in unique_facts[:20]))
            if unique_decisions:
                content_parts.append("## Decisions\n" + "\n".join(f"- {d}" for d in unique_decisions[:10]))
            if contradictions:
                content_parts.append("## Contradictions Resolved\n" + "\n".join(
                    f"- {old} → {new}" for old, new in contradictions
                ))
            content = "\n\n".join(content_parts)

            # Upsert by dream file_path
            file_path = f"dream:{project}"
            existing = self._conn.execute(
                "SELECT doc_id FROM documents WHERE file_path=?", (file_path,)
            ).fetchone()

            expires_at = time.time() + 90 * 86400  # 90 day TTL
            effective_project = project if project != "_default" else None

            action = {
                "type": "update" if existing else "create",
                "project": project,
                "title": title,
                "facts_count": len(unique_facts),
                "decisions_count": len(unique_decisions),
                "contradictions": len(contradictions),
            }
            result["planned_actions"].append(action)

            if dry_run:
                result["patterns_found"] += len(unique_facts) + len(unique_decisions)
                continue

            # Log to consolidation_log
            self._log_consolidation(
                project=project,
                action="update" if existing else "create",
                facts_merged=len(all_facts) - len(unique_facts),
                contradictions_resolved=len(contradictions),
                sessions_count=len(project_sessions),
            )

            if existing:
                self._execute_write(
                    """UPDATE documents SET title=?, summary=?, key_facts=?, decisions=?,
                       raw_content=?, expires_at=?, project=? WHERE doc_id=?""",
                    (title, summary, json.dumps(unique_facts), json.dumps(unique_decisions),
                     content, expires_at, effective_project, existing["doc_id"]),
                )
                result["updated"] += 1
            else:
                self._execute_write(
                    """INSERT INTO documents (title, summary, key_facts, decisions,
                       raw_content, priority, source, generator, file_path, expires_at, project)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, summary, json.dumps(unique_facts), json.dumps(unique_decisions),
                     content, "P1", "dream", "dream", file_path, expires_at, effective_project),
                )
                result["created"] += 1

            result["patterns_found"] += len(unique_facts) + len(unique_decisions)

        return result

    def _log_consolidation(self, project: str, action: str,
                           facts_merged: int, contradictions_resolved: int,
                           sessions_count: int, phase: str = "consolidate") -> None:
        """Log a consolidation event for auditability."""
        # Ensure consolidation_log table exists (with phase column)
        try:
            self._execute_write(
                """CREATE TABLE IF NOT EXISTS consolidation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL DEFAULT (julianday('now')),
                    project TEXT,
                    action TEXT,
                    facts_merged INTEGER,
                    contradictions_resolved INTEGER,
                    sessions_count INTEGER,
                    phase TEXT DEFAULT 'consolidate'
                )"""
            )
        except Exception:
            pass
        # Migrate: add phase column if missing
        try:
            self._conn.execute("SELECT phase FROM consolidation_log LIMIT 1")
        except Exception:
            try:
                self._execute_write(
                    "ALTER TABLE consolidation_log ADD COLUMN phase TEXT DEFAULT 'consolidate'"
                )
            except Exception:
                pass
        try:
            self._execute_write(
                """INSERT INTO consolidation_log
                   (timestamp, project, action, facts_merged, contradictions_resolved,
                    sessions_count, phase)
                   VALUES (?,?,?,?,?,?,?)""",
                (time.time(), project, action, facts_merged, contradictions_resolved,
                 sessions_count, phase),
            )
        except Exception:
            logger.debug("Failed to log consolidation", exc_info=True)

    def _llm_consolidate(self, sessions: list[dict], existing_docs: list[dict]) -> dict | None:
        """Use LLM to find cross-session patterns and contradictions.

        Feeds both session metadata AND raw conversation messages to the LLM
        for deep cross-session pattern detection. Returns consolidation result
        dict or None on failure (graceful degradation to rule-based).
        """
        try:
            from .llm import chat

            # Build rich session context: metadata + raw messages
            session_texts = []
            for s in sessions[:12]:  # Cap to manage token budget
                sid = s.get("session_id", "")
                parts = [f"### Session: {s.get('topic', 'unknown')} ({sid[:8]})"]
                if s.get("summary"):
                    parts.append(f"Summary: {s['summary']}")
                facts = json.loads(s.get("key_facts") or "[]")
                if facts:
                    parts.append("Facts: " + "; ".join(facts[:5]))
                decisions = json.loads(s.get("decisions") or "[]")
                if decisions:
                    parts.append("Decisions: " + "; ".join(decisions[:3]))

                # Pull raw messages for richer context
                messages = self._get_session_messages(sid, limit=8)
                if messages:
                    parts.append("Key messages:")
                    for msg in messages:
                        role = msg["role"].upper()
                        content = msg["content"][:300].strip()
                        if content:
                            parts.append(f"  {role}: {content}")

                session_texts.append("\n".join(parts))

            # Build existing doc context for contradiction detection
            doc_texts = []
            for d in existing_docs[:10]:
                title = d.get("title", "")
                summary = d.get("summary", "")
                old_facts = json.loads(d.get("key_facts") or "[]")
                doc_line = f"[{title}] {summary}"
                if old_facts:
                    doc_line += f" | Facts: {'; '.join(old_facts[:3])}"
                doc_texts.append(doc_line)

            prompt = (
                "/no_think\n"
                "You are a memory consolidation engine performing background 'dreaming'. "
                "Your job: review recent sessions and existing knowledge, then produce "
                "consolidated insights.\n\n"
                "CRITICAL TASKS:\n"
                "1. Find CROSS-SESSION PATTERNS — topics that appear in multiple sessions\n"
                "2. Resolve CONTRADICTIONS — if session A says X and session B says Y, "
                "keep the most recent one and note what was replaced\n"
                "3. Convert relative dates to absolute dates when possible\n"
                "4. Merge duplicate facts into single authoritative statements\n"
                "5. Identify ROOT CAUSES that connect surface-level observations\n\n"
                "Return ONLY a JSON object:\n"
                "```json\n"
                '{"insights": [\n'
                '  {\n'
                '    "title": "<concise topic name>",\n'
                '    "summary": "<2-3 sentences: what we now know>",\n'
                '    "facts": ["<consolidated fact>"],\n'
                '    "decisions": ["<decision with date if available>"],\n'
                '    "contradictions_resolved": ["<old> → <new> (reason)"],\n'
                '    "root_causes": ["<deep pattern connecting multiple observations>"]\n'
                "  }\n"
                "]}\n"
                "```\n\n"
                f"## Recent Sessions ({len(sessions)})\n\n"
                + "\n---\n".join(session_texts)
                + "\n\n## Existing Knowledge Base\n\n"
                + ("\n".join(doc_texts) if doc_texts else "(empty)")
            )

            raw = chat([{"role": "user", "content": prompt}], timeout=45.0, think=False)
            if not raw:
                return None

            data = json.loads(raw)
            insights = data.get("insights", [])
            if not insights:
                return None

            result = {"patterns_found": 0, "created": 0, "updated": 0,
                      "contradictions_resolved": 0, "planned_actions": []}

            for insight in insights:
                title = str(insight.get("title", "")).strip()
                summary = str(insight.get("summary", "")).strip()
                if not title or not summary:
                    continue

                facts = [str(f).strip() for f in insight.get("facts", []) if str(f).strip()]
                decisions = [str(d).strip() for d in insight.get("decisions", []) if str(d).strip()]
                contradictions = [str(c).strip() for c in insight.get("contradictions_resolved", []) if str(c).strip()]
                root_causes = [str(r).strip() for r in insight.get("root_causes", []) if str(r).strip()]

                content_parts = [f"# {title}", summary]
                if facts:
                    content_parts.append("## Facts\n" + "\n".join(f"- {f}" for f in facts))
                if decisions:
                    content_parts.append("## Decisions\n" + "\n".join(f"- {d}" for d in decisions))
                if root_causes:
                    content_parts.append("## Root Causes\n" + "\n".join(f"- {r}" for r in root_causes))
                if contradictions:
                    content_parts.append("## Contradictions Resolved\n" + "\n".join(f"- {c}" for c in contradictions))
                content = "\n\n".join(content_parts)

                # Upsert by title-based file_path
                safe_title = title.lower().replace(" ", "-")[:40]
                file_path = f"dream:insight:{safe_title}"

                existing = self._conn.execute(
                    "SELECT doc_id FROM documents WHERE file_path=?", (file_path,)
                ).fetchone()

                expires_at = time.time() + 90 * 86400

                if existing:
                    self._execute_write(
                        """UPDATE documents SET title=?, summary=?, key_facts=?, decisions=?,
                           raw_content=?, expires_at=? WHERE doc_id=?""",
                        (title, summary, json.dumps(facts), json.dumps(decisions),
                         content, expires_at, existing["doc_id"]),
                    )
                    result["updated"] += 1
                else:
                    self._execute_write(
                        """INSERT INTO documents (title, summary, key_facts, decisions,
                           raw_content, priority, source, generator, file_path, expires_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (title, summary, json.dumps(facts), json.dumps(decisions),
                         content, "P1", "dream", "llm", file_path, expires_at),
                    )
                    result["created"] += 1

                result["patterns_found"] += len(facts) + len(decisions)
                result["contradictions_resolved"] += len(contradictions)

            return result

        except Exception:
            return None

    def _get_session_messages(self, session_id: str, limit: int = 8) -> list[dict]:
        """Pull raw messages for a session. Returns first 3 + last (limit-3)."""
        rows = self._conn.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
        if not rows:
            return []
        # First 3 (setup context) + last N (conclusions)
        if len(rows) > limit:
            sample = list(rows[:3]) + list(rows[-(limit - 3):])
        else:
            sample = list(rows)
        return [{"role": r["role"], "content": r["content"]} for r in sample]

    # --- Phase 3.5: Health Check ---

    def _health_check(self, existing_docs: list[dict], dry_run: bool = False) -> dict:
        """Phase 3.5: Staleness, cross-doc contradictions, redundancy merge.

        Operates on the ENTIRE knowledge base, not just new sessions.
        """
        result = {
            "stale_detected": 0,
            "cross_contradictions_resolved": 0,
            "redundant_merged": 0,
            "indexes_created": 0,
            "planned_actions": [],
        }

        # Re-read docs fresh (consolidation may have changed them)
        docs = self._conn.execute(
            """SELECT doc_id, title, summary, key_facts, decisions, code_sigs,
                      priority, source, project, created_at, expires_at
               FROM documents ORDER BY created_at DESC LIMIT 200"""
        ).fetchall()
        docs = [dict(r) for r in docs]

        stale = self._check_staleness(docs, dry_run=dry_run)
        result["stale_detected"] = stale["stale_detected"]
        result["planned_actions"].extend(stale.get("planned_actions", []))

        contra = self._check_cross_contradictions(docs, dry_run=dry_run)
        result["cross_contradictions_resolved"] = contra["cross_contradictions_resolved"]
        result["planned_actions"].extend(contra.get("planned_actions", []))

        merge = self._merge_redundant(docs, dry_run=dry_run)
        result["redundant_merged"] = merge["redundant_merged"]
        result["indexes_created"] = merge.get("indexes_created", 0)
        result["planned_actions"].extend(merge.get("planned_actions", []))

        return result

    def _resolve_project_path(self, project: str | None) -> Path | None:
        """Best-effort resolve a project name to a directory path."""
        if not project:
            return None
        # Try common locations
        for base in [Path.home() / "Projects", Path.home() / "src", Path.home()]:
            candidate = base / project
            if candidate.is_dir():
                return candidate
        return None

    def _check_staleness(self, docs: list[dict], dry_run: bool = False) -> dict:
        """Sub-check 1: Detect docs with stale code_sigs."""
        result = {"stale_detected": 0, "planned_actions": []}

        for doc in docs:
            if doc.get("priority") == "P0":
                continue
            sigs = json.loads(doc.get("code_sigs") or "[]")
            if not sigs:
                continue

            project_path = self._resolve_project_path(doc.get("project"))
            if not project_path:
                continue

            alive = 0
            for sig in sigs:
                sig_clean = sig.strip()
                if not sig_clean or len(sig_clean) < 4:
                    alive += 1  # too short to be meaningful, skip
                    continue
                try:
                    cp = subprocess.run(
                        ["grep", "-rq", "--include=*.py", "--include=*.ts",
                         "--include=*.js", "--include=*.go", "--include=*.rs",
                         sig_clean, str(project_path)],
                        capture_output=True, timeout=5,
                    )
                    if cp.returncode == 0:
                        alive += 1
                except (subprocess.TimeoutExpired, OSError):
                    alive += 1  # assume alive on error

            total = len(sigs)
            if total == 0:
                continue
            score = alive / total

            if score < 0.5:
                action = {
                    "type": "stale_downgrade",
                    "doc_id": doc["doc_id"],
                    "title": doc.get("title", ""),
                    "alive_ratio": f"{alive}/{total}",
                    "priority": doc.get("priority"),
                }
                result["planned_actions"].append(action)
                result["stale_detected"] += 1

                if not dry_run:
                    now = time.time()
                    if doc.get("priority") == "P1":
                        new_expires = now + 7 * 86400  # 7-day grace
                    else:
                        new_expires = now  # immediate prune candidate
                    self._execute_write(
                        "UPDATE documents SET expires_at=? WHERE doc_id=?",
                        (new_expires, doc["doc_id"]),
                    )
                    self._log_consolidation(
                        project=doc.get("project") or "_default",
                        action="stale_downgrade",
                        facts_merged=0,
                        contradictions_resolved=0,
                        sessions_count=0,
                        phase="health_check",
                    )
                    logger.info("Stale: %s — %d/%d sigs dead",
                                doc.get("title", "")[:50], total - alive, total)

        return result

    def _check_cross_contradictions(self, docs: list[dict], dry_run: bool = False) -> dict:
        """Sub-check 2: Detect contradictions across ALL documents."""
        result = {"cross_contradictions_resolved": 0, "planned_actions": []}

        # Group docs by project
        by_project: dict[str, list[dict]] = {}
        for doc in docs:
            proj = doc.get("project") or "_default"
            by_project.setdefault(proj, []).append(doc)

        for project, project_docs in by_project.items():
            # Build (doc_id, fact, created_at) tuples
            fact_entries: list[tuple[int, str, float]] = []
            for doc in project_docs:
                facts = json.loads(doc.get("key_facts") or "[]")
                created = doc.get("created_at") or 0
                for fact in facts:
                    fact_entries.append((doc["doc_id"], fact, created))

            # Check all pairs for contradictions (same key, different value)
            resolved = []
            for i in range(len(fact_entries)):
                for j in range(i + 1, len(fact_entries)):
                    doc_a, fact_a, ts_a = fact_entries[i]
                    doc_b, fact_b, ts_b = fact_entries[j]
                    if doc_a == doc_b:
                        continue  # skip intra-document (already handled)
                    a_lower, b_lower = fact_a.lower(), fact_b.lower()
                    if ":" not in a_lower or ":" not in b_lower:
                        continue
                    key_a = a_lower.split(":")[0].strip()
                    key_b = b_lower.split(":")[0].strip()
                    if _fuzzy_match(key_a, key_b) > 0.8:
                        val_a = a_lower.split(":", 1)[1].strip()
                        val_b = b_lower.split(":", 1)[1].strip()
                        if val_a != val_b:
                            # Keep newer, remove older
                            if ts_a >= ts_b:
                                old_doc, old_fact = doc_b, fact_b
                            else:
                                old_doc, old_fact = doc_a, fact_a
                            resolved.append((old_doc, old_fact, fact_a, fact_b))

            for old_doc_id, old_fact, fact_a, fact_b in resolved:
                action = {
                    "type": "cross_contradiction",
                    "doc_id": old_doc_id,
                    "removed_fact": old_fact[:80],
                    "conflicting_facts": [fact_a[:80], fact_b[:80]],
                }
                result["planned_actions"].append(action)
                result["cross_contradictions_resolved"] += 1

                if not dry_run:
                    # Remove old fact from its document
                    row = self._conn.execute(
                        "SELECT key_facts FROM documents WHERE doc_id=?",
                        (old_doc_id,),
                    ).fetchone()
                    if row:
                        facts = json.loads(row["key_facts"] or "[]")
                        if old_fact in facts:
                            facts.remove(old_fact)
                            self._execute_write(
                                "UPDATE documents SET key_facts=? WHERE doc_id=?",
                                (json.dumps(facts), old_doc_id),
                            )
                    logger.info("Cross-contradiction resolved: removed '%s' from doc %d",
                                old_fact[:50], old_doc_id)

            if resolved and not dry_run:
                self._log_consolidation(
                    project=project,
                    action="cross_contradiction",
                    facts_merged=0,
                    contradictions_resolved=len(resolved),
                    sessions_count=0,
                    phase="health_check",
                )

        return result

    def _merge_redundant(self, docs: list[dict], dry_run: bool = False) -> dict:
        """Sub-check 3: Merge docs with >70% fact overlap + generate concept indexes."""
        result = {"redundant_merged": 0, "indexes_created": 0, "planned_actions": []}
        deleted_ids: set[int] = set()

        # Group by project
        by_project: dict[str, list[dict]] = {}
        for doc in docs:
            proj = doc.get("project") or "_default"
            by_project.setdefault(proj, []).append(doc)

        for project, project_docs in by_project.items():
            # Track clusters for concept index generation
            clusters: dict[int, list[int]] = {}  # target_id -> [source_ids]

            for i in range(len(project_docs)):
                if project_docs[i]["doc_id"] in deleted_ids:
                    continue
                for j in range(i + 1, len(project_docs)):
                    if project_docs[j]["doc_id"] in deleted_ids:
                        continue

                    doc_a = project_docs[i]
                    doc_b = project_docs[j]

                    facts_a = set(json.loads(doc_a.get("key_facts") or "[]"))
                    facts_b = set(json.loads(doc_b.get("key_facts") or "[]"))

                    if not facts_a or not facts_b:
                        continue

                    # Fuzzy Jaccard: count fuzzy matches
                    matches = 0
                    for fa in facts_a:
                        for fb in facts_b:
                            if _fuzzy_match(fa, fb) > 0.85:
                                matches += 1
                                break
                    union_size = len(facts_a) + len(facts_b) - matches
                    if union_size == 0:
                        continue
                    overlap = matches / union_size

                    if overlap < 0.70:
                        continue

                    # Determine target (more facts, or higher priority)
                    prio_rank = {"P0": 3, "P1": 2, "P2": 1}
                    a_rank = prio_rank.get(doc_a.get("priority"), 0)
                    b_rank = prio_rank.get(doc_b.get("priority"), 0)

                    if a_rank > b_rank or (a_rank == b_rank and len(facts_a) >= len(facts_b)):
                        target, source = doc_a, doc_b
                        target_facts, source_facts = facts_a, facts_b
                    else:
                        target, source = doc_b, doc_a
                        target_facts, source_facts = facts_b, facts_a

                    # Find unique facts/decisions from source
                    unique_facts = []
                    for sf in source_facts:
                        is_dup = any(_fuzzy_match(sf, tf) > 0.85 for tf in target_facts)
                        if not is_dup:
                            unique_facts.append(sf)

                    source_decisions = set(json.loads(source.get("decisions") or "[]"))
                    target_decisions = set(json.loads(target.get("decisions") or "[]"))
                    unique_decisions = []
                    for sd in source_decisions:
                        is_dup = any(_fuzzy_match(sd, td) > 0.85 for td in target_decisions)
                        if not is_dup:
                            unique_decisions.append(sd)

                    action = {
                        "type": "redundancy_merge",
                        "source_id": source["doc_id"],
                        "source_title": source.get("title", "")[:60],
                        "target_id": target["doc_id"],
                        "target_title": target.get("title", "")[:60],
                        "overlap": f"{overlap:.0%}",
                        "unique_facts_transferred": len(unique_facts),
                    }
                    result["planned_actions"].append(action)
                    result["redundant_merged"] += 1

                    # Track cluster
                    tid = target["doc_id"]
                    clusters.setdefault(tid, []).append(source["doc_id"])

                    if not dry_run:
                        # Append unique facts/decisions to target
                        merged_facts = list(target_facts) + unique_facts
                        merged_decisions = list(target_decisions) + unique_decisions
                        self._execute_write(
                            "UPDATE documents SET key_facts=?, decisions=? WHERE doc_id=?",
                            (json.dumps(merged_facts), json.dumps(merged_decisions),
                             target["doc_id"]),
                        )
                        # Delete source + cascade
                        self._execute_write(
                            "DELETE FROM documents WHERE doc_id=?",
                            (source["doc_id"],),
                        )
                        try:
                            self._execute_write(
                                "DELETE FROM vec_documents WHERE document_id=?",
                                (source["doc_id"],),
                            )
                        except Exception:
                            pass
                        try:
                            self._execute_write(
                                "DELETE FROM doc_relations WHERE source_id=? OR target_id=?",
                                (source["doc_id"], source["doc_id"]),
                            )
                        except Exception:
                            pass
                        logger.info("Merged: %s → %s",
                                    source.get("title", "")[:40],
                                    target.get("title", "")[:40])

                    deleted_ids.add(source["doc_id"])

            # Generate concept indexes for clusters with 3+ members
            for target_id, source_ids in clusters.items():
                if len(source_ids) < 2:  # target + 2 sources = 3 total
                    continue

                target_doc = next((d for d in project_docs if d["doc_id"] == target_id), None)
                if not target_doc:
                    continue

                target_title = target_doc.get("title", "unknown")
                safe_topic = target_title.lower().replace(" ", "-")[:40]
                file_path = f"dream:index:{safe_topic}"

                # Build index content
                target_facts = json.loads(target_doc.get("key_facts") or "[]")
                index_parts = [
                    f"# Index: {target_title}",
                    f"Cluster of {len(source_ids) + 1} related documents consolidated into [{target_title}].",
                    "## Key Themes",
                ]
                for fact in target_facts[:5]:
                    index_parts.append(f"- {fact}")

                content = "\n\n".join(index_parts)
                expires_at = time.time() + 30 * 86400  # 30d TTL

                if not dry_run:
                    existing = self._conn.execute(
                        "SELECT doc_id FROM documents WHERE file_path=?", (file_path,)
                    ).fetchone()
                    if existing:
                        self._execute_write(
                            """UPDATE documents SET title=?, summary=?, key_facts=?,
                               raw_content=?, expires_at=? WHERE doc_id=?""",
                            (f"Index: {target_title}",
                             f"Concept index for {len(source_ids) + 1} related documents",
                             json.dumps(target_facts[:5]), content, expires_at,
                             existing["doc_id"]),
                        )
                    else:
                        self._execute_write(
                            """INSERT INTO documents (title, summary, key_facts, decisions,
                               raw_content, priority, source, generator, file_path,
                               expires_at, project)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (f"Index: {target_title}",
                             f"Concept index for {len(source_ids) + 1} related documents",
                             json.dumps(target_facts[:5]), "[]", content,
                             "P2", "dream", "dream", file_path, expires_at,
                             project if project != "_default" else None),
                        )
                    result["indexes_created"] += 1

                result["planned_actions"].append({
                    "type": "concept_index",
                    "topic": target_title,
                    "cluster_size": len(source_ids) + 1,
                })

            if not dry_run and (result["redundant_merged"] > 0 or result["indexes_created"] > 0):
                self._log_consolidation(
                    project=project,
                    action="redundancy_merge",
                    facts_merged=result["redundant_merged"],
                    contradictions_resolved=0,
                    sessions_count=0,
                    phase="health_check",
                )

        return result

    # --- Phase 4: Prune ---

    def _prune(self) -> int:
        """Remove expired documents and deduplicate dream documents."""
        now = time.time()
        expired = self._conn.execute(
            "SELECT doc_id FROM documents WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        ).fetchall()
        expired_ids = [r[0] for r in expired]
        if expired_ids:
            placeholders = ",".join("?" * len(expired_ids))
            self._execute_write(
                f"DELETE FROM documents WHERE doc_id IN ({placeholders})",
                expired_ids,
            )
            # Cascade to vec_documents
            try:
                self._execute_write(
                    f"DELETE FROM vec_documents WHERE document_id IN ({placeholders})",
                    expired_ids,
                )
            except Exception:
                pass
        return len(expired_ids)
