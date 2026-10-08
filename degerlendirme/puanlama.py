"""
puanlama.py
-----------
Bir cevabı puanlayan kontroller. Her biri ayrı bir soruya cevap verir:

1. Cevap doğru mu?        → hakem model (yerel) beklenen cevapla karşılaştırır: Doğru / Kısmen / Yanlış.
2. Sayılar tutuyor mu?    → modele güvenmeyen, kurallı kontrol: beklenen cevaptaki her sayı
                            sistemin cevabında (ya da SQL sonuç tablosunda) geçiyor mu?
3. Doğru yoldan mı gitti? → doküman sorusu dokümanlara, Excel sorusu SQL'e mi yönlendirildi?
4. Doğru kaynağı buldu mu?
     - Arama başarısı: doğru dosya/sayfa, aramada bulunan parçalar arasında var mı?
     - Atıf başarısı : cevapta [n] ile gösterilen kaynaklar arasında doğru dosya/sayfa var mı?
     İkisinin farkı hatanın yerini gösterir: arama bulmuş ama cevap göstermemişse sorun yazma
     aşamasında, arama hiç bulamamışsa sorun aramadadır.
"""

import json
import re
from pathlib import Path
from typing import Iterable, List, Optional, Set

from app import llm

from .sorular import TYPE_DATA, TYPE_DOC, Question

VERDICT_CORRECT = "Doğru"
VERDICT_PARTIAL = "Kısmen"
VERDICT_WRONG = "Yanlış"
VERDICT_UNCLEAR = "Belirsiz"
VERDICT_ERROR = "Hata"


# --- 2. Sayı kontrolü ---

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


def _number_readings(token: str) -> Set[float]:
    """'3.500' hem Türkçe (3500) hem İngilizce (3.5) yazım olabilir; ikisini de aday sayarız."""
    readings = set()
    for candidate in (token.replace(".", "").replace(",", "."), token.replace(",", "")):
        try:
            readings.add(float(candidate))
        except ValueError:
            pass
    return readings


def _numbers_in(texts: Iterable) -> Set[float]:
    found: Set[float] = set()
    for text in texts:
        if isinstance(text, (int, float)) and not isinstance(text, bool):
            found.add(float(text))
            continue
        for token in _NUMBER_RE.findall(str(text or "")):
            found |= _number_readings(token)
    return found


def check_numbers(expected: str, answer: str, table: Optional[dict] = None) -> str:
    """'evet', 'hayır', 'kısmen (1/2)' ya da beklenen cevapta sayı yoksa '—'."""
    tokens = _NUMBER_RE.findall(expected or "")
    if not tokens:
        return "—"
    haystack = [answer]
    if table:
        haystack += [v for row in table.get("rows", [])[:200] for v in row]
    available = _numbers_in(haystack)

    def matched(token):
        return any(abs(e - a) <= max(0.01, 0.005 * abs(e)) for e in _number_readings(token) for a in available)

    hits = sum(1 for t in tokens if matched(t))
    if hits == len(tokens):
        return "evet"
    return "hayır" if hits == 0 else f"kısmen ({hits}/{len(tokens)})"


# --- 3. Yol kontrolü ---

EXPECTED_MODE = {TYPE_DOC: "docs", TYPE_DATA: "data"}
MODE_LABEL = {"docs": "Doküman", "data": "SQL", None: "—"}


def check_route(question: Question, actual_mode: Optional[str]) -> str:
    expected = EXPECTED_MODE.get(question.kind)
    if expected is None:
        return "—"  # "dokümanda olmayan" sorularda hangi yoldan gittiği önemli değil
    return "evet" if actual_mode == expected else "hayır"


# --- 4. Kaynak kontrolü ---

def _same_file(expected: str, actual: str) -> bool:
    expected, actual = expected.strip().lower(), actual.strip().lower()
    if not expected:
        return False
    return actual == expected or Path(actual).stem == Path(expected).stem


def _source_matches(question: Question, source: dict) -> bool:
    if not _same_file(question.source_file, source.get("filename") or ""):
        return False
    return question.source_pages is None or source.get("page") in question.source_pages


def check_document_sources(question: Question, sources: List[dict]) -> tuple:
    """(arama başarısı, atıf başarısı) — her biri 'evet' / 'hayır' / '—'."""
    if question.kind != TYPE_DOC or not question.source_file:
        return "—", "—"
    retrieved = any(_source_matches(question, s) for s in sources)
    cited = any(_source_matches(question, s) for s in sources if s.get("cited"))
    return ("evet" if retrieved else "hayır"), ("evet" if cited else "hayır")


def check_table_source(question: Question, sql: Optional[str], tables_by_file: dict) -> str:
    """Excel sorusunda SQL, kaynak dosyadan oluşturulan tabloyu kullanmış mı?"""
    if question.kind != TYPE_DATA or not question.source_file:
        return "—"
    tables = next((t for name, t in tables_by_file.items() if _same_file(question.source_file, name)), [])
    if not sql or not tables:
        return "hayır"
    used = any(re.search(rf'(?<![\w]){re.escape(t)}(?![\w])', sql, flags=re.IGNORECASE) for t in tables)
    return "evet" if used else "hayır"


# --- 1. Hakem model ---

JUDGE_SYSTEM = """Sen bir soru-cevap sisteminin cevaplarını puanlayan tarafsız bir hakemsin. Sana bir soru,
beklenen (doğru) cevap ve sistemin verdiği cevap verilecek.

Karar kuralları:
- "dogru": Beklenen cevabın özündeki bilgi (sayılar, adlar, tarihler, süreler, koşullar) sistem cevabında
  doğru olarak var ve onunla çelişen bir bilgi yok. Farklı ifade etmek ya da ek doğru ayrıntı vermek sorun değil.
- "kismen": Beklenen bilginin bir kısmı doğru ama önemli bir kısmı eksik ya da küçük bir hata var.
- "yanlis": Bilgi yanlış veya çelişkili, ya da sistem cevap vermemiş / bilgiyi bulamadığını söylemiş.

Soru türü "dokümanda olmayan" ise beklenen cevap yoktur; o zaman:
- Sistem bilginin kaynaklarda olmadığını açıkça söylüyor ve tahmin yürütmüyorsa "dogru",
- Bir cevap veriyor, uyduruyor ya da genel bilgisinden tahmin yürütüyorsa "yanlis".

Köşeli parantezli kaynak numaralarını ([1] gibi) ve yazım biçimini dikkate alma.
SADECE şu JSON ile cevap ver: {"karar": "dogru" veya "kismen" veya "yanlis", "gerekce": "en fazla bir cümle"}"""


def _table_preview(table: Optional[dict], limit: int = 10) -> str:
    if not table or not table.get("columns"):
        return ""
    lines = [" | ".join(map(str, table["columns"]))]
    lines += [" | ".join("" if v is None else str(v) for v in row) for row in table.get("rows", [])[:limit]]
    return "\n".join(lines)


def judge(question: Question, answer: str, table: Optional[dict], model: Optional[str]) -> tuple:
    """(karar, gerekçe). Hakem modeli yerelde çalışır; veri dışarı çıkmaz."""
    parts = [
        f"Soru türü: {question.kind}",
        f"Soru: {question.text}",
        f"Beklenen cevap: {question.expected or '(yok — bilgi kaynaklarda bulunmuyor)'}",
        f"Sistemin cevabı: {answer or '(boş)'}",
    ]
    preview = _table_preview(table)
    if preview:
        parts.append(f"Sistemin cevabıyla birlikte gösterdiği sonuç tablosu (ilk satırlar):\n{preview}")
    messages = [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": "\n\n".join(parts)}]
    # Bazı model/Ollama sürümü birleşimleri katı JSON modunda BOŞ cevap döndürebiliyor. O zaman aynı soruyu
    # JSON modu olmadan tekrar sorar ve kararı düz metinden okuruz.
    attempts = []
    for json_mode in (True, False):
        try:
            result = llm.chat_full(messages, json_mode=json_mode, temperature=0.0, model=model, think=False)
        except llm.LLMError as e:
            return VERDICT_UNCLEAR, f"Hakem çalışamadı: {e}"
        attempts.append(result)
        parsed = _parse_verdict(result["content"])
        if parsed:
            return parsed
    # İki deneme de okunamadı: kararı modelin düşünme metninde arar, yoksa nedenini rapora yazar.
    parsed = _parse_verdict(attempts[-1]["thinking"])
    if parsed:
        return parsed
    last = attempts[-1]
    if not last["content"]:
        return VERDICT_UNCLEAR, (f"Hakem boş cevap verdi (bitiş nedeni: {last['done_reason'] or 'bilinmiyor'}, "
                                 f"düşünme metni: {len(last['thinking'])} karakter)")
    return VERDICT_UNCLEAR, f"Hakemin cevabı anlaşılamadı: {last['content'][:200]}"


_VERDICT_RE = re.compile(r"\b(dogru|kismen|yanlis)\b")
_VERDICTS = {"dogru": VERDICT_CORRECT, "kismen": VERDICT_PARTIAL, "yanlis": VERDICT_WRONG}


def _parse_verdict(text: str) -> Optional[tuple]:
    """Hakem cevabından (karar, gerekçe) çıkarır: önce JSON, olmazsa metindeki karar kelimesi."""
    text = (text or "").strip()
    if not text:
        return None
    reason = ""
    try:
        data = json.loads(text[text.find("{"):text.rfind("}") + 1]) if "{" in text else None
    except ValueError:
        data = None
    if isinstance(data, dict):
        verdict = str(data.get("karar", "")).strip().lower()
        reason = str(data.get("gerekce", "")).strip()
    else:
        verdict = text.lower()
    verdict = verdict.translate(str.maketrans("ğıüşöçİ", "giusoci"))
    # JSON'daki "karar" alanı ya da düz metinde "karar: ..." kısmı önceliklidir.
    match = re.search(r"karar\W*(dogru|kismen|yanlis)", verdict) or _VERDICT_RE.search(verdict)
    if not match:
        return None
    return _VERDICTS[match.group(1)], reason
