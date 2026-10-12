"""
pageindex_resmi.py
------------------
Deney: resmî PageIndex SDK'sı (pip install pageindex) ile İş Kanunu testi — tamamen yerel.

Bizim uygulamamızdaki PageIndex (app/pageindex.py) yöntemin kendi yazdığımız bir uyarlamasıdır.
Bu betik ise VectifyAI'ın resmî kütüphanesini kullanır:
  - Ağaç "PageIndex Flash" ile çıkarılır (yapı PDF'in düzeninden, modelsiz), düğüm özetlerini yerel
    model yazar.
  - Arama, kütüphanenin kendi ajanıyla yapılır: model araç çağırarak ağaca bakar ve sayfaları okur.
  - Bütün model çağrıları Ollama'nın OpenAI uyumlu arayüzüne (localhost:11434/v1) gider. Kütüphane
    yerel modda çalışmaya sabitlenir (PageIndexLocalClient); bulut API anahtarı verilmez.

Aynı 15 soru, aynı hakem ve aynı puanlama kuralları kullanılır; rapor önceki ölçümlerle aynı biçimdedir.
Uygulamanın kendisine dokunmaz; ayrı bir Python ortamında çalışır (bkz. pageindex_resmi_testi.sh).
"""

import argparse
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
DEFAULT_MODEL = "qwen3:30b-a3b"
CONTEXT_TOKENS = 16384  # Ollama'nın OpenAI uyumlu arayüzü num_ctx almaz; bu yüzden türetilmiş model açılır

_CITE_RE = re.compile(r"<cite\b[^>]*?page=\"?(\d+)[^>]*/?>", re.IGNORECASE)
_ANY_CITE_RE = re.compile(r"<cite\b[^>]*/?>|<doc=[^>]*>", re.IGNORECASE)
_PAGES_RE = re.compile(r"(\d+)\s*(?:-\s*(\d+))?")


def _local_only_environment() -> None:
    """Kütüphanenin model çağrılarını Ollama'ya yönlendirir; dış servislere giden yolları kapatır."""
    os.environ["OPENAI_BASE_URL"] = f"{OLLAMA}/v1"
    os.environ["OPENAI_API_KEY"] = "ollama"            # Ollama anahtar istemez; boş olmasın diye
    os.environ.pop("PAGEINDEX_API_KEY", None)           # bulut anahtarı asla kullanılmasın
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"  # fiyat listesini internetten indirme
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"   # Agents SDK izleme kayıtlarını gönderme
    os.environ["LITELLM_TELEMETRY"] = "False"
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"


def _context_model(base: str, log) -> str:
    """Daha geniş bağlam penceresiyle türetilmiş bir Ollama modeli oluşturur (bir kez; disk yeri harcamaz)."""
    import httpx

    name = f"{base.replace(':', '-')}-pi{CONTEXT_TOKENS // 1024}k"
    with httpx.Client(base_url=OLLAMA, timeout=600) as client:
        names = {m.get("name", "") for m in client.get("/api/tags").json().get("models", [])}
        if base not in names and f"{base}:latest" not in names:
            raise SystemExit(f"✖ '{base}' modeli yüklü değil. Önce: ollama pull {base}")
        if name not in names and f"{name}:latest" not in names:
            log(f"🔧 {name} hazırlanıyor ({base}, bağlam {CONTEXT_TOKENS} token)…")
            r = client.post("/api/create", json={"model": name, "from": base, "stream": False,
                                                 "parameters": {"num_ctx": CONTEXT_TOKENS}})
            r.raise_for_status()
    return name


def _pages_from_call(arguments) -> set:
    """get_page_content çağrısının "pages" değerinden ("12", "12-14", "3,7") sayfa numaraları."""
    if isinstance(arguments, str):
        import json
        try:
            arguments = json.loads(arguments)
        except ValueError:
            arguments = {"pages": arguments}
    pages = set()
    for start, end in _PAGES_RE.findall(str((arguments or {}).get("pages", ""))):
        start, end = int(start), int(end or start)
        pages.update(range(start, min(end, start + 50) + 1))
    return pages


def _ask(client, doc_id: str, question: str) -> dict:
    started = time.perf_counter()
    first, parts, read_pages, tool_calls, requested = None, [], set(), 0, {}
    try:
        stream = client.chat(question, doc_id=doc_id, stream=True, show_process=False, citations=True,
                             instructions="Türkçe cevap ver. Cevap dokümanda yoksa bunu açıkça söyle; "
                                          "tahmin yürütme.")
        for event in stream.events:
            kind = event.get("type")
            if kind == "answer":
                if first is None:
                    first = time.perf_counter() - started
                parts.append(event.get("delta") or "")
            elif kind == "tool_call":
                tool_calls += 1
                if event.get("name") == "get_page_content":
                    requested[event.get("call_id")] = _pages_from_call(event.get("arguments"))
            elif kind == "tool_result" and event.get("call_id") in requested:
                # Sadece gerçekten okunabilen sayfalar sayılır (aralık dışı istek hata döner).
                if '"error"' not in str(event.get("output"))[:200]:
                    read_pages |= requested[event.get("call_id")]
        error = None
    except Exception as e:  # kütüphane/ajan hatası: soruyu hatalı say, teste devam et
        error = f"{type(e).__name__}: {e}"[:300]
    raw = "".join(parts).strip()
    cited = {int(p) for p in _CITE_RE.findall(raw)}
    answer = re.sub(r"\s{2,}", " ", _ANY_CITE_RE.sub("", raw)).strip()
    return {"answer": answer, "read": read_pages, "cited": cited, "tool_calls": tool_calls, "error": error,
            "seconds": round(time.perf_counter() - started, 2),
            "first": round(first, 2) if first is not None else None}


def run(question_file: Path, pdf: Path, output_dir: Path, chat_model: str, index_model: str, judge_model: str,
        limit: int = None, log=print) -> Path:
    _local_only_environment()
    from pageindex import PageIndexLocalClient
    from importlib.metadata import version
    sdk_version = version("pageindex")

    from app import config, llm
    from . import puanlama
    from .rapor import write_report
    from .sorular import TYPE_NONE, load_questions

    questions = load_questions(question_file)[:limit] if limit else load_questions(question_file)
    chat_name = _context_model(chat_model, log)
    index_name = chat_name if index_model == chat_model else _context_model(index_model, log)
    storage = Path(tempfile.mkdtemp(prefix="pageindex-resmi-"))

    def make_client(index_backend):
        return PageIndexLocalClient(
            index={"model": f"openai/{index_name}", "storage_path": str(storage),
                   "summary_concurrency": 2, **({"backend": index_backend} if index_backend else {})},
            chat={"model": f"openai/{chat_name}"})

    started_at = datetime.now()
    log(f"\n📄 {pdf.name} resmî PageIndex (Flash) ile indeksleniyor — özetleri {index_name} yazıyor…")
    t0 = time.perf_counter()
    # Özet yazarken modelin "düşünme" adımını kapatmayı dene (çok daha hızlı); Ollama sürümü bunu
    # desteklemiyorsa düşünme açık olarak tekrar dene.
    try:
        client = make_client({"extra_body": {"reasoning_effort": "none"}})
        doc_id = client.submit_document(str(pdf))["doc_id"]
        thinking_note = "kapalı (reasoning_effort=none)"
    except Exception as e:
        log(f"   Düşünme kapatılamadı ({type(e).__name__}); düşünme açık olarak tekrar deneniyor…")
        client = make_client(None)
        doc_id = client.submit_document(str(pdf))["doc_id"]
        thinking_note = "açık (Ollama kapatmayı desteklemedi)"
    prep_seconds = round(time.perf_counter() - t0, 1)
    tree_nodes = client.get_tree(doc_id, node_summary=True, include_text=False).get("result") or []

    def walk(nodes, depth=0):
        for node in nodes or []:
            yield depth, node
            yield from walk(node.get("nodes"), depth + 1)

    flat = list(walk(tree_nodes))
    log(f"   ✓ {len(flat)} bölüm, {prep_seconds} sn")

    log(f"\n❓ {len(questions)} soru soruluyor (arama modeli: {chat_name})…")
    replies = []
    for n, q in enumerate(questions, start=1):
        reply = _ask(client, doc_id, q.text)
        replies.append(reply)
        status = "HATA" if reply["error"] else f"{len(reply['read'])} sayfa okudu"
        log(f"   [{n}/{len(questions)}] {reply['seconds']:>6.1f} sn  {status:<16} {q.text[:60]}")

    # Arama modeli (türetilmiş, geniş bağlamlı kopya) bellekten çıkarılır: hakem aynı ağırlıkları
    # kullansa bile Ollama onu ayrı bir model olarak yükler; ikisi birden 24 GB'a sığmaz.
    for name in {chat_name, index_name}:
        llm.unload(name)
    log(f"\n⚖️  Cevaplar puanlanıyor (hakem: {judge_model})…")
    results = []
    for q, reply in zip(questions, replies):
        sources = [{"number": i, "document_id": 0, "filename": pdf.name, "page": p, "text": "",
                    "cited": p in reply["cited"]} for i, p in enumerate(sorted(reply["read"] | reply["cited"]), 1)]
        if reply["error"]:
            verdict, reason = puanlama.VERDICT_ERROR, ""
        else:
            verdict, reason = puanlama.judge(q, reply["answer"], None, judge_model)
        retrieved_ok, cited_ok = puanlama.check_document_sources(q, sources)
        pages = ", ".join(map(str, sorted(q.source_pages))) if q.source_pages else ""
        results.append({
            "row": q.row, "kind": q.kind, "question": q.text, "expected": q.expected, "answer": reply["answer"],
            "verdict": verdict, "reason": reason,
            "numbers_ok": "—" if reply["error"] else puanlama.check_numbers(q.expected, reply["answer"], None),
            "expected_route": "Doküman", "actual_route": "Doküman", "route_ok": "—",
            "expected_source": (q.source_file + (f" (s. {pages})" if pages else "")) if q.source_file else "—",
            "retrieved_ok": retrieved_ok, "cited_ok": cited_ok,
            "shown_sources": "; ".join(f"s.{s['page']}" + (" (atıf)" if s["cited"] else "") for s in sources),
            "sql": "", "seconds": reply["seconds"], "first_token_seconds": reply["first"],
            "error": reply["error"] or "",
        })
    llm.unload(judge_model)

    settings = [
        ("Tarih", started_at.strftime("%d.%m.%Y %H:%M")),
        ("Yöntem", f"Resmî PageIndex SDK {sdk_version} — yerel mod (PageIndexLocalClient), Flash indeksleme"),
        ("Model bağlantısı", f"Ollama OpenAI uyumlu arayüz: {OLLAMA}/v1 (bulut anahtarı yok)"),
        ("Arama (chat) modeli", f"{chat_name} ({chat_model}, bağlam {CONTEXT_TOKENS} token)"),
        ("İndeks (özet) modeli", f"{index_name} — düşünme {thinking_note}"),
        ("Hakem modeli", judge_model + (" — arama modeliyle AYNI" if judge_model == chat_model else "")),
        ("Hazırlık süresi (indeksleme)", f"{prep_seconds} sn"),
        ("Bölüm sayısı", len(flat)),
        ("Soru dosyası", str(question_file)),
        ("Ortak ayar (hakem bağlamı)", config.LLM_CONTEXT_TOKENS),
    ]
    trees = {pdf.name: [{"depth": d, "title": node.get("title", ""), "start": node.get("start_index", 0),
                         "end": node.get("end_index", 0), "summary": node.get("summary", "")}
                        for d, node in flat]}
    output_dir.mkdir(parents=True, exist_ok=True)
    report = output_dir / f"rapor_{started_at:%Y-%m-%d_%H%M%S}_pageindex-resmi.xlsx"
    summary = write_report(report, results, settings,
                           [{"name": pdf.name, "type": "doküman", "status": "hazır",
                             "detail": f"{len(flat)} bölüm, {prep_seconds} sn"}], trees=trees)

    doc = summary["by_type"].get("doküman", {})
    none = summary["by_type"].get(TYPE_NONE, {})
    pct = lambda v: "—" if v is None else f"%{v * 100:.0f}"  # noqa: E731
    log("\n📊 Resmî PageIndex (yerel)")
    log(f"   Doğruluk {pct(summary['score'])} · kanunda olan sorular {pct(doc.get('score'))} · "
        f"kanunda olmayan sorular {pct(none.get('score'))}")
    log(f"   Doğru sayfayı okudu {pct(doc.get('retrieved'))} · cevapta gösterdi {pct(doc.get('cited'))}")
    log(f"   Bekleme (ilk kelime) ort. {summary['avg_wait']} sn · toplam ort. {summary['avg_seconds']} sn · "
        f"hazırlık {prep_seconds} sn")
    problems = [r["reason"] for r in results if r["verdict"] == puanlama.VERDICT_UNCLEAR]
    errors = [r["error"] for r in results if r["error"]]
    if problems:
        log(f"⚠️  Hakem {len(problems)} soruyu puanlayamadı. İlk neden: {problems[0]}")
    if errors:
        log(f"⚠️  {len(errors)} soruda kütüphane hata verdi. İlk hata: {errors[0]}")
    log(f"📄 Rapor: {report}")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="pageindex_resmi", description="Resmî PageIndex SDK ile yerel test.")
    parser.add_argument("--sorular", type=Path, default=ROOT / "ornekler" / "is_kanunu" / "sorular.xlsx")
    parser.add_argument("--pdf", type=Path, default=ROOT / "ornekler" / "is_kanunu" / "is_kanunu.pdf")
    parser.add_argument("--chat-model", default=DEFAULT_MODEL)
    parser.add_argument("--index-model", default=DEFAULT_MODEL)
    parser.add_argument("--hakem-model", default=DEFAULT_MODEL)
    parser.add_argument("--sadece", type=int, default=None)
    parser.add_argument("--cikti", type=Path, default=ROOT / "degerlendirme" / "raporlar")
    args = parser.parse_args(argv)
    for path in (args.sorular, args.pdf):
        if not path.exists():
            print(f"✖ Bulunamadı: {path} — önce: bash is_kanunu_testi.sh --hazirla")
            return 2
    os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pageindex-resmi-data-"))
    run(args.sorular, args.pdf, args.cikti, args.chat_model, args.index_model, args.hakem_model, args.sadece)
    return 0


if __name__ == "__main__":
    sys.exit(main())
