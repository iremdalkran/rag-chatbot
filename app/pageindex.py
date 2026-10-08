"""
pageindex.py
------------
PageIndex yöntemi (VectifyAI/PageIndex'ten uyarlanmıştır): vektör kullanmadan, "akıl yürüterek" arama.

Fikir: İnsan kalın bir kitapta bilgi ararken önce İÇİNDEKİLER sayfasına bakar, ilgili bölümü seçer,
sonra o sayfaları okur. Burada da öyle:

  1. Ağaç kurma (doküman yüklenince, bir kez):
     - Dokümanın sayfa metinleri saklanır.
     - PDF'in kendi içindekiler listesi varsa o kullanılır; yoksa yerel model sayfaları okuyup
       bölüm başlıklarını ve hangi sayfada başladıklarını çıkarır.
     - Başlıkların gerçekten o sayfada geçtiği kontrol edilir (uydurma başlıklar atılır).
     - Başlıklar seviyelerine göre ağaca dizilir; her bölümün bitiş sayfası hesaplanır; çok uzun
       bölümler sayfa aralıklarına bölünür; her bölüm için model kısa bir özet yazar.
  2. Arama (her soruda):
     - Model; dokümanların ağacını (başlık + sayfa + özet) ve soruyu görür, cevabı içerebilecek
       bölümleri seçer.
     - Seçilen bölümlerin sayfaları OLDUĞU GİBİ okunur ve cevap yazan modele verilir.

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


# --- 1. Ağaç kurma ---

TOC_PROMPT = """Aşağıda bir dokümanın bazı sayfaları var. Her sayfa <sayfa_N> ... </sayfa_N> etiketleri arasında.
Görevin: bu sayfalarda BAŞLAYAN bölüm başlıklarını (ana bölüm, alt bölüm) sırasıyla çıkarmak.

Kurallar:
- Sadece metinde gerçekten başlık olarak geçen ifadeleri, metindeki yazımıyla aynen yaz. Başlık uydurma.
- "seviye": 1 = ana bölüm, 2 = alt bölüm, 3 = daha alt bölüm. Numaralı başlıklarda numaraya bak
  (ör. "3" → 1, "3.2" → 2, "3.2.1" → 3).
- "sayfa": başlığın geçtiği sayfanın numarası (etiketteki N).
- Sayfa üst/alt bilgileri, tablo satırları, madde işaretli cümleler başlık değildir.
- Hiç başlık yoksa boş liste ver.
{previous}
Sadece şu biçimde JSON ver: {{"bolumler": [{{"seviye": 1, "baslik": "...", "sayfa": 3}}]}}

{pages}"""

SUMMARY_PROMPT = """Aşağıdaki doküman bölümünü 1-2 cümleyle özetle. Bölümde hangi konuların, kuralların,
sayıların, adların, tarihlerin geçtiğini belirt ki biri bu özete bakarak bir sorunun cevabının bu bölümde
olup olmadığına karar verebilsin. Metin, bölümün geçtiği sayfaların tamamıdır: sadece başlığı verilen bölümü
özetle, aynı sayfadaki başka bölümleri katma. Sadece özeti yaz.

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
                        found.append({"level": level, "title": title[:200], "page": candidate, "position": position})
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
        result.append({**item, "position": 0.0 if position is None else position})
    return result


def _build_nodes(headings: List[dict], page_count: int) -> List[dict]:
    """Düz başlık listesinden (sırasıyla) iç içe ağaç kurar ve bitiş sayfalarını hesaplar."""
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
        headings.insert(0, {"level": 1, "title": "Başlangıç", "page": 1, "position": 0.0})

    flat = []
    for i, h in enumerate(headings):
        nxt = headings[i + 1] if i + 1 < len(headings) else None
        if nxt is None:
            end = page_count
        elif nxt["position"] <= 0.15:  # sonraki başlık sayfanın en üstünde: bu bölüm bir önceki sayfada biter
            end = max(h["page"], nxt["page"] - 1)
        else:
            end = nxt["page"]
        flat.append({"title": h["title"], "level": h["level"], "start": h["page"], "end": end, "nodes": []})

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
            node["end"] = max(node["end"], child["end"])
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
                node["nodes"].append({"title": f"{node['title']} ({label})", "start": start, "end": end, "nodes": []})


def _number(nodes: List[dict], counter: List[int]) -> None:
    for node in nodes:
        counter[0] += 1
        node["id"] = f"{counter[0]:04d}"
        _number(node["nodes"], counter)


def _walk(nodes: List[dict]):
    for node in nodes:
        yield node
        yield from _walk(node["nodes"])


def _node_text(node: dict, pages: List[str], limit: int) -> str:
    return "\n".join(pages[n - 1] for n in range(node["start"], node["end"] + 1))[:limit]


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
    """Sayfalardan ağaç kurar; (ağaç, kaynak) döner. Kaynak: 'pdf-icindekiler' | 'model' | 'sayfa'."""
    headings, origin = [], "model"
    if outline:
        headings, origin = _verify_outline(outline, pages), "pdf-icindekiler"
    if not headings:
        headings, origin = _extract_headings(pages), "model"
    if headings:
        nodes = _build_nodes(headings, len(pages))
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


# --- 2. Arama ---

SEARCH_PROMPT = """Bir soruyu cevaplamak için hangi doküman bölümlerinin okunması gerektiğine karar vereceksin.
Aşağıda dokümanların içindekiler ağacı var. Her satır: bölüm kimliği, [sayfa aralığı], başlık ve kısa özet.
Alt bölümler girintili yazılmıştır.

{outline}

Soru: {question}

Kurallar:
- Cevabı içermesi en olası bölümleri seç; en olası olan ilk sırada olsun. En fazla {limit} bölüm seç.
- Mümkünse en dar (alt) bölümü seç; soru genel bir konuyu soruyorsa üst bölümü seçebilirsin.
- Cevap birden çok yere dağılmış olabilir (ör. karşılaştırma soruları); o zaman hepsini seç.
- Hiçbir bölüm ilgili değilse boş liste ver.
Sadece şu biçimde JSON ver: {{"dusunce": "kısa gerekçe", "bolumler": ["B3", "B7"]}}"""


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


def _outline(docs: List[dict], with_summaries: bool) -> Tuple[str, Dict[str, Tuple[dict, dict]]]:
    """Modele gösterilecek ağaç metni ve kısa kimlik → (doküman, bölüm) eşlemesi."""
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
                line += f" — {node['summary']}"
            lines.append(line)
            add(doc, node["nodes"], depth + 1)

    for doc in docs:
        lines.append(f"Doküman: {doc['filename']}")
        add(doc, doc["nodes"], 1)
    return "\n".join(lines), index


def search(user: dict, question: str) -> List[dict]:
    """Ağaçta akıl yürüterek ilgili bölümleri seçer ve sayfalarını kaynak olarak döner."""
    docs = _accessible_trees(user)
    if not docs:
        return []
    outline, index = _outline(docs, with_summaries=True)
    if len(outline) > _OUTLINE_LIMIT:  # çok doküman varsa önce özetleri bırak, yine sığmazsa kes
        outline, index = _outline(docs, with_summaries=False)
        outline = outline[:_OUTLINE_LIMIT]
    limit = max(1, config.PAGEINDEX_MAX_NODES)
    reply = llm.chat([{"role": "user", "content": SEARCH_PROMPT.format(outline=outline, question=question, limit=limit)}],
                     json_mode=True, temperature=0.0, think=config.PAGEINDEX_THINK)
    chosen = []
    for key in _parse_json(reply).get("bolumler") or []:
        key = str(key).strip().upper()
        if not key.startswith("B"):
            key = "B" + key
        if key in index and key not in chosen:
            chosen.append(key)
    return _read_sections([index[k] for k in chosen[:limit]])


def _read_sections(selected: List[Tuple[dict, dict]]) -> List[dict]:
    """Seçilen bölümlerin sayfalarını (sırayla, tekrarsız) okur; toplam metin sınırını aşmaz."""
    wanted: List[Tuple[dict, dict, int]] = []
    seen = set()
    for doc, node in selected:
        for page in range(node["start"], node["end"] + 1):
            if (doc["id"], page) not in seen:
                seen.add((doc["id"], page))
                wanted.append((doc, node, page))
    sources, budget = [], config.PAGEINDEX_MAX_CONTEXT_CHARS
    with db.get_conn() as conn:
        for doc, node, page in wanted:
            if budget <= 0:
                break
            row = conn.execute("SELECT text FROM doc_pages WHERE document_id = ? AND page_no = ?",
                               (doc["id"], page)).fetchone()
            if not row or not row["text"].strip():
                continue
            text = row["text"][:budget]
            budget -= len(text)
            sources.append({"document_id": doc["id"], "filename": doc["filename"],
                            "page": page if doc["paged"] else None, "section": node["title"], "text": text})
    return sources
