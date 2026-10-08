#!/usr/bin/env bash
# Uzun doküman testi: İş Kanunu (4857) ile vektör ve PageIndex yöntemlerini karşılaştırır.
#   1. Kanunun PDF'ini indirir (yalnızca ilk seferde) ve 15 soruluk test dosyasını hazırlar
#      (ornekler/is_kanunu/). Sayfa numaraları kanun metninden otomatik bulunur.
#   2. Aynı sorularla iki yöntemi karşılaştırır (bkz. ayar_karsilastir.sh).
# Kullanım:
#   bash is_kanunu_testi.sh            (hazırlık + karşılaştırma)
#   bash is_kanunu_testi.sh --hazirla  (sadece soru dosyasını hazırla)
# Süre: yaklaşık 30-45 dakika (PageIndex'in kanunu hazırlaması bunun bir kısmı).

set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Önce uygulamayı bir kez başlatın (bash baslat.sh); Python ortamı o sırada kurulur."
  exit 1
fi
.venv/bin/python -m degerlendirme.is_kanunu
if [ "${1:-}" = "--hazirla" ]; then
  exit 0
fi
exec bash ayar_karsilastir.sh ornekler/is_kanunu/sorular.xlsx --parca 1200 --topk 6 --modeller qwen3:14b \
  --yontemler vector,pageindex
