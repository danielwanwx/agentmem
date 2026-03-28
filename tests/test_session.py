"""Tests for SessionManager: lifecycle, messages, extraction."""
import json
import time


class TestSessionLifecycle:
    def test_start_returns_session_id(self, store):
        sid = store.session.start(project="myapp")
        assert isinstance(sid, str)
        assert len(sid) > 0

    def test_start_creates_session_record(self, store):
        sid = store.session.start(project="myapp", topic="debugging")
        row = store._conn.execute(
            "SELECT project, topic FROM sessions WHERE session_id=?", (sid,)
        ).fetchone()
        assert row["project"] == "myapp"
        assert row["topic"] == "debugging"

    def test_end_sets_ended_at(self, store):
        sid = store.session.start(project="myapp")
        store.session.end(sid, summary="Done")
        row = store._conn.execute(
            "SELECT ended_at, summary FROM sessions WHERE session_id=?", (sid,)
        ).fetchone()
        assert row["ended_at"] is not None
        assert row["summary"] == "Done"

    def test_start_closes_orphaned_sessions(self, store):
        sid1 = store.session.start(project="myapp")
        # Start a new session for same project without ending the first
        sid2 = store.session.start(project="myapp")
        row = store._conn.execute(
            "SELECT ended_at FROM sessions WHERE session_id=?", (sid1,)
        ).fetchone()
        assert row["ended_at"] is not None  # orphan was closed

    def test_delete_removes_session_and_messages(self, store):
        sid = store.session.start(project="myapp")
        store.session.save_message(sid, "user", "hello")
        store.session.delete(sid)
        sess = store._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (sid,)
        ).fetchone()
        msgs = store._conn.execute(
            "SELECT * FROM messages WHERE session_id=?", (sid,)
        ).fetchall()
        assert sess is None
        assert len(msgs) == 0


class TestMessages:
    def test_save_message(self, store):
        sid = store.session.start(project="myapp")
        store.session.save_message(sid, "user", "What is kafka?")
        store.session.save_message(sid, "assistant", "Kafka is a messaging system.")
        msgs = store.session.get_messages(sid)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"

    def test_get_messages_ordered(self, store):
        sid = store.session.start(project="myapp")
        store.session.save_message(sid, "user", "first")
        store.session.save_message(sid, "user", "second")
        msgs = store.session.get_messages(sid)
        assert msgs[0]["content"] == "first"
        assert msgs[1]["content"] == "second"


class TestResumeContext:
    def test_resume_context_with_summary(self, store):
        sid = store.session.start(project="myapp")
        store.session.save_message(sid, "user", "debug kafka")
        store.session.save_message(sid, "assistant", "checking logs")
        store.session.end(sid, summary="Debugged kafka rebalance",
                          key_facts=["timeout was 30s"], decisions=["increase to 600s"])
        ctx = store.session.get_resume_context(sid)
        assert ctx["summary"] == "Debugged kafka rebalance"
        assert "timeout was 30s" in ctx["key_facts"]
        assert len(ctx["recent_messages"]) == 2

    def test_resume_context_without_summary(self, store):
        sid = store.session.start(project="myapp")
        store.session.save_message(sid, "user", "hello")
        # Don't call end() — simulate orphaned session
        ctx = store.session.get_resume_context(sid)
        assert ctx["session_id"] == sid
        assert isinstance(ctx["recent_messages"], list)


class TestLatestSession:
    def test_get_latest_by_project(self, store):
        store.session.start(project="old")
        sid2 = store.session.start(project="myapp")
        latest = store.session.get_latest_session_id(project="myapp")
        assert latest == sid2

    def test_get_latest_returns_none_if_no_match(self, store):
        latest = store.session.get_latest_session_id(project="nonexistent")
        assert latest is None


class TestPromote:
    def test_session_end_promotes_to_documents(self, store):
        sid = store.session.start(project="myapp")
        store.session.save_message(sid, "user", "How should we handle auth?")
        store.session.save_message(sid, "assistant",
            "- **JWT**: use short-lived access tokens\n"
            "- **Refresh**: 7-day rotation\n"
            "Decision: Use JWT with refresh token rotation")
        store.session.end(sid)
        # Should have promoted to documents
        row = store._conn.execute(
            "SELECT * FROM documents WHERE file_path=?", (f"session:{sid}",)
        ).fetchone()
        assert row is not None
        assert row["source"] in ("session_extract", "session_note")

    def test_empty_session_not_promoted(self, store):
        sid = store.session.start(project="myapp")
        store.session.end(sid, summary="")
        row = store._conn.execute(
            "SELECT * FROM documents WHERE file_path=?", (f"session:{sid}",)
        ).fetchone()
        assert row is None

    def test_promote_upserts_on_re_promote(self, store):
        sid = store.session.start(project="myapp")
        store.session.save_message(sid, "assistant", "- **key1**: value1\n- **key2**: value2\n- **key3**: value3")
        store.session.end(sid, summary="First promote with enough content to pass gate")
        doc1 = store._conn.execute(
            "SELECT doc_id FROM documents WHERE file_path=?", (f"session:{sid}",)
        ).fetchone()
        # Re-promote should upsert same doc
        store.session._maybe_promote(sid)
        doc2 = store._conn.execute(
            "SELECT doc_id FROM documents WHERE file_path=?", (f"session:{sid}",)
        ).fetchone()
        assert doc1["doc_id"] == doc2["doc_id"]


class TestCheckpoint:
    def test_checkpoint_promotes_without_ending(self, store):
        sid = store.session.start(project="myapp")
        store.session.save_message(sid, "assistant",
            "- **timeout**: 600s\n- **retries**: 3\n- **backoff**: exponential\n"
            "Decision: Use exponential backoff")
        result = store.session.checkpoint(sid)
        # Session still open
        sess = store._conn.execute(
            "SELECT ended_at FROM sessions WHERE session_id=?", (sid,)
        ).fetchone()
        assert sess["ended_at"] is None
        # But knowledge promoted
        doc = store._conn.execute(
            "SELECT * FROM documents WHERE file_path=?", (f"session:{sid}",)
        ).fetchone()
        assert doc is not None


class TestZombieCleanup:
    def test_start_cleans_stale_sessions_across_projects(self, store):
        # Create an old session for a different project
        sid_old = store.session.start(project="old-project")
        store.session.save_message(sid_old, "assistant",
            "- **fact1**: v1\n- **fact2**: v2\n- **fact3**: v3\nDecision: Use X")
        # Backdate it to look stale (2 hours ago)
        store._conn.execute(
            "UPDATE sessions SET started_at=? WHERE session_id=?",
            (time.time() - 7200, sid_old),
        )
        store._conn.execute(
            "UPDATE messages SET timestamp=? WHERE session_id=?",
            (time.time() - 7200, sid_old),
        )
        store._conn.commit()
        # Start new session — should clean up the stale one
        sid_new = store.session.start(project="new-project")
        # Old session should be ended
        old = store._conn.execute(
            "SELECT ended_at FROM sessions WHERE session_id=?", (sid_old,)
        ).fetchone()
        assert old["ended_at"] is not None
        # And promoted to documents
        doc = store._conn.execute(
            "SELECT * FROM documents WHERE file_path=?", (f"session:{sid_old}",)
        ).fetchone()
        assert doc is not None


class TestAutoRelation:
    def test_session_end_creates_relations_between_session_docs(self, store):
        sid = store.session.start(project="myapp")
        store.state.set("current_session_id", sid)
        # Save two docs during this session
        id1 = store.save(title="Problem found", content="OOM on YARN", source="debug_solution")
        id2 = store.save(title="Fix applied", content="increase memory", source="debug_solution")
        store.session.end(sid, summary="Fixed YARN OOM")
        # Check relations were created
        rels = store._conn.execute(
            """SELECT * FROM doc_relations
               WHERE (doc_id_a=? AND doc_id_b=?) OR (doc_id_a=? AND doc_id_b=?)""",
            (id1, id2, id2, id1),
        ).fetchall()
        assert len(rels) >= 1
        assert rels[0]["relation_type"] == "related"
