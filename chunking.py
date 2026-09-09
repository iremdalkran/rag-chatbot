from pypdf import PdfReader

def read_pdf(file_path):
    """PDF dosyasını okuyup tüm metni tek bir string olarak döndürür."""
    reader = PdfReader(file_path)
    full_text = ""
    for page in reader.pages:
        full_text += page.extract_text() + "\n"
    return full_text


def chunk_text(text, chunk_size=500, overlap=50):
    """Metni belirli boyutta, üst üste binen parçalara böler."""
    words = text.split()
    chunks = []
    start = 0

    while start < len(words):
        end = start + chunk_size
        chunk = " ".join(words[start:end])
        chunks.append(chunk)
        start = end - overlap  # overlap kadar geriye kayıyoruz

    return chunks


if __name__ == "__main__":
    text = read_pdf("ornek_dokuman.pdf")
    chunks = chunk_text(text, chunk_size=100, overlap=20)

    print(f"Toplam {len(chunks)} chunk oluşturuldu.\n")

    for i, chunk in enumerate(chunks):
        kelime_sayisi = len(chunk.split())
        print(f"--- Chunk {i+1} ({kelime_sayisi} kelime) ---")
        print(chunk)
        print()