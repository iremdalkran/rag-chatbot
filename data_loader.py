"""
data_loader.py
----------------
Basitleştirilmiş "Data RAG" ingestion katmanı.

ÖNEMLİ DEĞİŞİKLİK (önceki sürüme göre): Sistem artık her zaman TEK bir aktif veri seti ile
çalışıyor (yeni dosya yüklenince öncekini siliyor — bkz. clear_all_data). Bu sayede "hangi
tabloya bakmalıyım" sorusunu embedding/vektör arama (Chroma + Voyage) ile bulmaya hiç gerek
kalmadı: tek tablo olduğu için doğrudan SQLite şemasını okuyup Claude'a veriyoruz.

Bunun iki faydası var:
  1. Kod basitleşti — Chroma/embedding karmaşıklığı tamamen kalktı.
  2. Voyage AI'ya (embedding servisi) giden istek sayısı azaldı — rate limit sorununu hafifletir.

PDF tarafı (query.py / embed_and_store.py) hâlâ Voyage ile embedding kullanıyor, bu dosyaya
dokunmadık — sadece Excel/CSV (veri) tarafını basitleştirdik.
"""

import os
import re
import sqlite3
from typing import Optional

import pandas as pd

DB_PATH = "./data.db"


def _slugify_table_name(filename: str) -> str:
    """Dosya adını güvenli bir SQLite tablo adına çevirir (urun_satislari.csv -> urun_satislari)."""
    base = os.path.splitext(os.path.basename(filename))[0]
    base = re.sub(r"[^a-zA-Z0-9_]", "_", base).strip("_").lower()
    if not base or base[0].isdigit():
        base = f"tablo_{base}"
    return base


def _read_any_table(file_path: str) -> pd.DataFrame:
    if file_path.lower().endswith(".csv"):
        return pd.read_csv(file_path)
    elif file_path.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(file_path)
    else:
        raise ValueError("Desteklenmeyen dosya türü. Sadece .csv, .xlsx, .xls kabul edilir.")


def clear_all_data() -> None:
    """
    Önceki tüm yüklenmiş tabloları siler (tek aktif veri seti mantığı).
    Yeni bir dosya yüklenmeden hemen önce çağrılır.
    """
    conn = sqlite3.connect(DB_PATH)
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    for (name,) in tables:
        conn.execute(f'DROP TABLE IF EXISTS "{name}"')
    conn.commit()
    conn.close()


def load_tabular_file(file_path: str, table_name: Optional[str] = None) -> dict:
    """
    CSV/Excel dosyasını okur, önceki veriyi temizler, yeni veriyi SQLite'a yazar.

    Returns: {"table_name": ..., "row_count": ..., "columns": [...]}
    """
    df = _read_any_table(file_path)
    if df.empty:
        raise ValueError("Dosya boş görünüyor.")

    clear_all_data()

    if table_name is None:
        table_name = _slugify_table_name(file_path)

    # Kolon adlarını da SQL-dostu hale getir
    df.columns = [re.sub(r"[^a-zA-Z0-9_]", "_", str(c)).strip("_").lower() for c in df.columns]

    conn = sqlite3.connect(DB_PATH)
    df.to_sql(table_name, conn, if_exists="replace", index=False)
    conn.close()

    return {"table_name": table_name, "row_count": len(df), "columns": list(df.columns)}


def has_any_table() -> bool:
    conn = sqlite3.connect(DB_PATH)
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()
    return len(tables) > 0


def get_active_schema_context() -> Optional[str]:
    """
    Şu an aktif olan (tek) tablonun şema açıklamasını döner. sql_engine.py bunu doğrudan
    Claude'a bağlam olarak veriyor — embedding/vektör aramaya artık gerek yok.

    Tablo yoksa None döner.
    """
    conn = sqlite3.connect(DB_PATH)
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    if not tables:
        conn.close()
        return None

    table_name = tables[0][0]
    df_sample = pd.read_sql_query(f'SELECT * FROM "{table_name}" LIMIT 3', conn)
    row_count = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
    conn.close()

    columns_desc = []
    for col in df_sample.columns:
        dtype = str(df_sample[col].dtype)
        sample_vals = df_sample[col].dropna().unique()[:3]
        sample_str = ", ".join(str(v) for v in sample_vals)
        columns_desc.append(f"  - {col} ({dtype}) — örnek değerler: {sample_str}")

    sample_rows = df_sample.to_string(index=False)

    return f"""Tablo adı: {table_name}
Satır sayısı: {row_count}
Kolonlar:
{chr(10).join(columns_desc)}

Örnek satırlar:
{sample_rows}
"""


def get_active_table_name() -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()
    return tables[0][0] if tables else None