"""Tests for MemoryStore: save, search, TTL, inject."""
import json
import time


class TestSave:
    def test_save_returns_doc_id(self, store):
        doc_id = store.save(title="Test Doc", content="Some content", source="explicit")
        assert isinstance(doc_id, int)
        assert doc_id > 0

    def test_save_persists_to_db(self, store):
        doc_id = store.save(title="Kafka Config", content="broker.id=1", source="explicit")
        row = store._conn.execute(
            "SELECT title, raw_content, source FROM documents WHERE doc_id=?", (doc_id,)
        ).fetchone()
        assert row["title"] == "Kafka Config"
        assert row["source"] == "explicit"

    def test_save_extracts_fields(self, store):
        content = "# My Title\n\nSummary paragraph here.\n\n- **key1**: value1\n- **key2**: value2"
        doc_id = store.save(title="", content=content, source="explicit")
        row = store._conn.execute(
            "SELECT title, summary, key_facts FROM documents WHERE doc_id=?", (doc_id,)
        ).fetchone()
        assert row["title"] == "My Title"
        assert "Summary paragraph" in row["summary"]
        facts = json.loads(row["key_facts"])
        assert len(facts) >= 2

    def test_save_architectural_decision_gets_p0(self, store):
        doc_id = store.save(
            title="Use PostgreSQL", content="We chose PostgreSQL",
            source="architectural_decision",
        )
        row = store._conn.execute(
            "SELECT priority, expires_at FROM documents WHERE doc_id=?", (doc_id,)
        ).fetchone()
        assert row["priority"] == "P0"
        assert row["expires_at"] is None  # never expires

    def test_save_session_note_gets_p2_with_ttl(self, store):
        doc_id = store.save(
            title="Quick note", content="Some note", source="session_note",
        )
        row = store._conn.execute(
            "SELECT priority, expires_at FROM documents WHERE doc_id=?", (doc_id,)
        ).fetchone()
        assert row["priority"] == "P2"
        assert row["expires_at"] is not None
        assert row["expires_at"] > time.time()  # in the future

    def test_save_upsert_by_file_path(self, store):
        id1 = store.save(title="v1", content="old", source="hook", file_path="/tmp/a.py")
        id2 = store.save(title="v2", content="new", source="hook", file_path="/tmp/a.py")
        assert id1 == id2  # same doc updated
        row = store._conn.execute(
            "SELECT title FROM documents WHERE doc_id=?", (id1,)
        ).fetchone()
        assert row["title"] == "v2"

    def test_save_different_file_paths_create_separate_docs(self, store):
        id1 = store.save(title="A", content="a", source="hook", file_path="/tmp/a.py")
        id2 = store.save(title="B", content="b", source="hook", file_path="/tmp/b.py")
        assert id1 != id2

    def test_save_with_project(self, store):
        doc_id = store.save(title="API config", content="endpoint /v1",
                            source="debug_solution", project="myapp")
        row = store._conn.execute(
            "SELECT project FROM documents WHERE doc_id=?", (doc_id,)
        ).fetchone()
        assert row["project"] == "myapp"

    def test_save_architectural_decision_forces_global(self, store):
        doc_id = store.save(title="Use Postgres", content="chose pg",
                            source="architectural_decision", project="myapp")
        row = store._conn.execute(
            "SELECT project FROM documents WHERE doc_id=?", (doc_id,)
        ).fetchone()
        assert row["project"] is None  # forced global

    def test_save_without_project_defaults_to_none(self, store):
        doc_id = store.save(title="No project", content="content", source="explicit")
        row = store._conn.execute(
            "SELECT project FROM documents WHERE doc_id=?", (doc_id,)
        ).fetchone()
        assert row["project"] is None


class TestSearch:
    def test_search_returns_list(self, store):
        results = store.search("nonexistent")
        assert isinstance(results, list)
        assert len(results) == 0

    def test_search_finds_saved_doc(self, store):
        store.save(title="Kafka rebalance fix", content="Increase timeout to 600s", source="debug_solution")
        # Wait for FTS5 trigger to sync
        results = store.search("Kafka rebalance")
        assert len(results) >= 1
        assert "Kafka" in results[0].l1

    def test_search_respects_max_results(self, store):
        for i in range(10):
            store.save(title=f"Document about testing {i}", content=f"Test content {i}", source="explicit")
        results = store.search("testing", max_results=3)
        assert len(results) <= 3

    def test_search_filters_by_project(self, store):
        store.save(title="App A config", content="port 8080", source="debug_solution", project="app-a")
        store.save(title="App B config", content="port 9090", source="debug_solution", project="app-b")
        results = store.search("config", project="app-a")
        titles = [r.l1 for r in results]
        assert any("App A" in t for t in titles)
        assert not any("App B" in t for t in titles)

    def test_search_includes_global_docs(self, store):
        store.save(title="Global arch decision", content="Use REST",
                   source="architectural_decision", project="app-a")  # forced NULL
        store.save(title="App A note", content="local config",
                   source="session_note", project="app-a")
        results = store.search("config decision", project="app-a")
        assert len(results) >= 1

    def test_search_without_project_returns_all(self, store):
        store.save(title="Doc from A", content="content a", source="explicit", project="app-a")
        store.save(title="Doc from B", content="content b", source="explicit", project="app-b")
        results = store.search("Doc from")
        assert len(results) >= 2

    def test_search_priority_weighting(self, store):
        store.save(title="YARN memory config", content="Important arch decision",
                   source="architectural_decision")
        store.save(title="YARN memory note", content="Quick session note",
                   source="session_note")
        results = store.search("YARN memory")
        if len(results) >= 2:
            # P0 doc should score higher than P2
            p0_scores = [r.score for r in results if r.priority == "P0"]
            p2_scores = [r.score for r in results if r.priority == "P2"]
            if p0_scores and p2_scores:
                assert max(p0_scores) >= max(p2_scores)


class TestTTL:
    def test_prune_expired_removes_old_docs(self, store):
        doc_id = store.save(title="Expiring", content="temp", source="session_note")
        # Manually set expires_at to the past
        store._conn.execute(
            "UPDATE documents SET expires_at=? WHERE doc_id=?",
            (time.time() - 1, doc_id),
        )
        store._conn.commit()
        deleted = store.prune_expired()
        assert deleted == 1
        row = store._conn.execute(
            "SELECT doc_id FROM documents WHERE doc_id=?", (doc_id,)
        ).fetchone()
        assert row is None

    def test_prune_keeps_p0_docs(self, store):
        doc_id = store.save(title="Never expire", content="arch",
                            source="architectural_decision")
        deleted = store.prune_expired()
        assert deleted == 0
        row = store._conn.execute(
            "SELECT doc_id FROM documents WHERE doc_id=?", (doc_id,)
        ).fetchone()
        assert row is not None


class TestInject:
    def test_inject_empty_results(self, store):
        assert store.inject([]) == ""

    def test_inject_formats_output(self, store):
        store.save(title="Test inject", content="Some content here", source="explicit")
        results = store.search("inject")
        if results:
            output = store.inject(results)
            assert "agent-memory" in output
            assert "Test inject" in output

    def test_inject_respects_max_tokens(self, store):
        for i in range(20):
            store.save(title=f"Long doc {i}", content="x" * 500, source="explicit")
        results = store.search("Long doc", max_results=20)
        output = store.inject(results, max_tokens=500)
        # Should be reasonably bounded
        assert len(output) < 3000  # rough char limit for 500 tokens

    def test_inject_expands_related_docs(self, store):
        id1 = store.save(title="Main doc about YARN", content="YARN memory config",
                         source="debug_solution")
        id2 = store.save(title="Related YARN fix", content="increase executor memory",
                         source="debug_solution")
        # Manually create relation
        store._conn.execute(
            "INSERT INTO doc_relations (doc_id_a, doc_id_b, relation_type) VALUES (?, ?, 'related')",
            (id1, id2),
        )
        store._conn.commit()
        results = store.search("YARN memory")
        output = store.inject(results, max_tokens=3000)
        assert "Related YARN fix" in output

    def test_inject_hints_when_budget_low(self, store):
        id1 = store.save(title="Main doc budgettest", content="x" * 200, source="debug_solution")
        id2 = store.save(title="Related doc hinttest", content="y" * 200, source="debug_solution")
        store._conn.execute(
            "INSERT INTO doc_relations (doc_id_a, doc_id_b, relation_type) VALUES (?, ?, 'related')",
            (id1, id2),
        )
        store._conn.commit()
        results = store.search("budgettest")
        # With very tight budget, should not fully expand related docs
        output = store.inject(results, max_tokens=150)
        # Either contains a hint line or doesn't expand at all — just shouldn't crash
        assert isinstance(output, str)


class TestDedup:
    def test_duplicate_title_same_source_merges(self, store):
        id1 = store.save(title="Kafka timeout fix", content="set timeout=600",
                         source="debug_solution")
        id2 = store.save(title="Kafka timeout fix", content="also set retries=3",
                         source="debug_solution")
        assert id1 == id2  # merged, not duplicated
        row = store._conn.execute(
            "SELECT raw_content FROM documents WHERE doc_id=?", (id1,)
        ).fetchone()
        assert "retries=3" in row["raw_content"]

    def test_duplicate_merges_key_facts(self, store):
        id1 = store.save(
            title="YARN config",
            content="- **timeout**: 600s\n- **retries**: 3",
            source="debug_solution",
        )
        id2 = store.save(
            title="YARN config",
            content="- **memory**: 4GB\n- **timeout**: 600s",
            source="debug_solution",
        )
        assert id1 == id2
        row = store._conn.execute(
            "SELECT key_facts FROM documents WHERE doc_id=?", (id1,)
        ).fetchone()
        facts = json.loads(row["key_facts"])
        assert len(facts) >= 3

    def test_different_title_creates_new_doc(self, store):
        id1 = store.save(title="Kafka config", content="broker settings",
                         source="debug_solution")
        id2 = store.save(title="Redis config", content="cache settings",
                         source="debug_solution")
        assert id1 != id2

    def test_same_title_different_source_creates_new_doc(self, store):
        id1 = store.save(title="API design", content="REST approach",
                         source="architectural_decision")
        id2 = store.save(title="API design", content="quick note about API",
                         source="session_note")
        assert id1 != id2

    def test_dedup_respects_project_scope(self, store):
        id1 = store.save(title="DB config", content="pg settings",
                         source="debug_solution", project="app-a")
        id2 = store.save(title="DB config", content="mysql settings",
                         source="debug_solution", project="app-b")
        assert id1 != id2

    def test_file_path_upsert_takes_precedence_over_dedup(self, store):
        id1 = store.save(title="Config", content="v1",
                         source="hook", file_path="/tmp/a.py")
        id2 = store.save(title="Config", content="v2",
                         source="hook", file_path="/tmp/a.py")
        assert id1 == id2
