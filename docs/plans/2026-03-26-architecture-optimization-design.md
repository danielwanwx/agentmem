# Architecture Optimization Design

## Overview

Three architecture enhancements for `agentmem`, implemented in dependency order. All three share the same "Ollama-optional graceful degradation" design philosophy — better quality with Ollama, fully functional without.

---

## 1. Multi-Project Namespace Isolation

### Problem

All documents from all projects share one flat `documents` table. As knowledge grows, cross-project noise reduces search precision.

### Design

- Add `project TEXT` column to `documents` table
- `NULL` = global knowledge (visible to all projects)
- Non-NULL = project-private (e.g. `"myapp"`)
- `source = 'architectural_decision'` → automatically set `project = NULL` (global)
- All other sources → bind to current project from `state.get("current_project")`

### Search Behavior

Default: `WHERE (project = ? OR project IS NULL)` — returns current project + global.

FTS5 `documents_fts` is not changed (does not index `project`). Namespace filtering happens at the SQL JOIN/WHERE layer after FTS5 MATCH.

### Migration

- `ALTER TABLE documents ADD COLUMN project TEXT`
- `CREATE INDEX idx_documents_project ON documents(project)`
- Existing data stays `NULL` (treated as global) — zero migration risk

---

## 2. Write-Time Knowledge Deduplication & Merge

### Problem

Repeated `am_save` calls for the same knowledge produce duplicate documents. No dedup mechanism exists except `file_path`-based upsert.

### Design

`save()` method gains a dedup check before INSERT:

```
save(title, content, source, project, ...)
  │
  ├─ 1. Dedup detection
  │     ├─ FTS5: search title in documents_fts, same project scope
  │     │   top-1 BM25 score > threshold AND same source → candidate duplicate
  │     ├─ Vector (Ollama available): cosine similarity > 0.85 → confirmed duplicate
  │     │   cosine 0.6–0.85 → not duplicate, but create 'related' relation
  │     └─ No Ollama: FTS5-only determination
  │
  ├─ 2a. Duplicate → field-level merge UPDATE
  │     title     = new
  │     summary   = new
  │     key_facts = list(set(old + new))  # union dedup
  │     decisions = list(set(old + new))  # union dedup
  │     code_sigs = union(old, new)
  │     metrics   = union(old, new)
  │     Re-run _embed_async()
  │
  └─ 2b. Not duplicate → INSERT (existing logic + project field)
```

### Similarity Thresholds

| Method | Duplicate | Related | Unrelated |
|--------|-----------|---------|-----------|
| FTS5 BM25 | score > threshold + same source | — | below threshold |
| Vector cosine | > 0.85 | 0.6–0.85 | < 0.6 |

FTS5 is always available (write-path critical). Vector is enhancement-only.

---

## 3. Document Relation Graph

### Problem

Documents are completely isolated. Related knowledge (e.g. problem description, root cause, fix — all from one debug session) cannot be discovered via association.

### Design

#### Schema

```sql
CREATE TABLE doc_relations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id_a      INTEGER NOT NULL REFERENCES documents(doc_id),
    doc_id_b      INTEGER NOT NULL REFERENCES documents(doc_id),
    relation_type TEXT NOT NULL,  -- 'related' | 'derived_from' | 'supersedes'
    created_at    REAL DEFAULT (julianday('now')),
    UNIQUE(doc_id_a, doc_id_b, relation_type)
);

CREATE INDEX idx_doc_relations_a ON doc_relations(doc_id_a);
CREATE INDEX idx_doc_relations_b ON doc_relations(doc_id_b);
```

#### Relation Types

| Type | Meaning | Created By |
|------|---------|------------|
| `related` | Topically associated | Session promote (same session) / dedup (0.6–0.85 cosine) |
| `derived_from` | A was extracted from B | Session promote (session → document) |
| `supersedes` | A replaces B | Future: manual or dedup merge chain |

#### Automatic Relation Creation

**Session promote (`session.end()`):**
- Collect all doc_ids produced during this session
- Insert pairwise `related` relations: `INSERT OR IGNORE INTO doc_relations`

**Dedup detection (`store.save()`):**
- Cosine similarity 0.6–0.85 (similar but not duplicate) → insert `related` relation

#### Search Result Presentation (Adaptive)

```
inject(results, max_tokens=3000)
  │
  └─ For each result:
       ├─ token_budget > 300:
       │   Query doc_relations, expand up to 2 related docs as L1
       │   Format:  ├─ [related] Title
       │            │   Summary...
       │
       └─ token_budget ≤ 300:
           Append hint line only:
           "Related: #12 'YARN配置', #15 'Spark调优'"
```

**Skip expansion when:**
- Related doc already in current search results (avoid duplication)
- Related doc expired (filtered by WHERE clause)

#### Related Doc Query

```sql
SELECT d.doc_id, d.title, d.summary, d.key_facts, r.relation_type
FROM doc_relations r
JOIN documents d ON d.doc_id = CASE
    WHEN r.doc_id_a = ? THEN r.doc_id_b
    ELSE r.doc_id_a END
WHERE (r.doc_id_a = ? OR r.doc_id_b = ?)
  AND (d.expires_at IS NULL OR d.expires_at > strftime('%s','now'))
ORDER BY d.priority, d.created_at DESC
LIMIT 3
```

---

## Implementation Order

```
Phase 1: Namespace (foundation)
  db.py       — migration: add project column + index
  store.py    — save() adds project param, auto-NULL for architectural_decision
  search.py   — all queries add project WHERE clause
  mcp_server.py — am_search/am_save inject current project from state
  cli.py      — --project flag propagation

Phase 2: Dedup & Merge (data quality)
  store.py    — save() dedup detection before INSERT
                FTS5 title match + optional vector cosine
                field-level merge logic
  vector.py   — expose single-doc cosine comparison helper

Phase 3: Relation Graph (advanced)
  db.py       — migration: create doc_relations table + indexes
  store.py    — save() creates 'related' on 0.6–0.85 cosine
  session.py  — end() creates pairwise 'related' for session docs
  store.py    — inject() adaptive expansion with token budget
```

---

## Files Changed

| File | Phase | Changes |
|------|-------|---------|
| `db.py` | 1, 3 | Migration: project column, doc_relations table |
| `store.py` | 1, 2, 3 | save() project/dedup/merge, inject() expansion |
| `search.py` | 1 | Namespace WHERE clause on all search paths |
| `session.py` | 3 | Auto-relate docs on session end |
| `mcp_server.py` | 1 | Inject current project into search/save |
| `cli.py` | 1 | --project flag propagation |
| `vector.py` | 2 | Single-doc cosine helper |
| `models.py` | 3 | SearchResult gains `related` field |

## Design Principles

- **Ollama-optional degradation** — every feature works without Ollama, better with it
- **Zero-config for users** — project auto-detected, dedup automatic, relations automatic
- **Minimal migration risk** — forward-only ALTER TABLE, existing data unaffected
- **Token-budget-aware** — relation expansion respects inject() budget, never bloats context
