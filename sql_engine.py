"""
sql_engine.py
-------------
"Data RAG" akışının çekirdeği. query.py'deki PDF-RAG akışıyla birebir aynı iskeleti izler:

    1. RETRIEVE : soru embed edilir, Chroma'daki "veri_semalari" koleksiyonunda en alakalı
                   tablo şema(ları) bulunur.
    2. AUGMENT  : bulunan şema + örnek satırlar, Claude'a bağlam olarak verilir.
    3. GENERATE : Claude sadece bu bağlama dayanarak bir SQL (SELECT) sorgusu üretir.
    4. EXECUTE  : sorgu güvenlik kontrolünden geçirilip SQLite üzerinde çalıştırılır.
    5. ANSWER   : sonuç tablosu + soru, Claude'a tekrar verilir; kısa bir doğal dil cevabı ve
                  3 takip sorusu önerisi üretilir.

Sonuç, index.html'in tablo + grafik + "Kullanılan SQL" + öneri çipleri olarak göstereceği
yapıya (JSON) dönüştürülür.
"""

import os
import re
import json
import sqlite3

import pandas as pd
import voyageai
import anthropic

from dotenv import load_dotenv
from data_loader import schema_collection, DB_PATH

load_dotenv()

voyage_client = voyageai.Client(api_key=os.getenv("VOYAGE_API_KEY"))
anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

FORBIDDEN_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|attach|detach|pragma|vacuum|create|replace)\b",
    re.IGNORECASE,
)


class SQLEngineError(Exception):
    pass


def retrieve_schema_context(question: str, top_k: int = 3):
    """1) RETRIEVE — soruyla en alakalı tablo şemalarını Chroma'dan bulur."""
    result = voyage_client.embed([question], model="voyage-3.5", input_type="query")
    question_embedding = result.embeddings[0]

    n = min(top_k, max(schema_collection.count(), 1))
    results = schema_collection.query(query_embeddings=[question_embedding], n_results=n)

    docs = results["documents"][0] if results["documents"] else []
    tables = [m["table_name"] for m in results["metadatas"][0]] if results["metadatas"] else []
    return docs, tables


def generate_sql(question: str, schema_context: str) -> str:
    """2+3) AUGMENT + GENERATE — Claude'dan sadece bağlamdaki tablolara dair güvenli bir SELECT sorgusu ister."""
    system_prompt = f"""Sen bir metinden-SQL asistanısın. Sadece SQLite için geçerli, SADECE SELECT
içeren tek bir sorgu üretirsin.

Kurallar:
1. Sadece aşağıda şeması verilen tablo(lar)ı ve kolonları kullan. Şemada olmayan tablo/kolon UYDURMA.
2. Sorgu SADECE SELECT ile başlamalı. INSERT/UPDATE/DELETE/DROP/ALTER/PRAGMA gibi ifadeler YASAK.
3. Cevabında SADECE SQL sorgusunu döndür. Açıklama, markdown, kod bloğu işareti (```), yorum yazma.
4. Soru veriyle ilgili değilse veya cevaplanamıyorsa tek satırda şunu yaz: NO_QUERY

Tablo şeması / bağlam:
{schema_context}"""

    message = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=system_prompt,
        messages=[{"role": "user", "content": question}],
    )
    sql = message.content[0].text.strip()
    sql = re.sub(r"^```sql|```$|^```", "", sql, flags=re.IGNORECASE).strip()
    return sql


def _validate_sql(sql: str) -> None:
    if not sql or sql.upper() == "NO_QUERY":
        raise SQLEngineError("NO_QUERY")
    if not sql.strip().lower().startswith("select"):
        raise SQLEngineError("Güvenlik: sadece SELECT sorgularına izin veriliyor.")
    if FORBIDDEN_KEYWORDS.search(sql):
        raise SQLEngineError("Güvenlik: sorguda izin verilmeyen bir anahtar kelime var.")
    if ";" in sql.strip().rstrip(";"):
        raise SQLEngineError("Güvenlik: tek seferde yalnızca bir sorguya izin veriliyor.")


def execute_sql(sql: str) -> pd.DataFrame:
    """4) EXECUTE — doğrulanan sorguyu SQLite üzerinde çalıştırır."""
    _validate_sql(sql)
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql_query(sql, conn)
    finally:
        conn.close()
    return df


def summarize_and_suggest(question: str, sql: str, df: pd.DataFrame) -> dict:
    """5) ANSWER — sonuç tablosuna bakarak kısa bir cevap + 3 takip sorusu önerisi üretir."""
    preview = df.head(10).to_csv(index=False)
    system_prompt = """Sen bir veri analistisin. Sana bir soru, çalıştırılan SQL sorgusu ve
sonuç tablosunun bir önizlemesi verilecek. Görevin:

1. Sonuca dayanarak 2-3 cümlelik, sade bir Türkçe özet cevap yaz (rakamları/ürün adlarını kullan).
2. Kullanıcının sorabileceği 3 adet kısa takip sorusu öner (mevcut veriyle cevaplanabilir olmalı).

SADECE şu JSON formatında cevap ver, başka hiçbir şey yazma:
{"answer": "...", "suggestions": ["...", "...", "..."]}"""

    user_content = f"Soru: {question}\n\nÇalıştırılan SQL:\n{sql}\n\nSonuç önizlemesi (ilk 10 satır):\n{preview}"

    message = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    raw = message.content[0].text.strip()
    raw = re.sub(r"^```json|```$|^```", "", raw, flags=re.IGNORECASE).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"answer": raw, "suggestions": []}
    return parsed


def _infer_chart(df: pd.DataFrame):
    """Basit sezgisel kural: ilk kolon kategori, sayısal kolon(lar) seri olsun. Uygun değilse None."""
    if df.empty or len(df.columns) < 2:
        return None
    numeric_cols = [c for c in df.columns[1:] if pd.api.types.is_numeric_dtype(df[c])]
    if not numeric_cols:
        return None
    label_col = df.columns[0]
    return {
        "style": "bar",
        "x_labels": df[label_col].astype(str).tolist()[:25],
        "series": [
            {"name": c, "values": df[c].fillna(0).tolist()[:25]} for c in numeric_cols[:4]
        ],
    }


def ask_data(question: str) -> dict:
    """Tüm Data RAG akışını uçtan uca çalıştırır ve arayüzün ihtiyacı olan JSON'u döner."""
    docs, tables = retrieve_schema_context(question)
    if not docs:
        return {
            "mode": "data",
            "answer": "Henüz analiz edebileceğim bir veri tablosu yok. Lütfen önce bir CSV/Excel dosyası yükleyin.",
            "sql": None,
            "table": None,
            "chart": None,
            "suggestions": [],
        }

    schema_context = "\n\n---\n\n".join(docs)

    sql = generate_sql(question, schema_context)
    try:
        _validate_sql(sql)
    except SQLEngineError as e:
        if str(e) == "NO_QUERY":
            return {
                "mode": "data",
                "answer": "Bu soruyu mevcut verilerle ilişkilendiremedim. Elimdeki tablolarla "
                          "ilgili (satış, ciro, adet vb.) bir soru sorabilir misiniz?",
                "sql": None,
                "table": None,
                "chart": None,
                "suggestions": [],
            }
        raise

    df = execute_sql(sql)
    summary = summarize_and_suggest(question, sql, df)

    return {
        "mode": "data",
        "answer": summary.get("answer", ""),
        "sql": sql,
        "table": {
            "columns": list(df.columns),
            "rows": df.astype(object).where(pd.notnull(df), None).values.tolist(),
            "row_count": len(df),
        },
        "chart": _infer_chart(df),
        "suggestions": summary.get("suggestions", []),
        "used_tables": tables,
    }
