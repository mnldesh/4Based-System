"""
base_runner.py — Gemeinsame Playwright-Logik für alle Account-Runner

Loop-Design:
  - Einmal einloggen, Browser bleibt offen
  - Direkt mit Chat 0 beginnen (kein Vorscannen)
  - Snapshot der Top-30-Previews beim Start
  - Alle 5–8 Minuten: Top-30 prüfen, neue Nachrichten sofort beantworten
  - User-Klassifizierung persistent in state/{name}_users.json (einmal pro User)
"""

import asyncio
import json
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import openai
from playwright.async_api import Page, BrowserContext

from shared.personas import PERSONAS
from shared.ai_client import make_client, clean_reply, TEXT_MODEL, ensure_ollama

ROOT            = Path("/home/kali/4based-system")
LOG_PATH        = ROOT / "logs" / "runner.jsonl"
_log_lock       = threading.Lock()
_BLACKLIST_PATH = ROOT / "config" / "blacklist.json"


def load_blacklist() -> set[str]:
    """Liest config/blacklist.json → Set von Usernamen die nie angeschrieben werden."""
    data = load_json(_BLACKLIST_PATH, [])
    return {str(u).lower() for u in data if u}

MAX_TRAILING          = 10
PREMIUM_HISTORY_LIMIT = 100
DEFAULT_HISTORY_LIMIT = 20
SNAPSHOT_SIZE         = 30   # Top-N Chats im periodischen Check
PASS_INTERVAL         = 5400  # 90 min zwischen Pässen
AI_RETRIES            = 3    # Versuche bevor Fallback-Nachricht

# ─── Selectors ────────────────────────────────────────────────────────────────

SEL = {
    "nav":      "text=Nachrichten",
    "items":    "chat-overview ion-item",
    "username": ".username",
    "preview":  "h3.subTitle span",
    "revenue":  "button.revenue",
    "textarea": "ion-footer.chat-write-area text-area",
    "send_btn": [
        "ion-footer.chat-write-area button:has-text('Senden')",
        "ion-footer.chat-write-area ion-button:has-text('Senden')",
    ],
    "bubbles":    "chat-bubble",
    "bubble_msg": "div.message",
    "bubble_txt": "span.label-bubble",
}

SCROLL_DOWN_JS = """
const selectors = ['chat-overview ion-content', 'chat-overview', 'ion-menu ion-content'];
for (const q of selectors) {
    const el = document.querySelector(q);
    if (!el) continue;
    const inner = el.shadowRoot && el.shadowRoot.querySelector('.inner-scroll');
    if (inner) { inner.scrollTop += 350; break; }
    el.scrollTop += 350;
    break;
}
"""

SCROLL_TOP_JS = """
const el = document.querySelector('chat-overview ion-content, chat-overview');
if (el) {
    const inner = el.shadowRoot && el.shadowRoot.querySelector('.inner-scroll');
    if (inner) inner.scrollTop = 0;
    else el.scrollTop = 0;
}
"""

# ─── Data types ───────────────────────────────────────────────────────────────

@dataclass
class Message:
    role: str   # "me" | "user"
    text: str


@dataclass
class ChatItem:
    username: str
    preview:  str
    revenue:  float


@dataclass
class Account:
    name:          str
    storage_state: Path
    config:        dict = field(default_factory=dict)

# ─── Utilities ────────────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] {path.name}: {e}")
        return default


def log_event(obj: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {"at": now_utc().isoformat(), **obj}
    with _log_lock:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def parse_revenue(text: str) -> float:
    m = re.search(r"[\d.]+", text or "")
    return float(m.group()) if m else 0.0


def trailing_own(history: list[Message]) -> int:
    count = 0
    for msg in reversed(history):
        if msg.role == "me":
            count += 1
        else:
            break
    return count


def format_history(history: list[Message]) -> str:
    if not history:
        return "(leer)"
    return "\n".join(
        ("Model" if m.role == "me" else "User") + ": " + m.text
        for m in history
    )


def classify_user(history: list[Message], revenue: float) -> str:
    has_user = any(m.role == "user" for m in history)
    trailing = trailing_own(history)
    if revenue > 20:  return "PREMIUM"
    if revenue > 0:   return "KAEUFER"
    if has_user:      return "AKTIV"
    if trailing >= 5: return "KALT_HART"
    if trailing >= 1: return "KALT"
    return "NEU"


def load_account(name: str, state_file: str) -> Account:
    cfg        = load_json(ROOT / "config" / f"{name}.json", {})
    state_path = ROOT / "state" / state_file
    if not state_path.exists():
        raise SystemExit(
            f"[ERROR] Storage-State fehlt: {state_path}\n"
            f"  → Einmalig einloggen mit: python scripts/{name}_runner.py --save-session"
        )
    return Account(name=name, storage_state=state_path, config=cfg)


async def save_session(name: str, state_file: str) -> None:
    """
    Liest Credentials aus config/{name}.env und loggt automatisch ein.
    Falls keine Credentials vorhanden → manueller Login mit ENTER.
    """
    import os
    from playwright.async_api import async_playwright

    state_path  = ROOT / "state" / state_file
    env_file    = ROOT / "config" / f"{name}.env"
    state_path.parent.mkdir(parents=True, exist_ok=True)

    # Credentials laden
    email = password = ""
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("EMAIL="):
                email = line.split("=", 1)[1].strip()
            elif line.startswith("PASSWORD="):
                password = line.split("=", 1)[1].strip()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx     = await browser.new_context(locale="de-DE")
        page    = await ctx.new_page()
        await page.goto("https://4based.com/login", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        if email and password:
            print(f"\n[{name.upper()}] Auto-Login mit {email}...")
            try:
                await page.fill("input[type='email'], input[name='email']", email, timeout=5000)
                await page.fill("input[type='password'], input[name='password']", password, timeout=5000)
                await page.click("button[type='submit'], ion-button:has-text('Login'), ion-button:has-text('Einloggen')", timeout=5000)
                await page.wait_for_timeout(4000)
                print(f"[{name.upper()}] Login abgeschickt — prüfe ob erfolgreich...")
            except Exception as e:
                print(f"[{name.upper()}] Auto-Login fehlgeschlagen: {e}")
                print("  Bitte manuell einloggen.")
        else:
            print(f"\n[{name.upper()}] Keine Credentials in {env_file}")
            print("  Bitte manuell einloggen.")

        await asyncio.get_event_loop().run_in_executor(
            None, input, "  Nach dem Login ENTER drücken..."
        )

        await ctx.storage_state(path=str(state_path))
        await browser.close()

    print(f"\n[{name.upper()}] Session gespeichert: {state_path}")
    print(f"  Jetzt starten mit: python scripts/{name}_runner.py")

# ─── Persistente User-States ─────────────────────────────────────────────────

def load_user_states(account_name: str) -> dict:
    """Liest state/{name}_users.json → {username: {type, revenue, seen_at}}"""
    return load_json(ROOT / "state" / f"{account_name}_users.json", {})


def save_user_states(account_name: str, states: dict) -> None:
    path = ROOT / "state" / f"{account_name}_users.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(states, ensure_ascii=False, indent=2), encoding="utf-8")


def get_or_classify(
    username: str,
    history:  list[Message],
    revenue:  float,
    states:   dict,
) -> str:
    """
    Gibt gecachten User-Typ zurück.
    Neu-klassifiziert nur wenn: unbekannt ODER Revenue gestiegen (Upgrade).
    """
    cached = states.get(username)
    if cached and revenue <= cached.get("revenue", 0.0):
        return cached["type"]

    user_type = classify_user(history, revenue)
    states[username] = {
        "type":    user_type,
        "revenue": revenue,
        "seen_at": now_utc().isoformat(),
    }
    return user_type

# ─── User-Präferenzen ────────────────────────────────────────────────────────

def extract_user_prefs(
    history: list[Message],
    client:  openai.OpenAI,
    model:   str = TEXT_MODEL,
) -> dict:
    """
    Extrahiert Vorlieben, Interessen und Persönlichkeit des Users aus dem Chatverlauf.
    Gibt ein Dict zurück das in user_states gespeichert wird.
    Nur aufrufen wenn genug User-Nachrichten vorhanden (≥ 2).
    """
    user_msgs = [m.text for m in history if m.role == "user"]
    if len(user_msgs) < 2:
        return {}

    verlauf = format_history(history)
    prompt = (
        f"Analysiere diesen Chatverlauf und extrahiere Infos über den User:\n\n"
        f"{verlauf}\n\n"
        f"Antworte NUR mit validem JSON:\n"
        f'{{\n'
        f'  "beruf": "<Beruf/Job falls erwähnt, sonst null>",\n'
        f'  "interessen": ["<interesse1>", "<interesse2>"],\n'
        f'  "content_wunsch": "<was er konkret sehen/haben möchte, falls erwähnt>",\n'
        f'  "kommunikation": "<wie er schreibt: kurz/lang, direkt/schüchtern, frech/höflich>",\n'
        f'  "persoenlichkeit": "<kurze Beschreibung in 1 Satz>",\n'
        f'  "besonderheiten": "<besondere Details die er erwähnte, zB Hobbys, Wohnort, etc>"\n'
        f'}}'
    )
    try:
        resp = client.chat.completions.create(
            model      = model,
            max_tokens = 200,
            timeout    = 20,
            messages   = [{"role": "user", "content": prompt}],
        )
        from shared.ai_client import parse_json_from_response
        data = parse_json_from_response(resp.choices[0].message.content or "")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _format_prefs(prefs: dict) -> str:
    """Formatiert gespeicherte Präferenzen als lesbaren String für Prompts."""
    if not prefs:
        return ""
    parts = []
    if prefs.get("beruf"):
        parts.append(f"Beruf: {prefs['beruf']}")
    if prefs.get("interessen"):
        parts.append(f"Interessen: {', '.join(prefs['interessen'])}")
    if prefs.get("content_wunsch"):
        parts.append(f"Content-Wunsch: {prefs['content_wunsch']}")
    if prefs.get("kommunikation"):
        parts.append(f"Kommunikationsstil: {prefs['kommunikation']}")
    if prefs.get("persoenlichkeit"):
        parts.append(f"Persönlichkeit: {prefs['persoenlichkeit']}")
    if prefs.get("besonderheiten"):
        parts.append(f"Besonderheiten: {prefs['besonderheiten']}")
    return "\n".join(parts)


# ─── Kaufabsicht-Erkennung ───────────────────────────────────────────────────

_INTENT_KEYWORDS = [
    "was kostet", "wie viel", "wieviel", "preis", "price", "kosten",
    "kaufen", "bestellen", "haben möchte", "haben will", "will ich",
    "möchte ich", "zeig mir", "schick mir", "send me", "how much",
    "was bekomme", "was hast du", "was gibt es", "was bietest",
    "video", "foto", "bild", "clip", "content", "pack", "set",
    "custom", "privat", "exclusive", "exklusiv",
]

def _has_intent_keywords(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in _INTENT_KEYWORDS)


def detect_content_intent(
    history: list[Message],
    client:  openai.OpenAI,
    model:   str = TEXT_MODEL,
) -> bool:
    """Keyword-Schnellcheck, dann AI-Check bei Unklarheit."""
    user_msgs = [m.text for m in history if m.role == "user"]
    if not user_msgs:
        return False
    if _has_intent_keywords(user_msgs[-1]):
        return True
    if len(user_msgs) < 2:
        return False
    recent = "\n".join(f"User: {m}" for m in user_msgs[-3:])
    prompt = (
        f"Nachrichten:\n{recent}\n\n"
        f"Fragt der User nach Content, Fotos, Videos, Preisen oder möchte kaufen? "
        f"Nur 'ja' oder 'nein'."
    )
    try:
        resp = client.chat.completions.create(
            model=model, max_tokens=5, timeout=15,
            messages=[{"role": "user", "content": prompt}],
        )
        return (resp.choices[0].message.content or "").strip().lower().startswith("ja")
    except Exception:
        return False


def get_offer_reply(
    history:      list[Message],
    username:     str,
    account_name: str,
    revenue:      float,
    client:       openai.OpenAI,
    user_states:  dict,
    model:        str = TEXT_MODEL,
) -> str:
    """
    Vollständig KI-generierte Angebots-Nachricht — kein Template.
    Berücksichtigt Chatverlauf, gespeicherte Vorlieben und konkreten Content-Wunsch.
    """
    persona   = PERSONAS[account_name]
    user_msgs = [m.text for m in history if m.role == "user"]
    last_msg  = user_msgs[-1] if user_msgs else ""
    prefs     = user_states.get(username, {}).get("prefs", {})
    prefs_str = _format_prefs(prefs)

    prefs_block = f"\nBEKANNTE INFOS ÜBER DEN USER:\n{prefs_str}\n" if prefs_str else ""

    prompt = (
        f"CHATVERLAUF:\n{format_history(history)}\n"
        f"{prefs_block}\n"
        f"Seine letzte Nachricht: '{last_msg}'\n\n"
        f"Er fragt konkret nach Content, Fotos, Videos oder Preisen.\n\n"
        f"Schreib als {persona['name']} eine Antwort die:\n"
        f"- Direkt auf seine Frage / seinen Wunsch eingeht (was er konkret wollte)\n"
        f"- Seine Vorlieben/Interessen einbaut falls bekannt\n"
        f"- Beschreibt was dich bei ihm anmacht / was du für IHN speziell anbieten würdest\n"
        f"- Den {persona['voucher_pct']}% Gutschein nur dann erwähnt wenn es sich organisch ergibt\n"
        f"- So klingt als wäre es eine echte persönliche Nachricht — nicht wie Werbung\n\n"
        f"STRENG VERBOTEN: vorgefertigte Phrasen, 'schau in mein Profil', generische Angebote, "
        f"Markdown, mehr als 3 Sätze.\n"
        f"Kein Präfix. Nur die Nachricht."
    )

    for attempt in range(1, AI_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model      = model,
                max_tokens = 250,
                timeout    = 45,
                messages   = [
                    {"role": "system", "content": persona["system"]},
                    {"role": "user",   "content": prompt},
                ],
            )
            text = clean_reply(resp.choices[0].message.content or "")
            if text:
                return text
        except Exception as e:
            print(f"  [OFFER] Fehler (Versuch {attempt}): {e}")
        if attempt < AI_RETRIES:
            time.sleep(2 ** attempt)

    # Letzter Ausweg: normaler Reply statt Template
    return get_ai_reply(history, username, account_name, revenue, client, user_states, model)


# ─── AI ───────────────────────────────────────────────────────────────────────

def get_ai_reply(
    history:      list[Message],
    username:     str,
    account_name: str,
    revenue:      float,
    client:       openai.OpenAI,
    user_states:  dict,
    model:        str = TEXT_MODEL,
) -> str:
    """
    Generiert immer eine Antwort. Retry bis AI_RETRIES mal.
    Bezieht gespeicherte User-Präferenzen mit ein.
    """
    persona   = PERSONAS[account_name]
    user_type = get_or_classify(username, history, revenue, user_states)
    trailing  = trailing_own(history)
    prefs     = user_states.get(username, {}).get("prefs", {})
    prefs_str = _format_prefs(prefs)

    user_msgs = [m.text for m in history if m.role == "user"]
    last_own  = [m.text for m in history if m.role == "me"][-3:]

    strategy = {
        "NEU":       "Stelle eine neugierige persönliche Frage basierend auf dem Verlauf. Kein Sales, kein Angebot.",
        "KALT":      "Knüpf an etwas Konkretes aus dem Verlauf an. Sei geheimnisvoll, weck Interesse. Kein Sales.",
        "KALT_HART": "Ignoriere Sales komplett. Stelle eine überraschend persönliche Frage die nichts mit Content zu tun hat.",
        "AKTIV":     "Geh auf seine letzte Nachricht ein, dann mach ein sanftes Angebot. Nur Gutschein wenn es sich natürlich ergibt.",
        "KAEUFER":   "Lies den Verlauf genau: Was hat er gesagt/gekauft? Nenn ein konkretes Detail. Erst Connection, dann sanft neuen Content erwähnen.",
        "PREMIUM":   "Sehr persönlich — bezieh dich auf ein spezifisches Detail. Behandle ihn wie jemanden den du wirklich magst.",
    }.get(user_type, "Antworte passend auf seine letzte Nachricht.")

    last_user_msg = user_msgs[-1] if user_msgs else "(keine)"
    last_own_str  = " | ".join(last_own) if last_own else "keine"
    prefs_block   = f"\nBEKANNTE INFOS ÜBER DEN USER:\n{prefs_str}\n" if prefs_str else ""

    prompt = (
        f"CHATVERLAUF:\n{format_history(history)}\n"
        f"{prefs_block}\n"
        f"KONTEXT: {username} | {user_type} | ${revenue:.0f} Umsatz | "
        f"{trailing}x keine Antwort\n"
        f"Seine letzte Nachricht: {last_user_msg}\n"
        f"Deine letzten Nachrichten (nicht wiederholen!): {last_own_str}\n\n"
        f"STRATEGIE: {strategy}\n\n"
        f"REGELN:\n"
        f"- Baue seine bekannten Vorlieben/Infos natürlich ein wenn vorhanden\n"
        f"- Bezieh dich konkret auf den Verlauf — nie allgemein\n"
        f"- Jede Einleitung anders formulieren\n"
        f"- VERBOTEN: 'Ich hab mir gemerkt', 'Ich habe gehört', roboterhafte Sprache, "
        f"generische Phrasen, gleiche Formulierung wie vorher\n\n"
        f"Schreib jetzt die nächste Nachricht von {persona['name']}. "
        f"1-2 Sätze. Kein Präfix. Kein Markdown. Nur die Nachricht. Erotisch, verführerisch, persönlich."
    )

    for attempt in range(1, AI_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model      = model,
                max_tokens = 600,
                timeout    = 45,
                messages   = [
                    {"role": "system", "content": persona["system"]},
                    {"role": "user",   "content": prompt},
                ],
            )
            text = clean_reply(resp.choices[0].message.content or "")
            if text:
                return text
            print(f"  [AI] Leere Antwort (Versuch {attempt}/{AI_RETRIES})")
        except openai.APIConnectionError as e:
            print(f"  [AI] Verbindung fehlgeschlagen (Versuch {attempt}/{AI_RETRIES}): {e}")
        except openai.APIStatusError as e:
            print(f"  [AI] API Fehler {e.status_code} (Versuch {attempt}/{AI_RETRIES})")
        except Exception as e:
            print(f"  [AI] Fehler (Versuch {attempt}/{AI_RETRIES}): {e}")

        if attempt < AI_RETRIES:
            time.sleep(2 ** attempt)   # 2s, 4s

    # Alle Versuche fehlgeschlagen → Fallback, nie überspringen
    fallback = persona.get("fallback_msg", "Hey, meld dich 🙂")
    print(f"  [AI] Fallback nach {AI_RETRIES} Versuchen")
    return fallback

# ─── Playwright helpers ───────────────────────────────────────────────────────

async def dismiss_consent(page: Page) -> None:
    try:
        banner = page.locator("consent-banner")
        if not await banner.count():
            return
        btns = banner.locator("button")
        for i in range(min(await btns.count(), 5)):
            try:
                await btns.nth(i).click(timeout=1000)
                await page.wait_for_timeout(200)
                return
            except Exception:
                pass
        handle = await banner.first.element_handle()
        if handle:
            await page.evaluate("el => el.remove()", handle)
    except Exception:
        pass


async def dismiss_template(page: Page) -> None:
    try:
        if not await page.locator("text=Vorlagen").count():
            return
        for sel in [
            "ion-button:has(ion-icon[name='close'])",
            "ion-button:has(ion-icon[name='close-outline'])",
        ]:
            try:
                loc = page.locator(sel)
                if await loc.count():
                    await loc.first.click(timeout=1000)
                    await page.wait_for_timeout(200)
                    return
            except Exception:
                pass
    except Exception:
        pass


async def get_history(page: Page, limit: int = DEFAULT_HISTORY_LIMIT) -> list[Message]:
    bubbles = page.locator(SEL["bubbles"])
    try:
        await bubbles.first.wait_for(timeout=8000)
    except Exception:
        return []

    total   = await bubbles.count()
    history: list[Message] = []

    for i in range(max(0, total - limit), total):
        bubble = bubbles.nth(i)
        role:   Optional[str] = None

        try:
            cls = await bubble.locator(SEL["bubble_msg"]).first.get_attribute("class") or ""
            if "message-mine"  in cls: role = "me"
            elif "message-other" in cls: role = "user"
        except Exception:
            pass

        text = ""
        try:
            text = (await bubble.locator(SEL["bubble_txt"]).first.inner_text(timeout=1000)).strip()
        except Exception:
            try:
                text = (await bubble.inner_text(timeout=1000)).strip()
            except Exception:
                pass

        if text and role:
            history.append(Message(role=role, text=text))

    return history


async def send_message(page: Page, text: str) -> None:
    await dismiss_consent(page)
    await dismiss_template(page)

    ta = page.locator(SEL["textarea"]).first
    await ta.wait_for(timeout=8000)

    handle = await ta.element_handle()
    if handle:
        await page.evaluate(
            """([el, v]) => {
                try { el.value = v; } catch(e) {}
                el.dispatchEvent(new Event('input',  { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            [handle, text],
        )
    else:
        await ta.click(timeout=1500)
        await page.keyboard.type(text, delay=12)

    await dismiss_template(page)

    for sel in SEL["send_btn"]:
        try:
            loc = page.locator(sel)
            if await loc.count():
                await loc.first.click(timeout=2500)
                await page.wait_for_timeout(500)
                await dismiss_template(page)
                return
        except Exception:
            pass

    raise RuntimeError("Kein Send-Button gefunden")


async def navigate_back(page: Page) -> None:
    try:
        await page.click("text=Nachrichten", timeout=4000)
        await page.wait_for_timeout(800)
    except Exception:
        try:
            await page.go_back()
            await page.wait_for_timeout(800)
        except Exception:
            pass


async def scroll_down(page: Page) -> None:
    await page.evaluate(SCROLL_DOWN_JS)
    await page.wait_for_timeout(600)


async def read_item(page: Page, index: int) -> Optional[ChatItem]:
    item = page.locator(SEL["items"]).nth(index)
    try:
        username = (await item.locator(SEL["username"]).first.inner_text(timeout=500)).strip()
    except Exception:
        return None
    if not username:
        return None

    preview = revenue_raw = ""
    try:
        preview = (await item.locator(SEL["preview"]).first.inner_text(timeout=500)).strip()
    except Exception:
        pass
    try:
        revenue_raw = (await item.locator(SEL["revenue"]).first.inner_text(timeout=500)).strip()
    except Exception:
        pass

    return ChatItem(username=username, preview=preview, revenue=parse_revenue(revenue_raw))


async def _read_username_fast(page: Page, index: int) -> Optional[str]:
    """Schnelles Lesen nur des Usernamens (für Scan-Loops)."""
    try:
        item = page.locator(SEL["items"]).nth(index)
        return (await item.locator(SEL["username"]).first.inner_text(timeout=200)).strip() or None
    except Exception:
        return None

# ─── Snapshot & Check ─────────────────────────────────────────────────────────

async def read_snapshot(page: Page) -> dict[str, str]:
    """Scrollt nach oben, liest Top-SNAPSHOT_SIZE Chats → {username: preview}"""
    await page.evaluate(SCROLL_TOP_JS)
    await page.wait_for_timeout(600)
    snapshot: dict[str, str] = {}
    count = await page.locator(SEL["items"]).count()
    for i in range(min(SNAPSHOT_SIZE, count)):
        chat = await read_item(page, i)
        if chat:
            snapshot[chat.username] = chat.preview
    return snapshot


async def find_next_unprocessed(
    page:          Page,
    processed:     set[str],
    last_previews: dict[str, str],
) -> Optional[ChatItem]:
    """
    Scannt die Inbox von oben nach unten.
    Gibt zurück:
      - Ersten Chat der noch nicht in processed ist, ODER
      - Einen bereits bearbeiteten Chat wenn sein Preview sich geändert hat
        (User hat geantwortet → sofort nochmal bearbeiten)
    Scrollt nach unten wenn nötig.
    """
    no_progress = 0
    prev_count  = 0

    while True:
        count = await page.locator(SEL["items"]).count()

        for i in range(count):
            username = await _read_username_fast(page, i)
            if not username:
                continue
            if username not in processed:
                return await read_item(page, i)
            # Bereits bearbeitet → in diesem Pass nicht nochmal

        if count <= prev_count:
            no_progress += 1
            if no_progress >= 3:
                return None   # Ende der Inbox
        else:
            no_progress = 0

        prev_count = count
        await scroll_down(page)


async def do_periodic_check(
    page:        Page,
    snapshot:    dict[str, str],
    processed:   set[str],
    account:     Account,
    client:      openai.OpenAI,
    user_states: dict,
    dry_run:     bool,
) -> dict[str, str]:
    """
    Scrollt zum Anfang, vergleicht Top-30 Previews mit Snapshot.
    Antwortet sofort auf jede neue Nachricht.
    Gibt aktualisierten Snapshot zurück.
    """
    current   = await read_snapshot(page)
    new_found = 0

    for username, preview in current.items():
        if preview == snapshot.get(username, "") or username in processed:
            continue
        new_found += 1
        print(f"  [CHECK] Neue Nachricht: {username}")
        count = await page.locator(SEL["items"]).count()
        for i in range(count):
            try:
                uname = (
                    await page.locator(SEL["items"]).nth(i)
                    .locator(SEL["username"]).first.inner_text(timeout=300)
                ).strip()
                if uname == username:
                    chat = await read_item(page, i)
                    if chat:
                        limit = PREMIUM_HISTORY_LIMIT if chat.revenue > 20 else DEFAULT_HISTORY_LIMIT
                        await process_chat(page, chat, account, client, dry_run, user_states, limit)
                        processed.add(username)
                    break
            except Exception:
                pass

    if new_found == 0:
        print(f"  [CHECK] Nichts Neues in Top-{SNAPSHOT_SIZE}")

    return current

# ─── Core: Chat verarbeiten ───────────────────────────────────────────────────

COOLDOWN_HOURS = 5   # Nicht wieder schreiben wenn letzte Nachricht < 5h

async def process_chat(
    page:        Page,
    item:        ChatItem,
    account:     Account,
    client:      openai.OpenAI,
    dry_run:     bool,
    user_states: dict,
    hist_limit:  int = DEFAULT_HISTORY_LIMIT,
) -> bool:
    # Blacklist-Check: gesperrte Accounts nie anschreiben
    if item.username.lower() in load_blacklist():
        print(f"  [SKIP] Blacklist: {item.username}")
        return False

    # Cooldown-Check: letzte eigene Nachricht < 5h → überspringen
    last_sent = user_states.get(item.username, {}).get("last_sent_at")
    if last_sent:
        from datetime import timezone
        try:
            delta = now_utc() - datetime.fromisoformat(last_sent).replace(tzinfo=timezone.utc)
            if delta.total_seconds() < COOLDOWN_HOURS * 3600:
                remaining = int((COOLDOWN_HOURS * 3600 - delta.total_seconds()) / 60)
                print(f"  [SKIP] Kürzlich geschrieben ({int(delta.total_seconds()/60)}min ago, noch {remaining}min)")
                return False
        except (ValueError, TypeError):
            pass

    items   = page.locator(SEL["items"])
    count   = await items.count()
    clicked = False

    for i in range(count):
        try:
            uname = (await items.nth(i).locator(SEL["username"]).first.inner_text(timeout=400)).strip()
            if uname == item.username:
                it = items.nth(i)
                await it.scroll_into_view_if_needed()
                await page.wait_for_timeout(200)
                await it.click(timeout=2000)
                await page.wait_for_timeout(1800)
                clicked = True
                break
        except Exception:
            pass

    if not clicked:
        print("  [SKIP] Klick fehlgeschlagen")
        return False

    history = await get_history(page, hist_limit)
    if not history:
        print("  [SKIP] Leerer Verlauf")
        await navigate_back(page)
        return False

    trailing = trailing_own(history)
    if trailing > MAX_TRAILING:
        print(f"  [SKIP] {trailing}x hintereinander ohne Antwort")
        log_event({"kind": "skip", "account": account.name,
                   "username": item.username, "trailing": trailing})
        await navigate_back(page)
        return False

    # Präferenzen extrahieren und speichern (nur wenn genug User-Nachrichten)
    user_msg_count = sum(1 for m in history if m.role == "user")
    stored_prefs   = user_states.get(item.username, {}).get("prefs", {})
    if user_msg_count >= 2 and not stored_prefs:
        prefs = extract_user_prefs(history, client)
        if prefs:
            if item.username not in user_states:
                user_states[item.username] = {}
            user_states[item.username]["prefs"] = prefs
            print(f"  [PREFS] gespeichert: {list(prefs.keys())}")

    user_type = user_states.get(item.username, {}).get("type", "?")
    prefix    = "[DRY] " if dry_run else ""

    # Kaufabsicht erkennen → personalisierte Angebots-Nachricht
    if detect_content_intent(history, client):
        reply = get_offer_reply(history, item.username, account.name, item.revenue, client, user_states)
        print(f"  {user_type} | 💰OFFER | {prefix}{reply[:90]}")
        log_event({"kind": "offer_draft", "account": account.name, "username": item.username,
                   "reply": reply, "type": user_type, "dry_run": dry_run})
    else:
        reply = get_ai_reply(history, item.username, account.name, item.revenue, client, user_states)
        print(f"  {user_type} | {prefix}{reply[:90]}")
        log_event({"kind": "draft", "account": account.name, "username": item.username,
                   "reply": reply, "type": user_type, "dry_run": dry_run})

    if not dry_run:
        try:
            await send_message(page, reply)
            log_event({"kind": "sent", "account": account.name, "username": item.username})
            # Sendezeitpunkt merken → in nächsten Pässen überspringen
            if item.username not in user_states:
                user_states[item.username] = {}
            user_states[item.username]["last_sent_at"]      = now_utc().isoformat()
            user_states[item.username]["last_sent_preview"] = item.preview
            await asyncio.sleep(2)
        except Exception as e:
            print(f"  [SEND ERROR] {e}")
            await navigate_back(page)
            return False

    await navigate_back(page)
    await page.wait_for_timeout(300)
    return True

# ─── Main Runner ──────────────────────────────────────────────────────────────

async def run_account(
    account:  Account,
    dry_run:  bool,
    once:     bool,
    headless: bool = False,
) -> None:
    from playwright.async_api import async_playwright

    ensure_ollama()   # Ollama starten falls nicht aktiv
    client      = make_client()
    user_states = load_user_states(account.name)
    crash_delay = 20

    print(f"[{account.name.upper()}] Start {'(headless)' if headless else '(Browser sichtbar)'}")

    while True:
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=headless)
                ctx: BrowserContext = await browser.new_context(
                    storage_state = str(account.storage_state),
                    locale        = "de-DE",
                )
                page = await ctx.new_page()
                await page.goto("https://4based.com", wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)
                await dismiss_consent(page)

                # Login prüfen
                if "login" in page.url.lower() or await page.locator("input[type='email']").count():
                    raise RuntimeError("Session abgelaufen — bitte neu einloggen: python scripts/hilda_runner.py --save-session")

                # Nachrichten-Tab klicken
                await page.click("text=Nachrichten", timeout=10000)
                await page.wait_for_timeout(2000)
                await page.locator("chat-overview").wait_for(timeout=15000)

                crash_delay = 20

                # Fortschritt laden (falls Absturz/Neustart mitten im Pass)
                progress_file = ROOT / "state" / f"{account.name}_progress.json"
                def _load_progress() -> set[str]:
                    data = load_json(progress_file, [])
                    if data:
                        print(f"[{account.name.upper()}] Fortschritt geladen: {len(data)} bereits bearbeitet → weiter")
                    return set(data)

                def _save_progress(processed: set[str]) -> None:
                    progress_file.write_text(json.dumps(list(processed)), encoding="utf-8")

                def _clear_progress() -> None:
                    if progress_file.exists():
                        progress_file.unlink()

                # Einmaliger Snapshot der Top-30 als Baseline
                snapshot = await read_snapshot(page)
                pass_num = 0

                print(f"[{account.name.upper()}] Snapshot: {len(snapshot)} Chats | Starte direkt")

                while True:
                    pass_num += 1
                    sent = skip = 0
                    reply_only = pass_num > 1   # Ab Pass 2: nur Antwortende
                    processed_this_pass: set[str] = _load_progress()
                    last_previews:       dict[str, str] = {}

                    if reply_only:
                        print(f"\n[{account.name.upper()}] PASS {pass_num} | {now_utc().strftime('%H:%M:%S')} | NUR ANTWORTEN")
                    else:
                        print(f"\n[{account.name.upper()}] PASS {pass_num} | {now_utc().strftime('%H:%M:%S')}")

                    # Inbox von oben nach unten durcharbeiten — kein Unterbrechen
                    while True:
                        chat = await find_next_unprocessed(page, processed_this_pass, last_previews)
                        if not chat:
                            break   # Ende der Inbox — alle Chats bearbeitet

                        # Ab Pass 2: überspringen wenn User nicht geantwortet hat
                        if reply_only:
                            last_sent_preview = user_states.get(chat.username, {}).get("last_sent_preview")
                            if last_sent_preview is not None and chat.preview == last_sent_preview:
                                processed_this_pass.add(chat.username)
                                _save_progress(processed_this_pass)
                                skip += 1
                                continue   # Keine Antwort → überspringen

                        label  = f"[${chat.revenue:.0f}]" if chat.revenue > 0 else "[NEU]"
                        cached = user_states.get(chat.username, {}).get("type", "?")
                        marker = "↩" if reply_only else "→"
                        print(f"  {marker} {chat.username} {label} [{cached}]", end=" ", flush=True)

                        limit = PREMIUM_HISTORY_LIMIT if chat.revenue > 20 else DEFAULT_HISTORY_LIMIT
                        ok    = await process_chat(page, chat, account, client, dry_run, user_states, limit)

                        processed_this_pass.add(chat.username)
                        _save_progress(processed_this_pass)   # sofort persistieren
                        last_previews[chat.username] = chat.preview
                        snapshot[chat.username] = chat.preview
                        if ok:  sent += 1
                        else:   skip += 1

                    # Pass abgeschlossen — Fortschritt löschen
                    _clear_progress()
                    save_user_states(account.name, user_states)
                    print(
                        f"\n[{account.name.upper()}] Pass {pass_num} fertig | "
                        f"Gesendet:{sent} Geskippt:{skip} Besucht:{len(processed_this_pass)}"
                    )
                    log_event({
                        "kind": "pass_complete", "account": account.name, "pass": pass_num,
                        "sent": sent, "skipped": skip, "visited": len(processed_this_pass),
                        "dry_run": dry_run,
                    })

                    if once:
                        break

                    # 90 Minuten warten bis zum nächsten Pass
                    next_run = now_utc().strftime("%H:%M")
                    print(f"[{account.name.upper()}] Warte 90 Min — nächster Pass um ca. {next_run}")
                    await asyncio.sleep(PASS_INTERVAL)
                    snapshot = await read_snapshot(page)

                await ctx.close()
                await browser.close()
                break

        except asyncio.CancelledError:
            print(f"\n[{account.name.upper()}] Gestoppt")
            save_user_states(account.name, user_states)
            break
        except Exception as e:
            print(f"\n[{account.name.upper()}] CRASH: {type(e).__name__}: {e}")
            log_event({"kind": "crash", "account": account.name,
                       "error": f"{type(e).__name__}: {e}"})
            save_user_states(account.name, user_states)
            if once:
                break
            print(f"[{account.name.upper()}] Neustart in {crash_delay}s...")
            await asyncio.sleep(crash_delay)
            crash_delay = min(crash_delay * 2, 300)
