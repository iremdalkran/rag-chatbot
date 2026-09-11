import os
import shutil
import io
import base64
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

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

# Yüklenen excel verisini geçici olarak hafızada tutuyoruz
excel_df = None

# Kullanıcının "grafik yap" gibi bir istekte bulunduğunu anlamak için anahtar kelimeler
CHART_KEYWORDS = ["grafik", "çizdir", "görselleştir", "chart", "plot", "görsel"]


def detect_chart_type(text: str) -> str:
    text_lower = text.lower()
    if any(k in text_lower for k in ["pasta", "pie"]):
        return "pie"
    if any(k in text_lower for k in ["çizgi", "line", "trend"]):
        return "line"
    return "bar"


class Question(BaseModel):
    question: str


def is_chart_request(text: str) -> bool:
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in CHART_KEYWORDS)


def generate_chart(df: pd.DataFrame, chart_type: str = "bar") -> str:
    """Excel verisinden grafik üretir, base64 PNG string döner."""
    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    if not numeric_cols:
        raise ValueError("Excel dosyasında sayısal bir sütun bulunamadı.")

    # İlk sayısal olmayan sütunu etiket (x ekseni) olarak kullan, yoksa satır index'i kullan
    non_numeric_cols = df.select_dtypes(exclude="number").columns.tolist()
    labels = df[non_numeric_cols[0]].astype(str) if non_numeric_cols else df.index.astype(str)

    fig, ax = plt.subplots(figsize=(7, 4.5))

    if chart_type == "pie":
        # Pasta grafik sadece tek sayısal sütunla anlamlı, ilkini kullanıyoruz
        col = numeric_cols[0]
        ax.pie(df[col], labels=labels, autopct="%1.1f%%")
        ax.set_title(f"{col} Dağılımı")

    elif chart_type == "line":
        x = range(len(df))
        for col in numeric_cols:
            ax.plot(x, df[col], marker="o", label=col)
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels, rotation=45, ha="right")
        ax.legend()
        ax.set_title("Yüklenen Excel Verisi")

    else:  # bar (varsayılan)
        x = range(len(df))
        width = 0.8 / len(numeric_cols)
        for i, col in enumerate(numeric_cols):
            ax.bar([p + i * width for p in x], df[col], width=width, label=col)
        ax.set_xticks([p + width * (len(numeric_cols) - 1) / 2 for p in x])
        ax.set_xticklabels(labels, rotation=45, ha="right")
        ax.legend()
        ax.set_title("Yüklenen Excel Verisi")

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


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


@app.post("/upload-excel")
async def upload_excel(file: UploadFile = File(...)):
    global excel_df

    # 1. Dosya türü kontrolü
    if not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Sadece Excel dosyaları (.xlsx, .xls) kabul edilir.")

    # 2. Boyut kontrolü
    contents = await file.read()
    size_mb = len(contents) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=400,
            detail=f"Dosya çok büyük ({size_mb:.1f}MB). Maksimum {MAX_FILE_SIZE_MB}MB olmalı.",
        )

    # 3. Pandas ile oku — bozuk dosya burada patlayabilir
    try:
        df = pd.read_excel(io.BytesIO(contents))
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail="Excel dosyası okunamadı. Dosya bozuk olabilir, lütfen başka bir dosya deneyin.",
        )

    # 4. Sayısal veri var mı kontrolü
    if df.select_dtypes(include="number").empty:
        raise HTTPException(
            status_code=400,
            detail="Bu Excel dosyasında grafik oluşturmak için sayısal bir veri bulunamadı.",
        )

    excel_df = df
    return {
        "status": "başarılı",
        "dosya": file.filename,
        "satir_sayisi": len(df),
        "sutunlar": df.columns.tolist(),
    }


@app.post("/chat")
async def chat(q: Question):
    global excel_df

    # 1. Boş soru kontrolü
    question = q.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Soru boş olamaz.")

    # 2. Grafik isteği mi?
    if is_chart_request(question):
        if excel_df is None:
            return {"answer": "Grafik oluşturabilmem için önce bir Excel dosyası yüklemen gerekiyor. 📊"}
        try:
            chart_type = detect_chart_type(question)
            chart_base64 = generate_chart(excel_df, chart_type)
            return {"answer": "İşte yüklediğin veriye ait grafik:", "chart_base64": chart_base64}
        except Exception as e:
            print(f"Hata (chart): {e}")
            return {"answer": "Grafik oluşturulurken bir sorun oldu. Excel dosyanı kontrol edip tekrar yükleyebilir misin? 🤔"}

    # 3. Henüz doküman yüklenmemiş mi?
    if collection.count() == 0:
        return {"answer": "Merhaba! Henüz bir doküman yüklemediniz. Lütfen önce yukarıdan bir PDF yükleyin, sonra sorularınızı seve seve yanıtlarım. 📄"}

    try:
        answer = ask(question)
    except Exception as e:
        print(f"Hata (chat): {e}")
        return {"answer": "Bu sorunun cevabını yüklediğiniz dokümanda bulamadım. Sorunuzu farklı bir şekilde sorabilir ya da başka bir soru deneyebilirsiniz. 🤔"}

    return {"answer": answer}


@app.get("/")
async def root():
    return {"message": "RAG API çalışıyor"}