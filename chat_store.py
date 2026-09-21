"""
chat_store.py
-------------
Sol menüdeki sohbet listesini ve mesaj geçmişini KALICI olarak saklayan modül.

ÖNEMLİ DEĞİŞİKLİK: Önceki sürüm yerel bir SQLite dosyası (chats.db) kullanıyordu — Render'da
her deploy'da bu dosya siliniyordu. Şimdi Supabase'in ücretsiz barındırdığı bir PostgreSQL
veritabanına bağlanıyoruz; bu veritabanı Render'dan tamamen bağımsız olduğu için deploy'lar
arasında hiç silinmiyor, kalıcı.

Bağlantı bilgisi DATABASE_URL ortam değişkeninden okunuyor:
  - Yerelde: .env dosyasına DATABASE_URL=postgresql://... satırını eklemeniz gerekiyor.
  - Render'da: Environment sekmesinden aynı adla eklemeniz gerekiyor.

İki tablo var:
  chats(id, title, created_at)
  messages(id, chat_id, role, content, table_json, sql, chart_json, suggestions_json, created_at)
"""

import os
import json
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")


def _connect():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL ortam değişkeni tanımlı değil. .env dosyanıza (yerelde) ve "
            "Render > Environment sekmesine (canlıda) Supabase bağlantı adresini eklemeniz gerekiyor."
        )
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            chat_id INTEGER NOT NULL REFERENCES chats(id),
            role TEXT NOT NULL,
            content TEXT,
            table_json TEXT,
            sql TEXT,
            chart_json TEXT,
            suggestions_json TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_title(question: str) -> str:
    question = question.strip()
    return question[:40] + ("…" if len(question) > 40 else "")


def create_chat(title: Optional[str] = None) -> int:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO chats (title, created_at) VALUES (%s, %s) RETURNING id",
        (title or "Yeni sohbet", _now()),
    )
    chat_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return chat_id


def list_chats() -> list:
    conn = _connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, title, created_at FROM chats ORDER BY id DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


def chat_exists(chat_id: int) -> bool:
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT id FROM chats WHERE id = %s", (chat_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row is not None


def maybe_set_title_from_first_message(chat_id: int, question: str):
    """Sohbetin ilk kullanıcı mesajından otomatik başlık üretir (hâlâ 'Yeni sohbet' ise)."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT title FROM chats WHERE id = %s", (chat_id,))
    row = cur.fetchone()
    if row and row[0] == "Yeni sohbet":
        cur.execute("UPDATE chats SET title = %s WHERE id = %s", (_make_title(question), chat_id))
        conn.commit()
    cur.close()
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
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO messages
           (chat_id, role, content, table_json, sql, chart_json, suggestions_json, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
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
    cur.close()
    conn.close()


def get_messages(chat_id: int) -> list:
    conn = _connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM messages WHERE chat_id = %s ORDER BY id ASC", (chat_id,))
    rows = cur.fetchall()
    cur.close()
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