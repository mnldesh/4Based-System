"""
ai_client.py — Ollama / OpenAI-kompatibler Client

Optimierungen:
  - Regex auf Modulebene kompiliert (statt bei jedem Call)
  - Globaler gecachter Client (statt new-Instanz pro Call)
  - Timeout auf alle API-Calls
  - Retry mit exponential Backoff bei transienten Fehlern
"""

import re
import time
import base64
import functools
from pathlib import Path
from typing import Optional

import openai

OLLAMA_BASE   = "http://127.0.0.1:11434/v1"
TEXT_MODEL    = "qwen3:14b"
VISION_MODEL  = "llava:13b"
API_TIMEOUT   = 60       # Sekunden pro API-Call
MAX_RETRIES   = 3
MAX_IMAGE_BYTES = 20 * 1024 * 1024

# ─── Regex — einmal kompiliert ────────────────────────────────────────────────
_RE_THINK    = re.compile(r"<think>.*?</think>", re.DOTALL)
_RE_BOLD     = re.compile(r"\*\*(.+?)\*\*")
_RE_SPEAKER  = re.compile(r"(?m)^[\w][\w ]{0,20}:\s*")
_RE_CODE     = re.compile(r"```(?:json)?")

# ─── Globaler Client-Cache ────────────────────────────────────────────────────
_client: Optional[openai.OpenAI] = None

def make_client() -> openai.OpenAI:
    """Gibt immer denselben Client zurück (lazy init)."""
    global _client
    if _client is None:
        _client = openai.OpenAI(
            base_url=OLLAMA_BASE,
            api_key="ollama",
            timeout=API_TIMEOUT,
        )
    return _client

# ─── Retry-Decorator ──────────────────────────────────────────────────────────

def _with_retry(fn):
    """Decorator: wiederholt bei transienten Fehlern mit exp. Backoff."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        delay = 2.0
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return fn(*args, **kwargs)
            except openai.APIConnectionError as e:
                if attempt == MAX_RETRIES:
                    print(f"[AI] Verbindung fehlgeschlagen nach {MAX_RETRIES} Versuchen: {e}")
                    return ""
                print(f"[AI] Verbindungsfehler (Versuch {attempt}/{MAX_RETRIES}), warte {delay:.0f}s...")
                time.sleep(delay)
                delay *= 2
            except openai.RateLimitError:
                if attempt == MAX_RETRIES:
                    print("[AI] Rate Limit — aufgegeben")
                    return ""
                print(f"[AI] Rate Limit (Versuch {attempt}/{MAX_RETRIES}), warte {delay:.0f}s...")
                time.sleep(delay)
                delay *= 2
            except openai.APIStatusError as e:
                if e.status_code >= 500 and attempt < MAX_RETRIES:
                    print(f"[AI] Server-Fehler {e.status_code} (Versuch {attempt}), warte {delay:.0f}s...")
                    time.sleep(delay)
                    delay *= 2
                else:
                    print(f"[AI] API Fehler {e.status_code}: {e.message}")
                    return ""
            except Exception as e:
                print(f"[AI] Unbekannter Fehler: {e}")
                return ""
        return ""
    return wrapper

# ─── API-Calls ────────────────────────────────────────────────────────────────

@_with_retry
def chat(
    system:     str,
    user:       str,
    client:     Optional[openai.OpenAI] = None,
    max_tokens: int = 500,
    model:      str = TEXT_MODEL,
) -> str:
    """Text-Chat. Gibt leeren String bei dauerhaftem Fehler zurück."""
    if not system or not user:
        return ""
    c    = client or make_client()
    resp = c.chat.completions.create(
        model      = model,
        max_tokens = max_tokens,
        messages   = [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    )
    return resp.choices[0].message.content.strip()


@_with_retry
def vision(
    image_path: Path,
    prompt:     str,
    client:     Optional[openai.OpenAI] = None,
    max_tokens: int = 300,
) -> str:
    """Bild analysieren mit LLaVA. Gibt leeren String bei Fehler zurück."""
    if not image_path.exists():
        print(f"[VISION] Datei nicht gefunden: {image_path}")
        return ""
    if not prompt:
        return ""

    size = image_path.stat().st_size
    if size > MAX_IMAGE_BYTES:
        print(f"[VISION] Datei zu groß ({size // 1024 // 1024}MB): {image_path.name}")
        return ""

    ext  = image_path.suffix.lstrip(".").lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "webp": "image/webp",
            "gif": "image/gif"}.get(ext, "image/jpeg")

    c    = client or make_client()
    data = base64.b64encode(image_path.read_bytes()).decode()
    resp = c.chat.completions.create(
        model      = VISION_MODEL,
        max_tokens = max_tokens,
        messages   = [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}},
                {"type": "text",      "text": prompt},
            ],
        }],
    )
    return resp.choices[0].message.content.strip()


def parse_json_from_response(raw: str) -> Optional[dict]:
    """Extrahiert JSON sicher via JSONDecoder.raw_decode()."""
    import json
    if not raw:
        return None
    decoder = json.JSONDecoder()
    text    = raw.strip()

    start = text.find("{")
    if start != -1:
        try:
            obj, _ = decoder.raw_decode(text, start)
            return obj
        except json.JSONDecodeError:
            pass

    # Fallback: Markdown-Code-Block entfernen
    cleaned = _RE_CODE.sub("", text).strip()
    start   = cleaned.find("{")
    if start != -1:
        try:
            obj, _ = decoder.raw_decode(cleaned, start)
            return obj
        except json.JSONDecodeError:
            pass

    return None


def clean_reply(text: str) -> str:
    """Thinking-Tags, Markdown-Bold und Speaker-Prefix entfernen."""
    text = _RE_THINK.sub("", text)
    text = _RE_BOLD.sub(r"\1", text)
    text = _RE_SPEAKER.sub("", text)
    text = text.strip()
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    return lines[0] if lines else text
