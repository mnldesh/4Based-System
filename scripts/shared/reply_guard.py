"""
reply_guard.py — Regelbasierter Qualitätsfilter für generierte Antworten

Prüft jede Antwort VOR dem Versand auf:
  - Leer / zu kurz
  - Kauderwelsch / kaputt
  - Zu früh sexuell (je nach User-Segment)
  - Zu früh Sales/Angebot (je nach User-Segment)
  - Zu generisch (Floskel-Blacklist)
  - Zu ähnlich zur letzten Nachricht (Jaccard-Ähnlichkeit)

Gibt (ok: bool, reason: str) zurück. Kein KI-Aufruf.
"""

import re

# ─── Verbotene Phrasen ────────────────────────────────────────────────────────

BANNED_PHRASES = [
    "ich zieh gerade",
    "ich steh unter der dusche",
    "mein bauch zittert",
    "was würdest du jetzt tun",
    "nur für dich",
    "du erregst mich",
    "ich stelle mir vor",
    "ich spüre wie",
    "ich spüre dass",
    "ich kann das geräusch",
    "die tür ist nur halb",
    "hast du heute schon daran gedacht",
    "ich wette du fragst dich",
    "ich hab mir gemerkt",
    "ich habe gehört",
    "meine finger wandern",
    "meine lippen sind",
    "ich trage gerade nur",
    "unter meinem kleid",
    "mein herz schlägt schneller",
    "es kribbelt",
]

# ─── Zu explizit für kalte Segmente ──────────────────────────────────────────

_EXPLICIT_WORDS = [
    "nackt", "sex", "ficken", "blasen", "schwanz", "pussy",
    "brüste", "titten", "feucht", "geil", "erregt", "orgasmus",
]

_COLD_SEGMENTS = {"NEU", "KALT", "KALT_HART"}

# ─── Zu früh Sales für kalte Segmente ────────────────────────────────────────

_SALES_WORDS = [
    "gutschein", "rabatt", "% off", "% rabatt", "code:", "kaufen",
    "bestellen", "content kaufen", "schau in mein profil",
    "link in bio", "nur heute", "limited", "exklusiv für dich",
]

# ─── Generische Floskeln ──────────────────────────────────────────────────────

_GENERIC_PHRASES = [
    "wie kann ich dir helfen",
    "ich bin hier für dich",
    "was beschäftigt dich",
    "lass uns reden",
    "schreib mir gerne",
    "freue mich von dir zu hören",
    "ich hoffe du hast einen schönen tag",
    "alles gut bei dir",
    "wie geht es dir heute",
    "was machst du gerade so",
]

# ─── Kauderwelsch-Erkennung ───────────────────────────────────────────────────
_RE_GARBLED   = re.compile(r"[a-zäöüß]{18,}", re.IGNORECASE)
_RE_NON_LATIN = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\u3400-\u4dbf]")
_RE_ONLY_PUNCT = re.compile(r"^[\W\d_]+$")


# ─── Ähnlichkeits-Check ───────────────────────────────────────────────────────

def _jaccard(a: str, b: str) -> float:
    sa, sb = set(a.lower().split()), set(b.lower().split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ─── Haupt-Check ──────────────────────────────────────────────────────────────

def check(
    text:       str,
    user_type:  str,
    user_state: dict,
) -> tuple[bool, str]:
    """
    Prüft eine generierte Antwort regelbasiert.

    Args:
        text:       Die generierte Antwort
        user_type:  Segment (NEU / KALT / KALT_HART / AKTIV / KAEUFER / PREMIUM)
        user_state: Dict aus user_states[username] (enthält recent_msgs etc.)

    Returns:
        (True, "ok") wenn die Antwort OK ist
        (False, reason) wenn abgelehnt
    """
    stripped = text.strip()

    # 1. Leer oder zu kurz
    if not stripped:
        return False, "empty"
    if len(stripped) < 5:
        return False, "too_short"

    # 2. Nur Satzzeichen / Emojis übrig
    alpha = sum(1 for c in stripped if c.isalpha())
    if alpha < 3:
        return False, "no_alpha"

    # 3. Kauderwelsch: sehr lange Zeichenketten ohne Leerzeichen
    if _RE_GARBLED.search(stripped):
        return False, "garbled"

    # 4. Fremdzeichen (CJK-Leak von qwen-Modellen)
    if _RE_NON_LATIN.search(stripped):
        return False, "foreign_chars"

    lower = stripped.lower()

    # 5. Verbotene Phrasen
    for phrase in BANNED_PHRASES:
        if phrase in lower:
            return False, f"banned_phrase:{phrase[:30]}"

    # 6. Zu explizit für kalte Segmente
    if user_type in _COLD_SEGMENTS:
        for w in _EXPLICIT_WORDS:
            if w in lower:
                return False, f"too_explicit_for_{user_type}"

    # 7. Zu früh Sales für kalte Segmente
    if user_type in _COLD_SEGMENTS:
        for w in _SALES_WORDS:
            if w in lower:
                return False, f"premature_sales_for_{user_type}"

    # 8. Generische Floskeln
    for phrase in _GENERIC_PHRASES:
        if phrase in lower:
            return False, f"generic_phrase:{phrase[:30]}"

    # 9. Zu ähnlich zu einer der letzten Nachrichten
    recent = (user_state or {}).get("recent_msgs", [])
    for old in recent[-10:]:
        if _jaccard(stripped, old) > 0.6:
            return False, "too_similar"

    return True, "ok"
