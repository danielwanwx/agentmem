"""Tests for DB schema and migrations."""
import sqlite3
from agent_memory.db import init_db


class TestMigration:
    def test_documents_has_project_column(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = init_db(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
        assert "project" in cols
        conn.close()

    def test_project_index_exists(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = init_db(db_path)
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(documents)")}
        assert "idx_documents_project" in indexes
        conn.close()

    def test_existing_data_gets_null_project(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = init_db(db_path)
        conn.execute(
            "INSERT INTO documents (title, summary, source) VALUES (?, ?, ?)",
            ("Old doc", "summary", "explicit"),
        )
        conn.commit()
        row = conn.execute("SELECT project FROM documents WHERE title='Old doc'").fetchone()
        assert row["project"] is None
        conn.close()

    def test_doc_relations_table_exists(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = init_db(db_path)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "doc_relations" in tables
        conn.close()

    def test_doc_relations_unique_constraint(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = init_db(db_path)
        conn.execute("INSERT INTO documents (title, source) VALUES ('A', 'explicit')")
        conn.execute("INSERT INTO documents (title, source) VALUES ('B', 'explicit')")
        conn.commit()
        conn.execute(
            "INSERT INTO doc_relations (doc_id_a, doc_id_b, relation_type) VALUES (1, 2, 'related')"
        )
        conn.commit()
        conn.execute(
            "INSERT OR IGNORE INTO doc_relations (doc_id_a, doc_id_b, relation_type) VALUES (1, 2, 'related')"
        )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM doc_relations").fetchone()[0]
        assert count == 1
        conn.close()
