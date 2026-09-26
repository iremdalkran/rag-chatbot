"""
auth.py
-------
GERÇEK kullanıcı hesapları sistemi (önceki "tek paylaşılan şifre" sürümünün yerine geçti).

İki tablo:
  users(id, name, email, password_hash, created_at)     — her kullanıcının hesabı
  sessions(token, user_id, created_at)                  — giriş yapınca üretilen oturum

Şifreler ASLA düz metin olarak saklanmıyor — bcrypt ile hash'leniyor. bcrypt, aynı şifreden
her seferinde FARKLI bir hash üretir (rastgele bir "salt" eklediği için), ama doğrulama
(checkpw) yine de doğru çalışır. Bu sayede veritabanı bir şekilde ele geçirilse bile şifreler
doğrudan okunamaz.

Akış:
  1. Kayıt: POST /register {name, email, password} -> hesap oluşur, otomatik giriş yapılır
  2. Giriş: POST /login {email, password} -> doğruysa oturum token'ı üretilir
  3. Frontend bu token'ı her istekte X-Auth-Token header'ında gönderir
  4. Korumalı endpoint'ler bu token'dan hangi kullanıcı olduğunu (get_session_user) bulur
"""

import os
import secrets
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.errors
import bcrypt
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")


class AuthError(Exception):
    """Kullanıcıya gösterilecek, beklenen giriş/kayıt hataları için (yanlış şifre, e-posta çakışması vb.)."""
    pass


def _connect():
    return psycopg2.connect(DATABASE_URL)


def init_tables():
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def register_user(name: str, email: str, password: str) -> dict:
    """Yeni kullanıcı kaydı oluşturur. Geçersizse ya da e-posta zaten kayıtlıysa AuthError fırlatır."""
    name = name.strip()
    email = email.strip().lower()

    if not name:
        raise AuthError("İsim boş olamaz.")
    if not email or "@" not in email:
        raise AuthError("Geçerli bir e-posta girin.")
    if len(password) < 4:
        raise AuthError("Şifre en az 4 karakter olmalı.")

    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    conn = _connect()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO users (name, email, password_hash, created_at) VALUES (%s, %s, %s, %s) RETURNING id",
            (name, email, password_hash, _now()),
        )
        user_id = cur.fetchone()[0]
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        raise AuthError("Bu e-posta ile zaten bir hesap var.")
    finally:
        cur.close()
        conn.close()

    return {"id": user_id, "name": name, "email": email}


def authenticate_user(email: str, password: str) -> dict:
    """E-posta+şifre doğruysa kullanıcı bilgisini döner, değilse AuthError fırlatır."""
    email = email.strip().lower()

    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT id, name, email, password_hash FROM users WHERE email = %s", (email,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    # Not: "e-posta yok" ve "şifre yanlış" için AYNI mesajı veriyoruz — böylece bir saldırgan
    # hangi e-postaların sistemde kayıtlı olduğunu bu şekilde deneme yanılmayla öğrenemez.
    if not row:
        raise AuthError("E-posta veya şifre yanlış.")

    user_id, name, user_email, password_hash = row
    if not bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8")):
        raise AuthError("E-posta veya şifre yanlış.")

    return {"id": user_id, "name": name, "email": user_email}


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sessions (token, user_id, created_at) VALUES (%s, %s, %s)",
        (token, user_id, _now()),
    )
    conn.commit()
    cur.close()
    conn.close()
    return token


def get_session_user(token: Optional[str]) -> Optional[dict]:
    """Token geçerliyse {id, name, email} döner, geçersiz/eksikse None döner."""
    if not token:
        return None

    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT u.id, u.name, u.email
        FROM sessions s JOIN users u ON s.user_id = u.id
        WHERE s.token = %s
    """, (token,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        return None
    return {"id": row[0], "name": row[1], "email": row[2]}


init_tables()