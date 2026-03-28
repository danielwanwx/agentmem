"""End-to-end integration test: namespace + dedup + relations."""
import json


class TestFullWorkflow:
    def test_multi_project_session_with_dedup_and_relations(self, store):
        # 1. Start session for project "backend"
        sid = store.session.start(project="backend")
        store.state.set("current_session_id", sid)
        store.state.set("current_project", "backend")

        # 2. Save architectural decision (should be global)
        id1 = store.save(
            title="Use PostgreSQL for all services",
            content="Decision: Use PostgreSQL. Reason: ACID compliance.",
            source="architectural_decision",
            project="backend",
        )
        row = store._conn.execute(
            "SELECT project, priority FROM documents WHERE doc_id=?", (id1,)
        ).fetchone()
        assert row["project"] is None  # global
        assert row["priority"] == "P0"

        # 3. Save debug solution (project-scoped)
        id2 = store.save(
            title="Connection pool timeout fix",
            content="- **pool_size**: 20\n- **timeout**: 30s",
            source="debug_solution",
            project="backend",
        )
        assert store._conn.execute(
            "SELECT project FROM documents WHERE doc_id=?", (id2,)
        ).fetchone()["project"] == "backend"

        # 4. Save duplicate — should merge
        id3 = store.save(
            title="Connection pool timeout fix",
            content="- **max_overflow**: 10\n- **pool_size**: 20",
            source="debug_solution",
            project="backend",
        )
        assert id2 == id3  # merged
        facts = json.loads(store._conn.execute(
            "SELECT key_facts FROM documents WHERE doc_id=?", (id2,)
        ).fetchone()["key_facts"])
        # Should have pool_size, timeout, AND max_overflow
        assert len(facts) >= 3

        # 5. End session — should create relations
        store.session.end(sid, summary="Set up connection pooling")
        rels = store._conn.execute(
            "SELECT * FROM doc_relations WHERE (doc_id_a=? OR doc_id_b=?) OR (doc_id_a=? OR doc_id_b=?)",
            (id1, id1, id2, id2),
        ).fetchall()
        assert len(rels) >= 1

        # 6. Search from "frontend" project — should see global but not backend-scoped
        store.save(title="Frontend CSS reset", content="normalize.css",
                   source="session_note", project="frontend")
        results = store.search("PostgreSQL", project="frontend")
        assert len(results) >= 1  # global arch decision visible

        results_pool = store.search("Connection pool timeout fix", project="frontend")
        # backend-only doc should NOT be visible from frontend
        backend_found = any("Connection pool" in r.l1 for r in results_pool)
        assert not backend_found

        # 7. Search from "backend" — should see both
        results_backend = store.search("Connection pool timeout fix", project="backend")
        assert len(results_backend) >= 1
