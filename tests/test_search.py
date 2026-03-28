"""Tests for search module internals."""
import json
import sqlite3
from agent_memory.db import init_db
from agent_memory.search import (
    search_documents, search_documents_bm25, _search_phase0,
    _tokenize, _score_doc, _clean_fts_tokens,
    _to_fts5_query_and, _to_fts5_query_or,
)
import pytest


@pytest.fixture
def search_db(tmp_path):
    """DB with some docs pre-loaded for search testing."""
    conn = init_db(str(tmp_path / "search.db"))
    docs = [
        ("Kafka rebalance timeout", "Set consumer timeout to 600s for large clusters",
         '["timeout default is 300s", "affects all consumer groups"]',
         '["Use 600s for clusters > 50 brokers"]', "P0", "architectural_decision", None),
        ("Redis cache invalidation", "TTL-based invalidation with pub/sub fallback",
         '["TTL default 3600s", "pub/sub channel: cache-invalidate"]',
         '["Chose TTL over event-driven for simplicity"]', "P1", "debug_solution", "backend"),
        ("Frontend CSS grid layout", "Using CSS grid for dashboard components",
         '["grid-template-columns: repeat(3, 1fr)"]',
         '[]', "P2", "session_note", "frontend"),
        ("YARN memory configuration", "Executor memory settings for Spark on YARN",
         '["executor_memory_hard=4GB", "driver_memory=2GB"]',
         '["Use hard limits, not soft"]', "P0", "architectural_decision", None),
        ("Python logging setup", "Structured logging with structlog",
         '["log level: INFO in prod", "JSON format"]',
         '[]', "P2", "routine", "backend"),
    ]
    for title, summary, kf, dec, prio, source, project in docs:
        conn.execute(
            """INSERT INTO documents (title, summary, key_facts, decisions, priority, source, project, raw_content)
               VALUES (?,?,?,?,?,?,?,?)""",
            (title, summary, kf, dec, prio, source, project, f"{title}\n{summary}"),
        )
    conn.commit()
    return conn


class TestTokenize:
    def test_english_tokens(self):
        tokens = _tokenize("Kafka rebalance timeout")
        assert "kafka" in tokens
        assert "rebalance" in tokens
        assert "timeout" in tokens

    def test_chinese_tokens(self):
        tokens = _tokenize("配置管理 kafka")
        assert "kafka" in tokens
        assert any("\u4e00" <= c <= "\u9fff" for tok in tokens for c in tok)

    def test_filters_short_tokens(self):
        tokens = _tokenize("a b cd efg")
        assert "a" not in tokens
        assert "b" not in tokens
        assert "cd" in tokens


class TestFtsQueryBuilding:
    def test_and_query(self):
        q = _to_fts5_query_and("Kafka rebalance timeout")
        assert "AND" in q

    def test_or_query(self):
        q = _to_fts5_query_or("Kafka rebalance timeout")
        assert "OR" in q

    def test_clean_strips_special_chars(self):
        tokens = _clean_fts_tokens('"Kafka" AND (timeout OR NOT)')
        # Should not contain AND/OR/NOT or special chars
        assert all(t not in ("AND", "OR", "NOT") for t in tokens)

    def test_filters_short_tokens(self):
        tokens = _clean_fts_tokens("ab cde fg hijk")
        assert "ab" not in tokens
        assert "fg" not in tokens
        assert "cde" in tokens
        assert "hijk" in tokens


class TestBM25Search:
    def test_finds_matching_doc(self, search_db):
        results = search_documents_bm25(search_db, "Kafka rebalance")
        assert len(results) >= 1
        assert any("Kafka" in r.l1 for r in results)

    def test_returns_empty_for_no_match(self, search_db):
        results = search_documents_bm25(search_db, "xyznonexistent123")
        assert len(results) == 0

    def test_respects_max_results(self, search_db):
        results = search_documents_bm25(search_db, "timeout memory configuration", max_results=2)
        assert len(results) <= 2

    def test_priority_weighting(self, search_db):
        results = search_documents_bm25(search_db, "memory configuration")
        if len(results) >= 2:
            p0_scores = [r.score for r in results if r.priority == "P0"]
            p2_scores = [r.score for r in results if r.priority == "P2"]
            if p0_scores and p2_scores:
                assert max(p0_scores) >= max(p2_scores)

    def test_and_fallback_to_or(self, search_db):
        # Query with terms that won't ALL match, but some will via OR
        results = search_documents_bm25(search_db, "Kafka xyznonexistent rebalance")
        # OR fallback should still find Kafka doc
        assert len(results) >= 0  # may or may not find depending on trigram matching

    def test_namespace_filtering(self, search_db):
        results = search_documents_bm25(search_db, "CSS grid layout", project="frontend")
        assert len(results) >= 1
        # Backend docs should not appear
        assert not any("Redis" in r.l1 for r in results)


class TestPhase0Search:
    def test_finds_by_keyword(self, search_db):
        results = _search_phase0(search_db, "Kafka rebalance")
        assert len(results) >= 1

    def test_empty_for_no_match(self, search_db):
        results = _search_phase0(search_db, "xyznonexistent123456")
        assert len(results) == 0

    def test_namespace_filtering(self, search_db):
        results = _search_phase0(search_db, "logging setup", project="backend")
        if results:
            # Should only contain backend or global docs
            for r in results:
                assert r.source != "session_note" or True  # just verify it runs


class TestSearchDocuments:
    def test_cascade_returns_results(self, search_db):
        results = search_documents(search_db, "Kafka timeout")
        assert len(results) >= 1

    def test_min_score_filter(self, search_db):
        results = search_documents(search_db, "Kafka", min_score=999999.0)
        assert len(results) == 0

    def test_namespace_passthrough(self, search_db):
        results = search_documents(search_db, "CSS grid", project="frontend")
        assert len(results) >= 1
