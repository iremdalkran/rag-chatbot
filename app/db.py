"""
db.py
-----
Uygulamanın tek veritabanı: data/app.db (SQLite). Kullanıcılar, oturumlar, sohbetler,
mesajlar, dokümanlar ve doküman parçaları (vektörleriyle birlikte) burada durur.
Ayrı bir veritabanı sunucusu kurmak gerekmez; yedek almak için data/ klasörünü kopyalamak yeter.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chats_user ON chats(user_id, updated_at);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    extra_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    shared INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error TEXT,
    page_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    page INTEGER,
    text TEXT NOT NULL,
    embedding BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);

-- Anahtar kelime araması için tam metin dizini (rowid = chunks.id).
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, tokenize = 'unicode61 remove_diacritics 2'
);

-- Dokümanların sayfa metinleri (PageIndex bölüm okurken kullanır). Sayfası olmayan türlerde
-- (Word, TXT) metin ~3000 karakterlik "sanal sayfalara" bölünür.
CREATE TABLE IF NOT EXISTS doc_pages (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_no INTEGER NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (document_id, page_no)
);

CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    table_name TEXT NOT NULL,
    filename TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    columns_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, table_name)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn():
    """`with get_conn() as conn:` — blok hatasız biterse kaydeder, hata olursa geri alır."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Sonradan eklenen kolonlar: eski veritabanlarına da eklenir (veri kaybı olmadan).
_ADDED_COLUMNS = {
    "documents": [
        ("paged", "INTEGER NOT NULL DEFAULT 1"),      # 0: Word/TXT gibi gerçek sayfası olmayan dokümanlar
        ("tree_json", "TEXT"),                         # PageIndex içindekiler ağacı
        ("tree_status", "TEXT NOT NULL DEFAULT 'none'"),  # none | pending | processing | ready | error
        ("tree_error", "TEXT"),
        ("tree_seconds", "REAL"),
    ],
}


def _migrate(conn) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.TABLES_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        # WAL modu: okuma ve yazma aynı anda yapılabilir (birden çok kullanıcı için önemli).
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        # Sunucu bir doküman işlenirken kapandıysa o doküman "işleniyor"da takılı kalmasın.
        conn.execute(
            "UPDATE documents SET status = 'error', error = 'İşlem yarıda kaldı, lütfen tekrar yükleyin.' "
            "WHERE status = 'processing'"
        )
        # Yarıda kalan ağaç kurulumları yeniden kuyruğa alınır (bkz. pageindex.resume_pending).
        conn.execute("UPDATE documents SET tree_status = 'pending' WHERE tree_status = 'processing'")
        conn.commit()
    finally:
        conn.close()
