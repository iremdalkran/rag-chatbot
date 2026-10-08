"""
main.py
-------
Web sunucusu (FastAPI). Hem API'yi hem de arayüzü (static/ klasörü) aynı adresten sunar,
bu yüzden arayüzde sabit bir sunucu adresi yoktur ve CORS gerekmez.

Çalıştırmak için:  uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

import json
import logging
import threading
from contextlib import asynccontextmanager
from typing import Literal, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool
from pydantic import BaseModel, Field

from app import auth, chats, config, db, documents, ingest, llm, rag, tabular

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("app")

COOKIE_NAME = "session"

db.init_db()


@asynccontextmanager
async def lifespan(_app):
    # Yarıda kalan / yeni gereken PageIndex ağaçlarını arka planda kurmaya başla.
    queued = documents.resume_tree_builds()
    if queued:
        log.info("%d doküman için PageIndex içindekiler ağacı kuruluyor", queued)
    yield


app = FastAPI(title="Yerel Doküman Asistanı", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


# --- Güvenlik başlıkları ve basit CSRF koruması ---

@app.middleware("http")
async def security_middleware(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "Geçersiz istek kaynağı."}, status_code=403)
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# --- Kimlik doğrulama yardımcıları ---

def current_user(request: Request) -> dict:
    user = auth.get_session_user(request.cookies.get(COOKIE_NAME))
    if user is None:
        raise HTTPException(status_code=401, detail="Giriş yapmanız gerekiyor.")
    return user


def admin_user(user: dict = Depends(current_user)) -> dict:
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Bu işlem için yönetici yetkisi gerekiyor.")
    return user


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME, token, max_age=config.SESSION_DAYS * 86400, httponly=True,
        samesite="strict", secure=config.COOKIE_SECURE, path="/",
    )


async def _read_upload(file: UploadFile) -> bytes:
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"Dosya çok büyük. En fazla {config.MAX_UPLOAD_MB} MB olabilir.")
    if not data:
        raise HTTPException(status_code=400, detail="Dosya boş.")
    return data


# --- Genel durum ---

@app.get("/api/health")
def health():
    status = llm.health()
    status["setup_required"] = auth.user_count() == 0
    status["registration_open"] = auth.registration_open()
    status["rag_method"] = config.RAG_METHOD
    return status


# --- Hesap ---

class RegisterBody(BaseModel):
    name: str = Field(max_length=100)
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


class LoginBody(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


class PasswordBody(BaseModel):
    old_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


@app.post("/api/auth/register")
def register(body: RegisterBody, response: Response):
    if not auth.registration_open():
        raise HTTPException(status_code=403, detail="Kayıt kapalı. Hesap açılması için yöneticinize başvurun.")
    try:
        user = auth.create_user(body.name, body.email, body.password)
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _set_session_cookie(response, auth.create_session(user["id"]))
    return user


@app.post("/api/auth/login")
def login(body: LoginBody, response: Response):
    try:
        user = auth.authenticate(body.email, body.password)
    except auth.AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    _set_session_cookie(response, auth.create_session(user["id"]))
    return user


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    auth.delete_session(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: dict = Depends(current_user)):
    return user


@app.post("/api/auth/password")
def change_password(body: PasswordBody, response: Response, user: dict = Depends(current_user)):
    try:
        auth.change_password(user["id"], body.old_password, body.new_password)
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Tüm oturumlar kapandı; bu tarayıcı için yenisini açıyoruz.
    _set_session_cookie(response, auth.create_session(user["id"]))
    return {"ok": True}


# --- Sohbetler ---

class RenameBody(BaseModel):
    title: str = Field(max_length=200)


@app.get("/api/chats")
def list_chats(user: dict = Depends(current_user)):
    return chats.list_chats(user["id"])


@app.post("/api/chats")
def create_chat(user: dict = Depends(current_user)):
    return chats.create_chat(user["id"])


@app.patch("/api/chats/{chat_id}")
def rename_chat(chat_id: int, body: RenameBody, user: dict = Depends(current_user)):
    if not chats.rename_chat(chat_id, user["id"], body.title):
        raise HTTPException(status_code=404, detail="Sohbet bulunamadı.")
    return {"ok": True}


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: int, user: dict = Depends(current_user)):
    if not chats.delete_chat(chat_id, user["id"]):
        raise HTTPException(status_code=404, detail="Sohbet bulunamadı.")
    return {"ok": True}


@app.get("/api/chats/{chat_id}/messages")
def chat_messages(chat_id: int, user: dict = Depends(current_user)):
    if not chats.get_chat(chat_id, user["id"]):
        raise HTTPException(status_code=404, detail="Sohbet bulunamadı.")
    return chats.get_messages(chat_id, user["id"])


# --- Soru sorma (cevap akarak gelir) ---

class AskBody(BaseModel):
    question: str = Field(max_length=4000)
    chat_id: Optional[int] = None
    mode: Literal["auto", "docs", "data"] = "auto"


def _event(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


# Model cevap veremezse (Ollama kapalı vb.) kullanılacak basit yedek kural.
_DATA_HINTS = ("toplam", "ortalama", "kaç tane", "kaç adet", "en çok", "en az", "en yüksek", "en düşük",
               "sırala", "grafik", "yüzde", "oran", "sayısı", "listele", "tablo")


def _choose_mode(user: dict, question: str, requested: str, has_docs: bool, has_data: bool,
                 history: list, last_mode: Optional[str]) -> Optional[str]:
    """
    Soru doküman aramasıyla mı ('docs'), tablolarda SQL ile mi ('data') cevaplanmalı?
    Kullanıcı seçiciden elle seçtiyse ona uyulur; tek tür kaynak varsa o seçilir; ikisi de varsa
    karar modele sorulur. Model, kaynakların adlarını/kolonlarını ve (takip sorularında) bir önceki
    soruyu da görür.
    """
    if requested == "docs":
        return "docs" if has_docs else None
    if requested == "data":
        return "data" if has_data else None
    if has_docs and not has_data:
        return "docs"
    if has_data and not has_docs:
        return "data"
    if not has_docs and not has_data:
        return None

    datasets = tabular.list_datasets(user["id"])[:8]
    tables = "; ".join(f"{d['table_name']} ({', '.join(d['columns'][:12])})" for d in datasets)
    doc_names = ", ".join(d["filename"] for d in documents.list_documents(user) if d["status"] == "ready")[:600]
    previous = next((m["content"] for m in reversed(history) if m["role"] == "user"), None)
    context = ""
    if previous:
        kind = {"data": "VERI", "docs": "DOKUMAN"}.get(last_mode or "", "bilinmiyor")
        context = (f"Sohbetteki önceki soru: {previous[:300]}\nÖnceki soru şuradan cevaplandı: {kind}\n"
                   "(Yeni soru öncekinin devamıysa, örneğin “peki geçen ay?”, genelde aynı kaynak seçilmelidir.)\n\n")
    prompt = (
        "Kullanıcının elinde iki tür kaynak var:\n"
        f"- VERI: Excel/CSV tabloları — {tables}. Sayma, toplama, ortalama, sıralama, filtreleme, karşılaştırma "
        "ve grafik isteyen sorular; ya da tablolardaki kolonlarla ilgili kayıt arama soruları.\n"
        f"- DOKUMAN: PDF/Word/metin belgeleri — {doc_names}. Kurallar, prosedürler, tanımlar, açıklamalar, "
        "sözleşme maddeleri gibi metin içeriği soruları.\n\n"
        f"{context}"
        "Aşağıdaki soru hangisiyle cevaplanmalı? Sadece VERI ya da DOKUMAN yaz.\n\n"
        f"Soru: {question}"
    )
    try:
        decision = llm.chat([{"role": "user", "content": prompt}], temperature=0.0, think=False).upper()
    except llm.LLMError:
        q = question.lower()
        return "data" if any(h in q for h in _DATA_HINTS) else "docs"
    if "DOKUMAN" in decision or "DOKÜMAN" in decision:
        return "docs"
    return "data" if "VERI" in decision or "VERİ" in decision else "docs"


@app.post("/api/ask")
def ask(body: AskBody, user: dict = Depends(current_user)):
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Soru boş olamaz.")

    chat = chats.get_chat(body.chat_id, user["id"]) if body.chat_id else None
    if chat is None:
        chat = chats.create_chat(user["id"])
    chat_id = chat["id"]
    history = chats.history_for_llm(chat_id, config.HISTORY_MESSAGES)
    last_mode = chats.last_answer_mode(chat_id)
    chats.add_message(chat_id, "user", question, question=question)

    recorder = _AnswerRecorder(chat_id)

    def generate():
        yield _event({"type": "meta", "chat_id": chat_id})
        has_docs = documents.has_ready_documents(user)
        has_data = tabular.has_datasets(user["id"])
        try:
            mode = _choose_mode(user, question, body.mode, has_docs, has_data, history, last_mode)
            if mode is None:
                answer = ("Henüz soru sorabileceğim bir kaynak yok. Sol menüdeki “Kaynaklar” bölümünden bir doküman "
                          "(PDF, Word, TXT) veya veri dosyası (Excel, CSV) yükleyebilirsiniz.")
                if body.mode == "docs" and has_data:
                    answer = "Henüz hazır bir dokümanınız yok. Önce bir doküman yükleyin veya “Veri” modunu seçin."
                elif body.mode == "data" and has_docs:
                    answer = "Henüz yüklenmiş bir veri tablonuz yok. Önce bir Excel/CSV yükleyin veya “Doküman” modunu seçin."
                recorder.save(answer)
                yield _event({"type": "done", "message": {"role": "assistant", "content": answer}})
                return

            if mode == "data":
                yield _event({"type": "route", "mode": "data"})
                result = tabular.ask(user["id"], question, history)
                # Model "bu soru tablolarla ilgili değil" dediyse ve kullanıcı modu kendisi seçmediyse,
                # soruyu boş cevapla bırakmak yerine dokümanlarda arıyoruz.
                if result.get("no_query") and has_docs and body.mode == "auto":
                    mode = "docs"
                else:
                    extra = {k: result[k] for k in ("sql", "table", "chart", "suggestions") if result.get(k)}
                    extra["mode"] = "data"
                    recorder.save(result["answer"], extra)
                    yield _event({"type": "done", "message": {"role": "assistant", "content": result["answer"], **extra}})
                    return

            yield _event({"type": "route", "mode": "docs", "method": config.RAG_METHOD})
            sources, stream = rag.answer_stream(user, question, history)
            recorder.sources = sources
            for piece in stream:
                if recorder.cancelled.is_set():
                    return  # kullanıcı durdurdu; yarım cevabı stream_body kaydeder
                recorder.parts.append(piece)
                yield _event({"type": "token", "text": piece})
            answer = "".join(recorder.parts).strip() or "Bir cevap oluşturulamadı, lütfen tekrar deneyin."
            extra = {"mode": "docs", "method": config.RAG_METHOD, "sources": rag.cited_sources(answer, sources)}
            recorder.save(answer, extra)
            yield _event({"type": "done", "message": {"role": "assistant", "content": answer, **extra}})
        except (llm.LLMError, tabular.DataError) as e:
            recorder.save(f"⚠️ {e}", {"error": True})
            yield _event({"type": "error", "message": str(e)})
        except Exception:
            log.exception("Soru cevaplanırken beklenmeyen hata")
            message = "Beklenmeyen bir hata oluştu. Lütfen tekrar deneyin."
            recorder.save(f"⚠️ {message}", {"error": True})
            yield _event({"type": "error", "message": message})

    async def stream_body():
        iterator = generate()
        try:
            async for chunk in iterate_in_threadpool(iterator):
                yield chunk
        finally:
            # Tarayıcı bağlantıyı kestiyse ("durdur" düğmesi, sayfa kapandı) buraya gelinir.
            if not recorder.finished:
                recorder.cancelled.set()
                await run_in_threadpool(recorder.save_partial)
                try:
                    await run_in_threadpool(iterator.close)  # Ollama bağlantısını da kapatır
                except ValueError:
                    pass  # o an başka bir iş parçacığında çalışıyor; bir sonraki adımda kendisi durur

    return StreamingResponse(stream_body(), media_type="application/x-ndjson")


class _AnswerRecorder:
    """Bir sorunun cevabını tam olarak BİR kez kaydeder: ya tamamı ya da durdurulduysa yazılan kısmı."""

    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.parts: list = []
        self.sources: list = []
        self.cancelled = threading.Event()
        self.finished = False
        self._lock = threading.Lock()

    def save(self, content: str, extra: Optional[dict] = None) -> None:
        with self._lock:
            if self.finished:
                return
            self.finished = True
            chats.add_message(self.chat_id, "assistant", content, extra)

    def save_partial(self) -> None:
        partial = "".join(self.parts).strip()
        if partial:
            self.save(partial + "\n\n*(durduruldu)*",
                      {"mode": "docs", "method": config.RAG_METHOD,
                       "sources": rag.cited_sources(partial, self.sources)})


# --- Dokümanlar ---

@app.get("/api/documents")
def list_documents(user: dict = Depends(current_user)):
    return documents.list_documents(user)


@app.post("/api/documents")
async def upload_document(file: UploadFile = File(...), shared: bool = Form(False), user: dict = Depends(current_user)):
    data = await _read_upload(file)
    try:
        return documents.add_document(user, file.filename or "dosya", data, shared)
    except ingest.IngestError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: int, user: dict = Depends(current_user)):
    if not documents.delete_document(user, doc_id):
        raise HTTPException(status_code=404, detail="Doküman bulunamadı veya silme yetkiniz yok.")
    return {"ok": True}


# --- Veri tabloları (Excel/CSV) ---

@app.get("/api/datasets")
def list_datasets(user: dict = Depends(current_user)):
    return tabular.list_datasets(user["id"])


@app.post("/api/datasets")
async def upload_dataset(file: UploadFile = File(...), user: dict = Depends(current_user)):
    filename = (file.filename or "").replace("\\", "/").split("/")[-1]
    if not filename.lower().endswith(tabular.SUPPORTED_EXTENSIONS):
        raise HTTPException(status_code=400, detail="Sadece Excel (.xlsx, .xls) veya CSV dosyaları kabul edilir.")
    data = await _read_upload(file)
    try:
        return tabular.load_file(user["id"], filename, data)
    except tabular.DataError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/datasets/{dataset_id}")
def delete_dataset(dataset_id: int, user: dict = Depends(current_user)):
    if not tabular.delete_dataset(user["id"], dataset_id):
        raise HTTPException(status_code=404, detail="Tablo bulunamadı.")
    return {"ok": True}


# --- Yönetim (sadece yöneticiler) ---

class NewUserBody(BaseModel):
    name: str = Field(max_length=100)
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)
    is_admin: bool = False


class UpdateUserBody(BaseModel):
    is_admin: Optional[bool] = None
    password: Optional[str] = Field(default=None, max_length=200)


@app.get("/api/admin/users")
def admin_list_users(admin: dict = Depends(admin_user)):
    return auth.list_users()


@app.post("/api/admin/users")
def admin_create_user(body: NewUserBody, admin: dict = Depends(admin_user)):
    try:
        return auth.create_user(body.name, body.email, body.password, body.is_admin)
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(user_id: int, body: UpdateUserBody, admin: dict = Depends(admin_user)):
    try:
        if body.is_admin is not None:
            auth.set_admin(user_id, body.is_admin, admin["id"])
        if body.password:
            auth.set_password(user_id, body.password)
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, admin: dict = Depends(admin_user)):
    try:
        auth.delete_user(user_id, admin["id"])
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    tabular.delete_user_files(user_id)
    return {"ok": True}


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def api_not_found(path: str):
    raise HTTPException(status_code=404, detail="Bulunamadı.")


# Arayüz dosyaları (en sonda, API adreslerini gölgelemesin diye).
app.mount("/", StaticFiles(directory=str(config.BASE_DIR / "static"), html=True), name="static")
