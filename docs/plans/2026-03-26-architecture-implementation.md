# Architecture Optimization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add multi-project namespace isolation, write-time knowledge deduplication with field-level merge, and a document relation graph with adaptive inject expansion.

**Architecture:** Three layered enhancements to the existing SQLite-backed memory store. Phase 1 (namespace) is foundation — Phase 2 (dedup) and Phase 3 (relations) depend on it. All changes follow the existing "Ollama-optional graceful degradation" pattern.

**Tech Stack:** Python 3.12, SQLite FTS5, sqlite-vec (optional), pytest

**Baseline:** 45 tests passing, 50% coverage. Guard command: `pytest tests/ -v`

---

## Phase 1: Multi-Project Namespace Isolation

### Task 1: Schema migration — add `project` column to documents

**Files:**
- Modify: `agent_memory/db.py:112-134` (`_migrate` function)
- Test: `tests/test_db.py` (new file)

**Step 1: Write the failing test**

```python
# tests/test_db.py
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
        """Simulate pre-migration DB: insert doc without project, then run migration."""
        db_path = str(tmp_path / "test.db")
        conn = init_db(db_path)
        conn.execute(
            "INSERT INTO documents (title, summary, source) VALUES (?, ?, ?)",
            ("Old doc", "summary", "explicit"),
        )
        conn.commit()
        row = conn.execute("SELECT project FROM documents WHERE title='Old doc'").fetchone()
        assert row["project"] is None  # NULL = global
        conn.close()
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py -v`
Expected: FAIL — `"project" not in cols` (column doesn't exist yet)

**Step 3: Write minimal implementation**

Add to `agent_memory/db.py` `_migrate()` function, after the `last_accessed_at` migration (line 125):

```python
    if "project" not in doc_cols:
        conn.execute("ALTER TABLE documents ADD COLUMN project TEXT")
        conn.commit()
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project)"
        )
        conn.commit()
    except Exception:
        pass
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_db.py -v`
Expected: PASS

**Step 5: Run full suite to check no regressions**

Run: `pytest tests/ -v`
Expected: All 48 tests pass (45 existing + 3 new)

**Step 6: Commit**

```bash
git add agent_memory/db.py tests/test_db.py
git commit -m "feat: add project column to documents table for namespace isolation"
```

---

### Task 2: `store.save()` — accept and persist `project` param

**Files:**
- Modify: `agent_memory/store.py:68-139` (`save` method)
- Test: `tests/test_store.py` (add to existing)

**Step 1: Write the failing tests**

Add to `tests/test_store.py` class `TestSave`:

```python
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
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_store.py::TestSave::test_save_with_project -v`
Expected: FAIL — `save()` does not accept `project` param

**Step 3: Write minimal implementation**

Modify `agent_memory/store.py` `save()` method signature (line 68) to add `project` param:

```python
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
```

After line 86 (`expires_at = ...`), add project logic:

```python
        # architectural_decision is always global (project=NULL)
        effective_project = None if source == "architectural_decision" else project
```

Then modify the INSERT statement (line 117-134) to include `project`:

```python
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
```

Also update the file_path UPDATE branch (line 94-111) to include project:

```python
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
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_store.py::TestSave -v`
Expected: All 10 tests pass (7 existing + 3 new)

**Step 5: Commit**

```bash
git add agent_memory/store.py tests/test_store.py
git commit -m "feat: save() accepts project param, architectural_decision forced global"
```

---

### Task 3: `search_documents()` — namespace filtering

**Files:**
- Modify: `agent_memory/search.py:85-99` (`_run_fts5_query`), `292-306` (`_search_phase0`), `152-184` (`search_documents`)
- Test: `tests/test_store.py` (add to TestSearch)

**Step 1: Write the failing tests**

Add to `tests/test_store.py` class `TestSearch`:

```python
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
        # Should find both: the global arch decision AND the app-a note
        assert len(results) >= 1

    def test_search_without_project_returns_all(self, store):
        store.save(title="Doc from A", content="content a", source="explicit", project="app-a")
        store.save(title="Doc from B", content="content b", source="explicit", project="app-b")
        results = store.search("content")
        assert len(results) >= 2  # both visible when no project filter
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_store.py::TestSearch::test_search_filters_by_project -v`
Expected: FAIL — `search()` does not accept `project` param

**Step 3: Write minimal implementation**

3a. Add `project` param to `MemoryStore.search()` in `store.py:206`:

```python
    def search(self, query: str, max_results: int = 5, project: str = None) -> list[SearchResult]:
```

Pass it through to `search_documents()`:

```python
        results = search_documents(self._conn, query, max_results=max_results, project=project)
```

3b. Add `project` param to `search_documents()` in `search.py:152`:

```python
def search_documents(conn: sqlite3.Connection, query: str,
                     max_results: int = 5,
                     min_score: float = 0.0,
                     project: str = None) -> list[SearchResult]:
```

Pass it through to each sub-function:

```python
        results = search_hybrid(conn, query, max_results, project=project)
        ...
        results = search_documents_bm25(conn, query, max_results, project=project)
        ...
        results = _search_phase0(conn, query, max_results, project=project)
```

3c. Add `project` param to `_run_fts5_query()` in `search.py:85`:

```python
def _run_fts5_query(conn: sqlite3.Connection, fts_query: str,
                    max_results: int, project: str = None) -> list[sqlite3.Row]:
    if project:
        return conn.execute(
            """SELECT d.doc_id, d.title, d.summary, d.key_facts, d.decisions,
                      d.code_sigs, d.metrics, d.raw_content, d.priority, d.source,
                      fts.rank
               FROM documents_fts fts
               JOIN documents d ON d.doc_id = fts.rowid
               WHERE documents_fts MATCH ?
                 AND (d.expires_at IS NULL OR d.expires_at > strftime('%s','now'))
                 AND (d.project = ? OR d.project IS NULL)
               ORDER BY fts.rank
               LIMIT ?""",
            (fts_query, project, max_results)
        ).fetchall()
    return conn.execute(
        """SELECT d.doc_id, d.title, d.summary, d.key_facts, d.decisions,
                  d.code_sigs, d.metrics, d.raw_content, d.priority, d.source,
                  fts.rank
           FROM documents_fts fts
           JOIN documents d ON d.doc_id = fts.rowid
           WHERE documents_fts MATCH ?
             AND (d.expires_at IS NULL OR d.expires_at > strftime('%s','now'))
           ORDER BY fts.rank
           LIMIT ?""",
        (fts_query, max_results)
    ).fetchall()
```

3d. Add `project` param to `_search_phase0()` in `search.py:292`:

```python
def _search_phase0(conn: sqlite3.Connection, query: str,
                   max_results: int = 5, project: str = None) -> list[SearchResult]:
```

Update the query (line 299-306):

```python
    if project:
        rows = conn.execute(
            """SELECT doc_id, title, summary, key_facts, decisions,
                      code_sigs, metrics, raw_content, priority, source
               FROM documents
               WHERE (expires_at IS NULL OR expires_at > strftime('%s','now'))
                 AND (project = ? OR project IS NULL)
               ORDER BY created_at DESC
               LIMIT 200""",
            (project,)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT doc_id, title, summary, key_facts, decisions,
                      code_sigs, metrics, raw_content, priority, source
               FROM documents
               WHERE expires_at IS NULL OR expires_at > strftime('%s','now')
               ORDER BY created_at DESC
               LIMIT 200"""
        ).fetchall()
```

3e. Add `project` param to `search_documents_bm25()` and `search_hybrid()`, passing through to `_run_fts5_query()`. Same pattern — add `project: str = None` param, pass it down.

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_store.py::TestSearch -v`
Expected: All 7 tests pass (4 existing + 3 new)

**Step 5: Run full suite**

Run: `pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add agent_memory/store.py agent_memory/search.py tests/test_store.py
git commit -m "feat: namespace filtering in search — project + global docs"
```

---

### Task 4: MCP server — inject current project into search/save

**Files:**
- Modify: `agent_memory/mcp_server.py:126-138`
- Test: no unit test (MCP requires async server — verified via integration)

**Step 1: Modify am_search handler** (line 126-131)

```python
        if name == "am_search":
            query = arguments["query"]
            limit = int(arguments.get("limit", 5))
            project = store.state.get("current_project")
            results = store.search(query, max_results=limit, project=project)
            output = store.inject(results, max_tokens=1500)
            return [types.TextContent(type="text", text=output or "No results found.")]
```

**Step 2: Modify am_save handler** (line 133-138)

```python
        elif name == "am_save":
            title = arguments["title"]
            content = arguments["content"]
            source = arguments["source"]
            project = store.state.get("current_project")
            doc_id = store.save(title=title, content=content, source=source, project=project)
            return [types.TextContent(type="text", text=f"Saved as doc_id={doc_id}")]
```

**Step 3: Run full suite**

Run: `pytest tests/ -v`
Expected: All tests pass (MCP not tested directly but no regressions)

**Step 4: Commit**

```bash
git add agent_memory/mcp_server.py
git commit -m "feat: MCP server injects current_project into search/save"
```

---

## Phase 2: Write-Time Knowledge Deduplication & Merge

### Task 5: FTS5-based dedup detection in `save()`

**Files:**
- Modify: `agent_memory/store.py:68-139`
- Test: `tests/test_store.py` (add TestDedup class)

**Step 1: Write the failing tests**

Add new class to `tests/test_store.py`:

```python
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
        # New content overwrites raw_content
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
        # Union: should have timeout, retries, AND memory
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
        assert id1 != id2  # different projects, no dedup

    def test_file_path_upsert_takes_precedence_over_dedup(self, store):
        id1 = store.save(title="Config", content="v1",
                         source="hook", file_path="/tmp/a.py")
        id2 = store.save(title="Config", content="v2",
                         source="hook", file_path="/tmp/a.py")
        assert id1 == id2  # file_path upsert, not title dedup
```

Add `import json` at top of test file if not already present.

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_store.py::TestDedup::test_duplicate_title_same_source_merges -v`
Expected: FAIL — `id1 != id2` (no dedup logic yet)

**Step 3: Write minimal implementation**

Add a `_find_duplicate()` method and a `_merge_fields()` method to `MemoryStore` in `store.py`.

Insert after `_effective_priority` function (line 57), before `class MemoryStore`:

```python
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
```

Add `_find_duplicate()` method to `MemoryStore` class, after `__init__`:

```python
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
            if rows:
                # FTS5 rank is negative; closer to 0 = better match.
                # Threshold: rank > -5.0 means very strong title overlap.
                if rows[0]["rank"] > -5.0:
                    return rows[0]["doc_id"]
        except Exception:
            pass
        return None
```

Now modify `save()` method. After `effective_project = ...` and before the `if file_path:` block, add dedup logic:

```python
        # Dedup: check for existing doc with same title + source + project
        # file_path upsert takes precedence (skip dedup if file_path provided)
        if not file_path:
            dup_id = self._find_duplicate(actual_title, source, effective_project)
            if dup_id is not None:
                # Field-level merge: union key_facts/decisions, overwrite title/summary
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
                return dup_id
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_store.py::TestDedup -v`
Expected: All 6 tests pass

**Step 5: Run full suite**

Run: `pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add agent_memory/store.py tests/test_store.py
git commit -m "feat: write-time dedup via FTS5 title match with field-level merge"
```

---

## Phase 3: Document Relation Graph

### Task 6: Schema — create `doc_relations` table

**Files:**
- Modify: `agent_memory/db.py:112-181` (`_migrate`)
- Test: `tests/test_db.py` (add to existing)

**Step 1: Write the failing test**

Add to `tests/test_db.py` `TestMigration`:

```python
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
        # Create two docs first
        conn.execute("INSERT INTO documents (title, source) VALUES ('A', 'explicit')")
        conn.execute("INSERT INTO documents (title, source) VALUES ('B', 'explicit')")
        conn.commit()
        # Insert relation
        conn.execute(
            "INSERT INTO doc_relations (doc_id_a, doc_id_b, relation_type) VALUES (1, 2, 'related')"
        )
        conn.commit()
        # Duplicate should be ignored
        try:
            conn.execute(
                "INSERT OR IGNORE INTO doc_relations (doc_id_a, doc_id_b, relation_type) VALUES (1, 2, 'related')"
            )
            conn.commit()
        except Exception:
            pass
        count = conn.execute("SELECT COUNT(*) FROM doc_relations").fetchone()[0]
        assert count == 1
        conn.close()
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py::TestMigration::test_doc_relations_table_exists -v`
Expected: FAIL — `"doc_relations" not in tables`

**Step 3: Write minimal implementation**

Add to `_migrate()` in `db.py`, at the end of the function (after FTS backfill block):

```python
    # Create doc_relations table for document relation graph
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS doc_relations (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id_a      INTEGER NOT NULL REFERENCES documents(doc_id),
                doc_id_b      INTEGER NOT NULL REFERENCES documents(doc_id),
                relation_type TEXT NOT NULL,
                created_at    REAL DEFAULT (julianday('now')),
                UNIQUE(doc_id_a, doc_id_b, relation_type)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_relations_a ON doc_relations(doc_id_a)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_relations_b ON doc_relations(doc_id_b)")
        conn.commit()
    except Exception:
        pass
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_db.py -v`
Expected: All 5 tests pass

**Step 5: Commit**

```bash
git add agent_memory/db.py tests/test_db.py
git commit -m "feat: create doc_relations table for document graph"
```

---

### Task 7: Auto-relate docs on session end

**Files:**
- Modify: `agent_memory/session.py:119-155` (`end` method)
- Test: `tests/test_session.py` (add TestRelations class)

**Step 1: Write the failing test**

Add to `tests/test_session.py`:

```python
class TestAutoRelation:
    def test_session_end_creates_relations_between_session_docs(self, store):
        sid = store.session.start(project="myapp")
        # Save two docs during this session (simulate Claude calling am_save)
        id1 = store.save(title="Problem found", content="OOM on YARN", source="debug_solution")
        id2 = store.save(title="Fix applied", content="increase memory", source="debug_solution")
        # Track which docs belong to this session
        store.state.set(f"session_docs:{sid}", [id1, id2])
        store.session.end(sid, summary="Fixed YARN OOM")
        # Check relations were created
        rels = store._conn.execute(
            "SELECT * FROM doc_relations WHERE doc_id_a=? AND doc_id_b=?",
            (id1, id2),
        ).fetchall()
        assert len(rels) >= 1
        assert rels[0]["relation_type"] == "related"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_session.py::TestAutoRelation -v`
Expected: FAIL — no relation creation logic yet

**Step 3: Write minimal implementation**

Add a method to `SessionManager` in `session.py`, after `end()`:

```python
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
        # Clean up session doc tracker
        state.set(f"session_docs:{session_id}", [])
```

Call it from `end()` — add before `self._conn.commit()` at the end of `end()` method (line 155):

```python
        self._create_session_relations(session_id)
```

Also modify `MemoryStore.save()` in `store.py` — after a successful save (both INSERT and merge paths), track the doc_id in session state:

```python
        # Track doc for session relations
        current_sid = self.state.get("current_session_id")
        if current_sid:
            existing_docs = self.state.get(f"session_docs:{current_sid}") or []
            if doc_id not in existing_docs:
                existing_docs.append(doc_id)
                self.state.set(f"session_docs:{current_sid}", existing_docs)
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_session.py::TestAutoRelation -v`
Expected: PASS

**Step 5: Run full suite**

Run: `pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add agent_memory/session.py agent_memory/store.py tests/test_session.py
git commit -m "feat: auto-relate docs produced in the same session"
```

---

### Task 8: `inject()` — adaptive relation expansion

**Files:**
- Modify: `agent_memory/store.py:312-348` (`inject` method)
- Test: `tests/test_store.py` (add to TestInject)

**Step 1: Write the failing tests**

Add to `tests/test_store.py` `TestInject`:

```python
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
        id1 = store.save(title="Main doc", content="x" * 200, source="debug_solution")
        id2 = store.save(title="Related doc hint", content="y" * 200, source="debug_solution")
        store._conn.execute(
            "INSERT INTO doc_relations (doc_id_a, doc_id_b, relation_type) VALUES (?, ?, 'related')",
            (id1, id2),
        )
        store._conn.commit()
        results = store.search("Main doc")
        output = store.inject(results, max_tokens=200)  # very tight budget
        # Should either contain hint or nothing, not full expansion
        if "Related doc hint" in output:
            assert "Related:" in output  # hint format, not full L1
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_store.py::TestInject::test_inject_expands_related_docs -v`
Expected: FAIL — "Related YARN fix" not in output

**Step 3: Write minimal implementation**

Add a helper method to `MemoryStore`:

```python
    def _get_related_docs(self, doc_id: int, exclude_ids: set) -> list[dict]:
        """Fetch related documents for inject expansion."""
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
```

Modify `inject()` method. Replace the for loop (lines 331-343) with:

```python
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
                    # Full expansion: up to 2 related docs as L1
                    for rel in related[:2]:
                        rel_l1 = f"### {rel['title']}\n{rel['summary']}"
                        rel_tokens = len(rel_l1) // 4
                        if rel_tokens > token_budget:
                            break
                        blocks.append(f"  [{rel['relation_type']}]\n{rel_l1}")
                        token_budget -= rel_tokens
                        result_ids.add(rel["doc_id"])
                else:
                    # Hint only
                    hints = ", ".join(f"#{rel['doc_id']} '{rel['title'][:30]}'" for rel in related[:3])
                    blocks.append(f"Related: {hints}")
                    token_budget -= 8  # ~30 chars
            except Exception:
                pass  # doc_relations table may not exist yet
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_store.py::TestInject -v`
Expected: All 5 tests pass (3 existing + 2 new)

**Step 5: Run full suite**

Run: `pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add agent_memory/store.py tests/test_store.py
git commit -m "feat: adaptive relation expansion in inject() with token budget"
```

---

### Task 9: Final integration test and cleanup

**Files:**
- Test: `tests/test_integration.py` (new file)

**Step 1: Write integration test**

```python
# tests/test_integration.py
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
        store.state.set(f"session_docs:{sid}", [id1, id2])
        store.session.end(sid, summary="Set up connection pooling")
        rels = store._conn.execute(
            "SELECT * FROM doc_relations WHERE (doc_id_a=? OR doc_id_b=?)",
            (id1, id1),
        ).fetchall()
        assert len(rels) >= 1

        # 6. Search from "frontend" project — should see global but not backend-scoped
        store.save(title="Frontend CSS reset", content="normalize.css",
                   source="session_note", project="frontend")
        results = store.search("PostgreSQL", project="frontend")
        assert len(results) >= 1  # global arch decision visible
        results_pool = store.search("Connection pool", project="frontend")
        assert len(results_pool) == 0  # backend-only, not visible

        # 7. Search from "backend" — should see both
        results_backend = store.search("Connection pool", project="backend")
        assert len(results_backend) >= 1
```

**Step 2: Run test**

Run: `pytest tests/test_integration.py -v`
Expected: PASS (all previous tasks built the required functionality)

**Step 3: Run full suite with coverage**

Run: `pytest tests/ -v --cov=agent_memory --cov-report=term-missing`
Expected: All tests pass, coverage improved from 50% baseline

**Step 4: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: add end-to-end integration test for namespace + dedup + relations"
```

---

## Summary

| Task | Phase | What | Files Changed |
|------|-------|------|---------------|
| 1 | 1 | Schema: project column | db.py, test_db.py |
| 2 | 1 | save() project param | store.py, test_store.py |
| 3 | 1 | search() namespace filter | search.py, store.py, test_store.py |
| 4 | 1 | MCP project injection | mcp_server.py |
| 5 | 2 | FTS5 dedup + merge | store.py, test_store.py |
| 6 | 3 | Schema: doc_relations | db.py, test_db.py |
| 7 | 3 | Auto-relate on session end | session.py, store.py, test_session.py |
| 8 | 3 | Adaptive inject expansion | store.py, test_store.py |
| 9 | 3 | Integration test | test_integration.py |

**Guard command (run after every task):** `pytest tests/ -v`
