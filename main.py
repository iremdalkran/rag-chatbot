import os
from typing import Optional
import traceback
from fastapi import FastAPI, UploadFile, File, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from embed_and_store import embed_and_store
from query import ask, collection

from data_loader import load_tabular_file, has_any_table
from sql_engine import ask_data
import chat_store
import auth

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_FILE_SIZE_MB = 10


def _friendly_error_message(e: Exception, default: str) -> str:
    """
    Bir hatanın 'rate limit' (Voyage/Anthropic'in dakikadaki istek sınırı) kaynaklı olup
    olmadığını anlar. Öyleyse kullanıcıya bunu doğru şekilde açıklayan bir mesaj döner,
    değilse çağıranın verdiği genel (default) mesajı döner.
    """
    text = str(e).lower()
    rate_limit_signals = ("rate limit", "rate_limit", "429", "reduced rate limits", "too many requests")
    if any(signal in text for signal in rate_limit_signals):
        return ("Sistem şu an çok fazla istek aldığı için geçici bir hız sınırına (rate limit) "
                "takıldı. Bu bir dosya/soru hatası değil — lütfen birkaç saniye bekleyip tekrar "
                "deneyin. ⏳")
    return default


# NOT: Eski kod burada `excel_df` (global DataFrame), `CHART_KEYWORDS`, `is_chart_request`,
# `detect_chart_type`, `generate_chart` (matplotlib) fonksiyonlarını içeriyordu. Bunlar tek bir
# statik grafik üretiyordu ve gerçek bir sorguya dayanmıyordu. Artık her soruya özel SQL üretilip
# çalıştırıldığı ve sonuç tablo + grafik + kullanılan SQL olarak döndüğü için bu mantık
# data_loader.py + sql_engine.py'ye taşındı ve buradan kaldırıldı.


class Question(BaseModel):
    question: str
    chat_id: Optional[int] = None


class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


@app.post("/register")
async def register(body: RegisterRequest):
    """Yeni kullanıcı kaydı oluşturur ve otomatik giriş yapar (bir oturum token'ı döner)."""
    try:
        user = auth.register_user(body.name, body.email, body.password)
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))

    token = auth.create_session(user["id"])
    return {"token": token, "name": user["name"], "email": user["email"]}


@app.post("/login")
async def login(body: LoginRequest):
    """E-posta+şifreyi doğrular, doğruysa bir oturum token'ı döner."""
    try:
        user = auth.authenticate_user(body.email, body.password)
    except auth.AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    token = auth.create_session(user["id"])
    return {"token": token, "name": user["name"], "email": user["email"]}


async def require_auth(x_auth_token: Optional[str] = Header(default=None)) -> dict:
    """
    Korumalı endpoint'lerin önüne konan bağımlılık (dependency). Geçerli bir X-Auth-Token
    header'ı yoksa isteği 401 ile reddeder; geçerliyse o oturumun ait olduğu kullanıcının
    bilgisini ({id, name, email}) döner — endpoint'ler bunu `user["id"]` şeklinde kullanır.
    """
    user = auth.get_session_user(x_auth_token)
    if user is None:
        raise HTTPException(status_code=401, detail="Giriş yapmanız gerekiyor.")
    return user


@app.get("/me")
async def get_me(user: dict = Depends(require_auth)):
    """Giriş yapmış kullanıcının bilgisini döner (frontend'de profil etiketi için)."""
    return user


@app.get("/chats")
async def get_chats(user: dict = Depends(require_auth)):
    """Sol menüde gösterilecek sohbet listesi — SADECE bu kullanıcının kendi sohbetleri."""
    return chat_store.list_chats(user["id"])


@app.post("/chats")
async def create_new_chat(user: dict = Depends(require_auth)):
    """'Yeni sohbet' butonuna basınca bu kullanıcı için boş bir sohbet oluşturur."""
    chat_id = chat_store.create_chat(user["id"])
    return {"id": chat_id, "title": "Yeni sohbet"}


@app.get("/chats/{chat_id}/messages")
async def get_chat_messages(chat_id: int, user: dict = Depends(require_auth)):
    """Bir sohbete tıklanınca geçmiş mesajları getirir — sadece kendi sohbetiyse."""
    if not chat_store.chat_exists(chat_id, user["id"]):
        raise HTTPException(status_code=404, detail="Sohbet bulunamadı.")
    return chat_store.get_messages(chat_id, user["id"])


@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...), user: dict = Depends(require_auth)):
    # Not: PDF/doküman koleksiyonu şu an TÜM kullanıcılar arasında ortak — sadece Excel/CSV
    # verisi (aşağıdaki /upload-excel) kişiye özel hale getirildi.

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Sadece PDF dosyaları kabul edilir.")

    contents = await file.read()
    size_mb = len(contents) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=400,
            detail=f"Dosya çok büyük ({size_mb:.1f}MB). Maksimum {MAX_FILE_SIZE_MB}MB olmalı.",
        )

    file_path = f"uploaded_{file.filename}"
    with open(file_path, "wb") as f:
        f.write(contents)

    try:
        embed_and_store(file_path)
    except Exception as e:
        print(f"Hata (upload pdf): {e}")
        os.remove(file_path)
        raise HTTPException(
            status_code=400,
            detail=_friendly_error_message(
                e, "PDF okunamadı. Dosya bozuk olabilir, lütfen başka bir dosya deneyin."
            ),
        )

    return {"status": "başarılı", "dosya": file.filename}


@app.post("/upload-excel")
async def upload_excel(file: UploadFile = File(...), user: dict = Depends(require_auth)):
    # Not: Endpoint adı geriye dönük uyumluluk için "upload-excel" olarak kaldı, ama artık
    # CSV dosyalarını da kabul ediyor. Yüklenen veri bu kullanıcıya özel bir tabloya yazılıyor
    # (data_loader.py'de user_id önekiyle) — başka kullanıcıların verisiyle karışmıyor.

    if not file.filename.lower().endswith((".xlsx", ".xls", ".csv")):
        raise HTTPException(
            status_code=400,
            detail="Sadece Excel (.xlsx, .xls) veya CSV dosyaları kabul edilir.",
        )

    contents = await file.read()
    size_mb = len(contents) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=400,
            detail=f"Dosya çok büyük ({size_mb:.1f}MB). Maksimum {MAX_FILE_SIZE_MB}MB olmalı.",
        )

    file_path = f"uploaded_{file.filename}"
    with open(file_path, "wb") as f:
        f.write(contents)

    try:
        info = load_tabular_file(file_path, user_id=user["id"])
    except Exception as e:
        print(f"Hata (upload excel): {e}")
        traceback.print_exc()
        os.remove(file_path)
        raise HTTPException(
            status_code=400,
            detail=_friendly_error_message(
                e,
                "Dosya okunamadı. Bozuk olabilir veya desteklenmeyen bir formatta, lütfen başka bir dosya deneyin.",
            ),
        )

    return {
        "status": "başarılı",
        "dosya": file.filename,
        "tablo": info["table_name"],
        "satir_sayisi": info["row_count"],
        "sutunlar": info["columns"],
    }


@app.post("/chat")
async def chat(q: Question, user: dict = Depends(require_auth)):
    question = q.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Soru boş olamaz.")

    chat_id = q.chat_id
    if chat_id is None or not chat_store.chat_exists(chat_id, user["id"]):
        chat_id = chat_store.create_chat(user["id"])

    chat_store.add_message(chat_id, "user", content=question)
    chat_store.maybe_set_title_from_first_message(chat_id, question)

    has_pdf = collection.count() > 0
    has_data = has_any_table(user["id"])

    if not has_pdf and not has_data:
        answer = ("Merhaba! Henüz bir dosya yüklemediniz. Verilerinizi analiz etmemi "
                  "istiyorsanız bir Excel/CSV, dokümanlarınızla ilgili soru sormak "
                  "istiyorsanız bir PDF yükleyebilirsiniz. 📄📊")
        chat_store.add_message(chat_id, "assistant", content=answer)
        return {"answer": answer, "chat_id": chat_id}

    if has_data:
        try:
            result = ask_data(question, user_id=user["id"])
        except Exception as e:
            print(f"Hata (chat - data): {e}")
            traceback.print_exc()
            answer = _friendly_error_message(
                e, "Veriniz üzerinde bir sorgu çalıştırırken bir sorun oldu. Sorunuzu farklı bir şekilde sorabilir misiniz? 🤔"
            )
            chat_store.add_message(chat_id, "assistant", content=answer)
            return {"answer": answer, "chat_id": chat_id}

        if result.get("sql") is None and result.get("table") is None and has_pdf:
            try:
                answer = ask(question)
                chat_store.add_message(chat_id, "assistant", content=answer)
                return {"answer": answer, "chat_id": chat_id}
            except Exception as e:
                print(f"Hata (chat - pdf fallback): {e}")
                answer = _friendly_error_message(
                    e, "Bu sorunun cevabını yüklediğiniz dokümanda bulamadım. Sorunuzu farklı bir şekilde sorabilir ya da başka bir soru deneyebilirsiniz. 🤔"
                )
                chat_store.add_message(chat_id, "assistant", content=answer)
                return {"answer": answer, "chat_id": chat_id}

        chat_store.add_message(
            chat_id,
            "assistant",
            content=result.get("answer"),
            table=result.get("table"),
            sql=result.get("sql"),
            chart=result.get("chart"),
            suggestions=result.get("suggestions"),
        )
        result["chat_id"] = chat_id
        return result

    try:
        answer = ask(question)
    except Exception as e:
        print(f"Hata (chat - pdf): {e}")
        answer = _friendly_error_message(
            e, "Bu sorunun cevabını yüklediğiniz dokümanda bulamadım. Sorunuzu farklı bir şekilde sorabilir ya da başka bir soru deneyebilirsiniz. 🤔"
        )

    chat_store.add_message(chat_id, "assistant", content=answer)
    return {"answer": answer, "chat_id": chat_id}


@app.get("/")
async def root():
    return {"message": "RAG API çalışıyor"}