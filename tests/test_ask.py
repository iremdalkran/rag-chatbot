from pathlib import Path

from app import llm
from tests.conftest import ask, register, wait_ready

SAMPLE_PDF = Path(__file__).resolve().parent.parent / "ornekler" / "ornek_dokuman.pdf"
CSV = "urun,adet\nelma,10\narmut,5\nelma,3\n".encode()


def test_no_sources_gives_guidance(client):
    register(client)
    events = ask(client, "Merhaba")
    assert events[0]["type"] == "meta"
    assert "yükleyebilirsiniz" in events[-1]["message"]["content"]


def test_document_answer_streams_with_sources(client, ollama):
    register(client)
    doc = client.post("/api/documents", files={"file": ("kahve.pdf", SAMPLE_PDF.read_bytes())}).json()
    ready = wait_ready(client, doc["id"])
    assert ready["status"] == "ready" and ready["page_count"] == 1

    events = ask(client, "Kahve İstanbul'a ne zaman ulaştı?")
    tokens = "".join(e["text"] for e in events if e["type"] == "token")
    done = events[-1]
    assert done["type"] == "done"
    assert "gizli düşünce" not in tokens  # modelin düşünme bölümü kullanıcıya gösterilmez
    assert tokens.strip() == done["message"]["content"]
    sources = done["message"]["sources"]
    assert sources[0]["filename"] == "kahve.pdf" and sources[0]["page"] == 1 and sources[0]["cited"]

    # Kaynak metni modele gerçekten verilmiş olmalı ve düşünme kapalı olmalı.
    request = ollama.chat_requests[-1]
    assert "[1] (kahve.pdf, sayfa 1)" in request["messages"][0]["content"]
    assert request["think"] is False and request["options"]["num_ctx"] >= 8192

    # Geçmiş kaydedildi mi?
    chat_id = events[0]["chat_id"]
    messages = client.get(f"/api/chats/{chat_id}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["sources"][0]["filename"] == "kahve.pdf"
    assert client.get("/api/chats").json()[0]["title"].startswith("Kahve İstanbul")


def test_follow_up_question_gets_history(client, ollama):
    register(client)
    doc = client.post("/api/documents", files={"file": ("kahve.pdf", SAMPLE_PDF.read_bytes())}).json()
    wait_ready(client, doc["id"])
    first = ask(client, "Kahve nereden geldi?")
    chat_id = first[0]["chat_id"]
    ask(client, "Peki ya sonra?", chat_id=chat_id)
    messages = ollama.chat_requests[-1]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "Kahve nereden geldi?"


def test_data_question_returns_table_sql_and_chart(client, ollama):
    register(client)
    r = client.post("/api/datasets", files={"file": ("satislar.csv", CSV)})
    assert r.json()[0]["row_count"] == 3

    done = ask(client, "Ürünlere göre toplam satış nedir?")[-1]["message"]
    assert done["mode"] == "data"
    assert done["table"]["rows"] == [["elma", 13], ["armut", 5]]
    assert done["chart"]["x_labels"] == ["elma", "armut"]
    assert done["content"] == "En çok satan ürün elma."
    assert len(done["suggestions"]) == 3


def test_bad_sql_is_retried_then_reported(client, ollama):
    register(client)
    client.post("/api/datasets", files={"file": ("satislar.csv", CSV)})
    ollama.sql = "SELECT olmayan_kolon FROM satislar"
    events = ask(client, "Toplam ne?")
    assert events[-1]["type"] == "error"
    assert "farklı sormayı" in events[-1]["message"]


def test_auto_mode_routes_between_docs_and_data(client, ollama):
    register(client)
    doc = client.post("/api/documents", files={"file": ("kahve.pdf", SAMPLE_PDF.read_bytes())}).json()
    wait_ready(client, doc["id"])
    client.post("/api/datasets", files={"file": ("satislar.csv", CSV)})
    assert ask(client, "Toplam satış ne kadar?")[-1]["message"]["mode"] == "data"
    assert ask(client, "Kahvenin kökeni nedir?")[-1]["message"]["mode"] == "docs"
    assert ask(client, "Kahvenin kökeni nedir?", mode="data")[-1]["message"]["mode"] == "data"


def test_ollama_down_gives_friendly_error(client, ollama):
    register(client)
    client.post("/api/datasets", files={"file": ("satislar.csv", CSV)})
    ollama.down = True
    events = ask(client, "Toplam ne?")
    assert events[-1]["type"] == "error"
    assert "Ollama" in events[-1]["message"]


def test_failed_ingestion_is_reported(client, ollama):
    register(client)
    ollama.down = True
    doc = client.post("/api/documents", files={"file": ("not.txt", "Bir miktar metin içeriği.".encode())}).json()
    ready = wait_ready(client, doc["id"])
    assert ready["status"] == "error" and "Ollama" in ready["error"]


def test_upload_size_limit(client, monkeypatch):
    from app import config
    register(client)
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 1)
    r = client.post("/api/documents", files={"file": ("buyuk.txt", b"a" * (1024 * 1024 + 10))})
    assert r.status_code == 413


def test_strip_thinking_across_split_tags():
    pieces = ["<thi", "nk>uzun düşün", "ce</th", "ink>\n\nCe", "vap <", "b>kalın</b>"]
    assert "".join(llm._strip_thinking(iter(pieces))) == "Cevap <b>kalın</b>"
