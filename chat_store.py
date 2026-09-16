"""
chat_store.py
-------------
Sol menüdeki sohbet listesi ve her sohbetin mesaj geçmişini saklayan modül.

Ayrı bir SQLite dosyası (chats.db) kullanıyor — data.db (yüklenen Excel/CSV tabloları) ile
karışmasın diye. İki tablo var:

  chats(id, title, created_at)
  messages(id, chat_id, role, content, table_json, sql, chart_json, suggestions_json, created_at)

role: "user" ya da "assistant"
table_json / chart_json / suggestions_json: JSON string olarak saklanır (None olabilir).
"""

import sqlite3
import json
from datetime import datetime, timezone
from typing import Optional

CHATS_DB_PATH = "./chats.db"


def _connect():
    conn = sqlite3.connect(CHATS_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            table_json TEXT,
            sql TEXT,
            chart_json TEXT,
            suggestions_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (chat_id) REFERENCES chats(id)
        )
    """)
    conn.commit()
    conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_title(question: str) -> str:
    question = question.strip()
    return question[:40] + ("…" if len(question) > 40 else "")


def create_chat(title: Optional[str] = None) -> int:
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO chats (title, created_at) VALUES (?, ?)",
        (title or "Yeni sohbet", _now()),
    )
    conn.commit()
    chat_id = cur.lastrowid
    conn.close()
    return chat_id


def list_chats() -> list:
    conn = _connect()
    rows = conn.execute("SELECT id, title, created_at FROM chats ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def chat_exists(chat_id: int) -> bool:
    conn = _connect()
    row = conn.execute("SELECT id FROM chats WHERE id = ?", (chat_id,)).fetchone()
    conn.close()
    return row is not None


def maybe_set_title_from_first_message(chat_id: int, question: str):
    """Sohbetin ilk kullanıcı mesajından otomatik başlık üretir (hâlâ 'Yeni sohbet' ise)."""
    conn = _connect()
    row = conn.execute("SELECT title FROM chats WHERE id = ?", (chat_id,)).fetchone()
    if row and row["title"] == "Yeni sohbet":
        conn.execute("UPDATE chats SET title = ? WHERE id = ?", (_make_title(question), chat_id))
        conn.commit()
    conn.close()


def add_message(
    chat_id: int,
    role: str,
    content: Optional[str] = None,
    table: Optional[dict] = None,
    sql: Optional[str] = None,
    chart: Optional[dict] = None,
    suggestions: Optional[list] = None,
):
    conn = _connect()
    conn.execute(
        """INSERT INTO messages
           (chat_id, role, content, table_json, sql, chart_json, suggestions_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            chat_id,
            role,
            content,
            json.dumps(table) if table is not None else None,
            sql,
            json.dumps(chart) if chart is not None else None,
            json.dumps(suggestions) if suggestions is not None else None,
            _now(),
        ),
    )
    conn.commit()
    conn.close()


def get_messages(chat_id: int) -> list:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM messages WHERE chat_id = ? ORDER BY id ASC", (chat_id,)
    ).fetchall()
    conn.close()

    result = []
    for r in rows:
        item = {
            "role": r["role"],
            "content": r["content"],
            "sql": r["sql"],
            "table": json.loads(r["table_json"]) if r["table_json"] else None,
            "chart": json.loads(r["chart_json"]) if r["chart_json"] else None,
            "suggestions": json.loads(r["suggestions_json"]) if r["suggestions_json"] else None,
            "created_at": r["created_at"],
        }
        result.append(item)
    return result


init_db()
