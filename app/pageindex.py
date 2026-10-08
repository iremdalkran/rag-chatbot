"""
pageindex.py
------------
PageIndex yöntemi (VectifyAI/PageIndex'ten uyarlanmıştır): vektör kullanmadan, "akıl yürüterek" arama.

Fikir: İnsan kalın bir kitapta bilgi ararken önce İÇİNDEKİLER sayfasına bakar, ilgili bölümü seçer,
sonra o sayfaları okur. Burada da öyle:

  1. Ağaç kurma (doküman yüklenince, bir kez):
     - Dokümanın sayfa metinleri saklanır.
     - Kanun/yönetmelik gibi "MADDE 12 –" yapısındaki metinlerde kısım, bölüm ve maddeler doğrudan
       metinden okunur (model gerekmez). Değilse PDF'in kendi içindekiler listesi kullanılır; o da yoksa
       yerel model sayfaları okuyup bölüm başlıklarını ve hangi sayfada başladıklarını çıkarır.
     - Başlıkların gerçekten o sayfada geçtiği kontrol edilir (uydurma başlıklar atılır).
     - Başlıklar seviyelerine göre ağaca dizilir; her bölümün bitiş sayfası hesaplanır; çok uzun
       bölümler sayfa aralıklarına bölünür; her bölüm için model kısa bir özet yazar.
  2. Arama (her soruda):
     - Model; dokümanların ağacını (başlık + sayfa + özet) ve soruyu görür, cevabı içerebilecek
       bölümleri seçer.
     - Ağaç modele bir seferde gösterilemeyecek kadar büyükse arama iki adımda yapılır: önce ana
       bölümler, sonra seçilen bölümlerin alt bölümleri (maddeleri) özetleriyle gösterilir.
     - Seçilen bölümlerin metni OLDUĞU GİBİ okunur (madde gibi yeri tam bilinen bölümlerde sayfanın
       tamamı değil, sadece o bölüm) ve cevap yazan modele verilir.

Özgün PageIndex bulut modelleri (OpenAI vb.) ve ek kütüphanelerle çalışır; buradaki sürüm aynı
yöntemi yalnızca Ollama'daki yerel modelle, ek bağımlılık olmadan uygular.
"""

import json
import logging
import re
import time
import unicodedata
from typing import Dict, List, Optional, Tuple

from app import config, db, ingest, llm

log = logging.getLogger(__name__)

PSEUDO_PAGE_CHARS = 3000  # sayfası olmayan dokümanlarda (Word, TXT) "sanal sayfa" boyutu
_SHORT_TEXT = 500          # bundan kısa bölümlerde özet yerine metnin kendisi kullanılır
_OUTLINE_LIMIT = 14000     # arama sırasında modele gösterilen ağacın en fazla uzunluğu (karakter)


# --- Sayfaları saklama ---

def split_pseudo_pages(text: str, size: int = PSEUDO_PAGE_CHARS) -> List[str]:
    """Sayfası olmayan metni satır/cümle sınırlarından ~size karakterlik sayfalara böler.
    Satır sonları korunur: başlıklar ayrı satırda kalsın ki model onları tanıyabilsin."""
    pages, current = [], ""
    for line in text.split("\n"):
        for unit in ([line] if len(line) <= size else ingest._split_units(line, size)):
            if current and len(current) + len(unit) + 1 > size:
                pages.append(current.strip())
                current = ""
            current = f"{current}\n{unit}" if current else unit
    if current:
        pages.append(current)
    return pages or [text]


def store_pages(conn, doc_id: int, pages: List["ingest.Page"], outline: Optional[List[dict]] = None) -> None:
    """Dokümanın sayfa metinlerini kaydeder (documents._process içinden, aynı işlemde çağrılır)."""
    paged = bool(pages) and pages[0].number is not None
    texts = [p.text for p in pages] if paged else split_pseudo_pages("\n\n".join(p.text for p in pages))
    conn.executemany("INSERT OR REPLACE INTO doc_pages (document_id, page_no, text) VALUES (?, ?, ?)",
                     [(doc_id, n, t) for n, t in enumerate(texts, start=1)])
    seed = json.dumps({"outline": outline}, ensure_ascii=False) if outline else None
    conn.execute("UPDATE documents SET paged = ?, tree_json = ?, tree_status = ? WHERE id = ?",
                 (int(paged), seed, "pending" if build_enabled() else "none", doc_id))


def build_enabled() -> bool:
    return config.RAG_METHOD == "pageindex" or config.PAGEINDEX_BUILD == "always"


def _load_pages(doc_id: int) -> List[str]:
    with db.get_conn() as conn:
        rows = conn.execute("SELECT text FROM doc_pages WHERE document_id = ? ORDER BY page_no", (doc_id,)).fetchall()
        if rows:
            return [r["text"] for r in rows]
        # Bu özellikten önce yüklenmiş dokümanlar: sayfaları parçalardan yeniden oluştur.
        chunks = conn.execute("SELECT page, text FROM chunks WHERE document_id = ? ORDER BY chunk_index",
                              (doc_id,)).fetchall()
        doc = conn.execute("SELECT page_count FROM documents WHERE id = ?", (doc_id,)).fetchone()
        paged = bool(doc and doc["page_count"])
        by_page: Dict[int, str] = {}
        for chunk in chunks:
            key = chunk["page"] if paged and chunk["page"] else 1
            by_page[key] = _join_overlapping(by_page.get(key, ""), chunk["text"])
        if paged:
            texts = [by_page.get(n, "") for n in range(1, max(by_page, default=0) + 1)]
        else:
            texts = split_pseudo_pages(by_page.get(1, ""))
        conn.execute("UPDATE documents SET paged = ? WHERE id = ?", (int(paged), doc_id))
        conn.executemany("INSERT OR REPLACE INTO doc_pages (document_id, page_no, text) VALUES (?, ?, ?)",
                         [(doc_id, n, t) for n, t in enumerate(texts, start=1)])
        return texts


def _join_overlapping(previous: str, text: str) -> str:
    """Komşu parçaların üst üste binen kısmını bir kez yazarak birleştirir."""
    if not previous:
        return text
    for size in range(min(len(previous), len(text)), 0, -1):
        if previous.endswith(text[:size]):
            return previous + text[size:]
    return previous + " " + text


# --- Başlık doğrulama yardımcıları ---

def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower().replace("ı", "i").replace("İ", "i"))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^\w]+", " ", text).strip()


def _position(title: str, page_text: str) -> Optional[float]:
    """Başlık sayfada geçiyorsa sayfadaki göreli konumu (0 = en üst, 1 = en alt), geçmiyorsa None.
    Model başlığı bazen biraz farklı yazar; bu yüzden kelimelerin çoğunun art arda geçmesi yeterli sayılır."""
    page = _norm(page_text)
    wanted = _norm(title)
    if not wanted or not page:
        return None
    index = page.find(wanted)
    if index == -1:
        words = wanted.split()
        # Numarasını atıp tekrar dene ("3.2 Yıllık İzin" → "Yıllık İzin").
        while words and re.fullmatch(r"[\divxlc]+", words[0]):
            words = words[1:]
        if not words:
            return None
        index = page.find(" ".join(words))
        if index == -1 and len(words) >= 3:
            index = page.find(" ".join(words[: max(2, int(len(words) * 0.7))]))
        if index == -1:
            return None
    return index / max(len(page), 1)


def _find_offset(title: str, page_text: str) -> Optional[int]:
    """Başlığın sayfa metnindeki tam yeri (karakter sırası); bulunamazsa None."""
    words = re.findall(r"\w+", title)
    if not words:
        return None
    pattern = r"\W+".join(re.escape(w) for w in words)
    match = re.search(pattern, page_text) or re.search(pattern, page_text, re.IGNORECASE)
    return match.start() if match else None


# --- Kanun / yönetmelik yapısı (model gerektirmez) ---

_ORDINAL = (r"(?:(?:ON|YİRMİ|OTUZ|On|Yirmi|Otuz)\s?)?[A-ZÇĞİÖŞÜ][A-Za-zÇĞİÖŞÜçğıöşü]*?"
            r"(?:NCI|NCİ|NCU|NCÜ|ncı|nci|ncu|ncü)")
_SECTION_RE = re.compile(rf"^[ \t]*({_ORDINAL})\s+(KISIM|BÖLÜM|Kısım|Bölüm)\b[ \t:–—-]*(.*)$", re.MULTILINE)
_ARTICLE_RE = re.compile(r"^[ \t]*((?:Ek|EK|Geçici|GEÇİCİ)\s+)?(?:MADDE|Madde)\s+(\d+)\s*[-–—]", re.MULTILINE)
_MIN_ARTICLES = 5


def _short_line(line: str) -> bool:
    """Başlık olabilecek kısa bir satır mı? (cümle sonu noktalaması yok, büyük harfle başlıyor)"""
    line = line.strip()
    return (0 < len(line) <= 80 and line[0].isupper() and not line.endswith((".", ",", ";", ":"))
            and not _ARTICLE_RE.match(line) and not _SECTION_RE.match(line))


def _legal_headings(pages: List[str]) -> List[dict]:
    """Kanun/yönetmelik metinlerinde kısım, bölüm ve maddeleri doğrudan metinden çıkarır.
    "MADDE 12 –" kalıbı en az birkaç kez geçmiyorsa boş liste döner (bu tür bir metin değildir)."""
    sections, articles, seen = [], [], set()
    for number, text in enumerate(pages, start=1):
        consumed = set()  # bölüm adı olarak kullanılan satırlar madde başlığı sayılmasın
        for m in _SECTION_RE.finditer(text):
            title = f"{m.group(1)} {m.group(2)}"
            rest = m.group(3).strip()
            if rest:
                title += f" – {rest}"
            else:
                # Bölüm adı çoğu zaman bir alt satırdadır ("BİRİNCİ BÖLÜM" / "Genel Hükümler"). O satırın
                # hemen altında madde başlıyorsa, o satır bölümün değil maddenin başlığıdır.
                after = text[m.end():].lstrip("\n").split("\n")
                if after and _short_line(after[0]) and not (len(after) > 1 and _ARTICLE_RE.match(after[1])):
                    title += f" – {after[0].strip()}"
                    consumed.add(after[0].strip())
            sections.append({"kind": m.group(2).upper(), "title": title, "page": number, "offset": m.start()})
        for m in _ARTICLE_RE.finditer(text):
            raw = (m.group(1) or "").strip()
            prefix = "Geçici " if raw.upper().startswith("GE") else ("Ek " if raw else "")
            key = (prefix, m.group(2))
            if key in seen:
                continue
            seen.add(key)
            title, page, offset = f"{prefix}Madde {m.group(2)}", number, m.start()
            # Maddenin konu başlığı genelde bir üst satırdadır ("Çalışma süresi" / "MADDE 63 – ...").
            # Madde sayfanın en başındaysa başlık önceki sayfanın son satırında kalmış olabilir.
            # Değişiklik notları ("(Değişik: ...)") atlanır.
            before_text, before_page = text[:m.start()], number
            if not before_text.strip() and number > 1:
                before_text, before_page = pages[number - 2], number - 1
            before = before_text.rstrip().split("\n")
            for back in range(1, min(3, len(before)) + 1):
                line = before[-back].strip()
                if line.startswith("("):
                    continue
                if _short_line(line) and line not in consumed:
                    title += f" – {line}"
                    page = before_page
                    offset = before_text.rfind(before[-back])
                break
            articles.append({"title": title, "page": page, "offset": offset})
    if len(articles) < _MIN_ARTICLES:
        return []
    has_part = any(s["kind"] == "KISIM" for s in sections)
    headings = []
    for s in sections:
        level = 1 if s["kind"] == "KISIM" or not has_part else 2
        headings.append({"level": level, "title": s["title"], "page": s["page"], "offset": s["offset"]})
    article_level = max((h["level"] for h in headings), default=0) + 1
    headings += [{"level": article_level, **a} for a in articles]
    for h in headings:
        h["position"] = h["offset"] / max(len(pages[h["page"] - 1]), 1)
    return headings


# --- 1. Ağaç kurma ---

TOC_PROMPT = """Aşağıda bir dokümanın bazı sayfaları var. Her sayfa <sayfa_N> ... </sayfa_N> etiketleri arasında.
Görevin: bu sayfalarda BAŞLAYAN bölüm başlıklarını (ana bölüm, alt bölüm) sırasıyla çıkarmak.

Kurallar:
- Sadece metinde gerçekten başlık olarak geçen ifadeleri, metindeki yazımıyla aynen yaz. Başlık uydurma.
- "seviye": 1 = ana bölüm, 2 = alt bölüm, 3 = daha alt bölüm. Numaralı başlıklarda numaraya bak
  (ör. "3" → 1, "3.2" → 2, "3.2.1" → 3).
- "sayfa": başlığın geçtiği sayfanın numarası (etiketteki N).
- Kanun, yönetmelik ve sözleşmelerdeki numaralı maddeler ("Madde 12 – ...", "MADDE 12 -") birer alt
  bölümdür: her birini ayrı başlık olarak yaz (varsa maddenin üstündeki konu başlığıyla birlikte).
- Sayfa üst/alt bilgileri, tablo satırları ve "•", "-", "a)" gibi liste işaretiyle başlayan satırlar başlık değildir.
- Hiç başlık yoksa boş liste ver.
{previous}
Sadece şu biçimde JSON ver: {{"bolumler": [{{"seviye": 1, "baslik": "...", "sayfa": 3}}]}}

{pages}"""

SUMMARY_PROMPT = """Aşağıdaki doküman bölümünü 1-2 cümleyle özetle. Bölümde hangi konuların, kuralların,
sayıların, adların, tarihlerin geçtiğini belirt ki biri bu özete bakarak bir sorunun cevabının bu bölümde
olup olmadığına karar verebilsin. Metnin başında ya da sonunda komşu bölümlerden parçalar olabilir: sadece
başlığı verilen bölümü özetle. Sadece özeti yaz.

Bölüm başlığı: {title}

{text}"""


def _page_groups(pages: List[str]) -> List[List[int]]:
    """Sayfaları, bir seferde modele verilebilecek gruplara ayırır (sayfa numaraları 1'den başlar)."""
    groups, current, size = [], [], 0
    for number, text in enumerate(pages, start=1):
        length = min(len(text), config.PAGEINDEX_GROUP_CHARS)
        if current and size + length > config.PAGEINDEX_GROUP_CHARS:
            groups.append(current)
            current, size = [], 0
        current.append(number)
        size += length
    if current:
        groups.append(current)
    return groups


def _parse_json(text: str) -> dict:
    try:
        data = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text or "", re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return {}
    return data if isinstance(data, dict) else {}


def _extract_headings(pages: List[str]) -> List[dict]:
    """Model sayfaları gruplar halinde okuyup başlıkları çıkarır; doğrulanan başlıklar döner."""
    found: List[dict] = []
    for group in _page_groups(pages):
        tagged = "\n\n".join(f"<sayfa_{n}>\n{pages[n - 1][:config.PAGEINDEX_GROUP_CHARS]}\n</sayfa_{n}>" for n in group)
        previous = ""
        if found:
            last = "; ".join(f"{h['title']} (seviye {h['level']})" for h in found[-3:])
            previous = f"- Önceki sayfalarda bulunan son başlıklar: {last}. Seviyeleri bunlarla tutarlı ver.\n"
        reply = llm.chat([{"role": "user", "content": TOC_PROMPT.format(previous=previous, pages=tagged)}],
                         json_mode=True, temperature=0.0, think=False)
        for item in _parse_json(reply).get("bolumler") or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("baslik") or "").strip()
            try:
                page = int(item.get("sayfa"))
                level = max(1, min(int(item.get("seviye") or 1), 4))
            except (TypeError, ValueError):
                continue
            if not title:
                continue
            # Doğrulama: başlık söylenen sayfada (ya da bir yanındakinde) gerçekten geçiyor mu?
            for candidate in (page, page - 1, page + 1):
                if 1 <= candidate <= len(pages):
                    position = _position(title, pages[candidate - 1])
                    if position is not None:
                        found.append({"level": level, "title": title[:200], "page": candidate, "position": position,
                                      "offset": _find_offset(title, pages[candidate - 1])})
                        break
    return found


def _verify_outline(outline: List[dict], pages: List[str]) -> List[dict]:
    """PDF'in kendi içindekiler listesini sayfalarla eşleştirir (konumu bulmak için)."""
    result = []
    for item in outline:
        page = item["page"]
        if not 1 <= page <= len(pages):
            continue
        position = _position(item["title"], pages[page - 1])
        result.append({**item, "position": 0.0 if position is None else position,
                       "offset": _find_offset(item["title"], pages[page - 1])})
    return result


def _build_nodes(headings: List[dict], pages: List[str]) -> List[dict]:
    """Düz başlık listesinden (sırasıyla) iç içe ağaç kurar; her bölümün nerede bittiğini hesaplar.
    Başlığın sayfadaki yeri biliniyorsa bölümün sınırları karakter düzeyinde tutulur (start_off/end_off):
    böylece okurken sayfanın tamamı değil, sadece o bölüm okunur."""
    page_count = len(pages)
    headings = sorted(headings, key=lambda h: (h["page"], h["position"]))
    # Aynı başlık aynı sayfada iki kez gelmesin.
    unique, seen = [], set()
    for h in headings:
        key = (_norm(h["title"]), h["page"])
        if key not in seen:
            seen.add(key)
            unique.append(h)
    headings = unique
    if headings and headings[0]["page"] > 1:
        headings.insert(0, {"level": 1, "title": "Başlangıç", "page": 1, "position": 0.0, "offset": 0})

    flat = []
    for i, h in enumerate(headings):
        nxt = headings[i + 1] if i + 1 < len(headings) else None
        end_off = None
        if nxt is None:
            end = page_count
        elif nxt.get("offset") is not None:
            if not pages[nxt["page"] - 1][:nxt["offset"]].strip():  # sonraki başlık sayfanın en başında
                end = max(h["page"], nxt["page"] - 1)
            else:
                end, end_off = nxt["page"], nxt["offset"]
        elif nxt["position"] <= 0.15:  # sonraki başlık sayfanın en üstünde: bu bölüm bir önceki sayfada biter
            end = max(h["page"], nxt["page"] - 1)
        else:
            end = nxt["page"]
        flat.append({"title": h["title"], "level": h["level"], "start": h["page"], "end": end,
                     "start_off": h.get("offset") or None, "end_off": end_off, "nodes": []})

    roots: List[dict] = []
    stack: List[dict] = []
    for node in flat:
        while stack and stack[-1]["level"] >= node["level"]:
            stack.pop()
        (stack[-1]["nodes"] if stack else roots).append(node)
        stack.append(node)

    def close(node):
        for child in node["nodes"]:
            close(child)
        if node["nodes"]:  # üst bölüm, son alt bölümünün bittiği yerde biter
            node["end"], node["end_off"] = node["nodes"][-1]["end"], node["nodes"][-1]["end_off"]
        del node["level"]

    for root in roots:
        close(root)
    return roots


def _split_large(nodes: List[dict]) -> None:
    """Çok uzun (alt bölümü olmayan) bölümleri sayfa aralıklarına böler: model daha isabetli seçer."""
    limit = max(1, config.PAGEINDEX_MAX_PAGES_PER_NODE)
    for node in nodes:
        if node["nodes"]:
            _split_large(node["nodes"])
        elif node["end"] - node["start"] + 1 > limit:
            for start in range(node["start"], node["end"] + 1, limit):
                end = min(start + limit - 1, node["end"])
                label = f"s. {start}" if start == end else f"s. {start}-{end}"
                node["nodes"].append({"title": f"{node['title']} ({label})", "start": start, "end": end,
                                      "start_off": node.get("start_off") if start == node["start"] else None,
                                      "end_off": node.get("end_off") if end == node["end"] else None,
                                      "nodes": []})


def _number(nodes: List[dict], counter: List[int]) -> None:
    for node in nodes:
        counter[0] += 1
        node["id"] = f"{counter[0]:04d}"
        _number(node["nodes"], counter)


def _walk(nodes: List[dict]):
    for node in nodes:
        yield node
        yield from _walk(node["nodes"])


def _page_slice(node: dict, page: int, text: str) -> Tuple[int, int]:
    """Bölümün bu sayfadaki kısmı: (başlangıç, bitiş) karakter sırası."""
    start = (node.get("start_off") or 0) if page == node["start"] else 0
    end = node.get("end_off") if page == node["end"] and node.get("end_off") is not None else len(text)
    return start, max(start, end)


def _node_text(node: dict, pages: List[str], limit: int) -> str:
    parts = []
    for n in range(node["start"], node["end"] + 1):
        a, b = _page_slice(node, n, pages[n - 1])
        parts.append(pages[n - 1][a:b])
    return "\n".join(parts).strip()[:limit]


def _summarize(nodes: List[dict], pages: List[str]) -> None:
    for node in _walk(nodes):
        if node["nodes"]:
            # Üst bölüm: alt başlıklarından oluşan kısa bir açıklama (ayrı model çağrısı gerekmez).
            node["summary"] = "Alt bölümler: " + "; ".join(child["title"] for child in node["nodes"])[:400]
            continue
        text = _node_text(node, pages, 6000)
        if len(text) <= _SHORT_TEXT:
            node["summary"] = re.sub(r"\s+", " ", text).strip()
            continue
        summary = llm.chat([{"role": "user", "content": SUMMARY_PROMPT.format(title=node["title"], text=text)}],
                           temperature=0.0, think=False)
        node["summary"] = re.sub(r"\s+", " ", summary).strip()[:400]


def make_tree(pages: List[str], outline: Optional[List[dict]] = None) -> Tuple[List[dict], str]:
    """Sayfalardan ağaç kurar; (ağaç, kaynak) döner. Kaynak: 'mevzuat' | 'pdf-icindekiler' | 'model' | 'sayfa'."""
    headings, origin = _legal_headings(pages), "mevzuat"
    if not headings and outline:
        headings, origin = _verify_outline(outline, pages), "pdf-icindekiler"
    if not headings:
        headings, origin = _extract_headings(pages), "model"
    if headings:
        nodes = _build_nodes(headings, pages)
    else:
        # Başlık bulunamadı: her sayfa bir bölüm olur.
        origin = "sayfa"
        nodes = [{"title": f"Sayfa {n}", "start": n, "end": n, "nodes": []} for n in range(1, len(pages) + 1)]
    _split_large(nodes)
    _number(nodes, [0])
    _summarize(nodes, pages)
    return nodes, origin


def build_tree(doc_id: int) -> None:
    """Bir dokümanın ağacını kurar ve kaydeder (arka planda, doküman işleme kuyruğunda çalışır)."""
    with db.get_conn() as conn:
        row = conn.execute("SELECT status, tree_json, tree_status FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if not row or row["status"] != "ready" or row["tree_status"] == "ready":
            return
        conn.execute("UPDATE documents SET tree_status = 'processing', tree_error = NULL WHERE id = ?", (doc_id,))
    started = time.perf_counter()
    try:
        seed = _parse_json(row["tree_json"] or "")
        pages = _load_pages(doc_id)
        nodes, origin = make_tree(pages, seed.get("outline"))
        tree = {"origin": origin, "nodes": nodes, "outline": seed.get("outline")}
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE documents SET tree_json = ?, tree_status = 'ready', tree_seconds = ? WHERE id = ?",
                (json.dumps(tree, ensure_ascii=False), round(time.perf_counter() - started, 2), doc_id),
            )
        log.info("Doküman %s için PageIndex ağacı kuruldu: %d bölüm (%s)", doc_id, len(list(_walk(nodes))), origin)
    except llm.LLMError as e:
        _mark_tree_error(doc_id, str(e))
    except Exception:
        log.exception("Doküman %s için ağaç kurulurken beklenmeyen hata", doc_id)
        _mark_tree_error(doc_id, "İçindekiler ağacı kurulurken beklenmeyen bir hata oluştu.")


def _mark_tree_error(doc_id: int, message: str) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE documents SET tree_status = 'error', tree_error = ? WHERE id = ?", (message, doc_id))


def pending_documents() -> List[int]:
    """Ağacı kurulmayı bekleyen dokümanlar. PageIndex açıksa, ağacı hiç kurulmamış eski dokümanlar da."""
    with db.get_conn() as conn:
        if build_enabled():
            conn.execute("UPDATE documents SET tree_status = 'pending' "
                         "WHERE status = 'ready' AND tree_status IN ('none', 'error')")
        rows = conn.execute("SELECT id FROM documents WHERE status = 'ready' AND tree_status = 'pending' ORDER BY id").fetchall()
    return [r["id"] for r in rows]


def node_count(tree_json: Optional[str]) -> int:
    tree = _parse_json(tree_json or "")
    return len(list(_walk(tree.get("nodes") or [])))


ORIGIN_LABEL = {"mevzuat": "kısım/bölüm/madde yapısı metinden okundu", "pdf-icindekiler": "PDF'in kendi içindekiler listesi", "model": "model başlıkları çıkardı",
                "sayfa": "başlık bulunamadı, her sayfa bir bölüm"}


def tree_origin(tree_json: Optional[str]) -> Optional[str]:
    return _parse_json(tree_json or "").get("origin")


def tree_rows(tree_json: Optional[str]) -> List[dict]:
    """Ağacı düz bir liste olarak döner (rapor için): derinlik, başlık, sayfalar, özet."""
    rows = []

    def add(nodes, depth):
        for node in nodes:
            rows.append({"depth": depth, "title": node["title"], "start": node["start"], "end": node["end"],
                         "summary": node.get("summary", "")})
            add(node["nodes"], depth + 1)

    add(_parse_json(tree_json or "").get("nodes") or [], 0)
    return rows


# --- 2. Arama ---

SEARCH_PROMPT = """Bir soruyu cevaplamak için hangi doküman bölümlerinin okunması gerektiğine karar vereceksin.
Aşağıda dokümanların içindekiler ağacı var. Her satır: bölüm kimliği, [sayfa aralığı], başlık ve kısa özet.
Alt bölümler girintili yazılmıştır.{note}

{outline}

Soru: {question}

Kurallar:
- Cevabı içermesi en olası bölümleri seç; en olası olan ilk sırada olsun. En fazla {limit} bölüm seç.
- Mümkünse en dar (alt) bölümü seç; soru genel bir konuyu soruyorsa üst bölümü seçebilirsin.
- Cevap birden çok yere dağılmış olabilir (ör. karşılaştırma soruları); o zaman hepsini seç.
- Hiçbir bölüm ilgili değilse boş liste ver.
Sadece şu biçimde JSON ver: {{"dusunce": "kısa gerekçe", "bolumler": ["B3", "B7"]}}"""

GROUP_NOTE = """
Ağaç büyük olduğu için bu İLK ADIM: sadece ana bölümler gösteriliyor. Cevabın hangi ana bölümlerde
olabileceğini seç; bir sonraki adımda seçtiğin bölümlerin alt bölümlerini (maddelerini) göreceksin."""
_GROUP_LIMIT = 3  # iki adımlı aramada ilk adımda seçilecek en fazla ana bölüm


def _accessible_trees(user: dict) -> List[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, filename, paged, tree_json, tree_status FROM documents "
            "WHERE (owner_id = ? OR shared = 1) AND status = 'ready' ORDER BY id",
            (user["id"],),
        ).fetchall()
        docs = []
        for row in rows:
            tree = _parse_json(row["tree_json"] or "") if row["tree_status"] == "ready" else {}
            nodes = tree.get("nodes")
            if not nodes:
                # Ağacı henüz hazır değil: geçici olarak her sayfa bir bölüm (ilk satırlarıyla).
                pages = conn.execute("SELECT page_no, text FROM doc_pages WHERE document_id = ? ORDER BY page_no",
                                     (row["id"],)).fetchall()
                nodes = [{"id": f"{p['page_no']:04d}", "title": f"Sayfa {p['page_no']}", "start": p["page_no"],
                          "end": p["page_no"], "summary": re.sub(r"\s+", " ", p["text"][:200]), "nodes": []}
                         for p in pages]
            docs.append({"id": row["id"], "filename": row["filename"], "paged": bool(row["paged"]), "nodes": nodes})
    return docs


def _outline(docs: List[dict], with_summaries: bool, collapse: bool = False) -> Tuple[str, Dict[str, Tuple[dict, dict]]]:
    """Modele gösterilecek ağaç metni ve kısa kimlik → (doküman, bölüm) eşlemesi.
    collapse=True: sadece en üst bölümler gösterilir (alt bölümleri gizlenir)."""
    lines, index = [], {}

    def add(doc, nodes, depth):
        for node in nodes:
            key = f"B{len(index) + 1}"
            index[key] = (doc, node)
            pages = f"s. {node['start']}" if node["start"] == node["end"] else f"s. {node['start']}-{node['end']}"
            if not doc["paged"]:
                pages = "bölüm"
            line = f"{'  ' * depth}{key} [{pages}] {node['title']}"
            if with_summaries and node.get("summary"):
                summary = node["summary"]
                if collapse and len(summary) > 300:
                    summary = summary[:300] + "…"
                line += f" — {summary}"
            lines.append(line)
            if not collapse:
                add(doc, node["nodes"], depth + 1)

    for doc in docs:
        lines.append(f"Doküman: {doc['filename']}")
        add(doc, doc["nodes"], 1)
    return "\n".join(lines), index


def _fitting_outline(docs: List[dict], collapse: bool = False) -> Tuple[str, Dict[str, Tuple[dict, dict]]]:
    """Sığıyorsa özetli ağaç; sığmıyorsa sadece başlıklar (o da sığmazsa kesilmiş hali)."""
    outline, index = _outline(docs, with_summaries=True, collapse=collapse)
    if len(outline) > _OUTLINE_LIMIT:
        outline, index = _outline(docs, with_summaries=False, collapse=collapse)
        outline = outline[:_OUTLINE_LIMIT]
    return outline, index


def _ask_sections(outline: str, index: dict, question: str, limit: int, note: str = "") -> List[Tuple[dict, dict]]:
    prompt = SEARCH_PROMPT.format(outline=outline, question=question, limit=limit, note=note)
    reply = llm.chat([{"role": "user", "content": prompt}], json_mode=True, temperature=0.0,
                     think=config.PAGEINDEX_THINK)
    chosen = []
    for key in _parse_json(reply).get("bolumler") or []:
        key = str(key).strip().upper()
        if not key.startswith("B"):
            key = "B" + key
        if key in index and key not in chosen:
            chosen.append(key)
    return [index[k] for k in chosen[:limit]]


def search(user: dict, question: str) -> List[dict]:
    """Ağaçta akıl yürüterek ilgili bölümleri seçer ve metinlerini kaynak olarak döner."""
    docs = _accessible_trees(user)
    if not docs:
        return []
    limit = max(1, config.PAGEINDEX_MAX_NODES)
    outline, index = _outline(docs, with_summaries=True)
    if len(outline) <= _OUTLINE_LIMIT:
        return _read_sections(_ask_sections(outline, index, question, limit))

    # Ağaç tek seferde sığmıyor: önce ana bölümleri, sonra seçilenlerin alt bölümlerini göster.
    has_children = any(node["nodes"] for doc in docs for node in doc["nodes"])
    if not has_children:
        outline, index = _fitting_outline(docs)
        return _read_sections(_ask_sections(outline, index, question, limit))
    outline, index = _fitting_outline(docs, collapse=True)
    groups = _ask_sections(outline, index, question, _GROUP_LIMIT, GROUP_NOTE)
    if not groups:
        return []
    narrowed: Dict[int, dict] = {}
    for doc, node in groups:
        narrowed.setdefault(doc["id"], {**doc, "nodes": []})["nodes"].append(node)
    outline, index = _fitting_outline(list(narrowed.values()))
    return _read_sections(_ask_sections(outline, index, question, limit))


def _read_sections(selected: List[Tuple[dict, dict]]) -> List[dict]:
    """Seçilen bölümlerin metnini (sırayla, tekrarsız) okur; toplam metin sınırını aşmaz.
    Yeri tam bilinen bölümlerde (ör. kanun maddeleri) sayfanın tamamı değil, sadece bölümün kendisi okunur.
    Hem bir bölüm hem de onun alt bölümü seçildiyse daha dar olan (alt bölüm) okunur."""
    selected = [(doc, node) for doc, node in selected
                if not any(other is not node and any(other is n for n in _walk(node["nodes"]))
                           for _, other in selected)]
    sources, budget, seen, full_pages = [], config.PAGEINDEX_MAX_CONTEXT_CHARS, set(), set()
    with db.get_conn() as conn:
        for doc, node in selected:
            for page in range(node["start"], node["end"] + 1):
                if budget <= 0 or (doc["id"], page) in full_pages:
                    continue
                row = conn.execute("SELECT text FROM doc_pages WHERE document_id = ? AND page_no = ?",
                                   (doc["id"], page)).fetchone()
                if not row:
                    continue
                a, b = _page_slice(node, page, row["text"])
                if (doc["id"], page, a, b) in seen:
                    continue
                seen.add((doc["id"], page, a, b))
                if a == 0 and b == len(row["text"]):
                    full_pages.add((doc["id"], page))
                text = row["text"][a:b].strip()[:budget]
                if not text:
                    continue
                budget -= len(text)
                sources.append({"document_id": doc["id"], "filename": doc["filename"],
                                "page": page if doc["paged"] else None, "section": node["title"], "text": text})
    return sources
