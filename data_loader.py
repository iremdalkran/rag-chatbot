"""
data_loader.py
----------------
KİŞİYE ÖZEL "Data RAG" ingestion katmanı.

ÖNEMLİ DEĞİŞİKLİK: Artık her kullanıcının kendi aktif veri seti var — biri Excel yüklediğinde
sadece KENDİ önceki verisi silinip yerine yenisi geliyor, başka bir kullanıcının verisine hiç
dokunulmuyor. Bunu, SQLite tablo adlarının başına kullanıcı numarasını önek (prefix) olarak
ekleyerek yapıyoruz: "u3_urun_satislari" gibi — yani "3 numaralı kullanıcının urun_satislari
tablosu". Bir kullanıcının tabloları ararken sadece "u{user_id}_" ile başlayanlara bakıyoruz.

Şema, embedding/vektör arama olmadan doğrudan SQLite'tan okunuyor (tek aktif tablo mantığı
sayesinde buna hiç gerek yok — bkz. get_active_schema_context).
"""

import os
import re
import sqlite3
from typing import Optional

import pandas as pd

DB_PATH = "./data.db"


def _table_prefix(user_id: int) -> str:
    return f"u{user_id}_"


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


def _user_tables(conn, user_id: int) -> list:
    """Sadece bu kullanıcıya ait tabloların adlarını döner (önek eşleşmesiyle)."""
    prefix = _table_prefix(user_id)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?",
        (prefix + "%",),
    ).fetchall()
    return [r[0] for r in rows]


def clear_all_data(user_id: int) -> None:
    """
    Bu kullanıcıya ait tüm tabloları siler (tek aktif veri seti mantığı — kullanıcı bazlı).
    Başka bir kullanıcının tablolarına DOKUNMAZ. Yeni bir dosya yüklenmeden hemen önce çağrılır.
    """
    conn = sqlite3.connect(DB_PATH)
    for name in _user_tables(conn, user_id):
        conn.execute(f'DROP TABLE IF EXISTS "{name}"')
    conn.commit()
    conn.close()


def load_tabular_file(file_path: str, user_id: int, table_name: Optional[str] = None) -> dict:
    """
    CSV/Excel dosyasını okur, bu kullanıcının önceki verisini temizler, yenisini SQLite'a yazar.

    Returns: {"table_name": ..., "row_count": ..., "columns": [...]}
    """
    df = _read_any_table(file_path)
    if df.empty:
        raise ValueError("Dosya boş görünüyor.")

    clear_all_data(user_id)

    if table_name is None:
        table_name = _slugify_table_name(file_path)
    # Gerçek SQLite tablo adı, kullanıcı önekiyle birlikte
    full_table_name = _table_prefix(user_id) + table_name

    # Kolon adlarını da SQL-dostu hale getir
    df.columns = [re.sub(r"[^a-zA-Z0-9_]", "_", str(c)).strip("_").lower() for c in df.columns]

    conn = sqlite3.connect(DB_PATH)
    df.to_sql(full_table_name, conn, if_exists="replace", index=False)
    conn.close()

    # Dışarıya (frontend'e) kullanıcı önekini göstermeye gerek yok, orijinal adı döndürüyoruz
    return {"table_name": table_name, "row_count": len(df), "columns": list(df.columns)}


def has_any_table(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    tables = _user_tables(conn, user_id)
    conn.close()
    return len(tables) > 0


def get_active_schema_context(user_id: int) -> Optional[str]:
    """
    Bu kullanıcının aktif (tek) tablosunun şema açıklamasını döner. sql_engine.py bunu
    doğrudan Claude'a bağlam olarak veriyor.

    Kullanıcının hiç tablosu yoksa None döner.
    """
    conn = sqlite3.connect(DB_PATH)
    tables = _user_tables(conn, user_id)
    if not tables:
        conn.close()
        return None

    table_name = tables[0]
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


def get_active_table_name(user_id: int) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    tables = _user_tables(conn, user_id)
    conn.close()
    return tables[0] if tables else None