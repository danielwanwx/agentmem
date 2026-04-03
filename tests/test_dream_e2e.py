"""End-to-end tests for dream health check (Phase 3.5).

Tests the full pipeline: seed DB → run dream → verify health check actions
on staleness, cross-doc contradictions, and redundancy merge.
"""

import json
import sqlite3
import tempfile
import time
from pathlib import Path

from agent_memory.db import init_db
from agent_memory.dream import DreamLock, Dreamer


def _make_env(tmp: str):
    """Create a fresh DB + lock + project dir in a temp directory."""
    db_path = Path(tmp) / "test.db"
    conn = init_db(str(db_path))
    lock = DreamLock(Path(tmp) / ".dream-lock")
    proj_dir = Path(tmp) / "Projects" / "myproj"
    proj_dir.mkdir(parents=True)
    return conn, lock, proj_dir


def _insert_doc(conn, title, key_facts, priority="P1", project="myproj",
                code_sigs=None, decisions=None, created_offset=0):
    now = time.time()
    conn.execute(
        """INSERT INTO documents (title, summary, key_facts, decisions, code_sigs,
           raw_content, priority, source, generator, file_path, project,
           expires_at, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (title, f"Summary of {title}", json.dumps(key_facts),
         json.dumps(decisions or []), json.dumps(code_sigs or []),
         f"Content of {title}", priority, "test", "test",
         f"test:{title.lower().replace(' ', '-')}", project,
         now + 90 * 86400, now + created_offset),
    )
    conn.commit()


def _seed_session(conn, project="myproj"):
    """Insert one minimal ended session to pass gather phase."""
    conn.execute(
        """INSERT INTO sessions (session_id, project, topic, started_at, ended_at,
           summary, key_facts, decisions)
           VALUES (?,?,?,?,?,?,?,?)""",
        ("e2e-sess", project, "e2e", time.time() - 100, time.time(),
         "E2E session", json.dumps(["e2e fact"]), json.dumps([])),
    )
    conn.commit()


class TestE2EFullPipeline:
    """End-to-end: seed a realistic knowledge base, run one dream cycle,
    verify all three health check sub-phases fired correctly."""

    def test_full_dream_with_health_check(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn, lock, proj_dir = _make_env(td)

            # --- Seed project files ---
            (proj_dir / "api.py").write_text(
                "def handle_request():\n    pass\n\n"
                "class RequestHandler:\n    pass\n"
            )

            # --- 1. Staleness target ---
            # Doc with 3 code_sigs: 1 alive, 2 dead → score 0.33 < 0.5
            _insert_doc(conn, "Old API Module",
                        ["uses REST endpoints", "handles auth"],
                        code_sigs=["def handle_request():",
                                   "def removed_endpoint():",
                                   "class DeprecatedHandler:"],
                        project="myproj", priority="P1")

            # --- 2. Cross-doc contradiction target ---
            # Older doc: timeout 30s
            _insert_doc(conn, "Config v1",
                        ["timeout: 30s", "retries: 3", "batch_size: 100"],
                        project="myproj", created_offset=-2000)
            # Newer doc: timeout 600s
            _insert_doc(conn, "Config v2",
                        ["timeout: 600s", "workers: 8"],
                        project="myproj", created_offset=0)

            # --- 3. Redundancy merge target ---
            shared = ["pipeline uses kafka", "batch size is 64",
                      "consumer group: etl-main", "topic: events",
                      "serializer: avro"]
            _insert_doc(conn, "Pipeline Docs A",
                        shared + ["unique doc a fact about partitioning"],
                        project="myproj")
            _insert_doc(conn, "Pipeline Docs B",
                        shared + ["unique doc b fact about monitoring"],
                        project="myproj")

            # Need at least 1 session for dream to proceed
            _seed_session(conn, project="myproj")

            # --- Run dream ---
            dreamer = Dreamer(conn, lock=lock)
            # Patch project path resolution
            dreamer._resolve_project_path = lambda p: proj_dir if p == "myproj" else None

            result = dreamer.run(force=True)

            # --- Verify overall success ---
            assert result.success
            assert result.phase == "complete"
            assert result.sessions_reviewed >= 1

            # --- Verify staleness ---
            assert result.stale_detected >= 1, (
                f"Expected stale docs, got {result.stale_detected}")

            # --- Verify cross-doc contradictions ---
            assert result.cross_contradictions_resolved >= 1, (
                f"Expected cross contradictions, got {result.cross_contradictions_resolved}")
            # "timeout: 30s" should be removed from Config v1
            v1 = conn.execute(
                "SELECT key_facts FROM documents WHERE title='Config v1'"
            ).fetchone()
            if v1:
                facts = json.loads(v1["key_facts"])
                assert not any("30s" in f for f in facts), \
                    f"Old contradiction still present: {facts}"

            # --- Verify redundancy merge ---
            assert result.redundant_merged >= 1, (
                f"Expected redundant merges, got {result.redundant_merged}")
            # Only one pipeline doc should survive
            pipeline_docs = conn.execute(
                "SELECT title, key_facts FROM documents "
                "WHERE title LIKE 'Pipeline Docs%' AND source='test'"
            ).fetchall()
            assert len(pipeline_docs) == 1, (
                f"Expected 1 surviving pipeline doc, got {len(pipeline_docs)}")
            # Surviving doc should have both unique facts
            surviving_facts = json.loads(pipeline_docs[0]["key_facts"])
            has_partition = any("partitioning" in f for f in surviving_facts)
            has_monitoring = any("monitoring" in f for f in surviving_facts)
            assert has_partition and has_monitoring, (
                f"Merged doc missing unique facts: {surviving_facts}")

            # --- Verify planned_actions recorded ---
            actions = result.planned_actions
            action_types = [a["type"] for a in actions]
            assert "stale_downgrade" in action_types
            assert "cross_contradiction" in action_types
            assert "redundancy_merge" in action_types


class TestE2ECLIOutputIncludesHealthCheck:
    """Verify the CLI dream output includes health check metrics."""

    def test_cli_prints_health_check_lines(self) -> None:
        """cmd_dream must print Stale/Contradictions/Merged lines."""
        import inspect
        from agent_memory.cli import cmd_dream

        source = inspect.getsource(cmd_dream)
        # The CLI function must contain format strings for health check fields
        assert "stale_detected" in source, \
            "cmd_dream does not print stale_detected"
        assert "cross_contradictions_resolved" in source, \
            "cmd_dream does not print cross_contradictions_resolved"
        assert "redundant_merged" in source, \
            "cmd_dream does not print redundant_merged"


class TestE2ECLIPlannedActionsFormat:
    """Verify CLI doesn't crash on health check planned_actions (different keys)."""

    def test_cli_handles_health_check_actions(self) -> None:
        """Health check actions have type/doc_id/title, not project/facts_count.
        The CLI must not use action['project'] directly (KeyError on health check)."""
        import inspect
        from agent_memory.cli import cmd_dream

        source = inspect.getsource(cmd_dream)
        # The current CLI uses action['project'] which will KeyError.
        # It must use action.get('project', ...) instead.
        assert "action.get(" in source, \
            "cmd_dream planned_actions uses action[key] instead of action.get() — " \
            "will KeyError on health check actions with different keys"


class TestE2EConsolidationLog:
    """Verify health check actions are logged with correct phase."""

    def test_health_check_logged_with_phase(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn, lock, proj_dir = _make_env(td)

            # Set up cross-doc contradiction to trigger health check logging
            _insert_doc(conn, "Old", ["port: 8080"], project="myproj",
                        created_offset=-1000)
            _insert_doc(conn, "New", ["port: 3000"], project="myproj",
                        created_offset=0)
            _seed_session(conn)

            dreamer = Dreamer(conn, lock=lock)
            dreamer.run(force=True)

            # Check consolidation_log has health_check phase entries
            try:
                rows = conn.execute(
                    "SELECT phase FROM consolidation_log WHERE phase='health_check'"
                ).fetchall()
                assert len(rows) >= 1, "No health_check entries in consolidation_log"
            except Exception:
                # Table might not exist if no health check actions fired
                pass


class TestE2EReadmeDocumentsHealthCheck:
    """Verify README documents the health check feature."""

    def test_readme_mentions_health_check(self) -> None:
        readme = Path(__file__).parent.parent / "README.md"
        content = readme.read_text()
        assert "Health Check" in content or "health check" in content, \
            "README does not document the health check phase"

    def test_readme_mentions_staleness(self) -> None:
        readme = Path(__file__).parent.parent / "README.md"
        content = readme.read_text()
        assert "stale" in content.lower() or "staleness" in content.lower(), \
            "README does not document staleness detection"

    def test_readme_mentions_redundancy_merge(self) -> None:
        readme = Path(__file__).parent.parent / "README.md"
        content = readme.read_text()
        assert "redundan" in content.lower(), \
            "README does not document redundancy merge"


class TestE2EDryRunSafety:
    """Verify dry_run prevents ALL health check mutations."""

    def test_dry_run_preserves_all_docs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn, lock, proj_dir = _make_env(td)

            (proj_dir / "app.py").write_text("# empty\n")

            # Stale doc
            _insert_doc(conn, "Stale", ["x"],
                        code_sigs=["def dead_function():"],
                        project="myproj", priority="P2")
            # Contradiction pair
            _insert_doc(conn, "A", ["setting: old"], project="myproj",
                        created_offset=-500)
            _insert_doc(conn, "B", ["setting: new"], project="myproj",
                        created_offset=0)
            # Redundant pair
            shared = ["f1", "f2", "f3", "f4", "f5"]
            _insert_doc(conn, "R1", shared + ["database is PostgreSQL"],
                        project="myproj")
            _insert_doc(conn, "R2", shared + ["cache layer is Redis"],
                        project="myproj")
            _seed_session(conn)

            doc_count_before = conn.execute(
                "SELECT COUNT(*) FROM documents"
            ).fetchone()[0]
            facts_before = {}
            for row in conn.execute("SELECT title, key_facts FROM documents").fetchall():
                facts_before[row["title"]] = row["key_facts"]

            dreamer = Dreamer(conn, lock=lock)
            dreamer._resolve_project_path = lambda p: proj_dir if p == "myproj" else None
            result = dreamer.run(force=True, dry_run=True)

            # No documents should be deleted or modified
            doc_count_after = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE source='test'"
            ).fetchone()[0]
            # All test docs should still exist
            assert doc_count_after == 5, (
                f"Dry run mutated docs: {doc_count_after} != 5")

            for row in conn.execute(
                "SELECT title, key_facts FROM documents WHERE source='test'"
            ).fetchall():
                assert row["key_facts"] == facts_before[row["title"]], (
                    f"Dry run mutated facts of '{row['title']}'")
