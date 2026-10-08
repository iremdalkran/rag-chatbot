"""
is_kanunu.py
------------
Uzun doküman testi: 4857 sayılı İş Kanunu ve 15 soru.

  1. Kanunun resmi metnini (PDF) mevzuat.gov.tr'den indirir. Bu, herkese açık bir dosyanın
     indirilmesidir; sizin verilerinizden hiçbir şey dışarı gönderilmez. İndirilemezse, PDF'i
     tarayıcıdan indirip ornekler/is_kanunu/is_kanunu.pdf olarak kaydetmeniz yeterli.
  2. Her sorunun cevabının geçtiği maddeyi metinde bulur ve "Kaynak sayfa" sütununu PDF'in gerçek
     sayfa numaralarıyla doldurur (elle sayfa saymaya gerek kalmaz).
  3. Doğru cevabın gerçekten o maddede yazdığını kontrol eder; kanun değişmiş ve cevap artık
     tutmuyorsa uyarır. "Dokümanda olmayan" sorularda da cevabın metinde GEÇMEDİĞİNİ kontrol eder.
  4. ornekler/is_kanunu/sorular.xlsx dosyasını yazar.

Kullanım: bash is_kanunu_testi.sh   (hazırlık + iki yöntemin karşılaştırması)
"""

import argparse
import sys
import urllib.request
from pathlib import Path

from .sorular import TYPE_DOC, TYPE_NONE, write_question_file

ROOT = Path(__file__).resolve().parent.parent
FOLDER = ROOT / "ornekler" / "is_kanunu"
PDF_NAME = "is_kanunu.pdf"
PDF_URL = "https://www.mevzuat.gov.tr/MevzuatMetin/1.5.4857.pdf"

# (soru, doğru cevap, madde no, maddede geçmesi gereken ifadelerden en az biri)
DOCUMENT_QUESTIONS = [
    ("Haftalık çalışma süresi en fazla kaç saattir?",
     "Haftada en çok 45 saattir.", 63, ["kırkbeş", "kırk beş", "45"]),
    ("Bir işçiye bir yılda en fazla kaç saat fazla çalışma yaptırılabilir?",
     "Yılda en fazla 270 saat.", 41, ["ikiyüzyetmiş", "iki yüz yetmiş", "270"]),
    ("Fazla çalışmanın her saati için ödenecek ücret, normal saat ücretine göre ne kadar artırılır?",
     "Normal saat ücretinin yüzde elli (%50) yükseltilmesiyle ödenir.", 41, ["yüzde elli", "%50"]),
    ("7 yıldır aynı işyerinde çalışıyorum. Yılda kaç gün ücretli izin hakkım var?",
     "20 gün (kıdemi beş yıldan fazla, on beş yıldan az olanlar için).", 53, ["yirmi gün", "20 gün"]),
    ("Doğum yapacak bir kadın işçi doğumdan önce ve sonra toplam kaç hafta izin kullanır?",
     "Toplam 16 hafta: doğumdan önce 8, doğumdan sonra 8 hafta. Çoğul gebelikte doğumdan önceki süreye "
     "2 hafta eklenir.", 74, ["onaltı", "on altı", "16"]),
    ("İki buçuk yıldır çalışan bir işçinin sözleşmesini feshetmek isteyen işveren kaç hafta önceden "
     "bildirimde bulunmalıdır?",
     "6 hafta önceden (işi bir buçuk yıldan üç yıla kadar sürmüş işçi için).", 17, ["altı hafta", "6 hafta"]),
    ("Deneme süresi en fazla ne kadar olabilir?",
     "En çok iki ay; toplu iş sözleşmeleriyle dört aya kadar uzatılabilir.", 15, ["iki ay"]),
    ("Günde 6 saat çalışan bir işçiye en az ne kadar ara dinlenmesi verilmelidir?",
     "Yarım saat (dört saatten fazla, yedi buçuk saate kadar süren işlerde).", 68, ["yarım saat"]),
    ("Bir işçi gece en fazla kaç saat çalıştırılabilir?",
     "Gece çalışması yedi buçuk saati geçemez.", 69, ["yedi buçuk", "7,5"]),
    ("Maaşım ödeme gününden itibaren kaç gün içinde ödenmezse çalışmaktan kaçınabilirim?",
     "Ücret ödeme gününden itibaren 20 gün içinde ödenmezse işçi iş görmekten kaçınabilir.", 34,
     ["yirmi gün", "20 gün"]),
    ("İş güvencesi hükümlerinden yararlanmak için işyerinde en az kaç işçi çalışmalı ve işçinin kıdemi "
     "en az ne kadar olmalıdır?",
     "Otuz veya daha fazla işçi çalışan bir işyeri ve en az altı aylık kıdem.", 18, ["otuz", "30"]),
    ("100 işçi çalıştıran özel sektöre ait bir işyeri en az kaç engelli işçi çalıştırmak zorundadır?",
     "3 engelli işçi (özel sektör işyerlerinde işçilerin yüzde üçü).", 30, ["yüzde üç", "%3"]),
]

# (soru, metinde GEÇMEMESİ gereken ifadeler — geçiyorsa soru "dokümanda olmayan" sayılamaz)
NOT_IN_DOCUMENT = [
    ("Emekli olabilmek için en az kaç gün prim ödemiş olmak gerekir?", ["prim gün", "emeklilik yaşı"]),
    ("Yurt dışına görevlendirilen bir işçiye günlük ne kadar harcırah ödenir?", ["harcırah"]),
    ("İşten çıkarılan bir işçi kaç ay süreyle işsizlik ödeneği alabilir?", ["ay süreyle işsizlik ödeneği"]),
]


class PrepareError(Exception):
    pass


def download(target: Path, log=print) -> None:
    log(f"⬇️  İş Kanunu indiriliyor: {PDF_URL}")
    request = urllib.request.Request(PDF_URL, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
    except OSError as e:
        raise PrepareError(
            f"İndirilemedi ({e}). Tarayıcıda {PDF_URL} adresini açıp PDF'i indirin ve şu adla kaydedin: {target}"
        ) from e
    if not data.startswith(b"%PDF"):
        raise PrepareError(f"İndirilen dosya PDF değil. Tarayıcıda {PDF_URL} adresini açıp PDF'i indirin "
                           f"ve şu adla kaydedin: {target}")
    target.write_bytes(data)


def _article_nodes(pages: list) -> dict:
    """{madde no: bölüm} — PageIndex'in kullandığı aynı kanun ayrıştırıcısıyla."""
    from app import pageindex

    headings = pageindex._legal_headings(pages)
    if not headings:
        raise PrepareError("PDF'te kanun maddeleri ('Madde 12 –') bulunamadı. PDF metin içermiyor olabilir.")
    nodes = pageindex._build_nodes(headings, pages)
    articles = {}
    for node in pageindex._walk(nodes):
        title = node["title"]
        if title.startswith("Madde "):
            number = title.split()[1]
            if number.isdigit():
                articles.setdefault(int(number), node)
    return articles


def prepare(folder: Path = FOLDER, log=print) -> Path:
    """PDF'i (yoksa) indirir, soru dosyasını yazar ve yolunu döner."""
    from app import ingest, pageindex

    folder.mkdir(parents=True, exist_ok=True)
    pdf = folder / PDF_NAME
    if not pdf.exists():
        download(pdf, log)
    pages = [p.text for p in ingest.extract_pages(PDF_NAME, pdf.read_bytes())]
    articles = _article_nodes(pages)
    log(f"📄 {PDF_NAME}: {len(pages)} sayfa, {len(articles)} madde bulundu")

    rows, warnings = [], []
    for question, answer, number, must_contain in DOCUMENT_QUESTIONS:
        node = articles.get(number)
        if node is None:
            raise PrepareError(f"Madde {number} metinde bulunamadı.")
        text = pageindex._norm(pageindex._node_text(node, pages, 100000))
        if not any(pageindex._norm(word) in text for word in must_contain):
            warnings.append(f"Madde {number}: '{must_contain[0]}' ifadesi bulunamadı — soru: {question}")
        page_list = ", ".join(str(n) for n in range(node["start"], node["end"] + 1))
        rows.append((question, answer, PDF_NAME, page_list, TYPE_DOC))
    whole = pageindex._norm("\n".join(pages))
    for question, must_not_contain in NOT_IN_DOCUMENT:
        found = [w for w in must_not_contain if pageindex._norm(w) in whole]
        if found:
            warnings.append(f"'{found[0]}' metinde geçiyor; bu soru 'dokümanda olmayan' sayılmamalı: {question}")
        rows.append((question, "", "", "", TYPE_NONE))

    path = folder / "sorular.xlsx"
    write_question_file(path, rows)
    log(f"📝 {len(rows)} soru yazıldı: {path}")
    for warning in warnings:
        log(f"⚠️  {warning}")
    if warnings:
        log("   Uyarılı soruları kontrol edin (kanun değişmiş olabilir); test yine de çalışır.")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="is_kanunu", description="İş Kanunu test setini hazırlar.")
    parser.add_argument("--klasor", type=Path, default=FOLDER)
    args = parser.parse_args(argv)
    try:
        prepare(args.klasor)
    except PrepareError as e:
        print(f"✖ {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
