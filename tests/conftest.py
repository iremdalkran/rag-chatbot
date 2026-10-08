"""
Test ortamı. Gerçek Ollama yerine, aynı API'yi taklit eden sahte bir sunucu kullanılır;
böylece testler internet ve büyük modeller olmadan saniyeler içinde çalışır.
"""

import hashlib
import json
import os
import re
import tempfile
import time

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="rag-test-")
os.environ["ALLOW_REGISTRATION"] = "false"

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth, config, db, documents, llm  # noqa: E402
from app.main import app  # noqa: E402

DIM = 64


def fake_vector(text: str) -> list:
    """Kelime torbası: ortak kelimesi çok olan metinlerin vektörleri birbirine yakın olur."""
    vec = np.zeros(DIM, dtype=np.float32)
    for word in re.findall(r"\w+", text.lower()):
        vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM] += 1
    return vec.tolist()


class FakeOllama:
    def __init__(self):
        self.sql = "SELECT urun, SUM(adet) AS toplam FROM satislar GROUP BY urun ORDER BY toplam DESC"
        self.chat_requests = []
        self.judge_requests = []
        self.judge_verdict = "dogru"
        self.order = []
        self.pulled = []
        self.installed = []
        self.down = False

    def reply(self, payload: dict) -> str:
        system = payload["messages"][0]["content"]
        last = payload["messages"][-1]["content"]
        if "SQLite uzmanısın" in system:
            return self.sql
        if "tarafsız bir hakemsin" in system:
            self.judge_requests.append(payload)
            return json.dumps({"karar": self.judge_verdict, "gerekce": "test gerekçesi"})
        if payload.get("format") == "json":
            return json.dumps({"answer": "En çok satan ürün elma.", "suggestions": ["a?", "b?", "c?"]})
        if "VERI ya da DOKUMAN" in last:
            return "VERI" if "toplam" in last.split("Soru:")[-1].lower() else "DOKUMAN"
        return "<think>gizli düşünce</think>Kahve 16. yüzyılda İstanbul'a ulaştı [1]."

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("bağlantı yok")
        payload = json.loads(request.content or b"{}")
        if request.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [fake_vector(t) for t in payload["input"]]})
        if request.url.path == "/api/generate" and payload.get("keep_alive") == 0:
            self.order.append(("unload", payload["model"]))
            return httpx.Response(200, json={"done": True})
        if request.url.path == "/api/chat":
            self.chat_requests.append(payload)
            judging = "tarafsız bir hakemsin" in payload["messages"][0]["content"]
            self.order.append(("judge" if judging else "ask", payload["model"]))
            text = self.reply(payload)
            if not payload.get("stream"):
                return httpx.Response(200, json={"message": {"role": "assistant", "content": text}, "done": True})
            pieces = [text[i:i + 5] for i in range(0, len(text), 5)]
            lines = [json.dumps({"message": {"content": p}, "done": False}) for p in pieces]
            lines.append(json.dumps({"message": {"content": ""}, "done": True}))
            return httpx.Response(200, content="\n".join(lines).encode())
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": self.installed + [
                {"name": "qwen3:14b", "digest": "abc123def4567890",
                 "details": {"parameter_size": "14.8B", "quantization_level": "Q4_K_M"}},
                {"name": "bge-m3:latest", "details": {"parameter_size": "567M", "quantization_level": "F16"}},
                {"name": "qwen3:30b-a3b", "details": {"parameter_size": "30.5B", "quantization_level": "Q4_K_M"}}]})
        if request.url.path == "/api/pull":
            self.pulled.append(payload["model"])
            self.installed.append({"name": payload["model"], "details": {}})
            lines = [{"status": "pulling", "total": 100, "completed": c} for c in (0, 50, 100)] + [{"status": "success"}]
            return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines).encode())
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.0"})
        return httpx.Response(404, json={"error": "not found"})


@pytest.fixture(autouse=True)
def fresh_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(config, "TABLES_DIR", tmp_path / "tables")
    db.init_db()
    documents._cache.clear()
    auth._failed_logins.clear()
    yield


@pytest.fixture
def ollama(monkeypatch):
    fake = FakeOllama()
    transport = httpx.MockTransport(fake.handler)
    monkeypatch.setattr(llm, "_client", lambda timeout=None: httpx.Client(base_url="http://ollama", transport=transport))
    return fake


@pytest.fixture
def client(ollama):
    with TestClient(app) as c:
        yield c


def register(client, name="Ayşe", email="ayse@firma.com", password="guclu-sifre-1"):
    return client.post("/api/auth/register", json={"name": name, "email": email, "password": password})


def make_user(admin_client, name, email, password="guclu-sifre-1", is_admin=False):
    r = admin_client.post("/api/admin/users", json={"name": name, "email": email, "password": password,
                                                    "is_admin": is_admin})
    assert r.status_code == 200, r.text
    return r.json()


def login_client(email, password="guclu-sifre-1"):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return c


def wait_ready(client, doc_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        docs = {d["id"]: d for d in client.get("/api/documents").json()}
        if docs[doc_id]["status"] != "processing":
            return docs[doc_id]
        time.sleep(0.05)
    raise AssertionError("doküman işlenmedi")


def ask(client, question, **extra):
    r = client.post("/api/ask", json={"question": question, **extra})
    assert r.status_code == 200, r.text
    return [json.loads(line) for line in r.text.splitlines() if line.strip()]
