"""vector.py — ollama embedding, BLOB serialization, cosine similarity.

Graceful degradation: all public functions return None / [] if ollama is
unavailable. Callers never need to handle exceptions.
"""
import struct
import httpx

OLLAMA_URL  = "http://localhost:11434/api/embed"
EMBED_MODEL = "qwen3-embedding:8b"


def embed(texts: list[str], timeout: float = 30.0) -> list[list[float]] | None:
    """Return embeddings for a list of texts, or None if ollama is unreachable."""
    try:
        resp = httpx.post(
            OLLAMA_URL,
            json={"model": EMBED_MODEL, "input": texts},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["embeddings"]
    except Exception:
        return None


def embed_doc(title: str, summary: str, key_facts: list[str]) -> list[float] | None:
    """Embed a document using the same fields indexed by FTS5."""
    text = f"{title}\n{summary or ''}\n" + "\n".join(key_facts[:5])
    vecs = embed([text.strip()])
    return vecs[0] if vecs else None


def vec_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def blob_to_vec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na  = sum(x * x for x in a) ** 0.5
    nb  = sum(x * x for x in b) ** 0.5
    return dot / (na * nb + 1e-9)


def upsert_vec(conn, doc_id: int, vec: list[float]) -> None:
    """Insert or replace a vector in the sqlite-vec HNSW index.

    No-op if vec_documents table doesn't exist or vector dim doesn't match.
    """
    if len(vec) != 4096:
        return  # Only index 4096-dim vectors (qwen3-embedding:8b)
    try:
        blob = vec_to_blob(vec)
        conn.execute(
            "INSERT OR REPLACE INTO vec_documents(document_id, embedding) VALUES (?, ?)",
            (doc_id, blob),
        )
    except Exception:
        pass


def knn_search(conn, query_vec: list[float], k: int = 20,
               min_cosine: float = 0.3) -> list[tuple[int, float]]:
    """HNSW KNN via sqlite-vec, re-scored with cosine similarity.

    Returns [(doc_id, cosine_score), ...] sorted descending.
    Falls back to brute-force scan if vec_documents is unavailable.
    """
    # Try sqlite-vec KNN (O(log n) via HNSW)
    try:
        blob = vec_to_blob(query_vec)
        rows = conn.execute(
            "SELECT document_id, distance FROM vec_documents "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (blob, k),
        ).fetchall()
        if rows:
            # Re-score with cosine; vec returns L2 distance
            results = []
            doc_ids = [row[0] for row in rows]
            placeholders = ",".join("?" * len(doc_ids))
            emb_rows = conn.execute(
                f"SELECT doc_id, embedding FROM documents WHERE doc_id IN ({placeholders})",
                doc_ids,
            ).fetchall()
            emb_map = {r["doc_id"]: blob_to_vec(r["embedding"]) for r in emb_rows}
            for row in rows:
                doc_id = row[0]
                if doc_id in emb_map:
                    score = cosine(query_vec, emb_map[doc_id])
                    if score >= min_cosine:
                        results.append((doc_id, score))
            results.sort(key=lambda x: x[1], reverse=True)
            return results
    except Exception:
        pass

    # Brute-force fallback (vec_documents unavailable)
    # Only use 4096-dim vectors for consistency with the query vector
    db_rows = conn.execute(
        "SELECT doc_id, embedding FROM documents "
        "WHERE embedding IS NOT NULL AND length(embedding) = 16384 "
        "AND (expires_at IS NULL OR expires_at > strftime('%s','now'))"
    ).fetchall()
    if not db_rows:
        return []
    scored = [(row["doc_id"], cosine(query_vec, blob_to_vec(row["embedding"])))
              for row in db_rows]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [(doc_id, s) for doc_id, s in scored if s >= min_cosine][:k]
