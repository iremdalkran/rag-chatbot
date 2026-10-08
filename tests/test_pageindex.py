import json
import re
import sqlite3
import time
from pathlib import Path

from app import config, db, documents, llm, pageindex
from tests.conftest import ask, login_client, make_user, register, wait_ready

SAMPLE_PDF = Path(__file__).resolve().parent.parent / "ornekler" / "ornek_dokuman.pdf"
FILLER = "Bu paragraf bölümün devamıdır ve konuyla ilgili ayrıntılar içerir. " * 12


def wait_tree(client, doc_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        doc = {d["id"]: d for d in client.get("/api/documents").json()}[doc_id]
        if doc["tree_status"] in ("ready", "error"):
            return doc
        time.sleep(0.05)
    raise AssertionError("ağaç kurulmadı")


def test_pageindex_method_builds_tree_and_answers_from_selected_pages(client, ollama, monkeypatch):
    monkeypatch.setattr(config, "RAG_METHOD", "pageindex")
    register(client)
    doc = client.post("/api/documents", files={"file": ("kahve.pdf", SAMPLE_PDF.read_bytes())}).json()
    wait_ready(client, doc["id"])
    ready = wait_tree(client, doc["id"])
    assert ready["tree_status"] == "ready" and ready["tree_nodes"] >= 1

    events = ask(client, "Kahve İstanbul'a ne zaman ulaştı?")
    assert [e.get("method") for e in events if e["type"] == "route"] == ["pageindex"]
    done = events[-1]["message"]
    assert done["method"] == "pageindex"
    assert done["sources"][0]["filename"] == "kahve.pdf" and done["sources"][0]["page"] == 1

    search = ollama.tree_search_requests[-1]
    assert "Doküman: kahve.pdf" in search["messages"][0]["content"]
    assert search["think"] is False and search["format"] == "json"
    answer_prompt = ollama.chat_requests[-1]["messages"][0]["content"]
    assert "[1] (kahve.pdf, sayfa 1)" in answer_prompt and "İstanbul" in answer_prompt


def test_vector_method_does_not_build_trees(client, ollama):
    register(client)
    doc = client.post("/api/documents", files={"file": ("kahve.pdf", SAMPLE_PDF.read_bytes())}).json()
    wait_ready(client, doc["id"])
    time.sleep(0.2)
    listed = client.get("/api/documents").json()[0]
    assert listed["tree_status"] == "none" and not ollama.toc_requests
    done = ask(client, "Kahve İstanbul'a ne zaman ulaştı?")[-1]["message"]
    assert done["method"] == "vector" and not ollama.tree_search_requests


def test_switching_to_pageindex_builds_trees_for_existing_documents(client, ollama, monkeypatch):
    register(client)
    doc = client.post("/api/documents", files={"file": ("kahve.pdf", SAMPLE_PDF.read_bytes())}).json()
    wait_ready(client, doc["id"])
    monkeypatch.setattr(config, "RAG_METHOD", "pageindex")  # .env'de RAG_METHOD=pageindex + yeniden başlatma
    assert documents.resume_tree_builds() == 1
    assert wait_tree(client, doc["id"])["tree_status"] == "ready"


def _fake_chat(headings_by_page):
    def chat(messages, json_mode=False, temperature=0.2, model=None, think=None):
        prompt = messages[-1]["content"]
        if "BAŞLAYAN" in prompt:
            found = [{"seviye": lvl, "baslik": title, "sayfa": page}
                     for page, items in headings_by_page.items() for lvl, title in items
                     if f"<sayfa_{page}>" in prompt]
            return json.dumps({"bolumler": found})
        return "kısa özet"
    return chat


def test_tree_structure_end_pages_preface_and_splitting(monkeypatch):
    monkeypatch.setattr(config, "PAGEINDEX_MAX_PAGES_PER_NODE", 2)
    pages = [
        "Kapak ve önsöz. " + FILLER,                                    # 1: başlıksız → "Başlangıç"
        "1 Genel Hükümler\n" + FILLER,                                  # 2
        FILLER + "\n1.1 Amaç\n" + FILLER,                               # 3: alt bölüm sayfa ortasında
        "2 İzinler\n" + FILLER,                                         # 4
        FILLER, FILLER, FILLER,                                         # 5-7: uzun bölüm → bölünür
    ]
    monkeypatch.setattr(llm, "chat", _fake_chat({
        2: [(1, "1 Genel Hükümler")], 3: [(2, "1.1 Amaç")], 4: [(1, "2 İzinler")],
        5: [(1, "Uydurma Başlık")],  # metinde yok → atılmalı
    }))
    nodes, origin = pageindex.make_tree(pages)
    assert origin == "model"
    assert [(n["title"], n["start"], n["end"]) for n in nodes] == [
        ("Başlangıç", 1, 1), ("1 Genel Hükümler", 2, 3), ("2 İzinler", 4, 7)]
    general = nodes[1]
    # "1.1 Amaç" sayfanın ortasında başlıyor: üst bölümün kendi metni de 3. sayfada sürer.
    assert [(c["title"], c["start"], c["end"]) for c in general["nodes"]] == [("1.1 Amaç", 3, 3)]
    leave = nodes[2]
    assert [(c["start"], c["end"]) for c in leave["nodes"]] == [(4, 5), (6, 7)]
    assert all("Uydurma" not in n["title"] for n in pageindex._walk(nodes))
    assert [n["id"] for n in pageindex._walk(nodes)][:3] == ["0001", "0002", "0003"]
    assert general["summary"].startswith("Alt bölümler: 1.1 Amaç") and nodes[0]["summary"] == "kısa özet"


def test_pdf_outline_is_used_without_asking_model(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "chat", lambda messages, **kw: calls.append(messages) or "özet")
    pages = ["Giriş\n" + FILLER, "Sonuç\n" + FILLER]
    nodes, origin = pageindex.make_tree(pages, [{"level": 1, "title": "Giriş", "page": 1},
                                                {"level": 1, "title": "Sonuç", "page": 2}])
    assert origin == "pdf-icindekiler"
    assert [(n["title"], n["start"], n["end"]) for n in nodes] == [("Giriş", 1, 1), ("Sonuç", 2, 2)]
    assert not any("BAŞLAYAN" in m[-1]["content"] for m in calls)


def test_document_without_headings_falls_back_to_pages(monkeypatch):
    monkeypatch.setattr(llm, "chat", _fake_chat({}))
    nodes, origin = pageindex.make_tree([FILLER, FILLER])
    assert origin == "sayfa" and [n["title"] for n in nodes] == ["Sayfa 1", "Sayfa 2"]


def test_text_document_uses_virtual_pages_without_page_numbers(client, ollama, monkeypatch):
    monkeypatch.setattr(config, "RAG_METHOD", "pageindex")
    register(client)
    text = "Yemek Kartı\n" + "Çalışanlara her ay yemek kartı yüklenir. " * 150
    doc = client.post("/api/documents", files={"file": ("yan_haklar.txt", text.encode())}).json()
    wait_ready(client, doc["id"])
    wait_tree(client, doc["id"])
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM doc_pages WHERE document_id = ?", (doc["id"],)).fetchone()[0] >= 2
    ollama.picked_sections = ["B1"]
    done = ask(client, "Yemek kartı ne zaman yüklenir?", mode="docs")[-1]["message"]
    assert done["sources"][0]["filename"] == "yan_haklar.txt" and done["sources"][0]["page"] is None
    assert "B1 [bölüm] Yemek Kartı" in ollama.tree_search_requests[-1]["messages"][0]["content"]


def test_pageindex_only_sees_users_own_and_shared_documents(client, ollama, monkeypatch):
    monkeypatch.setattr(config, "RAG_METHOD", "pageindex")
    register(client)
    make_user(client, "Mehmet", "mehmet@firma.com")
    doc = client.post("/api/documents", files={"file": ("gizli_kahve.pdf", SAMPLE_PDF.read_bytes())}).json()
    wait_ready(client, doc["id"])
    other = login_client("mehmet@firma.com")
    other.post("/api/documents", files={"file": ("mehmet.txt", "Mehmet'in notları burada. ".encode() * 5)})
    time.sleep(0.5)
    ask(other, "Kahve nereden geldi?", mode="docs")
    outline = ollama.tree_search_requests[-1]["messages"][0]["content"]
    assert "gizli_kahve.pdf" not in outline and "mehmet.txt" in outline


def test_selected_pages_respect_context_limit(monkeypatch):
    monkeypatch.setattr(config, "PAGEINDEX_MAX_CONTEXT_CHARS", 1000)
    doc = {"id": 1, "filename": "a.pdf", "paged": True}
    monkeypatch.setattr(pageindex.db, "get_conn", _FakeConn.factory(["x" * 800, "y" * 800, "z" * 800]))
    sources = pageindex._read_sections([(doc, {"title": "A", "start": 1, "end": 3})])
    assert [len(s["text"]) for s in sources] == [800, 200]


class _FakeConn:
    def __init__(self, pages):
        self.pages = pages

    @classmethod
    def factory(cls, pages):
        from contextlib import contextmanager

        @contextmanager
        def get_conn():
            yield cls(pages)
        return get_conn

    def execute(self, sql, params):
        page = params[1]

        class Result:
            def fetchone(_self):
                return {"text": self.pages[page - 1]} if page <= len(self.pages) else None
        return Result()


def test_old_database_gets_new_columns_and_pages_rebuilt_from_chunks(tmp_path, monkeypatch):
    path = tmp_path / "eski.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT, password_hash TEXT,
                            is_admin INTEGER, created_at TEXT);
        CREATE TABLE documents (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, filename TEXT,
            shared INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, error TEXT, page_count INTEGER NOT NULL DEFAULT 0,
            chunk_count INTEGER NOT NULL DEFAULT 0, size_bytes INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        INSERT INTO users VALUES (1, 'A', 'a@b.co', 'x', 1, 'now');
        INSERT INTO documents (owner_id, filename, status, page_count, created_at) VALUES (1, 'eski.pdf', 'ready', 2, 'now');
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", path)
    db.init_db()
    with db.get_conn() as c:
        columns = {r[1] for r in c.execute("PRAGMA table_info(documents)")}
        assert {"paged", "tree_json", "tree_status", "tree_error", "tree_seconds"} <= columns
        for i, (page, text) in enumerate([(1, "Birinci cümle. İkinci cümle."), (1, "İkinci cümle. Üçüncü cümle."),
                                          (2, "Dördüncü cümle.")]):
            c.execute("INSERT INTO chunks (document_id, chunk_index, page, text, embedding) VALUES (1, ?, ?, ?, x'00')",
                      (i, page, text))
    assert pageindex._load_pages(1) == ["Birinci cümle. İkinci cümle. Üçüncü cümle.", "Dördüncü cümle."]


# --- Kanun / yönetmelik yapısı ---

ARTICLE_BODY = "Bu madde hükmüne göre işveren ile işçi arasındaki ilişkiler düzenlenir ve uygulanır. " * 4
LAW_PAGES = [
    "İŞ KANUNU\nKanun Numarası : 4857\nBİRİNCİ BÖLÜM\nGenel Hükümler\nAmaç ve kapsam\nMadde 1 – " + ARTICLE_BODY
    + "\nTanımlar\nMadde 2 – " + ARTICLE_BODY + "\nEşit davranma ilkesi",              # başlık sayfa sonunda kaldı
    "Madde 3 – " + ARTICLE_BODY + "\nİKİNCİ BÖLÜM\nİş Sözleşmesi\nDeneme süresi\n(Değişik: 1/1/2020-1/1 md.)\n"
    "Madde 4 – Deneme süresi en çok iki aydır. " + ARTICLE_BODY,
    ARTICLE_BODY + "\nÇalışma süresi\nMadde 5 – Çalışma süresi haftada en çok kırkbeş saattir. " + ARTICLE_BODY
    + "\nGeçici Madde 1 – Geçiş hükümleri uygulanır.",
]


def test_law_structure_is_read_from_text_without_model(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "chat", lambda messages, **kw: calls.append(messages) or "kısa özet")
    nodes, origin = pageindex.make_tree(LAW_PAGES)
    assert origin == "mevzuat"
    assert not any("BAŞLAYAN" in m[-1]["content"] for m in calls)  # başlık çıkarmak için model çağrılmadı
    assert [n["title"] for n in nodes] == ["BİRİNCİ BÖLÜM – Genel Hükümler", "İKİNCİ BÖLÜM – İş Sözleşmesi"]
    assert [c["title"] for c in nodes[0]["nodes"]] == [
        "Madde 1 – Amaç ve kapsam", "Madde 2 – Tanımlar", "Madde 3 – Eşit davranma ilkesi"]
    assert [c["title"] for c in nodes[1]["nodes"]] == [
        "Madde 4 – Deneme süresi", "Madde 5 – Çalışma süresi", "Geçici Madde 1"]
    article3 = nodes[0]["nodes"][2]
    assert (article3["start"], article3["end"]) == (1, 2)  # başlığı 1. sayfanın sonunda, metni 2. sayfada
    # Madde metinleri birbirine karışmıyor.
    article4_text = pageindex._node_text(nodes[1]["nodes"][0], LAW_PAGES, 5000)
    assert article4_text.startswith("Deneme süresi\n(Değişik") and "Madde 5" not in article4_text
    assert "Madde 3" not in article4_text and "İKİNCİ BÖLÜM" not in article4_text


def test_selected_article_is_read_without_rest_of_page(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda messages, **kw: "kısa özet")
    nodes, _ = pageindex.make_tree(LAW_PAGES)
    monkeypatch.setattr(pageindex.db, "get_conn", _FakeConn.factory(LAW_PAGES))
    doc = {"id": 1, "filename": "is_kanunu.pdf", "paged": True}
    article5 = nodes[1]["nodes"][1]
    sources = pageindex._read_sections([(doc, nodes[1]), (doc, article5)])  # üst bölüm + maddesi seçildi
    assert [s["page"] for s in sources] == [3]  # daha dar olan (madde) okundu
    assert sources[0]["text"].startswith("Çalışma süresi\nMadde 5") and "Geçici Madde" not in sources[0]["text"]
    assert sources[0]["section"] == "Madde 5 – Çalışma süresi"


def test_large_tree_is_searched_in_two_steps(monkeypatch):
    monkeypatch.setattr(pageindex, "_OUTLINE_LIMIT", 400)  # ağaç "sığmasın"
    monkeypatch.setattr(llm, "chat", lambda messages, **kw: "Bu madde uzun bir özet metnidir. " * 3)
    nodes, _ = pageindex.make_tree(LAW_PAGES)
    doc = {"id": 1, "filename": "is_kanunu.pdf", "paged": True, "nodes": nodes}
    monkeypatch.setattr(pageindex, "_accessible_trees", lambda user: [doc])
    monkeypatch.setattr(pageindex.db, "get_conn", _FakeConn.factory(LAW_PAGES))
    prompts = []

    def chat(messages, **kw):
        prompt = messages[-1]["content"]
        prompts.append(prompt)
        outline = prompt.split("Soru:")[0]
        if "İLK ADIM" in prompt:  # 1. adım: sadece bölümler görünür
            key = next(k for k, t in re.findall(r"(B\d+) \[[^]]*\] (.*)", outline) if "İKİNCİ BÖLÜM" in t)
        else:
            key = next(k for k, t in re.findall(r"(B\d+) \[[^]]*\] (.*)", outline) if "Çalışma süresi" in t)
        return json.dumps({"bolumler": [key]})

    monkeypatch.setattr(llm, "chat", chat)
    sources = pageindex.search({"id": 1}, "Haftalık çalışma süresi en fazla kaç saat?")
    assert len(prompts) == 2
    first = prompts[0].split("Soru:")[0]
    assert len(re.findall(r"B\d+ \[", first)) == 2              # 1. adımda sadece iki bölüm, maddeler ayrı satır değil
    second = prompts[1].split("Soru:")[0]
    assert "Madde 4 – Deneme süresi" in second and "Madde 1 –" not in second  # 2. adım: sadece seçilen bölüm
    assert sources[0]["section"] == "Madde 5 – Çalışma süresi" and sources[0]["page"] == 3


def test_ordinary_text_with_few_articles_is_not_treated_as_law():
    pages = ["Giriş\nMadde 1 – tek bir madde geçiyor. " + FILLER, FILLER]
    assert pageindex._legal_headings(pages) == []
