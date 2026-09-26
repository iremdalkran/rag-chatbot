"""
sql_engine.py
-------------
Basitleştirilmiş Data akışı (artık "retrieval" adımı embedding değil, doğrudan SQLite şema
okuma — bkz. data_loader.get_active_schema_context). Akış:

    1. ŞEMA     : data_loader'dan aktif tablonun şema açıklaması alınır (embedding YOK).
    2. GENERATE : Claude bu şemaya dayanarak bir SQL (SELECT) sorgusu üretir.
    3. EXECUTE  : sorgu güvenlik kontrolünden geçirilip SQLite üzerinde çalıştırılır.
    4. ANSWER   : sonuç tablosu + soru, Claude'a tekrar verilir; kısa bir doğal dil cevabı ve
                  3 takip sorusu önerisi üretilir.

Sonuç, index.html'in tablo + grafik + "Kullanılan SQL" + öneri çipleri olarak göstereceği
yapıya (JSON) dönüştürülür.
"""

import os
import re
import json
import sqlite3

import pandas as pd
import anthropic

from dotenv import load_dotenv
from data_loader import DB_PATH, get_active_schema_context, get_active_table_name

load_dotenv()

anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

FORBIDDEN_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|attach|detach|pragma|vacuum|create|replace)\b",
    re.IGNORECASE,
)

PIE_KEYWORDS = ("pasta", "dağılım", "dagilim", "oran", "yüzde", "yuzde", "pie")
LINE_KEYWORDS = ("çizgi", "cizgi", "trend", "line", "zaman içinde", "zaman icinde")


class SQLEngineError(Exception):
    pass


def detect_chart_style(question: str) -> str:
    """Soru metnindeki anahtar kelimelere göre tercih edilen grafik tipini döner."""
    q = question.lower()
    if any(k in q for k in PIE_KEYWORDS):
        return "pie"
    if any(k in q for k in LINE_KEYWORDS):
        return "line"
    return "bar"


def generate_sql(question: str, schema_context: str) -> str:
    """Claude'dan, aktif tablonun şemasına dayanan güvenli bir SELECT sorgusu ister."""
    system_prompt = f"""Sen bir metinden-SQL asistanısın. Sadece SQLite için geçerli, SADECE SELECT
içeren tek bir sorgu üretirsin.

Kurallar:
1. Sadece aşağıda şeması verilen tabloyu ve kolonları kullan. Şemada olmayan tablo/kolon UYDURMA.
2. Sorgu SADECE SELECT ile başlamalı. INSERT/UPDATE/DELETE/DROP/ALTER/PRAGMA gibi ifadeler YASAK.
3. Cevabında SADECE SQL sorgusunu döndür. Açıklama, markdown, kod bloğu işareti (```), yorum yazma.
4. Soru "grafik yap", "pasta grafik yap", "görselleştir", "çizdir" gibi SPESİFİK bir kritere
   (hangi kolon/ürün/metrik olduğu) değinmeyen genel bir görselleştirme isteğiyse: NO_QUERY DEME.
   Bunun yerine, tablodaki en anlamlı kategori kolonunu ve ilk 1-2 sayısal kolonu seçip makul
   bir özet sorgusu üret. Tabloda hangi kolonlar varsa onları kullan.
5. Soru gerçekten veriyle hiçbir şekilde ilişkilendirilemiyorsa tek satırda şunu yaz: NO_QUERY

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
    """Doğrulanan sorguyu SQLite üzerinde çalıştırır."""
    _validate_sql(sql)
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql_query(sql, conn)
    finally:
        conn.close()
    return df


def summarize_and_suggest(question: str, sql: str, df: pd.DataFrame) -> dict:
    """Sonuç tablosuna bakarak kısa bir cevap + 3 takip sorusu önerisi üretir."""
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


def _infer_chart(df: pd.DataFrame, preferred_style: str = "bar"):
    """
    Grafik için hangi kolonun kategori (etiket), hangilerinin sayısal seri olacağını belirler.

    ÖNEMLİ: Kolon SIRASINA değil, kolon TİPİNE bakıyoruz (önceki sürüm "ilk kolon her zaman
    etiket" varsayıyordu — Claude'un ürettiği SQL kolonları farklı sırada döndürürse bu
    varsayım yanlış çıkıp hiç grafik üretilememesine yol açabiliyordu).
    """
    if df.empty or len(df.columns) < 2:
        return None

    numeric_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    non_numeric_cols = [c for c in df.columns if c not in numeric_cols]

    if not numeric_cols:
        return None

    # Etiket (x ekseni) için sayısal olmayan ilk kolonu kullan; hiç yoksa (tüm kolonlar
    # sayısalsa) ilk sayısal kolonu hem etiket hem seri gibi kullanmak yerine, ikinci sayısal
    # kolonu seri yapıp ilkini etiket olarak kullanırız.
    if non_numeric_cols:
        label_col = non_numeric_cols[0]
    else:
        label_col = numeric_cols[0]
        numeric_cols = numeric_cols[1:]
        if not numeric_cols:
            return None

    if preferred_style == "pie":
        col = numeric_cols[0]
        return {
            "style": "pie",
            "x_labels": df[label_col].astype(str).tolist()[:12],
            "series": [{"name": col, "values": df[col].fillna(0).tolist()[:12]}],
        }

    return {
        "style": preferred_style if preferred_style == "line" else "bar",
        "x_labels": df[label_col].astype(str).tolist()[:25],
        "series": [
            {"name": c, "values": df[c].fillna(0).tolist()[:25]} for c in numeric_cols[:4]
        ],
    }


def ask_data(question: str, user_id: int) -> dict:
    """Tüm Data akışını uçtan uca çalıştırır — SADECE bu kullanıcının kendi verisiyle."""
    print(f"[DEBUG] ask_data çağrıldı, kullanıcı: {user_id}, soru: {question!r}")

    schema_context = get_active_schema_context(user_id)
    if schema_context is None:
        return {
            "mode": "data",
            "answer": "Henüz analiz edebileceğim bir veri tablosu yok. Lütfen önce bir CSV/Excel dosyası yükleyin.",
            "sql": None,
            "table": None,
            "chart": None,
            "suggestions": [],
        }

    sql = generate_sql(question, schema_context)
    print(f"[DEBUG] Üretilen SQL: {sql!r}")

    try:
        _validate_sql(sql)
    except SQLEngineError as e:
        if str(e) == "NO_QUERY":
            return {
                "mode": "data",
                "answer": "Bu soruyu mevcut verilerle ilişkilendiremedim. Elimdeki tablolarla "
                          "ilgili bir soru sorabilir misiniz?",
                "sql": None,
                "table": None,
                "chart": None,
                "suggestions": [],
            }
        print(f"[DEBUG] Güvenlik doğrulaması reddetti: {e}")
        raise

    try:
        df = execute_sql(sql)
    except Exception as e:
        print(f"[DEBUG] SQL ÇALIŞTIRMA HATASI: {e}")
        print(f"[DEBUG] Hatalı SQL: {sql}")
        raise

    summary = summarize_and_suggest(question, sql, df)
    chart_style = detect_chart_style(question)
    chart = _infer_chart(df, chart_style)

    if chart is None:
        print(f"[DEBUG] Grafik üretilemedi. Kolonlar/tipler: {dict(df.dtypes.astype(str))}")

    return {
        "mode": "data",
        "answer": summary.get("answer", ""),
        "sql": sql,
        "table": {
            "columns": list(df.columns),
            "rows": df.astype(object).where(pd.notnull(df), None).values.tolist(),
            "row_count": len(df),
        },
        "chart": chart,
        "suggestions": summary.get("suggestions", []),
        "used_table": get_active_table_name(user_id),
    }