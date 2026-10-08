#!/usr/bin/env bash
# Ayar karşılaştırması: değerlendirmeyi her seferinde tek bir ayarı değiştirerek tekrarlar
# (parça boyutu, bulunan parça sayısı, cevap modeli) ve sonuçları tek bir tabloda toplar.
# Kullanım:
#   bash ayar_karsilastir.sh ornekler/degerlendirme/sorular.xlsx
#   bash ayar_karsilastir.sh sorular.xlsx --tekrar 2              (her denemeyi 2 kez çalıştır)
#   bash ayar_karsilastir.sh sorular.xlsx --parca 800,1200 --topk 4,6 --modeller qwen3:8b,qwen3:14b
# Süre: deneme başına birkaç dakika (varsayılan 6-7 deneme → yaklaşık 30-60 dakika).

set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Önce uygulamayı bir kez başlatın (bash baslat.sh); Python ortamı o sırada kurulur."
  exit 1
fi
if [ $# -eq 0 ]; then
  echo "Kullanım: bash ayar_karsilastir.sh SORU_DOSYASI.xlsx [--tekrar N] [--parca 600,1200,2000] [--topk 3,6,10] [--modeller qwen3:8b,qwen3:14b]"
  exit 1
fi
OLLAMA_URL="$(grep -E '^OLLAMA_URL=' .env 2>/dev/null | cut -d= -f2- || true)"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
if ! curl -fs "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  if [ "$(uname)" = "Darwin" ] && open -a Ollama >/dev/null 2>&1; then
    echo "Ollama başlatılıyor…"
    for _ in $(seq 1 30); do curl -fs "$OLLAMA_URL/api/tags" >/dev/null 2>&1 && break; sleep 1; done
  fi
  curl -fs "$OLLAMA_URL/api/tags" >/dev/null 2>&1 || { echo "Ollama çalışmıyor. Ollama uygulamasını açıp tekrar deneyin."; exit 1; }
fi
# Mac uyku moduna geçip ölçümü yarıda kesmesin.
if command -v caffeinate >/dev/null 2>&1; then
  exec caffeinate -i .venv/bin/python -m degerlendirme.karsilastir "$@"
fi
exec .venv/bin/python -m degerlendirme.karsilastir "$@"
