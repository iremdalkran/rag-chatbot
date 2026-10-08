"""
config.py
---------
Bütün ayarlar tek yerde. Değerler .env dosyasından (veya ortam değişkenlerinden) okunur;
.env yoksa aşağıdaki varsayılanlar kullanılır. Hiçbir ayar dış bir servise işaret etmez:
tek dış bağlantı OLLAMA_URL'dir ve o da varsayılan olarak bu bilgisayardır (localhost).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "evet", "on")


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


# --- Ollama (yerel yapay zekâ) ---
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
CHAT_MODEL = os.getenv("CHAT_MODEL", "qwen3:14b")
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")
# Modelin aynı anda "görebildiği" metin miktarı (token). Ollama'nın varsayılanı küçüktür ve
# aşılırsa metni sessizce keser — doküman parçaları + sohbet geçmişi için yeterli alan açıyoruz.
LLM_CONTEXT_TOKENS = _int("LLM_CONTEXT_TOKENS", 8192)
# Qwen3 gibi "düşünen" modellerde düşünme adımını kapatır: cevaplar çok daha hızlı gelir.
LLM_DISABLE_THINKING = _bool("LLM_DISABLE_THINKING", True)
LLM_TIMEOUT_SECONDS = _int("LLM_TIMEOUT_SECONDS", 300)

# --- Veri klasörü (veritabanı, yüklenen tablolar) ---
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).resolve()
DB_PATH = DATA_DIR / "app.db"
TABLES_DIR = DATA_DIR / "tables"

# --- Doküman işleme ---
MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 50)
CHUNK_CHARS = _int("CHUNK_CHARS", 1200)
CHUNK_OVERLAP_CHARS = _int("CHUNK_OVERLAP_CHARS", 200)
RETRIEVAL_TOP_K = _int("RETRIEVAL_TOP_K", 6)
HISTORY_MESSAGES = _int("HISTORY_MESSAGES", 6)

# --- Doküman arama yöntemi ---
# "vector"    : parçalara bölme + anlam/kelime araması (varsayılan, hızlı).
# "pageindex" : PageIndex yöntemi — her doküman için içindekiler ağacı kurulur, model soruya göre
#               ağaçta ilgili bölümleri seçer ve o sayfaları okur (vektör yok).
RAG_METHOD = os.getenv("RAG_METHOD", "vector").strip().lower()
if RAG_METHOD not in ("vector", "pageindex"):
    RAG_METHOD = "vector"
# Ağaç ne zaman kurulsun? "auto": sadece RAG_METHOD=pageindex iken. "always": her yüklemede.
PAGEINDEX_BUILD = os.getenv("PAGEINDEX_BUILD", "auto").strip().lower()
PAGEINDEX_MAX_PAGES_PER_NODE = _int("PAGEINDEX_MAX_PAGES_PER_NODE", 4)   # daha uzun bölümler bölünür
PAGEINDEX_MAX_NODES = _int("PAGEINDEX_MAX_NODES", 4)                     # soru başına okunacak en fazla bölüm
PAGEINDEX_MAX_CONTEXT_CHARS = _int("PAGEINDEX_MAX_CONTEXT_CHARS", 12000) # modele verilecek en fazla metin
PAGEINDEX_GROUP_CHARS = _int("PAGEINDEX_GROUP_CHARS", 12000)             # ağaç çıkarılırken bir seferde okunan metin
PAGEINDEX_THINK = _bool("PAGEINDEX_THINK", False)                        # ağaç aramasında "düşünme" açık mı

# --- Güvenlik ---
SESSION_DAYS = _int("SESSION_DAYS", 7)
MIN_PASSWORD_LENGTH = _int("MIN_PASSWORD_LENGTH", 8)
# İlk kullanıcı her zaman kayıt olabilir (o yönetici olur). Sonrasında kendi kendine kayıt
# varsayılan olarak kapalıdır; kullanıcıları yönetici ekler.
ALLOW_REGISTRATION = _bool("ALLOW_REGISTRATION", False)
# HTTPS arkasında çalışıyorsanız true yapın (çerez sadece şifreli bağlantıda gönderilir).
COOKIE_SECURE = _bool("COOKIE_SECURE", False)
LOGIN_MAX_ATTEMPTS = _int("LOGIN_MAX_ATTEMPTS", 5)
LOGIN_LOCK_MINUTES = _int("LOGIN_LOCK_MINUTES", 15)
