#!/usr/bin/env bash
# Deney: resmî PageIndex SDK'sı (pip install pageindex) ile İş Kanunu testi — tamamen yerel (Ollama).
#   1. İş Kanunu soru dosyasını hazırlar (bkz. is_kanunu_testi.sh).
#   2. Resmî SDK'yı ayrı bir Python ortamına (.venv-pageindex) kurar (yalnızca ilk seferde).
#   3. Kanunu resmî PageIndex Flash ile indeksler, 15 soruyu kütüphanenin kendi arama ajanıyla sorar
#      (arama modeli: qwen3:30b-a3b) ve aynı hakemle puanlar.
#   4. Adil karşılaştırma için bizim vektör yöntemimizi de iki modelle ölçer: qwen3:14b ve qwen3:30b-a3b.
# Kullanım:  bash pageindex_resmi_testi.sh
# Süre: yaklaşık 1 saat.

set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Önce uygulamayı bir kez başlatın (bash baslat.sh); Python ortamı o sırada kurulur."
  exit 1
fi
OLLAMA_URL="$(grep -E '^OLLAMA_URL=' .env 2>/dev/null | cut -d= -f2- || true)"
export OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
if ! curl -fs "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  if [ "$(uname)" = "Darwin" ] && open -a Ollama >/dev/null 2>&1; then
    echo "Ollama başlatılıyor…"
    for _ in $(seq 1 30); do curl -fs "$OLLAMA_URL/api/tags" >/dev/null 2>&1 && break; sleep 1; done
  fi
  curl -fs "$OLLAMA_URL/api/tags" >/dev/null 2>&1 || { echo "Ollama çalışmıyor. Ollama uygulamasını açıp tekrar deneyin."; exit 1; }
fi
if ! curl -fs "$OLLAMA_URL/api/tags" | grep -q '"qwen3:30b-a3b"'; then
  echo "⬇️  qwen3:30b-a3b indiriliyor (yaklaşık 19 GB, yalnızca ilk seferde)…"
  ollama pull qwen3:30b-a3b
fi

echo "── 1/4 Soru dosyası hazırlanıyor"
.venv/bin/python -m degerlendirme.is_kanunu

if [ ! -x .venv-pageindex/bin/python ]; then
  echo "── 2/4 Resmî PageIndex SDK kuruluyor (yalnızca ilk seferde, birkaç dakika)"
  PY=""
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
  done
  "$PY" -m venv .venv-pageindex
  .venv-pageindex/bin/pip install -q --upgrade pip
  .venv-pageindex/bin/pip install -q -r requirements-pageindex.txt
else
  echo "── 2/4 Resmî PageIndex SDK zaten kurulu"
fi

RUN=""
command -v caffeinate >/dev/null 2>&1 && RUN="caffeinate -i"   # Mac uykuya geçip ölçümü kesmesin

echo "── 3/4 Resmî PageIndex (yerel) testi"
$RUN .venv-pageindex/bin/python -m degerlendirme.pageindex_resmi

echo "── 4/4 Bizim vektör yöntemimiz (qwen3:14b ve qwen3:30b-a3b)"
$RUN .venv/bin/python -m degerlendirme.karsilastir ornekler/is_kanunu/sorular.xlsx \
  --parca 1200 --topk 6 --modeller qwen3:14b,qwen3:30b-a3b
