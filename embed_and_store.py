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


def embed_and_store(pdf_path, user_id):
    """
    PDF'i parçalara bölüp embed'ler ve Chroma'ya kaydeder.

    KİŞİYE ÖZEL: Her parçaya metadata olarak {"user_id": ...} yazılıyor; query.py aramayı
    sadece bu etikete sahip parçalarla sınırlıyor. Böylece bir kullanıcının PDF'i başkasına
    görünmüyor.

    Bir kullanıcı yeni PDF yüklediğinde SADECE kendi önceki PDF parçaları silinir (tek aktif
    doküman mantığı — Excel/CSV tarafıyla aynı). Başkalarının parçalarına dokunulmaz.
    """
    # 1. Dokümanı oku ve chunk'lara böl
    text = read_pdf(pdf_path)
    chunks = chunk_text(text, chunk_size=100, overlap=20)
    print(f"{len(chunks)} chunk oluşturuldu.")

    # 2. Her chunk'ı embedding'e çevir
    result = voyage_client.embed(chunks, model="voyage-3.5", input_type="document")
    embeddings = result.embeddings
    print("Embedding'ler oluşturuldu.")

    # 3. Eski parçaları embedding BAŞARILI olduktan sonra sil (embedding hata verirse
    #    kullanıcının önceki PDF'i kaybolmasın diye sıra önemli).
    collection.delete(where={"user_id": user_id})

    # 4. Chroma'ya kaydet. id'lere user_id ekliyoruz: eskiden hep "chunk_0, chunk_1..." idi ve
    #    farklı yüklemeler birbirinin id'siyle çakışabiliyordu.
    ids = [f"u{user_id}_chunk_{i}" for i in range(len(chunks))]
    metadatas = [{"user_id": user_id} for _ in chunks]
    collection.add(
        ids=ids,
        embeddings=embeddings,
        documents=chunks,
        metadatas=metadatas,
    )
    print(f"{len(chunks)} chunk Chroma'ya kaydedildi (user_id={user_id}).")


if __name__ == "__main__":
    # Komut satırından hızlı deneme için: user_id=0 test kullanıcısı gibi davranır.
    embed_and_store("ornek_dokuman.pdf", user_id=0)
    print("\nToplam kayıtlı chunk sayısı:", collection.count())