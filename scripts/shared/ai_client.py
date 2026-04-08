"""
ai_client.py — Multi-Modell-Client mit zentralem Routing

Öffentliche API:
  chat(system, user, purpose="chat", max_tokens=500)
    purpose="chat" → Claude API + qwen2.5 Fallback   (Nachrichten/Antworten)
    purpose="plan" → deepseek-r1 via Ollama           (Planung/Strategie)

  vision(image_path, prompt, ...)  → llava:7b via Ollama (Bildanalyse)

Aufrufer müssen keine Modellnamen oder Client-Typen kennen.
"""

import os
import re
import subprocess
import time
import base64
import functools
from pathlib import Path
from typing import Optional
from urllib.request import urlopen

import openai

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except ImportError:
    pass  # python-dotenv nicht installiert — ANTHROPIC_API_KEY muss als Umgebungsvariable gesetzt sein

from shared.config import ROOT

# ─── Modell-Konstanten ────────────────────────────────────────────────────────
CLAUDE_MODEL    = "claude-sonnet-4-5-20251001"        # Primär: Chat/Nachrichten
PLANNING_MODEL  = "deepseek-r1:7b-qwen-distill-q4_K_M"  # Lokal: Planung/Strategie
TEXT_MODEL      = "qwen2.5:latest"                    # Ollama-Fallback: Chat
VISION_MODEL    = "llava:7b"                          # Lokal: Bildanalyse

OLLAMA_BASE     = "http://127.0.0.1:11434/v1"
OLLAMA_HEALTH   = "http://127.0.0.1:11434/api/tags"
API_TIMEOUT     = 180
MAX_RETRIES     = 3
MAX_IMAGE_BYTES = 20 * 1024 * 1024


# ─── Ollama starten ───────────────────────────────────────────────────────────

def ensure_ollama(wait: int = 30) -> None:
    """Prüft ob Ollama läuft. Startet es automatisch wenn nicht."""
    def _is_up() -> bool:
        try:
            urlopen(OLLAMA_HEALTH, timeout=3)
            return True
        except Exception:
            return False

    if _is_up():
        print("[OLLAMA] Läuft bereits ✓")
        return

    print("[OLLAMA] Nicht erreichbar — starte ollama serve...")
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise SystemExit("[OLLAMA] 'ollama' nicht gefunden. Bitte installieren: curl -fsSL https://ollama.ai/install.sh | sh")

    for i in range(wait):
        time.sleep(1)
        if _is_up():
            print(f"[OLLAMA] Gestartet nach {i+1}s ✓")
            return
        if i % 5 == 4:
            print(f"[OLLAMA] Warte... ({i+1}/{wait}s)")

    raise SystemExit(f"[OLLAMA] Konnte nach {wait}s nicht gestartet werden. Manuell prüfen: ollama serve")


# ─── Regex — einmal kompiliert ────────────────────────────────────────────────
_RE_THINK     = re.compile(r"<think>.*?</think>", re.DOTALL)
_RE_BOLD      = re.compile(r"\*\*(.+?)\*\*")
_RE_SPEAKER   = re.compile(r"(?m)^[\w][\w ]{0,20}:\s*")
_RE_CODE      = re.compile(r"```(?:json)?")
_RE_NON_LATIN = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\u3400-\u4dbf\u2e80-\u2eff\u1100-\u11ff\uac00-\ud7af]")
_RE_GARBLED   = re.compile(r"[a-zäöüß]{15,}", re.IGNORECASE)


# ─── Ollama Client (für Vision + Planung + Fallback) ─────────────────────────
_ollama_client: Optional[openai.OpenAI] = None

def make_client() -> openai.OpenAI:
    """Gibt immer denselben Ollama-Client zurück (lazy init)."""
    global _ollama_client
    if _ollama_client is None:
        _ollama_client = openai.OpenAI(
            base_url=OLLAMA_BASE,
            api_key="ollama",
            timeout=API_TIMEOUT,
        )
    return _ollama_client


# ─── Claude Client (für Chat/Nachrichten) ─────────────────────────────────────
_claude_client = None

def make_claude_client():
    """Gibt Anthropic-Client zurück. None wenn API Key fehlt oder Paket nicht installiert."""
    global _claude_client
    if _claude_client is None:
        try:
            import anthropic
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                print("[CLAUDE] ANTHROPIC_API_KEY nicht gesetzt — Fallback auf Ollama")
                return None
            _claude_client = anthropic.Anthropic(api_key=api_key, timeout=60.0)
        except ImportError:
            print("[CLAUDE] 'anthropic' Paket nicht installiert — Fallback auf Ollama")
            return None
    return _claude_client


# ─── Retry-Decorator (für Ollama) ─────────────────────────────────────────────

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


# ─── Öffentlicher Einstiegspunkt: Routing nach purpose ───────────────────────

def chat(
    system:     str,
    user:       str,
    purpose:    str = "chat",
    max_tokens: int = 500,
) -> str:
    """
    Zentraler Chat-Einstiegspunkt mit automatischem Modell-Routing.

    purpose="chat" → Claude API primär, qwen2.5:latest als Notfall-Fallback
    purpose="plan" → deepseek-r1:7b via Ollama (Planung, Strategie, Recherche)
    """
    if purpose == "plan":
        return _chat_ollama(system, user, max_tokens=max_tokens, model=PLANNING_MODEL)
    return _chat_claude(system, user, max_tokens=max_tokens)


# ─── Interne Implementierungen ────────────────────────────────────────────────

def _chat_claude(
    system:     str,
    user:       str,
    max_tokens: int = 500,
) -> str:
    """Claude API mit Ollama-Fallback."""
    if not user:
        return ""

    claude = make_claude_client()
    if claude:
        try:
            kwargs: dict = dict(
                model      = CLAUDE_MODEL,
                max_tokens = max_tokens,
                messages   = [{"role": "user", "content": user}],
            )
            if system:
                kwargs["system"] = system
            msg  = claude.messages.create(**kwargs)
            text = msg.content[0].text.strip() if msg.content else ""
            if text:
                return text
        except Exception as e:
            print(f"[CLAUDE] Fehler — Fallback auf Ollama: {e}")

    print(f"[CLAUDE→OLLAMA] Fallback auf {TEXT_MODEL}")
    return _chat_ollama_raw(system, user, max_tokens=max_tokens, model=TEXT_MODEL)


def _chat_ollama(
    system:     str,
    user:       str,
    max_tokens: int = 500,
    model:      str = PLANNING_MODEL,
) -> str:
    """Ollama-Chat für Planung/Strategie."""
    return _chat_ollama_raw(system, user, max_tokens=max_tokens, model=model)


# ─── Ollama-Raw-Call (mit Retry) ──────────────────────────────────────────────

@_with_retry
def _chat_ollama_raw(
    system:     str,
    user:       str,
    client:     Optional[openai.OpenAI] = None,
    max_tokens: int = 500,
    model:      str = TEXT_MODEL,
) -> str:
    """Rohaufruf gegen Ollama OpenAI-kompatible API. Intern — nicht direkt aufrufen."""
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


# ─── Vision: Ollama llava:7b ──────────────────────────────────────────────────

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


# ─── Hilfsfunktionen ──────────────────────────────────────────────────────────

def parse_json_from_response(raw: str) -> Optional[dict]:
    """Extrahiert JSON sicher via JSONDecoder.raw_decode(), repariert truncated JSON."""
    import json
    if not raw:
        return None
    decoder = json.JSONDecoder()
    text    = raw.strip()

    def _try_parse(s: str, offset: int = 0) -> Optional[dict]:
        try:
            obj, _ = decoder.raw_decode(s, offset)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None

    def _repair_and_parse(s: str) -> Optional[dict]:
        open_b = s.count('{') - s.count('}')
        if open_b > 0:
            repaired = s + '}' * open_b
            return _try_parse(repaired)
        return None

    start = text.find("{")
    if start != -1:
        result = _try_parse(text, start)
        if result is not None:
            return result
        result = _repair_and_parse(text[start:])
        if result is not None:
            return result

    cleaned = _RE_CODE.sub("", text).strip()
    start   = cleaned.find("{")
    if start != -1:
        result = _try_parse(cleaned, start)
        if result is not None:
            return result
        result = _repair_and_parse(cleaned[start:])
        if result is not None:
            return result

    return None


def clean_reply(text: str) -> str:
    """Thinking-Tags, Markdown-Bold, Speaker-Prefix, Fremdzeichen entfernen."""
    text = _RE_THINK.sub("", text)
    text = _RE_BOLD.sub(r"\1", text)
    text = _RE_SPEAKER.sub("", text)
    text = text.strip()
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    text = lines[0] if lines else text
    if _RE_NON_LATIN.search(text):
        idx  = _RE_NON_LATIN.search(text).start()
        text = text[:idx].rstrip()
    if _RE_GARBLED.search(text):
        text = _RE_GARBLED.sub("", text).strip()
        text = re.sub(r"  +", " ", text)
    return text


def is_reply_usable(text: str) -> bool:
    """Prüft ob eine bereinigte Antwort brauchbar ist."""
    if not text or len(text) < 10:
        return False
    alpha = sum(1 for c in text if c.isalpha())
    return alpha >= 5


# ─── Lokaler Test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    ensure_ollama()

    print("\n=== Chat-Route (Claude primär) ===")
    r = chat("Du bist ein Assistent.", "Sag 'Hallo' auf Deutsch.", purpose="chat", max_tokens=20)
    print(f"  → {r or '[LEER]'}")

    print("\n=== Plan-Route (deepseek-r1) ===")
    r = chat("Du bist ein Texter.", "Schreib eine kurze Caption.", purpose="plan", max_tokens=50)
    print(f"  → {r or '[LEER]'}")

    print("\n✓ Fertig")
