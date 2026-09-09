import os
import shutil
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from embed_and_store import embed_and_store
from query import ask, collection

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_FILE_SIZE_MB = 10


class Question(BaseModel):
    question: str


@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...)):
    # 1. Dosya türü kontrolü
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Sadece PDF dosyaları kabul edilir.")

    # 2. Dosyayı geçici olarak oku ve boyutunu kontrol et
    contents = await file.read()
    size_mb = len(contents) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=400,
            detail=f"Dosya çok büyük ({size_mb:.1f}MB). Maksimum {MAX_FILE_SIZE_MB}MB olmalı.",
        )

    # 3. Dosyayı diske kaydet
    file_path = f"uploaded_{file.filename}"
    with open(file_path, "wb") as f:
        f.write(contents)

    # 4. Chunking + embedding + Chroma'ya kaydetme — bozuk PDF burada patlayabilir
    try:
        embed_and_store(file_path)
    except Exception as e:
        os.remove(file_path)
        raise HTTPException(
            status_code=400,
            detail="PDF okunamadı. Dosya bozuk olabilir, lütfen başka bir dosya deneyin.",
        )

    return {"status": "başarılı", "dosya": file.filename}


@app.post("/chat")
async def chat(q: Question):
    # 1. Boş soru kontrolü
    question = q.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Soru boş olamaz.")

    # 2. Henüz doküman yüklenmemiş mi?
    if collection.count() == 0:
        raise HTTPException(
            status_code=400,
            detail="Henüz bir doküman yüklenmedi. Önce bir PDF yükleyin.",
        )

    try:
        answer = ask(question)
    except Exception as e:
        raise HTTPException(status_code=500, detail="Cevap üretilirken bir hata oluştu, lütfen tekrar deneyin.")

    return {"answer": answer}


@app.get("/")
async def root():
    return {"message": "RAG API çalışıyor"}