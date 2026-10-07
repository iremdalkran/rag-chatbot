"""
auth.py
-------
Kullanıcı hesapları ve oturumlar.

- Şifreler bcrypt ile hash'lenir, düz metin hiçbir yerde saklanmaz.
- Giriş yapınca rastgele bir oturum anahtarı üretilir. Tarayıcıya HttpOnly çerez olarak
  verilir (JavaScript okuyamaz → olası bir XSS açığında bile çalınamaz). Veritabanında
  anahtarın kendisi değil SHA-256 özeti tutulur.
- Oturumlar SESSION_DAYS gün sonra düşer; çıkış yapınca sunucuda da silinir.
- Art arda yanlış şifre denemesi o e-posta için girişi bir süre kilitler.
- İlk kayıt olan kullanıcı yönetici (admin) olur.
"""

import hashlib
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt

from app import config, db


class AuthError(Exception):
    """Kullanıcıya gösterilecek, beklenen hatalar (yanlış şifre, e-posta çakışması vb.)."""


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public_user(row) -> dict:
    return {"id": row["id"], "name": row["name"], "email": row["email"], "is_admin": bool(row["is_admin"])}


def _validate(name: str, email: str, password: str) -> tuple:
    name = (name or "").strip()
    email = (email or "").strip().lower()
    if not name:
        raise AuthError("İsim boş olamaz.")
    if len(name) > 100:
        raise AuthError("İsim çok uzun.")
    if "@" not in email or "." not in email.split("@")[-1] or len(email) > 254:
        raise AuthError("Geçerli bir e-posta girin.")
    if len(password or "") < config.MIN_PASSWORD_LENGTH:
        raise AuthError(f"Şifre en az {config.MIN_PASSWORD_LENGTH} karakter olmalı.")
    if len(password.encode("utf-8")) > 72:
        raise AuthError("Şifre en fazla 72 karakter olabilir.")
    return name, email


def user_count() -> int:
    with db.get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def registration_open() -> bool:
    return config.ALLOW_REGISTRATION or user_count() == 0


def create_user(name: str, email: str, password: str, is_admin: bool = False) -> dict:
    name, email = _validate(name, email, password)
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    with db.get_conn() as conn:
        # Sistemdeki ilk kullanıcı her zaman yönetici olur.
        first_user = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise AuthError("Bu e-posta ile zaten bir hesap var.")
        cur = conn.execute(
            "INSERT INTO users (name, email, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, email, password_hash, int(is_admin or first_user), db.now()),
        )
        row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _public_user(row)


# --- Kaba kuvvet (brute force) koruması: e-posta başına başarısız deneme sayacı ---
_failed_logins: dict = {}
_failed_lock = threading.Lock()


def _check_lock(email: str) -> None:
    with _failed_lock:
        entry = _failed_logins.get(email)
        if entry and entry["locked_until"] > time.time():
            minutes = int((entry["locked_until"] - time.time()) // 60) + 1
            raise AuthError(f"Çok fazla hatalı deneme. Lütfen {minutes} dakika sonra tekrar deneyin.")


def _register_failure(email: str) -> None:
    with _failed_lock:
        entry = _failed_logins.setdefault(email, {"count": 0, "locked_until": 0.0})
        entry["count"] += 1
        if entry["count"] >= config.LOGIN_MAX_ATTEMPTS:
            entry["locked_until"] = time.time() + config.LOGIN_LOCK_MINUTES * 60
            entry["count"] = 0


def _clear_failures(email: str) -> None:
    with _failed_lock:
        _failed_logins.pop(email, None)


# Kullanıcı yokken de bcrypt çalıştırıyoruz ki cevap süresi "bu e-posta kayıtlı mı"yı ele vermesin.
_DUMMY_HASH = bcrypt.hashpw(b"dummy-password", bcrypt.gensalt()).decode("utf-8")


def authenticate(email: str, password: str) -> dict:
    email = (email or "").strip().lower()
    _check_lock(email)
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    stored = row["password_hash"] if row else _DUMMY_HASH
    ok = bcrypt.checkpw((password or "").encode("utf-8")[:72], stored.encode("utf-8"))
    if not row or not ok:
        _register_failure(email)
        raise AuthError("E-posta veya şifre yanlış.")
    _clear_failures(email)
    return _public_user(row)


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=config.SESSION_DAYS)
    with db.get_conn() as conn:
        # Fırsat bu fırsat süresi dolmuş oturumları temizle.
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (db.now(),))
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (_hash_token(token), user_id, db.now(), expires.isoformat()),
        )
    return token


def get_session_user(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    with db.get_conn() as conn:
        row = conn.execute(
            """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = ? AND s.expires_at > ?""",
            (_hash_token(token), db.now()),
        ).fetchone()
    return _public_user(row) if row else None


def delete_session(token: Optional[str]) -> None:
    if not token:
        return
    with db.get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))


def change_password(user_id: int, old_password: str, new_password: str) -> None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row or not bcrypt.checkpw((old_password or "").encode("utf-8")[:72], row["password_hash"].encode("utf-8")):
        raise AuthError("Mevcut şifre yanlış.")
    set_password(user_id, new_password)


def set_password(user_id: int, new_password: str) -> None:
    if len(new_password or "") < config.MIN_PASSWORD_LENGTH:
        raise AuthError(f"Şifre en az {config.MIN_PASSWORD_LENGTH} karakter olmalı.")
    if len(new_password.encode("utf-8")) > 72:
        raise AuthError("Şifre en fazla 72 karakter olabilir.")
    password_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    with db.get_conn() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
        # Şifre değişince tüm açık oturumlar kapanır.
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


# --- Yönetici işlemleri ---

def list_users() -> list:
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    return [_public_user(r) | {"created_at": r["created_at"]} for r in rows]


def delete_user(user_id: int, acting_user_id: int) -> None:
    if user_id == acting_user_id:
        raise AuthError("Kendi hesabınızı silemezsiniz.")
    with db.get_conn() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
            raise AuthError("Kullanıcı bulunamadı.")
        # Tam metin dizini yabancı anahtara bağlı olmadığı için elle temizleniyor; geri kalan
        # her şey (oturumlar, sohbetler, dokümanlar, parçalar) ON DELETE CASCADE ile silinir.
        conn.execute(
            """DELETE FROM chunks_fts WHERE rowid IN (
                   SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id
                   WHERE d.owner_id = ?)""",
            (user_id,),
        )
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def set_admin(user_id: int, is_admin: bool, acting_user_id: int) -> None:
    if user_id == acting_user_id and not is_admin:
        raise AuthError("Kendi yönetici yetkinizi kaldıramazsınız.")
    with db.get_conn() as conn:
        conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (int(is_admin), user_id))
