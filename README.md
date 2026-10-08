# Doküman Asistanı (tamamen yerel)

Şirket dokümanlarına (PDF, Word, TXT) ve veri tablolarına (Excel, CSV) soru sorabileceğiniz bir asistan.
**Hiçbir veri dışarı gönderilmez.** Yapay zekâ modelleri [Ollama](https://ollama.com) ile bu bilgisayarda
veya şirketin kendi sunucusunda çalışır. İnternet sadece ilk kurulumda modelleri indirmek için gerekir.

## Neler yapabilir?

- **Doküman soru-cevap:** Yüklenen dokümanlarda arar ve cevabın her cümlesinin hangi dosyanın hangi
  sayfasından geldiğini gösterir. Kaynağa tıklayınca ilgili metin açılır.
- **Veri analizi:** Excel/CSV yükleyin, "Bölgelere göre toplam satış nedir?" diye sorun. Tablo, grafik ve
  kullanılan SQL sorgusu birlikte gösterilir.
- **Sohbet geçmişi:** Sohbetler kaydedilir. "Peki ya ikincisi?" gibi takip soruları anlaşılır.
- **Kullanıcılar:** İlk giren kişi yönetici olur. Yönetici kullanıcı ekler, siler ve şifre sıfırlar.
- **Şirket dokümanları:** Yöneticinin "Şirket dokümanı" olarak eklediği dosyaları herkes görür.
  Kişisel dokümanları ve tabloları yalnızca sahibi görür.
- Koyu ve açık tema, mobil uyumlu arayüz, cevabı yarıda durdurma.

## Mac'te kurulum (ilk sefer)

1. **Ollama'yı kurun:** https://ollama.com/download → Mac için indirip uygulamayı açın.
2. **Python'u kurun** (yoksa): https://www.python.org/downloads/ → en son sürüm.
3. Bu klasörde bir Terminal açın ve şunu çalıştırın:

   ```bash
   ./baslat.sh
   ```

   İlk çalıştırmada modeller indirilir (yaklaşık 10 GB, internet hızına göre 5-20 dakika).
   Bitince tarayıcı kendiliğinden `http://127.0.0.1:8000` adresini açar (açılmazsa bu adresi Safari'ye kendiniz yazın).
4. Açılan sayfada **yönetici hesabınızı** oluşturun.

Sonraki seferlerde de sadece `./baslat.sh` yeterlidir; birkaç saniyede açılır. Durdurmak için
Terminal'de `Ctrl + C`'ye basın.

## Hangi modeller kullanılıyor?

| Görev | Model | Boyut | Not |
|---|---|---|---|
| Sohbet / cevap yazma | `qwen3:14b` | ~9 GB | Türkçesi iyi, 24 GB bellekli Mac için dengeli seçim |
| Doküman arama | `bge-m3` | ~1,2 GB | Türkçe dahil çok dilli |

Modeli değiştirmek için `.env.example` dosyasını `.env` adıyla kopyalayıp `CHAT_MODEL` satırını
düzenleyin. Daha hızlı cevap için `qwen3:8b`, daha akıllı cevap için `qwen3:30b-a3b` kullanılabilir
(ikincisi 24 GB belleği zorlar, başka uygulamalar kapalıyken deneyin).

## Şirket ağında kullanım

- **Aynı ofisteki diğer bilgisayarlar bağlansın:** `.env` dosyasına `HOST=0.0.0.0` yazıp yeniden başlatın.
  Terminal'de diğer bilgisayarların kullanacağı adres gösterilir.
- **Şirket sunucusuna kurulum (Docker):**

  ```bash
  docker compose up -d
  docker compose exec ollama ollama pull qwen3:14b
  docker compose exec ollama ollama pull bge-m3
  ```

  Sonra `http://SUNUCU_ADRESI:8000` adresini açın. Ekran kartlı sunucular için `docker-compose.yml`
  içindeki açıklamaya bakın.
- İnternete açık bir adreste yayınlanacaksa önüne HTTPS koyun (ör. Caddy veya nginx) ve `.env` içinde
  `COOKIE_SECURE=true` yapın.

## Cevap kalitesini ölçmek

Sistemin gerçek modelle ne kadar doğru cevap verdiğini ölçmek için bir değerlendirme aracı var.
Bu araç otomatik testlerden ayrıdır: testler kodun çalıştığını kontrol eder, bu araç ise cevapların
**kalitesini** ölçer.

1. `degerlendirme/sablon.xlsx` dosyasını kopyalayın ve sorularınızı yazın. Her satıra şunları girin:
   soru, doğru cevap, kaynak dosya, kaynak sayfa ve soru türü (doküman / Excel / dokümanda olmayan).
2. Soru dosyasını, cevapların geçtiği dokümanlar ve Excel dosyalarıyla **aynı klasöre** koyun.
3. Çalıştırın:

   ```bash
   bash degerlendir.sh klasorum/sorular.xlsx
   ```

Hazır örnekle hemen denemek için: `bash degerlendir.sh ornekler/degerlendirme/sorular.xlsx`

Her soru sisteme sorulur. Sonuçlar `degerlendirme/raporlar/` klasörüne bir Excel raporu olarak yazılır.
Raporda her soru için şunlar bulunur:
- Cevabın doğru olup olmadığı.
- Doğru kaynağı bulup bulmadığı.
- Kaç saniye sürdüğü.

Raporun "Ayarlar" sayfasında, ölçümün hangi model ve ayarlarla yapıldığı yazar. Bir ayarı değiştirmeden
önce ve sonra aynı soru dosyasıyla çalıştırıp raporları karşılaştırabilirsiniz.

**Hangi ayar daha iyi?** Parça boyutu, bulunan parça sayısı ve cevap modeli için farklı değerleri
otomatik deneyip tek bir tabloda karşılaştırmak için:

```bash
bash ayar_karsilastir.sh ornekler/degerlendirme/sorular.xlsx
```

Bu işlem 30-60 dakika sürer. Sonunda önerilen ayarları `.env` dosyasına yazılacak şekilde gösterir.

## İki arama yöntemi: Vektör ve PageIndex

Dokümanlarda iki farklı yolla arama yapılabilir. Seçim `.env` dosyasındaki tek bir ayarla yapılır:

| Ayar | Nasıl çalışır? |
|---|---|
| `RAG_METHOD=vector` (varsayılan) | Doküman küçük parçalara bölünür; soruya anlamca ve kelimece en yakın parçalar bulunur. |
| `RAG_METHOD=pageindex` | Her doküman için bir **içindekiler ağacı** (bölüm başlıkları, sayfaları, kısa özetleri) çıkarılır. Model, soruyu bu ağaca bakarak hangi bölümleri okuyacağına karar verir ve o sayfaları okur. |

Ayarı değiştirip uygulamayı yeniden başlatmak yeterli. Ağacı olmayan dokümanlar için ağaç arka planda
kendiliğinden kurulur (sol menüde "İçindekiler hazırlanıyor" yazar). Cevabın üstündeki etiket hangi
yöntemin kullanıldığını gösterir ("Dokümanlar · PageIndex"). İki yöntem de yalnızca Ollama'daki
yerel modeli kullanır.

İki yöntemi aynı soru setiyle karşılaştırmak için:

```bash
bash ayar_karsilastir.sh ornekler/degerlendirme/sorular.xlsx --parca 1200 --topk 6 --modeller qwen3:14b --yontemler vector,pageindex
```

**Uzun doküman testi (İş Kanunu):** İki yöntemi uzun bir mevzuat metniyle karşılaştırmak için:

```bash
bash is_kanunu_testi.sh
```

Bu komut 4857 sayılı İş Kanunu'nun resmi PDF'ini mevzuat.gov.tr'den indirir (herkese açık bir dosya;
sizden hiçbir veri gönderilmez). Ardından 15 soruluk test dosyasını hazırlar ve iki yöntemi karşılaştırır.
Sayfa numaraları kanun metninden otomatik bulunur. Sorular ve doğru cevaplar
`degerlendirme/is_kanunu.py` dosyasındadır. Sadece soru dosyasını hazırlamak için:
`bash is_kanunu_testi.sh --hazirla`.

Ayrıntılar: [ARCHITECTURE.md](ARCHITECTURE.md#10-cevap-kalitesi-değerlendirmesi)

## Yedekleme

Bütün veriler (kullanıcılar, sohbetler, dokümanlar, tablolar) `data/` klasöründedir. Yedek almak için
uygulamayı durdurup bu klasörü kopyalamanız yeterlidir.

## Güvenlik özeti

- Şifreler bcrypt ile saklanır. Oturumlar 7 gün sonra düşer. 5 hatalı denemeden sonra giriş 15 dakika kilitlenir.
- Oturum bilgisi JavaScript'in okuyamadığı bir çerezde tutulur. Sayfa, dışarıdan kod çalıştırılmasını
  engelleyen başlıklarla sunulur.
- Her kullanıcının Excel/CSV verisi ayrı bir dosyada durur. Yapay zekânın yazdığı SQL sorguları salt-okunur
  çalışır ve veri silemez, değiştiremez.
- Kendi kendine kayıt varsayılan olarak kapalıdır. Kullanıcıları yönetici ekler.

## Sorun giderme

| Belirti | Çözüm |
|---|---|
| Sarı uyarı: "Ollama çalışmıyor" | Ollama uygulamasını açın. |
| Sarı uyarı: "Eksik model" | Uyarıda yazan `ollama pull ...` komutunu Terminal'de çalıştırın. |
| PDF "Hata" durumunda, "taranmış" diyor | Belge resim olarak kaydedilmiş. Metin seçilebilen bir PDF yükleyin. |
| Cevaplar yavaş | Diğer ağır uygulamaları kapatın veya `.env` içinde `CHAT_MODEL=qwen3:8b` yapın. |
| Şifremi unuttum | Yöneticiden "Şifre sıfırla" yapmasını isteyin. |

## Geliştiriciler için

Sistemin nasıl çalıştığı, veri akışı ve tasarım kararları için: [ARCHITECTURE.md](ARCHITECTURE.md)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest          # testler (gerçek Ollama gerekmez)
.venv/bin/uvicorn app.main:app --reload
```

Klasör yapısı:

```
app/
  main.py       web sunucusu ve API
  config.py     ayarlar (.env)
  db.py         SQLite veritabanı şeması
  auth.py       kullanıcılar, oturumlar
  llm.py        Ollama bağlantısı (dışarıyla tek bağlantı noktası)
  ingest.py     PDF/Word/TXT okuma ve parçalama
  documents.py  doküman kütüphanesi ve hibrit arama
  rag.py        kaynak gösteren cevap üretimi
  tabular.py    Excel/CSV → güvenli SQL analizi
  chats.py      sohbet geçmişi
degerlendirme/  cevap kalitesi ölçümü (bkz. degerlendir.sh)
static/         arayüz (internetten hiçbir şey yüklemez)
tests/          otomatik testler
```
