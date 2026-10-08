"""
llm.py
------
Ollama ile konuşan tek modül. Uygulamanın dışarıyla kurduğu TEK bağlantı buradadır ve
o da OLLAMA_URL'deki (varsayılan: bu bilgisayar) Ollama'ya gider.

- embed(): metinleri vektöre (sayı dizisine) çevirir — anlamca benzer metinleri bulmak için.
- chat(): modele mesaj gönderip cevabın tamamını alır.
- chat_stream(): cevabı kelime kelime (akarak) alır — arayüzde canlı yazılıyormuş gibi görünür.
"""

import json
import re
from typing import Iterator, List, Optional

import httpx
import numpy as np

from app import config

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_think_supported = True  # model "think" parametresini reddederse bir daha göndermeyiz


class LLMError(Exception):
    """Kullanıcıya gösterilebilecek, anlaşılır mesajlı yapay zekâ hatası."""


def _client(timeout: Optional[float] = None) -> httpx.Client:
    return httpx.Client(base_url=config.OLLAMA_URL,
                        timeout=httpx.Timeout(timeout or config.LLM_TIMEOUT_SECONDS, connect=5))


def _raise_for(response: httpx.Response, model: str) -> None:
    if response.status_code < 400:
        return
    try:
        detail = response.json().get("error", response.text)
    except ValueError:
        detail = response.text
    if response.status_code == 404 or "not found" in str(detail).lower():
        raise LLMError(
            f"'{model}' modeli bu bilgisayarda yüklü değil. Terminalde şunu çalıştırın: ollama pull {model}"
        )
    raise LLMError(f"Yapay zekâ motoru bir hata döndürdü: {detail}")


def _connection_error() -> LLMError:
    return LLMError(
        "Yapay zekâ motoruna (Ollama) ulaşılamıyor. Ollama uygulamasının açık olduğundan emin olun."
    )


def embed(texts: List[str], batch_size: int = 16) -> np.ndarray:
    """Metinleri birim uzunluklu vektörlere çevirir (satır başına bir metin)."""
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    vectors = []
    try:
        with _client() as client:
            for start in range(0, len(texts), batch_size):
                batch = texts[start:start + batch_size]
                response = client.post("/api/embed", json={"model": config.EMBED_MODEL, "input": batch})
                _raise_for(response, config.EMBED_MODEL)
                vectors.extend(response.json()["embeddings"])
    except httpx.TransportError as e:
        raise _connection_error() from e
    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def _payload(messages: list, stream: bool, json_mode: bool, temperature: float, model: Optional[str] = None,
             think: Optional[bool] = None) -> dict:
    payload = {
        "model": model or config.CHAT_MODEL,
        "messages": messages,
        "stream": stream,
        "options": {"num_ctx": config.LLM_CONTEXT_TOKENS, "temperature": temperature},
    }
    if json_mode:
        payload["format"] = "json"
    # think=None → genel ayar (LLM_DISABLE_THINKING); True/False → bu çağrı için zorla.
    wanted = (not config.LLM_DISABLE_THINKING) if think is None else think
    if _think_supported and (not wanted or think is True):
        payload["think"] = wanted
    return payload


def _is_think_rejection(response: httpx.Response) -> bool:
    return response.status_code == 400 and "think" in response.text.lower()


def chat(messages: list, json_mode: bool = False, temperature: float = 0.2, model: Optional[str] = None,
         think: Optional[bool] = None) -> str:
    """Cevabın tamamını tek seferde döner. `model` verilmezse ayarlardaki sohbet modeli kullanılır.
    `think=False`: yönlendirme gibi basit adımlarda modelin "düşünme" aşaması, genel ayar ne olursa olsun
    kapalı tutulur (cevap birkaç kat hızlı gelir, bu adımlarda doğruluğa katkısı yoktur)."""
    global _think_supported
    try:
        with _client() as client:
            response = client.post("/api/chat", json=_payload(messages, False, json_mode, temperature, model, think))
            if _is_think_rejection(response):
                _think_supported = False
                response = client.post("/api/chat", json=_payload(messages, False, json_mode, temperature, model, think))
            _raise_for(response, model or config.CHAT_MODEL)
            content = response.json()["message"]["content"]
    except httpx.TransportError as e:
        raise _connection_error() from e
    return _THINK_RE.sub("", content).strip()


def chat_stream(messages: list, temperature: float = 0.2) -> Iterator[str]:
    """Cevabı parça parça üretir. Modelin <think>...</think> bölümü kullanıcıya gösterilmez."""
    global _think_supported
    try:
        with _client() as client:
            for attempt in range(2):
                with client.stream("POST", "/api/chat", json=_payload(messages, True, False, temperature)) as response:
                    if response.status_code >= 400:
                        response.read()
                        if attempt == 0 and _is_think_rejection(response):
                            _think_supported = False
                            continue
                        _raise_for(response, config.CHAT_MODEL)
                    yield from _strip_thinking(_iter_content(response))
                    return
    except httpx.TransportError as e:
        raise _connection_error() from e


def _iter_content(response: httpx.Response) -> Iterator[str]:
    for line in response.iter_lines():
        if not line.strip():
            continue
        data = json.loads(line)
        if data.get("error"):
            raise LLMError(f"Yapay zekâ motoru bir hata döndürdü: {data['error']}")
        piece = (data.get("message") or {}).get("content", "")
        if piece:
            yield piece
        if data.get("done"):
            break


def _strip_thinking(pieces: Iterator[str]) -> Iterator[str]:
    """Akan metinden <think>...</think> bloğunu ayıklar (parçalar etiketi bölse bile)."""
    buffer = ""
    inside = False
    started = False
    for piece in pieces:
        buffer += piece
        while True:
            if inside:
                end = buffer.find("</think>")
                if end == -1:
                    buffer = buffer[-len("</think>"):]
                    break
                buffer = buffer[end + len("</think>"):].lstrip()
                inside = False
                continue
            start = buffer.find("<think>")
            if start != -1:
                if buffer[:start]:
                    started = True
                    yield buffer[:start]
                buffer = buffer[start + len("<think>"):]
                inside = True
                continue
            # Etiketin başı parçanın sonunda yarım kalmış olabilir; o kısmı bekletiyoruz.
            keep = 0
            for size in range(1, len("<think>")):
                if buffer.endswith("<think>"[:size]):
                    keep = size
            out = buffer[:len(buffer) - keep]
            if not started:
                out = out.lstrip()
            if out:
                started = True
                yield out
            buffer = buffer[len(buffer) - keep:]
            break
    if not inside and buffer:
        yield buffer


def health() -> dict:
    """Ollama çalışıyor mu, gerekli modeller yüklü mü?"""
    result = {"ollama": False, "chat_model": config.CHAT_MODEL, "embed_model": config.EMBED_MODEL,
              "chat_model_ready": False, "embed_model_ready": False}
    try:
        with _client(timeout=3) as client:
            response = client.get("/api/tags")
            response.raise_for_status()
            names = {m.get("name", "") for m in response.json().get("models", [])}
    except (httpx.HTTPError, ValueError):
        return result
    result["ollama"] = True
    result["chat_model_ready"] = _has_model(names, config.CHAT_MODEL)
    result["embed_model_ready"] = _has_model(names, config.EMBED_MODEL)
    return result


def pull(model: str, progress=None) -> None:
    """Modeli Ollama'ya indirir (yalnızca bir kez gerekir). progress(yüzde) ilerlemeyi bildirir."""
    try:
        with _client() as client:
            with client.stream("POST", "/api/pull", json={"model": model, "stream": True}) as response:
                if response.status_code >= 400:
                    response.read()
                    raise LLMError(f"'{model}' indirilemedi: {response.text[:200]}")
                for line in response.iter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if data.get("error"):
                        raise LLMError(f"'{model}' indirilemedi: {data['error']}")
                    if progress and data.get("total"):
                        progress(int(100 * data.get("completed", 0) / data["total"]))
    except httpx.TransportError as e:
        raise _connection_error() from e


def unload(model: str) -> None:
    """Modeli Ollama'nın belleğinden hemen boşaltır (yer açmak için). Hata olursa sessizce geçer."""
    try:
        with _client(timeout=60) as client:
            client.post("/api/generate", json={"model": model, "keep_alive": 0})
    except httpx.HTTPError:
        pass


def server_info() -> dict:
    """Değerlendirme raporu için: Ollama sürümü ve yüklü modellerin ayrıntıları (boyut, nicemleme)."""
    info = {"version": None, "models": {}}
    try:
        with _client(timeout=5) as client:
            info["version"] = client.get("/api/version").json().get("version")
            for model in client.get("/api/tags").json().get("models", []):
                details = model.get("details") or {}
                info["models"][model.get("name", "")] = {
                    "parameter_size": details.get("parameter_size"),
                    "quantization": details.get("quantization_level"),
                    "digest": (model.get("digest") or "")[:12],
                }
    except (httpx.HTTPError, ValueError):
        pass
    return info


def _has_model(names: set, wanted: str) -> bool:
    if ":" not in wanted:
        wanted_full: Optional[str] = wanted + ":latest"
    else:
        wanted_full = None
    return wanted in names or (wanted_full in names if wanted_full else False)
