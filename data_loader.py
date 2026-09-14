"""
data_loader.py
----------------
"Data RAG" katmanının ingestion (yükleme) kısmı.

Mevcut projede embed_and_store.py PDF metnini parça parça (chunk) embed'leyip
Chroma'ya "dokuman_chunklari" koleksiyonuna yazıyordu. Bu modül aynı RAG mantığını
tablo verisine uyguluyor:

  - CSV/Excel dosyası okunur, bir SQLite tablosuna yazılır (gerçek sorgular buradan çalışır).
  - Tablonun ŞEMASI (kolon adları/tipleri) + birkaç örnek satır, insan-okunur bir metne
    dönüştürülüp Voyage ile embed'lenir ve Chroma'da "veri_semalari" koleksiyonuna yazılır.

Böylece bir soru geldiğinde, hangi tablo/kolonların o soruyla alakalı olduğu retrieval ile
bulunabilir (tıpkı PDF chunk'larında olduğu gibi), sonra bu bağlam SQL üretimi için kullanılır.
"""

import os
import re
import sqlite3
from typing import Optional

import pandas as pd
import voyageai
import chromadb

from dotenv import load_dotenv

load_dotenv()

DB_PATH = "./data.db"
CHROMA_PATH = "./chroma_db"
SCHEMA_COLLECTION_NAME = "veri_semalari"

voyage_client = voyageai.Client(api_key=os.getenv("VOYAGE_API_KEY"))
chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)
schema_collection = chroma_client.get_or_create_collection(name=SCHEMA_COLLECTION_NAME)


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


def _schema_document(table_name: str, df: pd.DataFrame) -> str:
    """Retrieval'da kullanılacak insan-okunur şema açıklaması üretir."""
    columns_desc = []
    for col in df.columns:
        dtype = str(df[col].dtype)
        sample_vals = df[col].dropna().unique()[:3]
        sample_str = ", ".join(str(v) for v in sample_vals)
        columns_desc.append(f"  - {col} ({dtype}) — örnek değerler: {sample_str}")

    sample_rows = df.head(3).to_string(index=False)

    doc = f"""Tablo adı: {table_name}
Satır sayısı: {len(df)}
Kolonlar:
{chr(10).join(columns_desc)}

Örnek satırlar:
{sample_rows}
"""
    return doc


def load_tabular_file(file_path: str, table_name: Optional[str] = None) -> dict:
    """
    CSV/Excel dosyasını okur, SQLite'a yazar, şema dokümanını embed'leyip Chroma'ya kaydeder.

    Returns: {"table_name": ..., "row_count": ..., "columns": [...]}
    """
    df = _read_any_table(file_path)
    if df.empty:
        raise ValueError("Dosya boş görünüyor.")

    if table_name is None:
        table_name = _slugify_table_name(file_path)

    # Kolon adlarını da SQL-dostu hale getir
    df.columns = [re.sub(r"[^a-zA-Z0-9_]", "_", str(c)).strip("_").lower() for c in df.columns]

    # 1) SQLite'a yaz (gerçek sorgular burada çalışacak)
    conn = sqlite3.connect(DB_PATH)
    df.to_sql(table_name, conn, if_exists="replace", index=False)
    conn.close()

    # 2) Şema dokümanını embed'le ve Chroma'ya kaydet (retrieval için)
    schema_doc = _schema_document(table_name, df)
    embedding = voyage_client.embed([schema_doc], model="voyage-3.5", input_type="document").embeddings[0]

    # Aynı tablo tekrar yüklenirse eski kaydı temizle
    try:
        schema_collection.delete(ids=[table_name])
    except Exception:
        pass

    schema_collection.add(
        ids=[table_name],
        embeddings=[embedding],
        documents=[schema_doc],
        metadatas=[{"table_name": table_name, "row_count": len(df)}],
    )

    return {"table_name": table_name, "row_count": len(df), "columns": list(df.columns)}


def has_any_table() -> bool:
    return schema_collection.count() > 0