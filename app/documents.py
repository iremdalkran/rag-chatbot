"""
documents.py
------------
Doküman kütüphanesi: yükleme, listeleme, silme ve ARAMA.

Erişim kuralı:
  - Her kullanıcı kendi yüklediği dokümanları görür.
  - Yöneticinin "şirket dokümanı" (shared) olarak işaretlediği dokümanları herkes görür.
  - Başka birinin kişisel dokümanını kimse göremez (yönetici dahil).

Arama "hibrit"tir: hem anlam benzerliği (vektör) hem anahtar kelime eşleşmesi (tam metin)
kullanılır, sonuçlar birleştirilir. Böylece hem "izin hakkım ne kadar?" gibi doğal sorular
hem de "madde 7.3" ya da ürün kodu gibi birebir aranan ifadeler bulunur.

Yükleme arka planda işlenir; doküman bu sırada "işleniyor" durumunda görünür.
"""

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

import numpy as np

from app import config, db, ingest, llm, pageindex

log = logging.getLogger(__name__)

# Tek işçi: aynı anda tek doküman işlenir, Ollama'yı sohbet eden kullanıcılarla paylaşırken
# bilgisayarı boğmaz.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest")

# Doküman başına vektör önbelleği: {document_id: (chunk_id dizisi, vektör matrisi)}
_cache: Dict[int, Tuple[np.ndarray, np.ndarray]] = {}
_cache_lock = threading.Lock()


def _safe_filename(name: str) -> str:
    name = (name or "dosya").replace("\\", "/").split("/")[-1].strip()
    return name[:200] or "dosya"


def _row_to_doc(row, user: dict) -> dict:
    return {
        "id": row["id"],
        "filename": row["filename"],
        "shared": bool(row["shared"]),
        "status": row["status"],
        "error": row["error"],
        "page_count": row["page_count"],
        "chunk_count": row["chunk_count"],
        "size_bytes": row["size_bytes"],
        "created_at": row["created_at"],
        "owner_name": row["owner_name"],
        # PageIndex içindekiler ağacının durumu: none | pending | processing | ready | error
        "tree_status": row["tree_status"],
        "tree_error": row["tree_error"],
        "tree_nodes": pageindex.node_count(row["tree_json"]) if row["tree_status"] == "ready" else 0,
        "can_delete": row["owner_id"] == user["id"] or (bool(row["shared"]) and user["is_admin"]),
    }


def add_document(user: dict, filename: str, data: bytes, shared: bool = False) -> dict:
    filename = _safe_filename(filename)
    if not filename.lower().endswith(ingest.SUPPORTED_EXTENSIONS):
        raise ingest.IngestError("Desteklenmeyen dosya türü. PDF, Word (.docx), TXT veya MD yükleyebilirsiniz.")
    if shared and not user["is_admin"]:
        raise ingest.IngestError("Şirket dokümanı eklemek için yönetici olmanız gerekiyor.")
    with db.get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO documents (owner_id, filename, shared, status, size_bytes, created_at)
               VALUES (?, ?, ?, 'processing', ?, ?)""",
            (user["id"], filename, int(shared), len(data), db.now()),
        )
        doc_id = cur.lastrowid
    _executor.submit(_process, doc_id, filename, data)
    return get_document(user, doc_id)


def _process(doc_id: int, filename: str, data: bytes) -> None:
    try:
        pages = ingest.extract_pages(filename, data)
        outline = ingest.pdf_outline(data) if filename.lower().endswith(".pdf") else []
        chunks = ingest.chunk_pages(pages)
        vectors = llm.embed([c.text for c in chunks])
        with db.get_conn() as conn:
            # Bu arada kullanıcı dokümanı sildiyse yazmadan çık.
            if not conn.execute("SELECT 1 FROM documents WHERE id = ?", (doc_id,)).fetchone():
                return
            for chunk, vector in zip(chunks, vectors):
                cur = conn.execute(
                    "INSERT INTO chunks (document_id, chunk_index, page, text, embedding) VALUES (?, ?, ?, ?, ?)",
                    (doc_id, chunk.index, chunk.page, chunk.text, vector.astype(np.float32).tobytes()),
                )
                conn.execute("INSERT INTO chunks_fts (rowid, text) VALUES (?, ?)", (cur.lastrowid, chunk.text))
            page_count = len(pages) if pages and pages[0].number is not None else 0
            conn.execute(
                "UPDATE documents SET status = 'ready', error = NULL, page_count = ?, chunk_count = ? WHERE id = ?",
                (page_count, len(chunks), doc_id),
            )
            pageindex.store_pages(conn, doc_id, pages, outline)
        log.info("Doküman %s işlendi: %d parça", doc_id, len(chunks))
        if pageindex.build_enabled():
            # İçindekiler ağacı ayrı bir iş olarak kuyruğa girer; doküman bu arada vektör aramasıyla kullanılabilir.
            _executor.submit(pageindex.build_tree, doc_id)
    except (ingest.IngestError, llm.LLMError) as e:
        _mark_error(doc_id, str(e))
    except Exception:
        log.exception("Doküman %s işlenirken beklenmeyen hata", doc_id)
        _mark_error(doc_id, "Dosya işlenirken beklenmeyen bir hata oluştu.")


def resume_tree_builds() -> int:
    """Sunucu açılışında: ağacı kurulmayı bekleyen dokümanları kuyruğa alır."""
    pending = pageindex.pending_documents()
    for doc_id in pending:
        _executor.submit(pageindex.build_tree, doc_id)
    return len(pending)


def _mark_error(doc_id: int, message: str) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE documents SET status = 'error', error = ? WHERE id = ?", (message, doc_id))


_SELECT_DOCS = """
    SELECT d.*, u.name AS owner_name FROM documents d JOIN users u ON u.id = d.owner_id
    WHERE (d.owner_id = ? OR d.shared = 1)
"""


def list_documents(user: dict) -> List[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(_SELECT_DOCS + " ORDER BY d.shared DESC, d.id DESC", (user["id"],)).fetchall()
    return [_row_to_doc(r, user) for r in rows]


def get_document(user: dict, doc_id: int) -> Optional[dict]:
    with db.get_conn() as conn:
        row = conn.execute(_SELECT_DOCS + " AND d.id = ?", (user["id"], doc_id)).fetchone()
    return _row_to_doc(row, user) if row else None


def delete_document(user: dict, doc_id: int) -> bool:
    doc = get_document(user, doc_id)
    if not doc or not doc["can_delete"]:
        return False
    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE document_id = ?)", (doc_id,)
        )
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    with _cache_lock:
        _cache.pop(doc_id, None)
    return True


def has_ready_documents(user: dict) -> bool:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM documents WHERE (owner_id = ? OR shared = 1) AND status = 'ready' LIMIT 1",
            (user["id"],),
        ).fetchone()
    return row is not None


def _ready_doc_ids(user: dict) -> List[int]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM documents WHERE (owner_id = ? OR shared = 1) AND status = 'ready'", (user["id"],)
        ).fetchall()
    return [r["id"] for r in rows]


def _load_vectors(doc_id: int) -> Tuple[np.ndarray, np.ndarray]:
    with _cache_lock:
        if doc_id in _cache:
            return _cache[doc_id]
    with db.get_conn() as conn:
        rows = conn.execute("SELECT id, embedding FROM chunks WHERE document_id = ? ORDER BY id", (doc_id,)).fetchall()
    ids = np.array([r["id"] for r in rows], dtype=np.int64)
    matrix = (np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
              if rows else np.zeros((0, 0), dtype=np.float32))
    with _cache_lock:
        _cache[doc_id] = (ids, matrix)
    return ids, matrix


def _vector_search(doc_ids: List[int], query_vector: np.ndarray, limit: int) -> List[int]:
    all_ids, all_vectors = [], []
    for doc_id in doc_ids:
        ids, matrix = _load_vectors(doc_id)
        if len(ids) and matrix.shape[1] == query_vector.shape[0]:
            all_ids.append(ids)
            all_vectors.append(matrix)
    if not all_ids:
        return []
    ids = np.concatenate(all_ids)
    scores = np.vstack(all_vectors) @ query_vector
    order = np.argsort(-scores)[:limit]
    return [int(ids[i]) for i in order]


_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _keyword_search(doc_ids: List[int], query: str, limit: int) -> List[int]:
    words = [w for w in _WORD_RE.findall(query.lower()) if len(w) >= 3 or w.isdigit()]
    if not words or not doc_ids:
        return []
    # Her kelimeyi tırnak içine alıyoruz ki kullanıcının yazdığı şey FTS komutu gibi yorumlanmasın.
    match = " OR ".join(f'"{w}"' for w in dict.fromkeys(words))
    placeholders = ",".join("?" * len(doc_ids))
    with db.get_conn() as conn:
        rows = conn.execute(
            f"""SELECT f.rowid AS id FROM chunks_fts f JOIN chunks c ON c.id = f.rowid
                WHERE chunks_fts MATCH ? AND c.document_id IN ({placeholders})
                ORDER BY bm25(chunks_fts) LIMIT ?""",
            (match, *doc_ids, limit),
        ).fetchall()
    return [r["id"] for r in rows]


def search(user: dict, query: str, top_k: int = None) -> List[dict]:
    """Kullanıcının erişebildiği dokümanlarda soruya en uygun parçaları bulur."""
    top_k = top_k or config.RETRIEVAL_TOP_K
    doc_ids = _ready_doc_ids(user)
    if not doc_ids:
        return []
    query_vector = llm.embed([query])[0]
    pool = max(top_k * 4, 20)
    vector_hits = _vector_search(doc_ids, query_vector, pool)
    keyword_hits = _keyword_search(doc_ids, query, pool)

    # Reciprocal Rank Fusion: iki listede de üst sıralarda olan parça en yüksek puanı alır.
    scores: Dict[int, float] = {}
    for hits, weight in ((vector_hits, 1.0), (keyword_hits, 0.7)):
        for rank, chunk_id in enumerate(hits):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (60 + rank)
    best = sorted(scores, key=scores.get, reverse=True)[:top_k]
    if not best:
        return []

    placeholders = ",".join("?" * len(best))
    with db.get_conn() as conn:
        rows = conn.execute(
            f"""SELECT c.id, c.document_id, c.page, c.text, d.filename
                FROM chunks c JOIN documents d ON d.id = c.document_id
                WHERE c.id IN ({placeholders})""",
            best,
        ).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [
        {"chunk_id": cid, "document_id": by_id[cid]["document_id"], "filename": by_id[cid]["filename"],
         "page": by_id[cid]["page"], "text": by_id[cid]["text"]}
        for cid in best if cid in by_id
    ]
