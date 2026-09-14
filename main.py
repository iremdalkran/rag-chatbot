import os
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from embed_and_store import embed_and_store
from query import ask, collection

from data_loader import load_tabular_file, has_any_table
from sql_engine import ask_data

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_FILE_SIZE_MB = 10

# NOT: Eski kod burada `excel_df` (global DataFrame), `CHART_KEYWORDS`, `is_chart_request`,
# `detect_chart_type`, `generate_chart` (matplotlib) fonksiyonlarını içeriyordu. Bunlar tek bir
# statik grafik üretiyordu ve gerçek bir sorguya dayanmıyordu. Artık her soruya özel SQL üretilip
# çalıştırıldığı ve sonuç tablo + grafik + kullanılan SQL olarak döndüğü için bu mantık
# data_loader.py + sql_engine.py'ye taşındı ve buradan kaldırıldı.


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
        print(f"Hata (upload pdf): {e}")
        os.remove(file_path)
        raise HTTPException(
            status_code=400,
            detail="PDF okunamadı. Dosya bozuk olabilir, lütfen başka bir dosya deneyin.",
        )

    return {"status": "başarılı", "dosya": file.filename}


@app.post("/upload-excel")
async def upload_excel(file: UploadFile = File(...)):
    # Not: Endpoint adı geriye dönük uyumluluk için "upload-excel" olarak kaldı, ama artık
    # CSV dosyalarını da kabul ediyor. Eski davranıştan farkı: DataFrame'i hafızada tek bir
    # global değişkende tutup statik bir grafik üretmek yerine, veriyi SQLite'a yazıp şemasını
    # RAG için embed'liyor — böylece her soruya özel SQL sorgusu üretilip çalıştırılabiliyor.

    # 1. Dosya türü kontrolü
    if not file.filename.lower().endswith((".xlsx", ".xls", ".csv")):
        raise HTTPException(
            status_code=400,
            detail="Sadece Excel (.xlsx, .xls) veya CSV dosyaları kabul edilir.",
        )

    # 2. Boyut kontrolü
    contents = await file.read()
    size_mb = len(contents) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=400,
            detail=f"Dosya çok büyük ({size_mb:.1f}MB). Maksimum {MAX_FILE_SIZE_MB}MB olmalı.",
        )

    # 3. Dosyayı diske kaydet (data_loader dosya yolundan okuyor)
    file_path = f"uploaded_{file.filename}"
    with open(file_path, "wb") as f:
        f.write(contents)

    # 4. SQLite'a yaz + şemayı embed'le — bozuk dosya burada patlayabilir
    try:
        info = load_tabular_file(file_path)
    except Exception as e:
        print(f"Hata (upload excel): {e}")
        os.remove(file_path)
        raise HTTPException(
            status_code=400,
            detail="Dosya okunamadı. Bozuk olabilir veya desteklenmeyen bir formatta, lütfen başka bir dosya deneyin.",
        )

    return {
        "status": "başarılı",
        "dosya": file.filename,
        "tablo": info["table_name"],
        "satir_sayisi": info["row_count"],
        "sutunlar": info["columns"],
    }


@app.post("/chat")
async def chat(q: Question):
    # 1. Boş soru kontrolü
    question = q.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Soru boş olamaz.")

    has_pdf = collection.count() > 0
    has_data = has_any_table()

    # 2. Hiçbir şey yüklenmemişse
    if not has_pdf and not has_data:
        return {
            "answer": "Merhaba! Henüz bir dosya yüklemediniz. Verilerinizi analiz etmemi "
                      "istiyorsanız bir Excel/CSV, dokümanlarınızla ilgili soru sormak "
                      "istiyorsanız bir PDF yükleyebilirsiniz. 📄📊"
        }

    # 3. Veri tablosu varsa önce Data RAG akışını dene (tablo + grafik + SQL + öneri)
    if has_data:
        try:
            result = ask_data(question)
        except Exception as e:
            print(f"Hata (chat - data): {e}")
            return {"answer": "Veriniz üzerinde bir sorgu çalıştırırken bir sorun oldu. Sorunuzu farklı bir şekilde sorabilir misiniz? 🤔"}

        # Data RAG soruyu veriyle ilişkilendiremediyse (sql=None) ve PDF de varsa, doküman RAG'ını dene
        if result.get("sql") is None and result.get("table") is None and has_pdf:
            try:
                answer = ask(question)
                return {"answer": answer}
            except Exception as e:
                print(f"Hata (chat - pdf fallback): {e}")
                return {"answer": "Bu sorunun cevabını yüklediğiniz dokümanda bulamadım. Sorunuzu farklı bir şekilde sorabilir ya da başka bir soru deneyebilirsiniz. 🤔"}

        return result

    # 4. Sadece PDF varsa klasik doküman RAG akışı
    try:
        answer = ask(question)
    except Exception as e:
        print(f"Hata (chat - pdf): {e}")
        return {"answer": "Bu sorunun cevabını yüklediğiniz dokümanda bulamadım. Sorunuzu farklı bir şekilde sorabilir ya da başka bir soru deneyebilirsiniz. 🤔"}

    return {"answer": answer}


@app.get("/")
async def root():
    return {"message": "RAG API çalışıyor"}