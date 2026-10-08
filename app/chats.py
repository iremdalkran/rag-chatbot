"""
chats.py
--------
Sohbet geçmişi. Her sohbet bir kullanıcıya aittir; bütün okuma/yazma işlemleri sohbetin
gerçekten o kullanıcıya ait olduğunu kontrol eder.
"""

import json
from typing import List, Optional

from app import db

DEFAULT_TITLE = "Yeni sohbet"


def _title_from(question: str) -> str:
    question = " ".join(question.split())
    return question[:60] + ("…" if len(question) > 60 else "")


def create_chat(user_id: int) -> dict:
    now = db.now()
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO chats (user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (user_id, DEFAULT_TITLE, now, now),
        )
    return {"id": cur.lastrowid, "title": DEFAULT_TITLE, "created_at": now, "updated_at": now}


def list_chats(user_id: int) -> List[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM chats WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_chat(chat_id: int, user_id: int) -> Optional[dict]:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, title, created_at, updated_at FROM chats WHERE id = ? AND user_id = ?", (chat_id, user_id)
        ).fetchone()
    return dict(row) if row else None


def rename_chat(chat_id: int, user_id: int, title: str) -> bool:
    title = " ".join((title or "").split())[:100]
    if not title:
        return False
    with db.get_conn() as conn:
        cur = conn.execute("UPDATE chats SET title = ? WHERE id = ? AND user_id = ?", (title, chat_id, user_id))
    return cur.rowcount > 0


def delete_chat(chat_id: int, user_id: int) -> bool:
    with db.get_conn() as conn:
        cur = conn.execute("DELETE FROM chats WHERE id = ? AND user_id = ?", (chat_id, user_id))
    return cur.rowcount > 0


def add_message(chat_id: int, role: str, content: str, extra: Optional[dict] = None, question: str = None) -> None:
    now = db.now()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (chat_id, role, content, extra_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, role, content or "", json.dumps(extra, ensure_ascii=False) if extra else None, now),
        )
        conn.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id))
        if question:
            conn.execute(
                "UPDATE chats SET title = ? WHERE id = ? AND title = ?", (_title_from(question), chat_id, DEFAULT_TITLE)
            )


def get_messages(chat_id: int, user_id: int) -> List[dict]:
    if not get_chat(chat_id, user_id):
        return []
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT role, content, extra_json, created_at FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,)
        ).fetchall()
    result = []
    for r in rows:
        item = {"role": r["role"], "content": r["content"], "created_at": r["created_at"]}
        if r["extra_json"]:
            item.update(json.loads(r["extra_json"]))
        result.append(item)
    return result


def last_answer_mode(chat_id: int) -> Optional[str]:
    """Bu sohbetteki son cevap nereden geldi? ('docs', 'data' ya da bilinmiyorsa None)"""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT extra_json FROM messages WHERE chat_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    if not row or not row["extra_json"]:
        return None
    return json.loads(row["extra_json"]).get("mode")


def history_for_llm(chat_id: int, limit: int) -> List[dict]:
    """Modele verilecek son mesajlar (sadece rol + metin, eskiden yeniye)."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? AND content != '' ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
