#!/usr/bin/env bash
# Doküman Asistanı — tek komutla başlatma (macOS ve Linux).
# Kullanım:  ./baslat.sh
# Yaptıkları: Ollama'yı kontrol eder, eksik modelleri indirir, Python ortamını hazırlar,
# uygulamayı başlatır ve tarayıcıda açar. İkinci çalıştırmadan itibaren birkaç saniye sürer.

set -euo pipefail
cd "$(dirname "$0")"

# .env içindeki ayarları oku (varsa)
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
CHAT_MODEL="${CHAT_MODEL:-qwen3:14b}"
EMBED_MODEL="${EMBED_MODEL:-bge-m3}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

say() { printf "\n\033[1;34m▶ %s\033[0m\n" "$1"; }
fail() { printf "\n\033[1;31m✖ %s\033[0m\n" "$1"; exit 1; }

# 1) Ollama
say "Ollama kontrol ediliyor"
if ! command -v ollama >/dev/null 2>&1; then
  fail "Ollama yüklü değil. https://ollama.com/download adresinden indirip kurun, sonra bu betiği tekrar çalıştırın."
fi
if ! curl -fs "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  echo "Ollama çalışmıyor, başlatılıyor…"
  if [ "$(uname)" = "Darwin" ] && open -a Ollama >/dev/null 2>&1; then :; else
    (ollama serve >/dev/null 2>&1 &)
  fi
  for _ in $(seq 1 30); do
    curl -fs "$OLLAMA_URL/api/tags" >/dev/null 2>&1 && break
    sleep 1
  done
  curl -fs "$OLLAMA_URL/api/tags" >/dev/null 2>&1 || fail "Ollama başlatılamadı. Ollama uygulamasını elle açıp tekrar deneyin."
fi

# 2) Modeller (ilk seferde birkaç GB indirilir; sonra hiç internet gerekmez)
for model in "$CHAT_MODEL" "$EMBED_MODEL"; do
  if ollama list | awk 'NR>1 {print $1}' | grep -qx -e "$model" -e "$model:latest"; then
    echo "✓ $model hazır"
  else
    say "$model indiriliyor (yalnızca ilk seferde, internet gerekir)"
    ollama pull "$model"
  fi
done

# 3) Python ortamı
say "Python ortamı hazırlanıyor"
PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    PY="$candidate"; break
  fi
done
[ -n "$PY" ] || fail "Python 3.10 veya daha yenisi gerekli. https://www.python.org/downloads/ adresinden kurun (veya: brew install python@3.12)."

if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
fi
REQ_HASH="$(shasum requirements.txt 2>/dev/null || sha1sum requirements.txt)"
if [ ! -f .venv/.req-hash ] || [ "$(cat .venv/.req-hash)" != "$REQ_HASH" ]; then
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
  echo "$REQ_HASH" > .venv/.req-hash
fi
echo "✓ Python ortamı hazır"

# 4) Başlat
URL="http://127.0.0.1:$PORT"
say "Uygulama başlatılıyor: $URL  (durdurmak için Ctrl+C, bu pencereyi kapatmayın)"
if [ "$HOST" = "0.0.0.0" ]; then
  echo "Şirket ağındaki diğer bilgisayarlar şu adresle bağlanabilir: http://$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}'):$PORT"
fi
# Tarayıcıyı ancak uygulama gerçekten cevap vermeye başlayınca aç (ilk açılış bir dakikayı bulabilir).
(
  for _ in $(seq 1 120); do
    if curl -fs "$URL/api/health" >/dev/null 2>&1; then
      printf "\n\033[1;32m✓ Hazır! Tarayıcıda açın: %s\033[0m\n\n" "$URL" >&2
      (command -v open >/dev/null && open "$URL") || (command -v xdg-open >/dev/null && xdg-open "$URL") || true
      exit 0
    fi
    sleep 1
  done
) >/dev/null &
exec .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT"
