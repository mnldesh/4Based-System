"""
base_runner.py — Gemeinsame Playwright-Logik für alle Account-Runner

Loop-Logik:
  - Login einmal, Browser bleibt offen
  - Cursor geht Chat für Chat von oben nach unten durch die Inbox
  - Jeder Chat: öffnen → History → User-Typ (gecacht) → KI-Antwort → Senden → zurück
  - Alle 10–15 Minuten: zum Anfang scrollen, neue Nachrichten prüfen, sofort antworten,
    dann exakt dort weitermachen wo pausiert wurde
  - User-Typ wird nur einmal klassifiziert und in state/{name}_users.json gespeichert
"""

import asyncio
import json
import random
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import openai
from playwright.async_api import Page, BrowserContext

from shared.personas import PERSONAS
from shared.ai_client import make_client, clean_reply, TEXT_MODEL

ROOT      = Path(__file__).resolve().parents[2]
LOG_PATH  = ROOT / "logs" / "runner.jsonl"
_log_lock = threading.Lock()

# Check-Intervall: alle 10–15 Minuten nach neuen Nachrichten schauen
CHECK_MIN = 600   # 10 Minuten
CHECK_MAX = 900   # 15 Minuten

MAX_TRAILING          = 10
PREMIUM_HISTORY_LIMIT = 100
DEFAULT_HISTORY_LIMIT = 20
PEEK_TOP_N            = 30   # Obere N Chats beim Check vergleichen

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
        raise SystemExit(f"[ERROR] Storage-State fehlt: {state_path}")
    return Account(name=name, storage_state=state_path, config=cfg)

# ─── Persistente User-States ──────────────────────────────────────────────────

def load_user_states(name: str) -> dict:
    """Liest gespeicherte User-Typen aus state/{name}_users.json."""
    return load_json(ROOT / "state" / f"{name}_users.json", {})


def save_user_states(name: str, states: dict) -> None:
    """Schreibt User-Typen in state/{name}_users.json."""
    path = ROOT / "state" / f"{name}_users.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(states, ensure_ascii=False, indent=2), encoding="utf-8")


def get_or_classify(
    username:    str,
    history:     list[Message],
    revenue:     float,
    user_states: dict,
) -> str:
    """
    Gibt den gecachten User-Typ zurück.
    Re-klassifiziert nur wenn Revenue gestiegen ist (User hat gekauft).
    """
    cached = user_states.get(username)
    if cached:
        cached_revenue = cached.get("revenue", 0.0)
        if revenue <= cached_revenue:
            return cached["type"]

    user_type = classify_user(history, revenue)
    user_states[username] = {
        "type":    user_type,
        "revenue": revenue,
        "seen_at": now_utc().isoformat(),
    }
    return user_type

# ─── AI ───────────────────────────────────────────────────────────────────────

def get_ai_reply(
    history:      list[Message],
    username:     str,
    account_name: str,
    revenue:      float,
    client:       openai.OpenAI,
    model:        str = TEXT_MODEL,
    user_type:    Optional[str] = None,
) -> Optional[str]:
    persona   = PERSONAS[account_name]
    utype     = user_type or classify_user(history, revenue)
    trailing  = trailing_own(history)

    user_msgs = [m.text for m in history if m.role == "user"]
    last_own  = [m.text for m in history if m.role == "me"][-3:]

    prompt = (
        f"Username: {username}\n"
        f"Typ: {utype} | Rev: ${revenue:.0f} | OhneAntwort: {trailing}\n"
        f"Letzte user msg: {user_msgs[-1] if user_msgs else 'keine'}\n"
        f"Unsere letzten (nicht wiederholen): {' | '.join(last_own) if last_own else 'keine'}\n\n"
        f"Verlauf:\n{format_history(history)}\n\n"
        f"Nur die nächste Nachricht als {persona['name']}:"
    )

    try:
        resp = client.chat.completions.create(
            model      = model,
            max_tokens = 120,
            messages   = [
                {"role": "system", "content": persona["system"]},
                {"role": "user",   "content": prompt},
            ],
        )
        raw = resp.choices[0].message.content
        return clean_reply(raw) or None
    except openai.APIConnectionError as e:
        print(f"  [AI] Verbindungsfehler: {e}")
    except openai.APIStatusError as e:
        print(f"  [AI] API Fehler {e.status_code}: {e.message}")
    except Exception as e:
        print(f"  [AI] Fehler: {e}")
    return None

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
            """(el, v) => {
                try { el.value = v; } catch(e) {}
                el.dispatchEvent(new Event('input',  { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            handle, text,
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
        await page.click(SEL["nav"], timeout=4000)
        await page.wait_for_timeout(1000)
    except Exception:
        try:
            await page.go_back()
            await page.wait_for_timeout(1000)
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

# ─── Core: Chat verarbeiten ───────────────────────────────────────────────────

async def process_chat(
    page:        Page,
    item:        ChatItem,
    account:     Account,
    client:      openai.OpenAI,
    dry_run:     bool,
    user_states: dict,
    hist_limit:  int = DEFAULT_HISTORY_LIMIT,
) -> bool:
    """Öffnet einen Chat, generiert Antwort, sendet sie. Gibt True bei Erfolg zurück."""
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
    if trailing >= MAX_TRAILING:
        print(f"  [SKIP] {trailing}x hintereinander ohne Antwort")
        log_event({"kind": "skip", "account": account.name,
                   "username": item.username, "trailing": trailing})
        await navigate_back(page)
        return False

    user_type = get_or_classify(item.username, history, item.revenue, user_states)
    reply     = get_ai_reply(history, item.username, account.name, item.revenue, client,
                             user_type=user_type)
    if not reply:
        print("  [SKIP] Kein AI-Reply")
        await navigate_back(page)
        return False

    prefix = "[DRY] " if dry_run else ""
    print(f"  {user_type} | {prefix}{reply[:90]}")

    log_event({"kind": "draft", "account": account.name, "username": item.username,
               "reply": reply, "type": user_type, "dry_run": dry_run})

    if not dry_run:
        try:
            await send_message(page, reply)
            log_event({"kind": "sent", "account": account.name, "username": item.username})
            await asyncio.sleep(2)
        except Exception as e:
            print(f"  [SEND ERROR] {e}")
            await navigate_back(page)
            return False

    await navigate_back(page)
    await page.wait_for_timeout(300)
    return True

# ─── Periodischer Check: neue Nachrichten oben ────────────────────────────────

async def _peek_and_respond(
    page:         Page,
    last_previews: dict[str, str],
    account:      Account,
    client:       openai.OpenAI,
    dry_run:      bool,
    user_states:  dict,
) -> int:
    """
    Scrollt zum Anfang, liest obere PEEK_TOP_N Chats.
    Antwortet auf alle Chats deren Preview sich geändert hat.
    Gibt Anzahl der beantworteten Chats zurück.
    """
    await page.evaluate(SCROLL_TOP_JS)
    await page.wait_for_timeout(600)

    count     = await page.locator(SEL["items"]).count()
    new_items: list[ChatItem] = []

    for i in range(min(PEEK_TOP_N, count)):
        item = await read_item(page, i)
        if not item:
            continue
        if item.preview != last_previews.get(item.username, ""):
            new_items.append(item)

    for item in new_items:
        print(f"\n  [NEW] {item.username} — antworte...", end=" ", flush=True)
        limit = PREMIUM_HISTORY_LIMIT if item.revenue > 20 else DEFAULT_HISTORY_LIMIT
        ok    = await process_chat(page, item, account, client, dry_run, user_states, limit)
        if ok:
            last_previews[item.username] = item.preview

    return len(new_items)

# ─── Haupt-Runner ─────────────────────────────────────────────────────────────

async def run_account(
    account:  Account,
    dry_run:  bool,
    once:     bool,
    headless: bool = False,
) -> None:
    from playwright.async_api import async_playwright

    client      = make_client()
    user_states = load_user_states(account.name)
    crash_delay = 20

    print(f"[{account.name.upper()}] Start {'(headless)' if headless else '(Browser sichtbar)'}")

    while True:   # Crash-Recovery-Loop
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=headless)
                ctx: BrowserContext = await browser.new_context(
                    storage_state = str(account.storage_state),
                    locale        = "de-DE",
                )
                page = await ctx.new_page()
                await page.goto("https://4based.com", wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                await dismiss_consent(page)
                await page.click(SEL["nav"], timeout=10000)
                await page.wait_for_timeout(2000)
                await page.locator("chat-overview").wait_for(timeout=10000)

                crash_delay   = 20   # nach erfolgreichem Start zurücksetzen
                last_previews: dict[str, str] = {}
                pass_num      = 0

                while True:   # Pass-Loop
                    pass_num   += 1
                    sent = skip = 0
                    next_check  = asyncio.get_event_loop().time() + random.uniform(CHECK_MIN, CHECK_MAX)

                    print(f"\n[{account.name.upper()}] PASS {pass_num} | {now_utc().strftime('%H:%M:%S')}")

                    # Zum Anfang der Inbox scrollen
                    await page.evaluate(SCROLL_TOP_JS)
                    await page.wait_for_timeout(800)

                    cursor = 0

                    while True:   # Cursor-Loop (ein Durchgang durch die Inbox)

                        # ── Periodischer Check alle 10–15 Minuten ─────────────
                        if asyncio.get_event_loop().time() >= next_check:
                            replied = await _peek_and_respond(
                                page, last_previews, account, client, dry_run, user_states
                            )
                            if replied:
                                print(f"  [CHECK] {replied} neue Antwort(en) bearbeitet")
                            else:
                                print(f"  [CHECK] Keine neuen Nachrichten")

                            # Cursor-Position wiederherstellen
                            items = page.locator(SEL["items"])
                            if cursor > 0 and cursor < await items.count():
                                try:
                                    await items.nth(cursor).scroll_into_view_if_needed()
                                    await page.wait_for_timeout(400)
                                except Exception:
                                    pass

                            next_check = asyncio.get_event_loop().time() + random.uniform(CHECK_MIN, CHECK_MAX)

                        # ── Nächsten Chat lesen ───────────────────────────────
                        items = page.locator(SEL["items"])
                        count = await items.count()

                        if cursor >= count:
                            # Versuche mehr zu laden durch Scrollen
                            prev_count = count
                            await scroll_down(page)
                            await page.wait_for_timeout(500)
                            count = await items.count()
                            if count <= prev_count:
                                break   # Ende der Inbox

                        if cursor >= count:
                            break

                        item = await read_item(page, cursor)
                        if not item:
                            cursor += 1
                            continue

                        # Überspringen wenn Preview sich nicht geändert hat
                        if last_previews.get(item.username) == item.preview:
                            cursor += 1
                            continue

                        label      = "[NEU]" if item.revenue == 0 else f"[${item.revenue:.0f}]"
                        cached_typ = user_states.get(item.username, {}).get("type", "?")
                        print(f"  → {item.username} {label} [{cached_typ}]", end=" ", flush=True)

                        limit = PREMIUM_HISTORY_LIMIT if item.revenue > 20 else DEFAULT_HISTORY_LIMIT
                        ok    = await process_chat(page, item, account, client, dry_run, user_states, limit)

                        last_previews[item.username] = item.preview
                        if ok: sent += 1
                        else:  skip += 1

                        cursor += 1

                    # ── Pass abgeschlossen ─────────────────────────────────────
                    save_user_states(account.name, user_states)
                    print(
                        f"\n[{account.name.upper()}] Pass {pass_num} fertig | "
                        f"Gesendet:{sent} Geskippt:{skip}"
                    )
                    log_event({
                        "kind":    "pass_complete",
                        "account": account.name,
                        "pass":    pass_num,
                        "sent":    sent,
                        "skipped": skip,
                        "dry_run": dry_run,
                    })

                    if once:
                        break

                    if sent == 0:
                        print(f"[{account.name.upper()}] Nichts zu tun — warte 30s...")
                        await asyncio.sleep(30)

                await ctx.close()
                await browser.close()
                break   # Sauberer Exit

        except asyncio.CancelledError:
            print(f"\n[{account.name.upper()}] Gestoppt")
            break
        except Exception as e:
            print(f"\n[{account.name.upper()}] CRASH: {type(e).__name__}: {e}")
            log_event({"kind": "crash", "account": account.name,
                       "error": f"{type(e).__name__}: {e}"})
            if once:
                break
            print(f"[{account.name.upper()}] Neustart in {crash_delay}s...")
            await asyncio.sleep(crash_delay)
            crash_delay = min(crash_delay * 2, 300)
