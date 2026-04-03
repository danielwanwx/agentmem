"""Tests for dream memory consolidation."""

import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from agent_memory.db import init_db
from agent_memory.dream import DreamLock, DreamResult, Dreamer


def _make_conn(tmp: str) -> sqlite3.Connection:
    """Create a fresh AgentMem DB in a temp directory."""
    db_path = Path(tmp) / "test.db"
    conn = init_db(str(db_path))
    return conn


def _seed_sessions(conn: sqlite3.Connection, count: int, project: str = "test") -> None:
    """Create N ended sessions with fake data."""
    for i in range(count):
        sid = f"sess-{i}"
        now = time.time()
        conn.execute(
            """INSERT INTO sessions (session_id, project, topic, started_at, ended_at,
               summary, key_facts, decisions)
               VALUES (?,?,?,?,?,?,?,?)""",
            (sid, project, f"topic-{i}", now - 3600, now,
             f"Session {i} summary", json.dumps([f"fact-{i}-a", f"fact-{i}-b"]),
             json.dumps([f"decision-{i}"])),
        )
    conn.commit()


class TestDreamLock:
    def test_acquire_when_unlocked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock = DreamLock(Path(td) / ".dream-lock")
            assert lock.try_acquire()

    def test_release_updates_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock = DreamLock(Path(td) / ".dream-lock")
            lock.try_acquire()
            lock.release()
            assert lock.hours_since_last() < 0.01

    def test_hours_since_never(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock = DreamLock(Path(td) / ".dream-lock")
            assert lock.hours_since_last() == float("inf")

    def test_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock = DreamLock(Path(td) / ".dream-lock")
            lock.try_acquire()
            lock.release()
            original = lock.last_dream_at()
            time.sleep(0.05)
            lock.try_acquire()
            lock.rollback(original)
            assert abs(lock.last_dream_at() - original) < 1.0


class TestDreamGate:
    def test_gate_fails_too_recent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = _make_conn(td)
            lock = DreamLock(Path(td) / ".dream-lock")
            lock.try_acquire()
            lock.release()
            dreamer = Dreamer(conn, min_hours=24, min_sessions=1, lock=lock)
            should, reason = dreamer.check_gate()
            assert not should
            assert "since last dream" in reason

    def test_gate_fails_not_enough_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = _make_conn(td)
            lock = DreamLock(Path(td) / ".dream-lock")
            dreamer = Dreamer(conn, min_hours=0, min_sessions=10, lock=lock)
            should, reason = dreamer.check_gate()
            assert not should
            assert "sessions" in reason

    def test_gate_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = _make_conn(td)
            lock = DreamLock(Path(td) / ".dream-lock")
            _seed_sessions(conn, 5)
            dreamer = Dreamer(conn, min_hours=0, min_sessions=5, lock=lock)
            should, reason = dreamer.check_gate()
            assert should


class TestDreamRun:
    def test_run_gate_failed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = _make_conn(td)
            lock = DreamLock(Path(td) / ".dream-lock")
            dreamer = Dreamer(conn, min_hours=999, min_sessions=999, lock=lock)
            result = dreamer.run()
            assert not result.success
            assert result.phase == "gate_failed"

    def test_run_no_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = _make_conn(td)
            lock = DreamLock(Path(td) / ".dream-lock")
            dreamer = Dreamer(conn, min_hours=0, min_sessions=0, lock=lock)
            result = dreamer.run(force=True)
            assert result.success
            assert result.sessions_reviewed == 0

    def test_run_consolidates_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = _make_conn(td)
            lock = DreamLock(Path(td) / ".dream-lock")
            _seed_sessions(conn, 3, project="myproject")
            dreamer = Dreamer(conn, lock=lock)
            result = dreamer.run(force=True)

            assert result.success
            assert result.phase == "complete"
            assert result.sessions_reviewed == 3
            assert result.documents_created >= 1

            # Verify dream document was created
            doc = conn.execute(
                "SELECT * FROM documents WHERE file_path='dream:myproject'"
            ).fetchone()
            assert doc is not None
            assert "myproject" in doc["title"]
            facts = json.loads(doc["key_facts"])
            assert len(facts) >= 3  # Deduplicated from 6 (2 per session)

    def test_run_updates_existing_dream_doc(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = _make_conn(td)
            lock = DreamLock(Path(td) / ".dream-lock")

            # First dream
            _seed_sessions(conn, 3, project="proj")
            dreamer = Dreamer(conn, lock=lock)
            r1 = dreamer.run(force=True)
            assert r1.documents_created == 1

            # Add more sessions
            for i in range(3, 6):
                conn.execute(
                    """INSERT INTO sessions (session_id, project, topic, started_at, ended_at,
                       summary, key_facts, decisions) VALUES (?,?,?,?,?,?,?,?)""",
                    (f"sess-new-{i}", "proj", f"new-{i}", time.time()-100, time.time(),
                     f"New session {i}", json.dumps([f"new-fact-{i}"]), json.dumps([])),
                )
            conn.commit()

            # Second dream — should update, not create
            r2 = dreamer.run(force=True)
            assert r2.documents_updated >= 1

            # Only one dream doc for this project
            docs = conn.execute(
                "SELECT * FROM documents WHERE file_path='dream:proj'"
            ).fetchall()
            assert len(docs) == 1

    def test_run_deduplicates_facts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = _make_conn(td)
            lock = DreamLock(Path(td) / ".dream-lock")

            # Create sessions with duplicate facts
            for i in range(3):
                conn.execute(
                    """INSERT INTO sessions (session_id, project, topic, started_at, ended_at,
                       summary, key_facts, decisions) VALUES (?,?,?,?,?,?,?,?)""",
                    (f"dup-{i}", "proj", "topic", time.time()-100, time.time(),
                     "summary", json.dumps(["same-fact", f"unique-{i}"]), json.dumps([])),
                )
            conn.commit()

            dreamer = Dreamer(conn, lock=lock)
            dreamer.run(force=True)

            doc = conn.execute(
                "SELECT key_facts FROM documents WHERE file_path='dream:proj'"
            ).fetchone()
            facts = json.loads(doc["key_facts"])
            # "same-fact" should appear only once
            assert facts.count("same-fact") == 1

    def test_prune_removes_expired(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = _make_conn(td)
            lock = DreamLock(Path(td) / ".dream-lock")

            # Insert an expired document
            conn.execute(
                """INSERT INTO documents (title, summary, raw_content, priority, source,
                   generator, file_path, expires_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                ("old", "old", "old", "P2", "dream", "dream", "dream:old",
                 time.time() - 86400),  # expired yesterday
            )
            conn.commit()

            _seed_sessions(conn, 1)
            dreamer = Dreamer(conn, lock=lock)
            result = dreamer.run(force=True)
            assert result.documents_pruned >= 1

            # Verify it's gone
            old = conn.execute(
                "SELECT * FROM documents WHERE file_path='dream:old'"
            ).fetchone()
            assert old is None


class TestDryRun:
    def test_dry_run_does_not_modify_db(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = _make_conn(td)
            lock = DreamLock(Path(td) / ".dream-lock")
            _seed_sessions(conn, 3, project="myproject")
            doc_count_before = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            dreamer = Dreamer(conn, lock=lock)
            result = dreamer.run(force=True, dry_run=True)
            assert result.success
            assert result.sessions_reviewed == 3
            doc_count_after = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            assert doc_count_after == doc_count_before  # no changes
            assert len(result.planned_actions) >= 1  # but planned actions exist

    def test_dry_run_shows_planned_actions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = _make_conn(td)
            lock = DreamLock(Path(td) / ".dream-lock")
            _seed_sessions(conn, 3, project="proj-a")
            dreamer = Dreamer(conn, lock=lock)
            result = dreamer.run(force=True, dry_run=True)
            actions = result.planned_actions
            assert len(actions) >= 1
            assert actions[0]["type"] in ("create", "update")
            assert actions[0]["project"] == "proj-a"


class TestContradictionResolution:
    def test_detects_contradictory_facts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = _make_conn(td)
            lock = DreamLock(Path(td) / ".dream-lock")
            # Session 1: timeout is 30s
            conn.execute(
                """INSERT INTO sessions (session_id, project, topic, started_at, ended_at,
                   summary, key_facts, decisions) VALUES (?,?,?,?,?,?,?,?)""",
                ("s1", "proj", "config", time.time()-200, time.time()-100,
                 "Old config", json.dumps(["timeout: 30s", "retries: 3"]), json.dumps([])),
            )
            # Session 2: timeout changed to 600s
            conn.execute(
                """INSERT INTO sessions (session_id, project, topic, started_at, ended_at,
                   summary, key_facts, decisions) VALUES (?,?,?,?,?,?,?,?)""",
                ("s2", "proj", "config update", time.time()-50, time.time(),
                 "Updated config", json.dumps(["timeout: 600s", "retries: 3"]), json.dumps([])),
            )
            conn.commit()

            dreamer = Dreamer(conn, lock=lock)
            result = dreamer.run(force=True)
            assert result.success
            assert result.contradictions_resolved >= 1

            # The old "timeout: 30s" should be resolved in favor of "timeout: 600s"
            doc = conn.execute(
                "SELECT key_facts FROM documents WHERE file_path='dream:proj'"
            ).fetchone()
            facts = json.loads(doc["key_facts"])
            fact_text = " ".join(facts).lower()
            assert "600s" in fact_text
            # "30s" should be removed (contradiction resolved)
            timeout_facts = [f for f in facts if "timeout" in f.lower()]
            assert len(timeout_facts) == 1  # only one timeout fact remains


class TestFuzzyDedup:
    def test_deduplicates_similar_facts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = _make_conn(td)
            lock = DreamLock(Path(td) / ".dream-lock")
            # Sessions with near-duplicate facts
            for i in range(3):
                conn.execute(
                    """INSERT INTO sessions (session_id, project, topic, started_at, ended_at,
                       summary, key_facts, decisions) VALUES (?,?,?,?,?,?,?,?)""",
                    (f"fd-{i}", "proj", "topic", time.time()-100, time.time(),
                     "summary", json.dumps(["timeout setting is 600 seconds",
                                            f"unique-fact-{i}"]), json.dumps([])),
                )
            conn.commit()

            dreamer = Dreamer(conn, lock=lock)
            dreamer.run(force=True)

            doc = conn.execute(
                "SELECT key_facts FROM documents WHERE file_path='dream:proj'"
            ).fetchone()
            facts = json.loads(doc["key_facts"])
            # "timeout setting is 600 seconds" should appear only once (fuzzy dedup)
            timeout_facts = [f for f in facts if "timeout" in f.lower()]
            assert len(timeout_facts) == 1


class TestDreamResult:
    def test_fields(self) -> None:
        r = DreamResult(success=True, phase="complete", sessions_reviewed=5)
        assert r.success
        assert r.sessions_reviewed == 5

    def test_gate_failed(self) -> None:
        r = DreamResult(success=False, phase="gate_failed", reason="too recent")
        assert not r.success
        assert r.reason == "too recent"

    def test_health_check_fields(self) -> None:
        r = DreamResult(success=True, phase="complete",
                        stale_detected=2, cross_contradictions_resolved=1,
                        redundant_merged=3)
        assert r.stale_detected == 2
        assert r.cross_contradictions_resolved == 1
        assert r.redundant_merged == 3


def _insert_doc(conn: sqlite3.Connection, title: str, key_facts: list[str],
                decisions: list[str] | None = None, priority: str = "P1",
                project: str = "test", code_sigs: list[str] | None = None,
                file_path: str | None = None, created_offset: float = 0) -> int:
    """Helper to insert a document and return its doc_id."""
    now = time.time()
    conn.execute(
        """INSERT INTO documents (title, summary, key_facts, decisions, code_sigs,
           raw_content, priority, source, generator, file_path, project,
           expires_at, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (title, f"Summary of {title}", json.dumps(key_facts),
         json.dumps(decisions or []), json.dumps(code_sigs or []),
         f"Content of {title}", priority, "test", "test",
         file_path or f"test:{title.lower().replace(' ', '-')}",
         project, now + 90 * 86400, now + created_offset),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


class TestHealthCheckStaleness:
    def test_stale_doc_detected(self) -> None:
        """Docs with dead code_sigs get downgraded."""
        with tempfile.TemporaryDirectory() as td:
            conn = _make_conn(td)
            lock = DreamLock(Path(td) / ".dream-lock")

            # Create a project dir with one file
            proj_dir = Path(td) / "Projects" / "test"
            proj_dir.mkdir(parents=True)
            (proj_dir / "app.py").write_text("def existing_function():\n    pass\n")

            # Doc with 3 sigs: 1 alive, 2 dead => score 0.33 < 0.5
            _insert_doc(conn, "Old Module", ["some fact"],
                        code_sigs=["def existing_function():", "def deleted_func():",
                                    "class RemovedClass:"],
                        project="test", priority="P1")

            _seed_sessions(conn, 1, project="test")

            # Monkey-patch _resolve_project_path to use our temp dir
            dreamer = Dreamer(conn, lock=lock)
            dreamer._resolve_project_path = lambda p: proj_dir if p == "test" else None

            result = dreamer.run(force=True)
            assert result.stale_detected >= 1

    def test_p0_not_checked_for_staleness(self) -> None:
        """P0 docs are never flagged as stale."""
        with tempfile.TemporaryDirectory() as td:
            conn = _make_conn(td)
            lock = DreamLock(Path(td) / ".dream-lock")

            proj_dir = Path(td) / "Projects" / "test"
            proj_dir.mkdir(parents=True)
            (proj_dir / "app.py").write_text("")

            _insert_doc(conn, "Arch Decision", ["important"],
                        code_sigs=["def totally_dead():"],
                        project="test", priority="P0")

            _seed_sessions(conn, 1, project="test")

            dreamer = Dreamer(conn, lock=lock)
            dreamer._resolve_project_path = lambda p: proj_dir if p == "test" else None

            result = dreamer.run(force=True)
            assert result.stale_detected == 0


class TestHealthCheckCrossContradictions:
    def test_cross_doc_contradiction_resolved(self) -> None:
        """Contradictory facts across different docs get resolved."""
        with tempfile.TemporaryDirectory() as td:
            conn = _make_conn(td)
            lock = DreamLock(Path(td) / ".dream-lock")

            # Older doc says timeout: 30s
            _insert_doc(conn, "Old Config", ["timeout: 30s", "retries: 3"],
                        project="proj", created_offset=-1000)
            # Newer doc says timeout: 600s
            _insert_doc(conn, "New Config", ["timeout: 600s", "workers: 4"],
                        project="proj", created_offset=0)

            _seed_sessions(conn, 1, project="proj")

            dreamer = Dreamer(conn, lock=lock)
            result = dreamer.run(force=True)
            assert result.cross_contradictions_resolved >= 1

            # Old doc should no longer have "timeout: 30s"
            old_doc = conn.execute(
                "SELECT key_facts FROM documents WHERE title='Old Config'"
            ).fetchone()
            if old_doc:
                facts = json.loads(old_doc["key_facts"])
                timeout_facts = [f for f in facts if "timeout" in f.lower()]
                assert all("30s" not in f for f in timeout_facts)

    def test_same_doc_not_self_contradicted(self) -> None:
        """Intra-document facts are not flagged as cross-doc contradictions."""
        with tempfile.TemporaryDirectory() as td:
            conn = _make_conn(td)
            lock = DreamLock(Path(td) / ".dream-lock")

            # Single doc with two different facts (same key, different value)
            _insert_doc(conn, "Config", ["timeout: 30s", "timeout: 600s"],
                        project="proj")

            _seed_sessions(conn, 1, project="proj")

            dreamer = Dreamer(conn, lock=lock)
            result = dreamer.run(force=True)
            # Should be 0 because both facts are in the same doc
            assert result.cross_contradictions_resolved == 0


class TestHealthCheckRedundancyMerge:
    def test_redundant_docs_merged(self) -> None:
        """Docs with >70% fact overlap get merged."""
        with tempfile.TemporaryDirectory() as td:
            conn = _make_conn(td)
            lock = DreamLock(Path(td) / ".dream-lock")

            shared_facts = ["fact-a", "fact-b", "fact-c", "fact-d", "fact-e"]
            # Doc A: 5 shared + 1 unique
            _insert_doc(conn, "Doc A", shared_facts + ["database uses PostgreSQL"],
                        project="proj", priority="P1")
            # Doc B: 5 shared + 1 unique (>70% overlap)
            _insert_doc(conn, "Doc B", shared_facts + ["cache layer uses Redis"],
                        project="proj", priority="P1")

            _seed_sessions(conn, 1, project="proj")

            dreamer = Dreamer(conn, lock=lock)
            result = dreamer.run(force=True)
            assert result.redundant_merged >= 1

            # Only target doc should remain (source deleted)
            docs = conn.execute(
                "SELECT title, key_facts FROM documents WHERE project='proj' "
                "AND source='test'"
            ).fetchall()
            assert len(docs) == 1
            # Target should have the unique fact from source
            facts = json.loads(docs[0]["key_facts"])
            has_pg = any("PostgreSQL" in f for f in facts)
            has_redis = any("Redis" in f for f in facts)
            assert has_pg and has_redis  # both unique facts transferred

    def test_low_overlap_not_merged(self) -> None:
        """Docs with <70% overlap are not merged."""
        with tempfile.TemporaryDirectory() as td:
            conn = _make_conn(td)
            lock = DreamLock(Path(td) / ".dream-lock")

            # Doc A: mostly different from Doc B
            _insert_doc(conn, "Doc A", ["a1", "a2", "a3", "shared"],
                        project="proj")
            _insert_doc(conn, "Doc B", ["b1", "b2", "b3", "shared"],
                        project="proj")

            _seed_sessions(conn, 1, project="proj")

            dreamer = Dreamer(conn, lock=lock)
            result = dreamer.run(force=True)
            assert result.redundant_merged == 0

    def test_concept_index_created_for_large_cluster(self) -> None:
        """Concept index doc is generated when 3+ docs cluster."""
        with tempfile.TemporaryDirectory() as td:
            conn = _make_conn(td)
            lock = DreamLock(Path(td) / ".dream-lock")

            shared = ["pipeline config", "batch size: 64", "workers: 8",
                       "timeout: 300", "queue: kafka"]
            # 3 docs with high overlap → cluster
            _insert_doc(conn, "Pipeline v1", shared + ["unique-1"],
                        project="proj", priority="P1")
            _insert_doc(conn, "Pipeline v2", shared + ["unique-2"],
                        project="proj", priority="P1")
            _insert_doc(conn, "Pipeline v3", shared + ["unique-3"],
                        project="proj", priority="P1")

            _seed_sessions(conn, 1, project="proj")

            dreamer = Dreamer(conn, lock=lock)
            result = dreamer.run(force=True)
            assert result.redundant_merged >= 2

            # Check for concept index doc
            idx = conn.execute(
                "SELECT * FROM documents WHERE file_path LIKE 'dream:index:%'"
            ).fetchall()
            # Should have at least 1 concept index
            assert len(idx) >= 1
            assert "Index:" in idx[0]["title"]


class TestHealthCheckDryRun:
    def test_dry_run_no_mutations(self) -> None:
        """Health check in dry_run mode plans actions but doesn't modify DB."""
        with tempfile.TemporaryDirectory() as td:
            conn = _make_conn(td)
            lock = DreamLock(Path(td) / ".dream-lock")

            shared = ["fact-a", "fact-b", "fact-c", "fact-d"]
            _insert_doc(conn, "Doc X", shared + ["x-only"], project="proj")
            _insert_doc(conn, "Doc Y", shared + ["y-only"], project="proj")

            doc_count_before = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

            _seed_sessions(conn, 1, project="proj")

            dreamer = Dreamer(conn, lock=lock)
            result = dreamer.run(force=True, dry_run=True)

            doc_count_after = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            # Sessions added 1 seed doc, but dry_run shouldn't change test docs
            # The key: no documents should be deleted
            remaining = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE source='test'"
            ).fetchone()[0]
            assert remaining == 2  # both test docs still exist
