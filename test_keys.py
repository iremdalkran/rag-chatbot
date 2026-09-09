import os
from dotenv import load_dotenv
import voyageai
import anthropic

load_dotenv()  # .env dosyasındaki değişkenleri yükler

voyage_client = voyageai.Client(api_key=os.getenv("VOYAGE_API_KEY"))
anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Voyage'ı test et
result = voyage_client.embed(["merhaba dünya"], model="voyage-3.5")
print("Voyage çalışıyor. Vektör boyutu:", len(result.embeddings[0]))

# Claude'u test et
message = anthropic_client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=50,
    messages=[{"role": "user", "content": "Sadece 'merhaba' de"}]
)
print("Claude çalışıyor. Cevap:", message.content[0].text)