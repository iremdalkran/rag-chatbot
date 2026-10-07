"""Arayüzün internetten hiçbir şey yüklemediğini ve dış servis kütüphanesi kalmadığını doğrular."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED = ("http://www.w3.org/",)  # SVG ad alanı, ağ isteği değildir


def test_frontend_has_no_external_urls():
    for path in (ROOT / "static").glob("*.*"):
        text = path.read_text(encoding="utf-8")
        urls = [u for u in re.findall(r"https?://[^\s\"'<>)]+", text) if not u.startswith(ALLOWED)]
        assert not urls, f"{path.name} dış adres içeriyor: {urls}"


def test_no_cloud_ai_dependencies():
    requirements = (ROOT / "requirements.txt").read_text().lower()
    for banned in ("anthropic", "openai", "voyageai", "chromadb", "psycopg"):
        assert banned not in requirements
    for path in (ROOT / "app").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "import anthropic" not in source and "import voyageai" not in source
