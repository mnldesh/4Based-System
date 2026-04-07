"""
live_runner.py — 4Based automated message responder
Runs Playwright + local LLM (Ollama) to reply to DMs for configured accounts.
"""

import argparse
import asyncio
import json
import re
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import openai
from playwright.async_api import async_playwright, Page, BrowserContext

ROOT = Path("/home/kali/4based-system")
LOG_PATH = ROOT / "logs" / "runner.jsonl"

# ─── Selectors ────────────────────────────────────────────────────────────────

SEL = {
    "nav":        "text=Nachrichten",
    "items":      "chat-overview ion-item",
    "username":   ".username",
    "preview":    "h3.subTitle span",
    "revenue":    "button.revenue",
    "textarea":   "ion-footer.chat-write-area text-area",
    "send_btn":   [
        "ion-footer.chat-write-area button:has-text('Senden')",
        "ion-footer.chat-write-area ion-button:has-text('Senden')",
    ],
    "bubbles":    "chat-bubble",
    "bubble_msg": "div.message",
    "bubble_txt": "span.label-bubble",
}

SCROLL_JS = """
const selectors = [
    'chat-overview ion-content',
    'chat-overview',
    'ion-menu ion-content'
];
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
const el = document.querySelector(
    'chat-overview ion-content, chat-overview'
);
if (el) {
    const inner = el.shadowRoot && el.shadowRoot.querySelector('.inner-scroll');
    if (inner) inner.scrollTop = 0;
    else el.scrollTop = 0;
}
"""

# ─── Persona config ───────────────────────────────────────────────────────────

PERSONAS: dict[str, dict] = {
    "hilda": {
        "name": "Hilda Valentine",
        "voucher_pct": 35,
        "system": (
            "Du bist Hilda Valentine, 24J. Selbstbewusst, direkt, frech, flirty. "
            "Verkaufst Content auf 4Based. Gutschein: 35%.\n"
            "VERBOTEN: markdown, gleiche nachricht nochmal, username am satzanfang, mehr als 3 sätze.\n"
            "NEU/KALT: kein sales, neugier wecken. "
            "KALT_HART: unerwartete frage stellen. "
            "AKTIV: einwand lösen, konkretes angebot. "
            "KAEUFER: an kauf anknüpfen. "
            "PREMIUM: vip behandlung."
        ),
    },
    "tia": {
        "name": "Tia",
        "voucher_pct": 30,
        "system": (
            "Du bist Tia, 22J. Süß, verspielt, herzlich. "
            "Verkaufst Content auf 4Based. Gutschein: 30%.\n"
            "VERBOTEN: markdown, gleiche nachricht nochmal, username am satzanfang, mehr als 3 sätze.\n"
            "NEU/KALT: kein sales, interesse zeigen. "
            "KALT_HART: völlig unerwartetes schicken. "
            "AKTIV: connection aufbauen. "
            "KAEUFER: subtiler upsell. "
            "PREMIUM: fan behandeln."
        ),
    },
}

# ─── Data types ───────────────────────────────────────────────────────────────

@dataclass
class Account:
    name: str
    storage_state: Path
    config: dict = field(default_factory=dict)


@dataclass
class ChatItem:
    username: str
    preview: str
    revenue: float


@dataclass
class Message:
    role: str   # "me" | "user"
    text: str

# ─── Utilities ────────────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] Konnte {path} nicht lesen: {e}")
        return default


def log_event(obj: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {"at": now_utc().isoformat(), **obj}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def parse_revenue(text: str) -> float:
    """Extract first float/int from a string like '$42.50'."""
    match = re.search(r"[\d.]+", text or "")
    return float(match.group()) if match else 0.0


def trailing_own_messages(history: list[Message]) -> int:
    """Count consecutive 'me' messages at the end of history."""
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
    lines = []
    for msg in history:
        speaker = "Model" if msg.role == "me" else "User"
        lines.append(f"{speaker}: {msg.text}")
    return "\n".join(lines)


def clean_reply(text: str) -> str:
    """Strip thinking tags, markdown bold, speaker prefixes."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"\*\*.*?\*\*", "", text).strip()
    # Remove any "Name: " prefix at the start
    text = re.sub(r"^\w[\w ]{0,20}:\s*", "", text).strip()
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    return lines[0] if lines else text


def classify_user(history: list[Message], revenue: float) -> str:
    has_user_msg = any(m.role == "user" for m in history)
    trailing = trailing_own_messages(history)

    if revenue > 20:
        return "PREMIUM"
    if revenue > 0:
        return "KAEUFER"
    if has_user_msg:
        return "AKTIV"
    if trailing >= 5:
        return "KALT_HART"
    if trailing >= 1:
        return "KALT"
    return "NEU"


def load_account(name: str, state_file: str) -> Account:
    cfg = load_json(ROOT / "config" / f"{name}.json", {})
    state_path = ROOT / "state" / state_file
    if not state_path.exists():
        raise SystemExit(f"[ERROR] Storage-State fehlt: {state_path}")
    return Account(name=name, storage_state=state_path, config=cfg)

# ─── AI ───────────────────────────────────────────────────────────────────────

def make_ai_client() -> openai.OpenAI:
    return openai.OpenAI(
        base_url="http://127.0.0.1:11434/v1",
        api_key="ollama",
    )


def get_ai_reply(
    history: list[Message],
    username: str,
    account_name: str,
    revenue: float,
    client: openai.OpenAI,
) -> Optional[str]:
    persona = PERSONAS[account_name]
    user_type = classify_user(history, revenue)
    trailing = trailing_own_messages(history)

    user_msgs = [m.text for m in history if m.role == "user"]
    last_own = [m.text for m in history if m.role == "me"][-3:]

    prompt = (
        f"Username: {username}\n"
        f"Typ: {user_type} | Rev: ${revenue:.0f} | OhneAntwort: {trailing}\n"
        f"Letzte user msg: {user_msgs[-1] if user_msgs else 'keine'}\n"
        f"Unsere letzten (nicht wiederholen): "
        f"{' | '.join(last_own) if last_own else 'keine'}\n\n"
        f"Verlauf:\n{format_history(history)}\n\n"
        f"Nur die nächste Nachricht als {persona['name']}:"
    )

    try:
        response = client.chat.completions.create(
            model="qwen3:14b",
            max_tokens=120,
            messages=[
                {"role": "system", "content": persona["system"]},
                {"role": "user",   "content": prompt},
            ],
        )
        raw = response.choices[0].message.content
        return clean_reply(raw)
    except Exception as e:
        print(f"  [AI ERROR] {e}")
        return None

# ─── Playwright helpers ───────────────────────────────────────────────────────

async def dismiss_consent_banner(page: Page) -> None:
    try:
        banner = page.locator("consent-banner")
        if not await banner.count():
            return
        buttons = banner.locator("button")
        for i in range(min(await buttons.count(), 5)):
            try:
                await buttons.nth(i).click(timeout=1000)
                await page.wait_for_timeout(200)
                return
            except Exception:
                pass
        # Fallback: remove banner from DOM
        handle = await banner.first.element_handle()
        if handle:
            await page.evaluate("el => el.remove()", handle)
    except Exception:
        pass


async def dismiss_template_popup(page: Page) -> None:
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


async def get_chat_history(page: Page, limit: int = 20) -> list[Message]:
    bubbles = page.locator(SEL["bubbles"])
    try:
        await bubbles.first.wait_for(timeout=8000)
    except Exception:
        return []

    total = await bubbles.count()
    history: list[Message] = []

    for i in range(max(0, total - limit), total):
        bubble = bubbles.nth(i)
        role: Optional[str] = None

        try:
            classes = await bubble.locator(SEL["bubble_msg"]).first.get_attribute("class") or ""
            if "message-mine" in classes:
                role = "me"
            elif "message-other" in classes:
                role = "user"
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
    await dismiss_consent_banner(page)
    await dismiss_template_popup(page)

    textarea = page.locator(SEL["textarea"]).first
    await textarea.wait_for(timeout=8000)

    # Use JS to set value (avoids duplicate-text issue when combined with type())
    handle = await textarea.element_handle()
    if handle:
        await page.evaluate(
            """(el, v) => {
                try { el.value = v; } catch(e) {}
                el.dispatchEvent(new Event('input',  { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            handle,
            text,
        )
    else:
        # Fallback: click + type
        await textarea.click(timeout=1500)
        await page.keyboard.type(text, delay=12)

    await dismiss_template_popup(page)

    for sel in SEL["send_btn"]:
        try:
            loc = page.locator(sel)
            if await loc.count():
                await loc.first.click(timeout=2500)
                await page.wait_for_timeout(500)
                await dismiss_template_popup(page)
                return
        except Exception:
            pass

    raise RuntimeError("Senden fehlgeschlagen: Kein Send-Button gefunden")


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


async def scroll_chat_list_down(page: Page) -> None:
    await page.evaluate(SCROLL_JS)
    await page.wait_for_timeout(600)


async def read_chat_item(page: Page, index: int) -> Optional[ChatItem]:
    """Read username, preview and revenue from a chat list item by index."""
    item = page.locator(SEL["items"]).nth(index)
    try:
        username = (await item.locator(SEL["username"]).first.inner_text(timeout=500)).strip()
    except Exception:
        return None
    if not username:
        return None

    preview = ""
    revenue_raw = ""
    try:
        preview = (await item.locator(SEL["preview"]).first.inner_text(timeout=500)).strip()
    except Exception:
        pass
    try:
        revenue_raw = (await item.locator(SEL["revenue"]).first.inner_text(timeout=500)).strip()
    except Exception:
        pass

    return ChatItem(
        username=username,
        preview=preview,
        revenue=parse_revenue(revenue_raw),
    )

# ─── Main processing logic ────────────────────────────────────────────────────

async def process_chat(
    page: Page,
    item: ChatItem,
    account: Account,
    client: openai.OpenAI,
    dry_run: bool,
    history_limit: int = 20,
) -> bool:
    """
    Open a chat, generate and optionally send a reply.
    Returns True if a message was sent (or would be sent in dry-run).
    """
    loc = page.locator(SEL["items"])
    count = await loc.count()

    # Find and click the item
    clicked = False
    for i in range(count):
        try:
            uname = (await loc.nth(i).locator(SEL["username"]).first.inner_text(timeout=400)).strip()
            if uname == item.username:
                it = loc.nth(i)
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

    history = await get_chat_history(page, history_limit)
    if not history:
        print("  [SKIP] Leerer Verlauf")
        await navigate_back(page)
        return False

    trailing = trailing_own_messages(history)
    if trailing >= 10:
        print(f"  [SKIP] {trailing}x hintereinander geschrieben")
        log_event({
            "kind": "skip", "account": account.name,
            "username": item.username, "trailing": trailing,
        })
        await navigate_back(page)
        return False

    reply = get_ai_reply(history, item.username, account.name, item.revenue, client)
    if not reply:
        print("  [SKIP] Kein AI-Reply")
        await navigate_back(page)
        return False

    user_type = classify_user(history, item.revenue)
    prefix = "[DRY] " if dry_run else ""
    print(f"  {user_type} | {prefix}{reply[:90]}")

    log_event({
        "kind": "draft", "account": account.name,
        "username": item.username, "reply": reply,
        "type": user_type, "dry_run": dry_run,
    })

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


async def collect_all_chat_items(page: Page) -> list[ChatItem]:
    """
    Scroll through the full chat list and collect all visible items.
    Stops when no new items appear after 3 scroll attempts.
    """
    seen: set[str] = set()
    items: list[ChatItem] = []
    no_new_streak = 0

    while True:
        locs = page.locator(SEL["items"])
        count = await locs.count()
        found_new = False

        for i in range(count):
            chat = await read_chat_item(page, i)
            if chat and chat.username not in seen:
                seen.add(chat.username)
                items.append(chat)
                found_new = True

        if not found_new:
            no_new_streak += 1
            if no_new_streak >= 3:
                break
        else:
            no_new_streak = 0

        await scroll_chat_list_down(page)

    return items


async def check_for_replies(
    page: Page,
    snapshots: dict[str, str],
    account: Account,
    client: openai.OpenAI,
    dry_run: bool,
) -> list[str]:
    """
    Compare current chat previews against snapshots to detect new user replies.
    Returns list of usernames who replied.
    """
    replied: list[str] = []
    seen: set[str] = set()
    no_new_streak = 0
    prev_count = 0

    while True:
        locs = page.locator(SEL["items"])
        count = await locs.count()

        for i in range(count):
            chat = await read_chat_item(page, i)
            if not chat or chat.username in seen:
                continue
            seen.add(chat.username)
            if chat.username in snapshots and chat.preview != snapshots[chat.username]:
                replied.append(chat.username)
                print(f"  ↩ Neue Antwort von {chat.username}")

        if count <= prev_count:
            no_new_streak += 1
        else:
            no_new_streak = 0

        if no_new_streak >= 3:
            break

        prev_count = count
        await scroll_chat_list_down(page)

    # Now respond to each reply
    for username in replied:
        await page.evaluate(SCROLL_TOP_JS)
        await page.wait_for_timeout(500)

        # Find and open the chat
        found = False
        for _ in range(12):
            locs = page.locator(SEL["items"])
            count = await locs.count()
            for i in range(count):
                try:
                    uname = (
                        await locs.nth(i).locator(SEL["username"]).first.inner_text(timeout=400)
                    ).strip()
                    if uname == username:
                        it = locs.nth(i)
                        await it.scroll_into_view_if_needed()
                        await it.click(timeout=2000)
                        await page.wait_for_timeout(1800)
                        found = True
                        break
                except Exception:
                    pass
            if found:
                break
            await scroll_chat_list_down(page)

        if not found:
            print(f"  [WARN] Chat für {username} nicht gefunden")
            continue

        history = await get_chat_history(page, 20)
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

# ─── Runner ───────────────────────────────────────────────────────────────────

async def run(account: Account, dry_run: bool, once: bool) -> None:
    runtime = load_json(ROOT / "config" / "runtime.json", {})
    poll_seconds = int(runtime.get("pollSeconds", 120))
    client = make_ai_client()

    crash_delay = 20
    max_crash_delay = 300
    print(f"[{account.name.upper()}] Start")

    while True:
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=False)
                ctx: BrowserContext = await browser.new_context(
                    storage_state=str(account.storage_state),
                    locale="de-DE",
                )
                page = await ctx.new_page()

                await page.goto("https://4based.com", wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                await dismiss_consent_banner(page)
                await page.click(SEL["nav"], timeout=10000)
                await page.wait_for_timeout(2000)
                await page.locator("chat-overview").wait_for(timeout=10000)

                crash_delay = 20  # Reset backoff on successful start
                cycle = 0

                while True:
                    cycle += 1
                    print(f"\n[{account.name.upper()}] CYCLE {cycle} | {now_utc().strftime('%H:%M:%S')}")

                    # Collect all chats and their current previews
                    all_chats = await collect_all_chat_items(page)
                    snapshots = {c.username: c.preview for c in all_chats}

                    sent = skip = 0
                    for chat in all_chats:
                        label = "[NEU]" if chat.revenue == 0 else f"[${chat.revenue:.0f}]"
                        print(f"  → {chat.username} {label}", end=" ", flush=True)
                        limit = 100 if chat.revenue > 20 else 20
                        ok = await process_chat(page, chat, account, client, dry_run, limit)
                        if ok:
                            sent += 1
                        else:
                            skip += 1

                    print(
                        f"\n[{account.name.upper()}] Fertig | "
                        f"Gesendet: {sent} | Geskippt: {skip} | Besucht: {len(all_chats)}"
                    )

                    # Check for replies that came in during the cycle
                    replied = await check_for_replies(page, snapshots, account, client, dry_run)

                    log_event({
                        "kind": "cycle_complete",
                        "account": account.name,
                        "cycle": cycle,
                        "sent": sent,
                        "skipped": skip,
                        "visited": len(all_chats),
                        "replies": len(replied),
                        "dry_run": dry_run,
                    })

                    if once:
                        break

                    print(f"[{account.name.upper()}] Warte {poll_seconds}s...")
                    await asyncio.sleep(poll_seconds)

                await ctx.close()
                await browser.close()
                break  # Clean exit

        except asyncio.CancelledError:
            print(f"\n[{account.name.upper()}] Gestoppt (CancelledError)")
            break
        except Exception as e:
            print(f"\n[{account.name.upper()}] CRASH: {e}")
            log_event({"kind": "crash", "account": account.name, "error": str(e)})
            if once:
                break
            print(f"[{account.name.upper()}] Neustart in {crash_delay}s...")
            await asyncio.sleep(crash_delay)
            crash_delay = min(crash_delay * 2, max_crash_delay)  # Exponential backoff


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(description="4Based live runner")
    parser.add_argument("--dry-run", action="store_true", help="Keine Nachrichten senden")
    parser.add_argument("--once", action="store_true", help="Nur einen Zyklus")
    parser.add_argument(
        "--account",
        choices=["tia", "hilda", "both"],
        default="both",
        help="Welches Konto(en) laufen lassen",
    )
    args = parser.parse_args()

    accounts = {
        "tia":   load_account("tia",   "tia.storage.json"),
        "hilda": load_account("hilda", "hilda.storage.json"),
    }

    # Graceful shutdown on Ctrl+C
    loop = asyncio.get_event_loop()
    tasks: list[asyncio.Task] = []

    def shutdown(*_):
        print("\n[STOP] Beende alle Tasks...")
        for t in tasks:
            t.cancel()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if args.account == "both":
        tasks = [
            asyncio.create_task(run(accounts["hilda"], args.dry_run, args.once)),
            asyncio.create_task(run(accounts["tia"],   args.dry_run, args.once)),
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
    else:
        await run(accounts[args.account], args.dry_run, args.once)


if __name__ == "__main__":
    asyncio.run(main())
