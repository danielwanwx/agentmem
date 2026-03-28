"""agent_memory.db — SQLite connection and schema initialisation."""
import sqlite3
from pathlib import Path

# Try to import sqlite-vec for HNSW vector indexing.
# Graceful degradation: if unavailable, search falls back to brute-force cosine.
try:
    import sqlite_vec as _sqlite_vec
    _VEC_AVAILABLE = True
except ImportError:
    _sqlite_vec = None
    _VEC_AVAILABLE = False

# Embedding dimensionality — must match EMBED_MODEL in vector.py
VEC_DIMS = 4096

DB_PATH = Path.home() / ".agentmem" / "memory.db"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    timestamp   REAL DEFAULT (julianday('now')),
    project     TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    project     TEXT,
    topic       TEXT,
    started_at  REAL DEFAULT (julianday('now')),
    ended_at    REAL,
    summary     TEXT,
    key_facts   TEXT,
    decisions   TEXT,
    open_items  TEXT,
    source      TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    doc_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    summary     TEXT,
    key_facts   TEXT,
    decisions   TEXT,
    code_sigs   TEXT,
    metrics     TEXT,
    raw_content TEXT,
    priority    TEXT DEFAULT 'P1',
    source      TEXT DEFAULT 'explicit',
    generator   TEXT DEFAULT 'rule',
    embedding   BLOB,
    file_path   TEXT,
    created_at  REAL DEFAULT (julianday('now')),
    expires_at  REAL
);

CREATE INDEX IF NOT EXISTS idx_documents_priority ON documents(priority);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);
-- Note: idx_documents_file_path is created in _migrate() after ensuring file_path column exists

-- FTS5 index over structured fields (content table = documents, no data duplication)
-- trigram tokenizer: handles Chinese substrings + English without spaces needed
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    title,
    summary,
    key_facts,
    decisions,
    raw_content,
    content='documents',
    content_rowid='doc_id',
    tokenize='trigram'
);

-- Keep FTS in sync with documents table
CREATE TRIGGER IF NOT EXISTS docs_fts_insert
AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, title, summary, key_facts, decisions, raw_content)
    VALUES (new.doc_id, new.title, new.summary,
            new.key_facts, new.decisions, new.raw_content);
END;

CREATE TRIGGER IF NOT EXISTS docs_fts_update
AFTER UPDATE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, summary, key_facts, decisions, raw_content)
    VALUES ('delete', old.doc_id, old.title, old.summary,
            old.key_facts, old.decisions, old.raw_content);
    INSERT INTO documents_fts(rowid, title, summary, key_facts, decisions, raw_content)
    VALUES (new.doc_id, new.title, new.summary,
            new.key_facts, new.decisions, new.raw_content);
END;

CREATE TRIGGER IF NOT EXISTS docs_fts_delete
AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, summary, key_facts, decisions, raw_content)
    VALUES ('delete', old.doc_id, old.title, old.summary,
            old.key_facts, old.decisions, old.raw_content);
END;

CREATE TABLE IF NOT EXISTS state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL DEFAULT (julianday('now'))
);

"""

def _migrate(conn: sqlite3.Connection) -> None:
    """Apply forward-only schema migrations for existing databases."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN source TEXT")
        conn.commit()

    doc_cols = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    if "file_path" not in doc_cols:
        conn.execute("ALTER TABLE documents ADD COLUMN file_path TEXT")
        conn.commit()
    if "last_accessed_at" not in doc_cols:
        conn.execute("ALTER TABLE documents ADD COLUMN last_accessed_at REAL")
        conn.commit()
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

    # Create partial unique index if not present (idempotent)
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_file_path "
            "ON documents(file_path) WHERE file_path IS NOT NULL"
        )
        conn.commit()
    except Exception:
        pass

    # Create vec_documents HNSW table (only if sqlite-vec is loaded)
    try:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_documents "
            f"USING vec0(document_id INTEGER PRIMARY KEY, embedding float[{VEC_DIMS}])"
        )
        conn.commit()
        # Backfill vec_documents from existing document embeddings.
        # Only backfill vectors with correct dimensionality (VEC_DIMS * 4 bytes).
        expected_bytes = VEC_DIMS * 4
        indexed_ids = {
            row[0] for row in conn.execute("SELECT document_id FROM vec_documents").fetchall()
        }
        rows = conn.execute(
            "SELECT doc_id, embedding FROM documents "
            "WHERE embedding IS NOT NULL AND length(embedding) = ?",
            (expected_bytes,),
        ).fetchall()
        to_insert = [(row["doc_id"], row["embedding"]) for row in rows
                     if row["doc_id"] not in indexed_ids]
        if to_insert:
            conn.executemany(
                "INSERT INTO vec_documents(document_id, embedding) VALUES (?, ?)",
                to_insert,
            )
            conn.commit()
    except Exception:
        pass  # sqlite-vec not loaded or vec0 unavailable

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

    # Rebuild FTS5 if it lacks raw_content column (migration from old schema)
    try:
        # Check if raw_content is in the FTS5 index by trying a query
        conn.execute("SELECT raw_content FROM documents_fts LIMIT 0")
    except Exception:
        # raw_content not in FTS5 — rebuild
        try:
            conn.execute("DROP TABLE IF EXISTS documents_fts")
            conn.execute("DROP TRIGGER IF EXISTS docs_fts_insert")
            conn.execute("DROP TRIGGER IF EXISTS docs_fts_update")
            conn.execute("DROP TRIGGER IF EXISTS docs_fts_delete")
            conn.commit()
            # Re-run schema to recreate FTS5 with raw_content + triggers
            conn.executescript(SCHEMA)
        except Exception:
            pass

    # Backfill FTS index for any documents not yet indexed
    try:
        indexed = conn.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        if indexed < total:
            conn.execute("""
                INSERT INTO documents_fts(rowid, title, summary, key_facts, decisions, raw_content)
                SELECT doc_id, title, summary, key_facts, decisions, raw_content
                FROM documents
                WHERE doc_id NOT IN (
                    SELECT rowid FROM documents_fts
                )
            """)
            conn.commit()
    except Exception:
        pass  # FTS not available or already in sync


def _load_vec_extension(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec extension. Returns True if loaded successfully."""
    if not _VEC_AVAILABLE:
        return False
    try:
        conn.enable_load_extension(True)
        _sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False


def init_db(path: str = None) -> sqlite3.Connection:
    """Open (or create) the memory DB, run schema, return connection."""
    db_path = path or str(DB_PATH)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Load sqlite-vec BEFORE running schema (vec0 virtual table needs it)
    _load_vec_extension(conn)
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn
