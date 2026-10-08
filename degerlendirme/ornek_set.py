"""
ornek_set.py
------------
Boş şablonu (degerlendirme/sablon.xlsx) ve hemen denenebilecek örnek soru setini
(ornekler/degerlendirme/) üretir. Excel sorularının doğru cevapları veriden HESAPLANIR,
elle yazılmaz; böylece örnek setin kendisi hatalı olamaz.

Çalıştırmak için:  .venv/bin/python -m degerlendirme.ornek_set
"""

from datetime import date
from pathlib import Path

import pandas as pd

from .sorular import TYPE_DATA, TYPE_DOC, TYPE_NONE, write_question_file

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "degerlendirme" / "sablon.xlsx"
EXAMPLE_DIR = ROOT / "ornekler" / "degerlendirme"

REGIONS = ["İstanbul", "Ankara", "İzmir"]
PRODUCTS = [("Laptop", 32000), ("Monitör", 7500), ("Klavye", 1200)]


def tr_number(value: float) -> str:
    """1234567 → '1.234.567' (Türkçe yazım)."""
    return f"{value:,.0f}".replace(",", ".")


def sales_table() -> pd.DataFrame:
    rows = []
    for month in (1, 2, 3):
        for r, region in enumerate(REGIONS):
            for p, (product, price) in enumerate(PRODUCTS):
                quantity = (month * 7 + r * 5 + p * 3) % 11 + 2 + (8 if product == "Klavye" else 0)
                rows.append({
                    "Tarih": date(2026, month, 3 + r * 7 + p),
                    "Bölge": region,
                    "Ürün": product,
                    "Adet": quantity,
                    "Birim Fiyat": price,
                    "Tutar": quantity * price,
                })
    return pd.DataFrame(rows)


def excel_questions(df: pd.DataFrame) -> list:
    total = df["Tutar"].sum()
    by_region = df.groupby("Bölge")["Tutar"].sum().sort_values(ascending=False)
    march = df[pd.to_datetime(df["Tarih"]).dt.month == 3]["Adet"].sum()
    izmir = df[df["Bölge"] == "İzmir"].groupby("Ürün")["Adet"].sum().sort_values(ascending=False)
    assert by_region.iloc[0] != by_region.iloc[1] and izmir.iloc[0] != izmir.iloc[1], "örnek veride eşitlik var"
    src = "satislar.xlsx"
    return [
        ("Toplam satış tutarı ne kadar?", f"{tr_number(total)} TL", src, "", TYPE_DATA),
        ("Hangi bölgenin toplam satış tutarı en yüksek?",
         f"{by_region.index[0]} ({tr_number(by_region.iloc[0])} TL)", src, "", TYPE_DATA),
        ("Mart 2026'da toplam kaç adet ürün satıldı?", f"{march} adet", src, "", TYPE_DATA),
        ("İzmir bölgesinde adet olarak en çok satılan ürün hangisi?",
         f"{izmir.index[0]} ({izmir.iloc[0]} adet)", src, "", TYPE_DATA),
    ]


def hard_excel_questions(df: pd.DataFrame) -> list:
    """Filtre + gruplama ya da iki dönemi karşılaştırma gerektiren sorular."""
    months = pd.to_datetime(df["Tarih"]).dt.month
    ankara_laptop = df[(df["Bölge"] == "Ankara") & (df["Ürün"] == "Laptop")]["Tutar"].sum()
    january, march = df[months == 1]["Tutar"].sum(), df[months == 3]["Tutar"].sum()
    direction = "arttı" if march > january else "azaldı"
    src = "satislar.xlsx"
    return [
        ("Ankara'da Laptop satışlarından elde edilen toplam tutar nedir?",
         f"{tr_number(ankara_laptop)} TL", src, "", TYPE_DATA),
        ("Ocak ayından Mart ayına toplam satış tutarı arttı mı azaldı mı, ne kadar?",
         f"{direction}: Ocak {tr_number(january)} TL, Mart {tr_number(march)} TL, fark {tr_number(abs(march - january))} TL",
         src, "", TYPE_DATA),
    ]


DOCUMENT_QUESTIONS = [
    ("Beş yıldan az çalışan bir personelin yıllık izni kaç gündür?", "14 iş günü",
     "personel_yonetmeligi.pdf", 2, TYPE_DOC),
    ("Evlenen bir çalışana kaç gün izin verilir?", "3 gün ücretli mazeret izni",
     "personel_yonetmeligi.pdf", 2, TYPE_DOC),
    ("Haftada en fazla kaç gün uzaktan çalışılabilir?", "Birim müdürünün onayıyla haftada en fazla 2 gün",
     "personel_yonetmeligi.pdf", 1, TYPE_DOC),
    ("Yurt içi seyahatlerde gecelik konaklama için üst sınır nedir?", "3.500 TL",
     "personel_yonetmeligi.pdf", 3, TYPE_DOC),
    ("Masraflar en geç ne zamana kadar sisteme girilmeli?", "Harcama tarihinden itibaren en geç 30 gün içinde",
     "personel_yonetmeligi.pdf", 3, TYPE_DOC),
    ("Kahve İstanbul'a ne zaman ulaştı?", "16. yüzyılın başında", "kahve_tarihi.pdf", 1, TYPE_DOC),
    ("Dünyanın en büyük kahve üreticisi hangi ülkedir?", "Brezilya (küresel üretimin yaklaşık üçte biri)",
     "kahve_tarihi.pdf", 1, TYPE_DOC),
]

# Dokümandaki kelimeleri kullanmayan, kuralı uygulamayı ya da küçük bir hesap yapmayı gerektiren sorular.
HARD_DOCUMENT_QUESTIONS = [
    ("7 yıldır bu şirketteyim, yıllık kaç gün tatil hakkım var?", "20 iş günü",
     "personel_yonetmeligi.pdf", 2, TYPE_DOC),
    ("Babam vefat etti; kaç gün izin alabilirim ve bu yıllık iznimden düşer mi?",
     "3 gün ücretli mazeret izni; yıllık izinden düşülmez", "personel_yonetmeligi.pdf", 2, TYPE_DOC),
    ("İşe yeni başladım, ilk ayımda evden çalışabilir miyim?",
     "Hayır; deneme süresindeki (2 ay) çalışanlar uzaktan çalışamaz", "personel_yonetmeligi.pdf", 1, TYPE_DOC),
    ("Ankara'ya 2 gecelik iş seyahatinde konaklamaya en fazla ne kadar harcayabilirim?",
     "Gecelik 3.500 TL, iki gece için toplam 7.000 TL", "personel_yonetmeligi.pdf", 3, TYPE_DOC),
    ("Hastalık raporu aldığım günlerin ücretini kim öder?", "İlk 2 günün ücretini şirket öder",
     "personel_yonetmeligi.pdf", 2, TYPE_DOC),
    ("Londra'daki kahvehanelere hangi ad verilmişti ve neden?",
     "'Penny üniversiteleri'; bir bardak kahve karşılığında saatlerce entelektüel tartışmalara katılmak mümkündü",
     "kahve_tarihi.pdf", 1, TYPE_DOC),
]

# Dokümandakine çok benzeyen ama cevabı dokümanda OLMAYAN sorular (en zor uydurma tuzakları).
HARD_NOT_IN_DOCUMENTS = [
    ("Yurt dışı iş seyahatlerinde gecelik konaklama üst sınırı nedir?", "", "", "", TYPE_NONE),
    ("Doğum yapan bir çalışana kaç gün doğum izni verilir?", "", "", "", TYPE_NONE),
]

NOT_IN_DOCUMENTS = [
    ("Çalışanlara özel sağlık sigortası sağlanıyor mu?", "", "", "", TYPE_NONE),
    ("Şirketin 2025 yılı net kârı ne kadar?", "", "", "", TYPE_NONE),
    ("Kahvenin kilogram fiyatı bugün kaç TL?", "", "", "", TYPE_NONE),
]


def main() -> None:
    write_question_file(TEMPLATE)
    print(f"Şablon: {TEMPLATE}")

    EXAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    df = sales_table()
    df.to_excel(EXAMPLE_DIR / "satislar.xlsx", index=False, sheet_name="Satışlar")
    rows = (DOCUMENT_QUESTIONS + excel_questions(df) + NOT_IN_DOCUMENTS
            + HARD_DOCUMENT_QUESTIONS + hard_excel_questions(df) + HARD_NOT_IN_DOCUMENTS)
    write_question_file(EXAMPLE_DIR / "sorular.xlsx", rows)
    print(f"Örnek set: {EXAMPLE_DIR} ({len(rows)} soru)")


if __name__ == "__main__":
    main()
