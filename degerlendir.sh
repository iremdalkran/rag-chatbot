#!/usr/bin/env bash
# Cevap kalitesi değerlendirmesi.
# Kullanım:
#   bash degerlendir.sh ornekler/degerlendirme/sorular.xlsx
#   bash degerlendir.sh benim_sorularim.xlsx --hakem-model qwen3:30b-a3b
#   bash degerlendir.sh benim_sorularim.xlsx --sadece 5        (ilk 5 soruyla hızlı deneme)
# Raporlar degerlendirme/raporlar/ klasörüne yazılır. Gerçek verileriniz (data/) kullanılmaz.

set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Önce uygulamayı bir kez başlatın (bash baslat.sh); Python ortamı o sırada kurulur."
  exit 1
fi
if [ $# -eq 0 ]; then
  echo "Kullanım: bash degerlendir.sh SORU_DOSYASI.xlsx [--hakem-model MODEL] [--sadece N]"
  echo "Boş şablon: degerlendirme/sablon.xlsx   Örnek set: ornekler/degerlendirme/sorular.xlsx"
  exit 1
fi
# Ollama açık mı?
OLLAMA_URL="$(grep -E '^OLLAMA_URL=' .env 2>/dev/null | cut -d= -f2- || true)"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
if ! curl -fs "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  if [ "$(uname)" = "Darwin" ] && open -a Ollama >/dev/null 2>&1; then
    echo "Ollama başlatılıyor…"
    for _ in $(seq 1 30); do curl -fs "$OLLAMA_URL/api/tags" >/dev/null 2>&1 && break; sleep 1; done
  fi
  curl -fs "$OLLAMA_URL/api/tags" >/dev/null 2>&1 || { echo "Ollama çalışmıyor. Ollama uygulamasını açıp tekrar deneyin."; exit 1; }
fi

# --hakem-model verildiyse ve bu bilgisayarda yoksa indir (yalnızca ilk seferde).
JUDGE=""
PREV=""
for arg in "$@"; do
  case "$arg" in
    --hakem-model=*) JUDGE="${arg#--hakem-model=}" ;;
  esac
  if [ "$PREV" = "--hakem-model" ]; then JUDGE="$arg"; fi
  PREV="$arg"
done
if [ -n "$JUDGE" ]; then
  INSTALLED="$(ollama list 2>/dev/null | awk 'NR>1 {print $1}')"
  if ! printf '%s\n' "$INSTALLED" | grep -qx -e "$JUDGE" -e "$JUDGE:latest"; then
    FREE_GB="$(df -Pk . | awk 'NR==2 {print int($4 / 1024 / 1024)}')"
    echo "Hakem modeli '$JUDGE' bu bilgisayarda yok; indirilecek (yalnızca ilk seferde, internet gerekir)."
    echo "Diskte boş yer: ${FREE_GB} GB. (qwen3:30b-a3b için yaklaşık 19 GB gerekir.)"
    if [ "$FREE_GB" -lt 22 ]; then
      echo "Diskte yeterli boş yer olmayabilir. Yer açıp tekrar deneyin ya da daha küçük bir hakem seçin."
      exit 1
    fi
    ollama pull "$JUDGE"
  fi
fi

exec .venv/bin/python -m degerlendirme "$@"
