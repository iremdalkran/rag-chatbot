"""
auth.py
-------
Basit, PAYLAŞILAN ŞİFREYLE giriş sistemi.

Bu, kullanıcı bazlı hesaplar/roller İÇERMEZ — tek bir ortak şifre var (APP_PASSWORD ortam
değişkeni), "bu şifreyi bilen herkes içeri girer" mantığı. Küçük bir iç araç için yeterli bir
koruma seviyesi. Tam kullanıcı ayrımı (herkesin sadece kendi verisini görmesi) için kullanıcı
tablosu, kimlik doğrulama ve veriye sahiplik (ownership) eklemek gerekir — bu, ayrı ve daha
büyük bir iş.

Akış:
  1. Kullanıcı şifreyi girer -> POST /login
  2. Şifre doğruysa, rastgele bir "oturum token'ı" üretilir ve Postgres'e kaydedilir
     (Postgres'e kaydediyoruz ki Render yeniden başlasa bile oturumlar geçerli kalsın)
  3. Frontend bu token'ı her istekte X-Auth-Token header'ında gönderir
  4. Korumalı endpoint'ler (upload, chat, chats...) bu token'ı doğrular
"""

import os
import secrets
from datetime import datetime, timezone
from typing import Optional

import psycopg2
from dotenv import load_dotenv

load_dotenv()

APP_PASSWORD = os.getenv("APP_PASSWORD")
DATABASE_URL = os.getenv("DATABASE_URL")


def _connect():
    return psycopg2.connect(DATABASE_URL)


def init_sessions_table():
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


def check_password(password: str) -> bool:
    """Girilen şifreyi APP_PASSWORD ortam değişkeniyle karşılaştırır."""
    if not APP_PASSWORD:
        # APP_PASSWORD tanımlanmamışsa, güvenlik açısından erişimi tamamen kapatıyoruz
        # (yanlışlıkla şifresiz/açık kalmasın diye).
        return False
    return password == APP_PASSWORD


def create_session() -> str:
    """Yeni bir oturum token'ı üretir, Postgres'e kaydeder, token'ı döner."""
    token = secrets.token_urlsafe(32)
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sessions (token, created_at) VALUES (%s, %s)",
        (token, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    cur.close()
    conn.close()
    return token


def is_valid_session(token: Optional[str]) -> bool:
    """Verilen token Postgres'teki sessions tablosunda var mı diye kontrol eder."""
    if not token:
        return False
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT token FROM sessions WHERE token = %s", (token,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row is not None


init_sessions_table()
