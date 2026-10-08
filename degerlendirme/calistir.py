"""
calistir.py
-----------
Değerlendirmeyi uçtan uca çalıştırır:

  1. Geçici, boş bir veri klasörü açar — sizin gerçek verilerinize (data/) DOKUNMAZ.
  2. Uygulamayı bu geçici klasörle arka planda başlatır (gerçek kullanımdaki aynı kod yolu).
  3. Soru dosyasının bulunduğu klasördeki dokümanları ve Excel/CSV dosyalarını yükler.
  4. Her soruyu YENİ bir sohbette, "Otomatik" modda sorar; cevabı, kaynakları ve süreyi kaydeder.
  5. Her cevabı puanlar (bkz. puanlama.py) ve Excel raporu yazar.

Kullanım:
  bash degerlendir.sh SORU_DOSYASI.xlsx [--hakem-model MODEL] [--sadece N]
  (veya: .venv/bin/python -m degerlendirme SORU_DOSYASI.xlsx)
"""

import argparse
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC_EXTENSIONS = (".pdf", ".docx", ".txt", ".md")
DATA_EXTENSIONS = (".xlsx", ".xls", ".csv")


class EvaluationError(Exception):
    """Değerlendirmeyi başlatmayı engelleyen, kullanıcıya gösterilecek hatalar."""


# --- Uygulamayı arka planda çalıştırma ---

class _BackgroundServer:
    """Uygulamayı bu işlem içinde, rastgele boş bir portta başlatır. Gerçek HTTP üzerinden
    konuşuruz ki cevabın akışı ve ilk kelimenin geliş süresi gerçek kullanımdaki gibi ölçülsün."""

    def __init__(self, app):
        import uvicorn

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                                    log_level="warning", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 20
        while not self.server.started:
            if time.time() > deadline or not self.thread.is_alive():
                raise EvaluationError("Uygulama başlatılamadı.")
            time.sleep(0.05)
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


# --- Yardımcılar ---

def _git_version() -> str:
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                                text=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "app", "static"], cwd=ROOT,
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return (commit or "bilinmiyor") + (" (kaydedilmemiş değişiklikler var)" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return "bilinmiyor"


def _memory_gb() -> str:
    try:
        return f"{os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / 1024 ** 3:.0f} GB"
    except (ValueError, OSError, AttributeError):
        return "bilinmiyor"


def _model_line(name: str, info: dict) -> str:
    details = info["models"].get(name) or info["models"].get(f"{name}:latest") or {}
    extra = ", ".join(f"{k}: {v}" for k, v in (("boyut", details.get("parameter_size")),
                                               ("nicemleme", details.get("quantization")),
                                               ("özet", details.get("digest"))) if v)
    return f"{name} ({extra})" if extra else name


def _collect_files(folder: Path, question_file: Path) -> list:
    files = []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.name.startswith(("~$", ".")):
            continue
        if path.resolve() == question_file.resolve() or path.name.lower().startswith("rapor_"):
            continue
        if path.suffix.lower() in DOC_EXTENSIONS + DATA_EXTENSIONS:
            files.append(path)
    return files


def _ask(client, question: str) -> dict:
    """Soruyu sorar, akan cevabı okur; süreleri ve son mesajı döner."""
    start = time.perf_counter()
    first_token = None
    routes, final, error = [], None, None
    with client.stream("POST", "/api/ask", json={"question": question, "mode": "auto"}) as response:
        if response.status_code != 200:
            response.read()
            return {"error": f"HTTP {response.status_code}: {response.text[:200]}", "seconds": 0.0,
                    "first_token_seconds": None, "message": {}, "routes": []}
        for line in response.iter_lines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event["type"] == "token" and first_token is None:
                first_token = time.perf_counter() - start
            elif event["type"] == "route":
                routes.append(event["mode"])
            elif event["type"] == "done":
                final = event["message"]
            elif event["type"] == "error":
                error = event["message"]
    seconds = time.perf_counter() - start
    return {"error": error, "seconds": round(seconds, 2),
            "first_token_seconds": round(first_token, 2) if first_token is not None else None,
            "message": final or {}, "routes": routes}


# --- Asıl iş ---

def run(question_file: Path, files_dir: Path, output_dir: Path, judge_model: str = None,
        limit: int = None, log=print) -> Path:
    """Değerlendirmeyi çalıştırır ve rapor dosyasının yolunu döner.
    Uygulama modülleri (app.*) bu fonksiyon çağrılmadan önce yüklenmiş ve geçici veri klasörüne
    yönlendirilmiş olmalıdır (main() bunu yapar; testler kendi geçici klasörlerini kullanır)."""
    import httpx

    from app import auth, config, llm
    from app.main import app

    # Uygulamanın kendi bilgi kayıtları ("doküman işlendi" vb.) ilerleme çıktısına karışmasın.
    logging.getLogger("app").setLevel(logging.WARNING)

    from . import puanlama
    from .rapor import write_report
    from .sorular import TYPE_DATA, load_questions

    questions = load_questions(question_file)
    if limit:
        questions = questions[:limit]
    judge_model = judge_model or config.CHAT_MODEL

    health = llm.health()
    if not health["ollama"]:
        raise EvaluationError("Ollama'ya ulaşılamıyor. Ollama uygulamasını açıp tekrar deneyin.")
    missing = [m for m, ok in ((config.CHAT_MODEL, health["chat_model_ready"]),
                               (config.EMBED_MODEL, health["embed_model_ready"])) if not ok]
    info = llm.server_info()
    if judge_model != config.CHAT_MODEL and not llm._has_model(set(info["models"]), judge_model):
        missing.append(judge_model)
    if missing:
        raise EvaluationError("Eksik model(ler): " + ", ".join(missing) + ". Yüklemek için: ollama pull " + missing[0])

    files = _collect_files(files_dir, question_file)
    if not files:
        raise EvaluationError(f"'{files_dir}' klasöründe yüklenecek doküman veya Excel/CSV dosyası yok.")
    names = {f.name.lower() for f in files} | {f.stem.lower() for f in files}
    unknown = sorted({q.source_file for q in questions
                      if q.source_file and q.source_file.lower() not in names
                      and Path(q.source_file).stem.lower() not in names})
    if unknown:
        log("⚠️  Soru dosyasında adı geçen ama klasörde bulunmayan dosyalar: " + ", ".join(unknown))

    started_at = datetime.now()
    password = "degerlendirme-" + os.urandom(6).hex()
    auth.create_user("Değerlendirme", "degerlendirme@yerel.test", password, is_admin=True)

    file_rows, tables_by_file = [], {}
    with _BackgroundServer(app) as base_url, httpx.Client(base_url=base_url, timeout=None) as client:
        client.post("/api/auth/login", json={"email": "degerlendirme@yerel.test", "password": password}).raise_for_status()

        log(f"\n📂 {len(files)} dosya yükleniyor…")
        doc_ids = {}
        for path in files:
            with path.open("rb") as fh:
                if path.suffix.lower() in DATA_EXTENSIONS:
                    r = client.post("/api/datasets", files={"file": (path.name, fh)})
                    if r.status_code == 200:
                        tables = [t["table_name"] for t in r.json()]
                        tables_by_file[path.name] = tables
                        file_rows.append({"name": path.name, "type": "Excel/CSV", "status": "hazır",
                                          "detail": "Tablolar: " + ", ".join(f"{t['table_name']} ({t['row_count']} satır)" for t in r.json())})
                    else:
                        file_rows.append({"name": path.name, "type": "Excel/CSV", "status": "hata",
                                          "detail": r.json().get("detail", r.text)})
                else:
                    r = client.post("/api/documents", files={"file": (path.name, fh)})
                    if r.status_code == 200:
                        doc_ids[r.json()["id"]] = path.name
                    else:
                        file_rows.append({"name": path.name, "type": "doküman", "status": "hata",
                                          "detail": r.json().get("detail", r.text)})
        # Dokümanların arka planda işlenmesini bekle.
        while True:
            docs = {d["id"]: d for d in client.get("/api/documents").json()}
            if all(docs[i]["status"] != "processing" for i in doc_ids):
                break
            time.sleep(0.5)
        for doc_id, name in doc_ids.items():
            d = docs[doc_id]
            detail = (f"{d['page_count']} sayfa, {d['chunk_count']} parça" if d["status"] == "ready" else d["error"])
            file_rows.append({"name": name, "type": "doküman", "status": "hazır" if d["status"] == "ready" else "hata",
                              "detail": detail})
        for row in file_rows:
            log(f"   {'✓' if row['status'] == 'hazır' else '✗'} {row['name']}: {row['detail']}")

        # 1. aşama: bütün soruları sor. Puanlama sonraya bırakılır; hakem farklı bir modelse
        # Ollama'nın her soruda iki modeli bellekte değiştirip durması böylece önlenir.
        log(f"\n❓ {len(questions)} soru soruluyor (model: {config.CHAT_MODEL})…")
        replies = []
        for n, q in enumerate(questions, start=1):
            reply = _ask(client, q.text)
            replies.append(reply)
            message = reply["message"]
            mode = message.get("mode") or (reply["routes"][-1] if reply["routes"] else None)
            status = "HATA" if reply["error"] else puanlama.MODE_LABEL.get(mode, "—")
            log(f"   [{n}/{len(questions)}] {reply['seconds']:>6.1f} sn  {status:<8} {q.text[:70]}")

    # 2. aşama: puanla.
    log(f"\n⚖️  Cevaplar puanlanıyor (hakem: {judge_model})…")
    results = []
    for n, (q, reply) in enumerate(zip(questions, replies), start=1):
        message = reply["message"]
        answer = message.get("content", "")
        mode = message.get("mode") or (reply["routes"][-1] if reply["routes"] else None)
        sources = message.get("sources") or []
        if reply["error"]:
            verdict, reason = puanlama.VERDICT_ERROR, ""
        else:
            verdict, reason = puanlama.judge(q, answer, message.get("table"), judge_model)
        retrieved_ok, cited_ok = puanlama.check_document_sources(q, sources)
        if q.kind == TYPE_DATA:
            cited_ok = puanlama.check_table_source(q, message.get("sql"), tables_by_file)
        pages = ", ".join(map(str, sorted(q.source_pages))) if q.source_pages else ""
        results.append({
            "row": q.row, "kind": q.kind, "question": q.text, "expected": q.expected,
            "answer": answer, "verdict": verdict, "reason": reason,
            "numbers_ok": "—" if reply["error"] else puanlama.check_numbers(q.expected, answer, message.get("table")),
            "expected_route": puanlama.MODE_LABEL[puanlama.EXPECTED_MODE.get(q.kind)],
            "actual_route": puanlama.MODE_LABEL.get(mode, "—"),
            "route_ok": puanlama.check_route(q, mode),
            "expected_source": (q.source_file + (f" (s. {pages})" if pages else "")) if q.source_file else "—",
            "retrieved_ok": retrieved_ok, "cited_ok": cited_ok,
            "shown_sources": "; ".join(f"[{s['number']}] {s['filename']}" + (f" s.{s['page']}" if s.get("page") else "")
                                       for s in sources if s.get("cited")),
            "sql": message.get("sql") or "", "seconds": reply["seconds"],
            "first_token_seconds": reply["first_token_seconds"], "error": reply["error"] or "",
        })
        mark = {"Doğru": "✓", "Kısmen": "~", "Yanlış": "✗"}.get(verdict, "!")
        log(f"   [{n}/{len(questions)}] {mark} {verdict:<8} {q.text[:70]}")

    finished_at = datetime.now()
    settings = [
        ("Tarih", started_at.strftime("%d.%m.%Y %H:%M")),
        ("Toplam çalışma süresi", f"{(finished_at - started_at).total_seconds() / 60:.1f} dakika"),
        ("Kod sürümü (git)", _git_version()),
        ("Soru dosyası", str(question_file)),
        ("Dosya klasörü", str(files_dir)),
        ("Soru sayısı", len(questions)),
        ("Soru modu", "Otomatik (sistem doküman/SQL kararını kendisi verdi); her soru yeni bir sohbette"),
        ("Sohbet modeli", _model_line(config.CHAT_MODEL, info)),
        ("Embedding (arama) modeli", _model_line(config.EMBED_MODEL, info)),
        ("Hakem modeli", _model_line(judge_model, info) + (" — sohbet modeliyle AYNI" if judge_model == config.CHAT_MODEL else "")),
        ("Ollama sürümü", info["version"] or "bilinmiyor"),
        ("Ollama adresi", config.OLLAMA_URL),
        ("Bağlam penceresi (LLM_CONTEXT_TOKENS)", config.LLM_CONTEXT_TOKENS),
        ("Düşünme kapalı (LLM_DISABLE_THINKING)", "evet" if config.LLM_DISABLE_THINKING else "hayır"),
        ("Aranan parça sayısı (RETRIEVAL_TOP_K)", config.RETRIEVAL_TOP_K),
        ("Parça boyutu (CHUNK_CHARS)", config.CHUNK_CHARS),
        ("Parça örtüşmesi (CHUNK_OVERLAP_CHARS)", config.CHUNK_OVERLAP_CHARS),
        ("Bilgisayar", f"{platform.system()} {platform.release()} · {platform.machine()} · RAM {_memory_gb()}"),
        ("İşlemci", platform.processor() or platform.machine()),
        ("Python", platform.python_version()),
    ]
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_model = "".join(c if c.isalnum() else "-" for c in config.CHAT_MODEL)
    report = output_dir / f"rapor_{started_at.strftime('%Y-%m-%d_%H%M%S')}_{safe_model}.xlsx"
    summary = write_report(report, results, settings, file_rows)

    log("\n📊 Özet")
    log(f"   Genel doğruluk puanı: %{summary['score'] * 100:.0f}   (toplam {summary['total']} soru)")
    for kind, s in summary["by_type"].items():
        log(f"   • {kind:<18} doğru {s['correct']}, kısmen {s['partial']}, yanlış {s['wrong']}"
            + (f", hata {s['other']}" if s["other"] else "") + f"  → puan %{s['score'] * 100:.0f}")
    def pct(value):
        return "—" if value is None else f"%{value * 100:.0f}"

    for kind, s in summary["by_type"].items():
        checks = [("yol doğru", s["route"]), ("aramada bulundu", s["retrieved"]),
                  ("cevapta gösterildi", s["cited"]), ("sayılar tuttu", s["numbers"])]
        shown = ", ".join(f"{label} {pct(v)}" for label, v in checks if v is not None)
        if shown:
            log(f"     {kind}: {shown}")
    log(f"   Ortalama süre: {summary['avg_seconds']} sn (ortanca {summary['median_seconds']}, "
        f"en uzun {summary['max_seconds']}); ilk kelime ortalaması {summary['avg_first']} sn")
    log(f"\n📄 Rapor: {report}")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="degerlendir", description="Cevap kalitesini ölçer ve Excel raporu yazar.")
    parser.add_argument("sorular", type=Path, help="Soru dosyası (.xlsx), şablon: degerlendirme/sablon.xlsx")
    parser.add_argument("--klasor", type=Path, default=None,
                        help="Yüklenecek dokümanların ve Excel'lerin klasörü (varsayılan: soru dosyasının klasörü)")
    parser.add_argument("--hakem-model", default=None,
                        help="Cevapları puanlayan model (varsayılan: sohbet modeli). Daha tarafsız ölçüm için "
                             "farklı bir model önerilir, ör. qwen3:30b-a3b")
    parser.add_argument("--cikti", type=Path, default=ROOT / "degerlendirme" / "raporlar",
                        help="Raporların yazılacağı klasör")
    parser.add_argument("--sadece", type=int, default=None, help="Sadece ilk N soruyu sor (hızlı deneme için)")
    args = parser.parse_args(argv)

    if not args.sorular.exists():
        print(f"✖ Soru dosyası bulunamadı: {args.sorular}")
        return 2
    files_dir = args.klasor or args.sorular.parent

    # Uygulama modülleri yüklenmeden ÖNCE geçici veri klasörünü ayarla: gerçek data/ klasörüne
    # hiçbir şey yazılmaz ve her değerlendirme temiz bir sistemle başlar.
    temp_dir = tempfile.mkdtemp(prefix="degerlendirme-")
    os.environ["DATA_DIR"] = temp_dir
    os.environ.setdefault("ALLOW_REGISTRATION", "false")

    from .sorular import QuestionFileError

    try:
        run(args.sorular, files_dir, args.cikti, args.hakem_model, args.sadece)
    except (EvaluationError, QuestionFileError) as e:
        print(f"\n✖ {e}")
        return 1
    except KeyboardInterrupt:
        print("\nDurduruldu.")
        return 130
    finally:
        # Yüklenen dokümanların geçici kopyalarını sil.
        shutil.rmtree(temp_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
