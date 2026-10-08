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
exec .venv/bin/python -m degerlendirme "$@"
