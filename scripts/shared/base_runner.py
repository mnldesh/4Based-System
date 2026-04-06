"""
base_runner.py — Gemeinsame Playwright-Logik für alle Account-Runner
"""

import asyncio
import json
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
_log_lock = threading.Lock()   # Thread-sicher: Hilda + Tia schreiben gleichzeitig

# Maximale eigene Nachrichten hintereinander bevor Skip
MAX_TRAILING = 10
# History-Limit für Premium-User
PREMIUM_HISTORY_LIMIT = 100
DEFAULT_HISTORY_LIMIT = 20

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
    """Zählt aufeinanderfolgende eigene Nachrichten am Ende."""
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

# ─── AI ───────────────────────────────────────────────────────────────────────

def get_ai_reply(
    history:      list[Message],
    username:     str,
    account_name: str,
    revenue:      float,
    client:       openai.OpenAI,
    model:        str = TEXT_MODEL,
) -> Optional[str]:
    persona   = PERSONAS[account_name]
    user_type = classify_user(history, revenue)
    trailing  = trailing_own(history)

    user_msgs = [m.text for m in history if m.role == "user"]
    last_own  = [m.text for m in history if m.role == "me"][-3:]

    prompt = (
        f"Username: {username}\n"
        f"Typ: {user_type} | Rev: ${revenue:.0f} | OhneAntwort: {trailing}\n"
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
        # Fallback: click + type
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


async def collect_all_items(page: Page) -> list[ChatItem]:
    seen:   set[str]       = set()
    items:  list[ChatItem] = []
    no_new = 0

    while True:
        count     = await page.locator(SEL["items"]).count()
        found_new = False

        for i in range(count):
            chat = await read_item(page, i)
            if chat and chat.username not in seen:
                seen.add(chat.username)
                items.append(chat)
                found_new = True

        if not found_new:
            no_new += 1
            if no_new >= 3:
                break
        else:
            no_new = 0

        await scroll_down(page)

    return items


async def process_chat(
    page:       Page,
    item:       ChatItem,
    account:    Account,
    client:     openai.OpenAI,
    dry_run:    bool,
    hist_limit: int = DEFAULT_HISTORY_LIMIT,
) -> bool:
    items = page.locator(SEL["items"])
    count = await items.count()
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
        print(f"  [SKIP] {trailing}x hintereinander")
        log_event({"kind": "skip", "account": account.name,
                   "username": item.username, "trailing": trailing})
        await navigate_back(page)
        return False

    reply = get_ai_reply(history, item.username, account.name, item.revenue, client)
    if not reply:
        print("  [SKIP] Kein AI-Reply")
        await navigate_back(page)
        return False

    user_type = classify_user(history, item.revenue)
    prefix    = "[DRY] " if dry_run else ""
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


async def check_replies(
    page:      Page,
    snapshots: dict[str, str],
    account:   Account,
    client:    openai.OpenAI,
    dry_run:   bool,
) -> list[str]:
    replied:   list[str] = []
    seen:      set[str]  = set()
    no_new     = 0
    prev_count = 0

    # Alle Chats scannen und Änderungen erkennen
    while True:
        count = await page.locator(SEL["items"]).count()
        for i in range(count):
            chat = await read_item(page, i)
            if not chat or chat.username in seen:
                continue
            seen.add(chat.username)
            if chat.username in snapshots and chat.preview != snapshots[chat.username]:
                replied.append(chat.username)
                print(f"  ↩ {chat.username}")

        no_new = no_new + 1 if count <= prev_count else 0
        if no_new >= 3:
            break
        prev_count = count
        await scroll_down(page)

    # Auf jede Antwort reagieren
    for username in replied:
        await page.evaluate(SCROLL_TOP_JS)
        await page.wait_for_timeout(500)

        found = False
        for _ in range(12):
            count = await page.locator(SEL["items"]).count()
            for i in range(count):
                try:
                    uname = (
                        await page.locator(SEL["items"]).nth(i)
                        .locator(SEL["username"]).first.inner_text(timeout=400)
                    ).strip()
                    if uname == username:
                        it = page.locator(SEL["items"]).nth(i)
                        await it.scroll_into_view_if_needed()
                        await it.click(timeout=2000)
                        await page.wait_for_timeout(1800)
                        found = True
                        break
                except Exception:
                    pass
            if found:
                break
            await scroll_down(page)

        if not found:
            print(f"  [WARN] Chat für {username} nicht gefunden")
            continue

        history = await get_history(page, DEFAULT_HISTORY_LIMIT)
        if not history or history[-1].role != "user":
            await navigate_back(page)
            continue

        reply = get_ai_reply(history, username, account.name, 0.0, client)
        if reply:
            prefix = "[DRY] " if dry_run else ""
            print(f"  [{username}] {prefix}{reply[:80]}")
            if not dry_run:
                try:
                    await send_message(page, reply)
                    await asyncio.sleep(2)
                except Exception as e:
                    print(f"  [{username}] SEND ERROR: {e}")

        await navigate_back(page)

    return replied


async def run_account(
    account:   Account,
    dry_run:   bool,
    once:      bool,
    headless:  bool = False,
) -> None:
    from playwright.async_api import async_playwright

    runtime     = load_json(ROOT / "config" / "runtime.json", {})
    poll        = int(runtime.get("pollSeconds", 120))
    client      = make_client()
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
                await page.wait_for_timeout(2000)
                await dismiss_consent(page)
                await page.click(SEL["nav"], timeout=10000)
                await page.wait_for_timeout(2000)
                await page.locator("chat-overview").wait_for(timeout=10000)

                crash_delay = 20   # Backoff zurücksetzen nach erfolgreichem Start
                cycle       = 0

                while True:
                    cycle += 1
                    print(f"\n[{account.name.upper()}] CYCLE {cycle} | {now_utc().strftime('%H:%M:%S')}")

                    all_chats = await collect_all_items(page)
                    snapshots = {c.username: c.preview for c in all_chats}
                    sent = skip = 0

                    for chat in all_chats:
                        label = "[NEU]" if chat.revenue == 0 else f"[${chat.revenue:.0f}]"
                        print(f"  → {chat.username} {label}", end=" ", flush=True)
                        limit = PREMIUM_HISTORY_LIMIT if chat.revenue > 20 else DEFAULT_HISTORY_LIMIT
                        ok    = await process_chat(page, chat, account, client, dry_run, limit)
                        if ok: sent += 1
                        else:  skip += 1

                    print(
                        f"\n[{account.name.upper()}] Fertig | "
                        f"Gesendet:{sent} Geskippt:{skip} Besucht:{len(all_chats)}"
                    )

                    replied = await check_replies(page, snapshots, account, client, dry_run)

                    log_event({
                        "kind": "cycle_complete", "account": account.name, "cycle": cycle,
                        "sent": sent, "skipped": skip, "visited": len(all_chats),
                        "replies": len(replied), "dry_run": dry_run,
                    })

                    if once:
                        break
                    print(f"[{account.name.upper()}] Warte {poll}s...")
                    await asyncio.sleep(poll)

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
            crash_delay = min(crash_delay * 2, 300)   # Max 5 Minuten
