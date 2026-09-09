import os
from dotenv import load_dotenv
import voyageai
import chromadb
from chunking import read_pdf, chunk_text

load_dotenv()

voyage_client = voyageai.Client(api_key=os.getenv("VOYAGE_API_KEY"))

# Chroma'yı diskte kalıcı olacak şekilde başlat
chroma_client = chromadb.PersistentClient(path="./chroma_db")
collection = chroma_client.get_or_create_collection(name="dokuman_chunklari")


def embed_and_store(pdf_path):
    # 1. Dokümanı oku ve chunk'lara böl
    text = read_pdf(pdf_path)
    chunks = chunk_text(text, chunk_size=100, overlap=20)
    print(f"{len(chunks)} chunk oluşturuldu.")

    # 2. Her chunk'ı embedding'e çevir
    result = voyage_client.embed(chunks, model="voyage-3.5", input_type="document")
    embeddings = result.embeddings
    print("Embedding'ler oluşturuldu.")

    # 3. Chroma'ya kaydet
    ids = [f"chunk_{i}" for i in range(len(chunks))]
    collection.add(
        ids=ids,
        embeddings=embeddings,
        documents=chunks,
    )
    print(f"{len(chunks)} chunk Chroma'ya kaydedildi.")


if __name__ == "__main__":
    embed_and_store("ornek_dokuman.pdf")
    print("\nToplam kayıtlı chunk sayısı:", collection.count())