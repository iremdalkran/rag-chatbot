"""
tabular.py
----------
Excel/CSV verisi üzerinde soru-cevap ("bu ay en çok satan ürün hangisi?").

Akış:
  1. Yüklenen tablo, KULLANICIYA ÖZEL ayrı bir SQLite dosyasına yazılır (data/tables/u<id>.db).
     Her kullanıcının verisi fiziksel olarak ayrı dosyada durduğu için bir kullanıcının sorgusu
     başka birinin verisine teknik olarak ulaşamaz.
  2. Yerel model, tabloların yapısına bakarak bir SQL (SELECT) sorgusu yazar.
  3. Sorgu, SALT-OKUNUR açılmış bağlantıda ve sadece okuma işlemlerine izin veren bir
     "bekçi" (authorizer) altında çalıştırılır. Silme/değiştirme/başka dosya açma imkânsızdır.
  4. Sonuç tablosuna bakılarak kısa bir özet, grafik ve takip sorusu önerileri üretilir.
"""

import io
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import List, Optional

import pandas as pd

from app import config, db, llm

MAX_RESULT_ROWS = 1000
QUERY_TIMEOUT_SECONDS = 10
SUPPORTED_EXTENSIONS = (".csv", ".xlsx", ".xls")

_TR_MAP = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")

PIE_KEYWORDS = ("pasta", "dağılım", "dagilim", "oran", "yüzde", "yuzde", "pie")
LINE_KEYWORDS = ("çizgi", "cizgi", "trend", "line", "zaman içinde", "zaman icinde", "aylık", "yıllık")


class DataError(Exception):
    """Kullanıcıya gösterilecek, anlaşılır mesajlı veri hatası."""


def _user_db_path(user_id: int) -> Path:
    return config.TABLES_DIR / f"u{int(user_id)}.db"


def _identifier(name: str, fallback: str) -> str:
    """Herhangi bir başlığı güvenli bir SQL adına çevirir: 'Satış Tutarı (TL)' -> 'satis_tutari_tl'."""
    text = str(name).translate(_TR_MAP).lower()
    text = re.sub(r"[^a-z0-9_]+", "_", text).strip("_")
    if not text:
        text = fallback
    if text[0].isdigit():
        text = f"{fallback}_{text}"
    return text[:60]


def _unique_columns(columns) -> List[str]:
    result, seen = [], {}
    for i, col in enumerate(columns, start=1):
        base = _identifier(col, f"kolon{i}")
        name = base
        while name in seen:
            seen[base] += 1
            name = f"{base}_{seen[base]}"
        seen.setdefault(name, 1)
        result.append(name)
    return result


def _read_frames(filename: str, data: bytes) -> dict:
    name = filename.lower()
    try:
        if name.endswith(".csv"):
            for encoding in ("utf-8-sig", "cp1254", "latin-1"):
                try:
                    # sep=None: ayırıcı (virgül, noktalı virgül, sekme) otomatik bulunur.
                    return {"": pd.read_csv(io.BytesIO(data), sep=None, engine="python", encoding=encoding)}
                except UnicodeDecodeError:
                    continue
            raise DataError("CSV dosyasının karakter kodlaması anlaşılamadı.")
        if name.endswith((".xlsx", ".xls")):
            return pd.read_excel(io.BytesIO(data), sheet_name=None)
    except DataError:
        raise
    except Exception as e:
        raise DataError("Dosya okunamadı. Bozuk olabilir veya desteklenmeyen bir biçimde.") from e
    raise DataError("Sadece Excel (.xlsx, .xls) veya CSV dosyaları kabul edilir.")


def load_file(user_id: int, filename: str, data: bytes) -> List[dict]:
    """Dosyadaki her dolu sayfayı ayrı bir tablo olarak kaydeder. Aynı adlı tablo varsa yenilenir."""
    frames = _read_frames(filename, data)
    base = _identifier(Path(filename).stem, "tablo")
    created = []
    conn = sqlite3.connect(_user_db_path(user_id))
    try:
        for sheet, df in frames.items():
            df = df.dropna(how="all").dropna(axis=1, how="all")
            if df.empty:
                continue
            df.columns = _unique_columns(df.columns)
            table = base if (len(frames) == 1 or not sheet) else f"{base}_{_identifier(sheet, 'sayfa')}"
            df.to_sql(table, conn, if_exists="replace", index=False)
            created.append({"table_name": table, "row_count": len(df), "columns": list(df.columns)})
        conn.commit()
    finally:
        conn.close()
    if not created:
        raise DataError("Dosya boş görünüyor.")

    with db.get_conn() as c:
        for item in created:
            c.execute("DELETE FROM datasets WHERE user_id = ? AND table_name = ?", (user_id, item["table_name"]))
            c.execute(
                """INSERT INTO datasets (user_id, table_name, filename, row_count, columns_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, item["table_name"], filename[:200], item["row_count"], json.dumps(item["columns"]), db.now()),
            )
    return created


def list_datasets(user_id: int) -> List[dict]:
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM datasets WHERE user_id = ? ORDER BY id DESC", (user_id,)).fetchall()
    return [
        {"id": r["id"], "table_name": r["table_name"], "filename": r["filename"], "row_count": r["row_count"],
         "columns": json.loads(r["columns_json"]), "created_at": r["created_at"]}
        for r in rows
    ]


def has_datasets(user_id: int) -> bool:
    with db.get_conn() as conn:
        return conn.execute("SELECT 1 FROM datasets WHERE user_id = ? LIMIT 1", (user_id,)).fetchone() is not None


def delete_dataset(user_id: int, dataset_id: int) -> bool:
    with db.get_conn() as conn:
        row = conn.execute("SELECT table_name FROM datasets WHERE id = ? AND user_id = ?", (dataset_id, user_id)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))
    table = row["table_name"]
    tconn = sqlite3.connect(_user_db_path(user_id))
    try:
        tconn.execute(f'DROP TABLE IF EXISTS "{table}"')
        tconn.commit()
    finally:
        tconn.close()
    return True


def delete_user_files(user_id: int) -> None:
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(_user_db_path(user_id)) + suffix)
        if path.exists():
            path.unlink()


MAX_SCHEMA_COLUMNS = 40
MAX_LISTED_VALUES = 15
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _describe_column(conn, table: str, col: str, sample: pd.Series) -> str:
    """
    Modelin doğru SQL yazabilmesi için bir kolonu tarif eder. Az sayıda farklı değeri olan metin
    kolonlarında değerlerin TAMAMINI listeler (ör. şehir: İstanbul, Ankara…) — böylece model
    WHERE koşulunda değeri tahmin etmek yerine birebir doğru yazar. Sayılarda en küçük/en büyük
    değeri, tarihlerde kullanılacak biçimi belirtir.
    """
    examples = [str(v) for v in sample.dropna().unique()[:3]]
    line = f"  - {col}"
    try:
        if pd.api.types.is_numeric_dtype(sample):
            low, high = conn.execute(f'SELECT MIN("{col}"), MAX("{col}") FROM "{table}"').fetchone()
            return f"{line} (sayı, en küçük {low}, en büyük {high})"
        if examples and all(_DATE_RE.match(e) for e in examples):
            low, high = conn.execute(f'SELECT MIN("{col}"), MAX("{col}") FROM "{table}"').fetchone()
            return (f"{line} (tarih, 'YYYY-AA-GG' ile başlayan metin, {str(low)[:10]} ile {str(high)[:10]} arası; "
                    f"ay/yıl için strftime('%Y-%m', {col}) kullan)")
        distinct = conn.execute(f'SELECT COUNT(DISTINCT "{col}") FROM "{table}"').fetchone()[0]
        if distinct <= MAX_LISTED_VALUES:
            values = [r[0] for r in conn.execute(
                f'SELECT DISTINCT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL ORDER BY 1 LIMIT ?',
                (MAX_LISTED_VALUES,))]
            return f"{line} (metin, olası değerlerin tamamı: {', '.join(repr(str(v)) for v in values)})"
        return f"{line} (metin, {distinct} farklı değer, örnek: {', '.join(examples)})"
    except sqlite3.Error:
        return f"{line} (örnek: {', '.join(examples)})"


def schema_context(user_id: int) -> Optional[str]:
    datasets = list_datasets(user_id)[:8]
    if not datasets:
        return None
    conn = sqlite3.connect(_user_db_path(user_id))
    parts = []
    try:
        for ds in datasets:
            table = ds["table_name"]
            try:
                sample = pd.read_sql_query(f'SELECT * FROM "{table}" LIMIT 3', conn)
            except Exception:
                continue
            cols = [_describe_column(conn, table, col, sample[col]) for col in sample.columns[:MAX_SCHEMA_COLUMNS]]
            parts.append(
                f"Tablo: {table}  (dosya: {ds['filename']}, {ds['row_count']} satır)\nKolonlar:\n"
                + "\n".join(cols)
            )
    finally:
        conn.close()
    return "\n\n".join(parts) if parts else None


# --- Güvenli sorgu çalıştırma ---

_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                    getattr(sqlite3, "SQLITE_RECURSIVE", 33)}


def _authorizer(action, *_args):
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def clean_sql(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    match = re.search(r"```(?:sql)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if match:
        text = match.group(1)
    return text.strip().rstrip(";").strip()


def validate_sql(sql: str) -> None:
    if not sql or sql.upper().startswith("NO_QUERY"):
        raise DataError("NO_QUERY")
    if not re.match(r"^(select|with)\b", sql, flags=re.IGNORECASE):
        raise DataError("Sadece veri okuyan (SELECT) sorgulara izin veriliyor.")
    if ";" in sql:
        raise DataError("Tek seferde yalnızca bir sorgu çalıştırılabilir.")


def run_query(user_id: int, sql: str) -> pd.DataFrame:
    validate_sql(sql)
    path = _user_db_path(user_id)
    if not path.exists():
        raise DataError("Henüz yüklenmiş bir veri tablonuz yok.")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
    try:
        conn.set_authorizer(_authorizer)
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
        cur = conn.execute(sql)
        columns = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(MAX_RESULT_ROWS)
    except sqlite3.Error as e:
        raise DataError(f"Sorgu çalıştırılamadı: {e}") from e
    finally:
        conn.close()
    return pd.DataFrame(rows, columns=columns)


# --- Yapay zekâ adımları ---

def _generate_sql(question: str, schema: str, history: list, previous_error: str = None) -> str:
    system = f"""Sen bir SQLite uzmanısın. Kullanıcının sorusunu cevaplayan TEK bir SQLite SELECT sorgusu yaz.

Kurallar:
1. Sadece aşağıdaki tabloları ve kolonları kullan; olmayan tablo veya kolon uydurma.
2. Sadece SELECT (gerekirse WITH) kullan. Veri değiştiren hiçbir komut yazma.
3. Cevap olarak SADECE SQL yaz; açıklama ya da yorum ekleme.
4. Sonuç çok satırlıysa anlamlı bir sıralama (ORDER BY) yap ve LIMIT 100 ekle.
5. Toplam, ortalama gibi hesaplamalarda kolonlara anlaşılır takma adlar (AS) ver.
6. Genel bir "grafik çiz / görselleştir" isteğinde en anlamlı kategori kolonu ile bir sayısal
   kolonu özetleyen bir sorgu yaz.
7. Metin karşılaştırmalarında, kolon tarifinde "olası değerlerin tamamı" listelenmişse değeri o listedeki
   yazımla BİREBİR kullan (büyük/küçük harf ve Türkçe karakterler dahil).
8. Soru bu verilerle hiçbir şekilde cevaplanamıyorsa (ör. tablolarda olmayan bir konu, bir doküman
   içeriği ya da sohbet) sadece NO_QUERY yaz.

Tablolar:
{schema}"""
    messages = [{"role": "system", "content": system}]
    messages += history[-4:]
    content = question
    if previous_error:
        content += f"\n\n(Önceki denemen şu hatayı verdi, düzelt: {previous_error})"
    messages.append({"role": "user", "content": content})
    return clean_sql(llm.chat(messages, temperature=0.0))


def _summarize(question: str, sql: str, df: pd.DataFrame) -> dict:
    preview = df.head(15).to_csv(index=False)
    system = """Sen bir veri analistisin. Sana bir soru, çalıştırılan SQL ve sonucun bir önizlemesi verilecek.
1. Sonuca dayanarak 1-3 cümlelik sade bir Türkçe cevap yaz; rakamları ve isimleri kullan. Önizlemede
   olmayan bir bilgiyi uydurma.
2. Bu veriyle cevaplanabilecek 3 kısa takip sorusu öner.
SADECE şu JSON biçiminde cevap ver: {"answer": "...", "suggestions": ["...", "...", "..."]}"""
    user = f"Soru: {question}\n\nSQL:\n{sql}\n\nSonuç ({len(df)} satır, ilk 15 satır):\n{preview}"
    raw = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}], json_mode=True)
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError
    except ValueError:
        parsed = {"answer": raw, "suggestions": []}
    suggestions = [str(s) for s in parsed.get("suggestions") or [] if str(s).strip()][:3]
    return {"answer": str(parsed.get("answer") or "").strip(), "suggestions": suggestions}


def _chart_style(question: str) -> str:
    q = question.lower()
    if any(k in q for k in PIE_KEYWORDS):
        return "pie"
    if any(k in q for k in LINE_KEYWORDS):
        return "line"
    return "bar"


def infer_chart(df: pd.DataFrame, style: str = "bar", sql: str = "") -> Optional[dict]:
    """
    Kolon TİPLERİNE bakarak hangi kolonun etiket, hangilerinin sayısal seri olacağını seçer.
    Sayısal kolonların ölçekleri çok farklıysa (ör. adet 2-13, tutar 160-400) hepsini aynı eksene
    çizmek küçük olanı görünmez yapar; o zaman sadece sorgunun sıraladığı kolon çizilir (tabloda
    diğerleri yine görünür).
    """
    if df.empty or len(df.columns) < 2 or len(df) < 2:
        return None
    numeric = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    others = [c for c in df.columns if c not in numeric]
    if not numeric:
        return None
    if others:
        label = others[0]
    else:
        label, numeric = numeric[0], numeric[1:]
        if not numeric:
            return None
    if len(numeric) > 1:
        peaks = [float(df[c].abs().max() or 0) for c in numeric]
        if max(peaks) > 10 * max(min(peaks), 1e-9):
            ordered = re.search(r"order\s+by\s+\"?(\w+)", sql or "", flags=re.IGNORECASE)
            chosen = ordered.group(1) if ordered and ordered.group(1) in numeric else numeric[peaks.index(max(peaks))]
            numeric = [chosen]
    limit = 12 if style == "pie" else 25
    series_cols = numeric[:1] if style == "pie" else numeric[:4]
    return {
        "style": style,
        "x_labels": df[label].astype(str).tolist()[:limit],
        "series": [{"name": c, "values": [float(v) for v in df[c].fillna(0).tolist()[:limit]]} for c in series_cols],
    }


def _table_payload(df: pd.DataFrame) -> dict:
    data = json.loads(df.to_json(orient="split", date_format="iso", default_handler=str))
    return {"columns": data["columns"], "rows": data["data"], "row_count": len(df)}


def ask(user_id: int, question: str, history: list) -> dict:
    schema = schema_context(user_id)
    if schema is None:
        return {"answer": "Henüz yüklenmiş bir veri tablonuz yok. Önce bir Excel veya CSV dosyası yükleyin."}

    error = None
    sql = ""
    for _attempt in range(2):  # model hatalı SQL yazarsa hatayı gösterip bir kez daha deniyoruz
        sql = _generate_sql(question, schema, history, error)
        try:
            df = run_query(user_id, sql)
            break
        except DataError as e:
            if str(e) == "NO_QUERY":
                return {"answer": "Bu soruyu yüklediğiniz tablolarla ilişkilendiremedim. "
                                  "Tablolardaki verilerle ilgili bir soru sorabilir misiniz?",
                        "no_query": True}
            error = str(e)
    else:
        raise DataError("Bu soru için çalışan bir sorgu oluşturamadım. Soruyu biraz farklı sormayı deneyin.")

    summary = _summarize(question, sql, df)
    return {
        "answer": summary["answer"] or f"Sorgu {len(df)} satır döndürdü.",
        "sql": sql,
        "table": _table_payload(df),
        "chart": infer_chart(df, _chart_style(question), sql),
        "suggestions": summary["suggestions"],
    }
