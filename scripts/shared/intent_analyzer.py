"""
intent_analyzer.py — Analyse-Zwischenschicht vor der AI-Antwort

Chatanalyse → Entscheidungslogik → persona-passende Formulierung

Ablauf:
  1. Verlauf lesen
  2. Letzte Nachricht + kurzer Kontext extrahieren
  3. Absicht klassifizieren
  4. Antwortziel festlegen
  5. Erst dann formulieren (in get_ai_reply)
"""

import json
from dataclasses import dataclass, asdict
from typing import Optional

import openai

from shared.ai_client import TEXT_MODEL, parse_json_from_response, clean_reply


# ─── Analyse-Ergebnis ────────────────────────────────────────────────────────

@dataclass
class ChatAnalysis:
    """Strukturierte Analyse einer Chat-Situation vor dem Antworten."""
    intent: str        # frage / flirt / kauf / unklar / boundary / reengage / smalltalk / sexuell
    topic: str         # worum geht es konkret (1 Satz)
    emotion: str       # neugierig / reserviert / direkt / genervt / spielerisch / geil / schüchtern
    reply_goal: str    # klären / vertiefen / spiegeln / eskalieren / verkaufen / zurückholen / necken
    persona_mode: str  # sanft / direkt / verspielt / intim / frech
    key_detail: str    # ein konkretes Detail aus der letzten User-Nachricht zum Aufgreifen
    confidence: float  # 0.0–1.0 wie sicher die Analyse ist

    def to_prompt_block(self) -> str:
        """Formatiert die Analyse als Anweisung für den Reply-Prompt."""
        return (
            f"CHAT-ANALYSE (befolge strikt):\n"
            f"- Intent: {self.intent}\n"
            f"- Thema: {self.topic}\n"
            f"- Emotion des Users: {self.emotion}\n"
            f"- Dein Antwortziel: {self.reply_goal}\n"
            f"- Dein Ton: {self.persona_mode}\n"
            f"- Aufgreifen: {self.key_detail}\n"
        )


# ─── Fallback wenn Analyse fehlschlägt ────────────────────────────────────────

def _fallback_analysis(
    last_user_msg: str,
    user_type: str,
    trailing: int,
) -> ChatAnalysis:
    """
    Regelbasierte Minimal-Analyse wenn AI-Analyse fehlschlägt.
    Besser als gar keine Steuerung.
    """
    msg = last_user_msg.lower() if last_user_msg else ""

    # Intent erkennen
    if not last_user_msg or last_user_msg == "(keine)":
        intent = "reengage"
    elif any(kw in msg for kw in ["?", "was ", "wie ", "wann", "warum", "kannst"]):
        intent = "frage"
    elif any(kw in msg for kw in ["kauf", "preis", "kosten", "video", "foto", "content", "schick"]):
        intent = "kauf"
    elif any(kw in msg for kw in ["geil", "heiß", "sexy", "nackt", "bild", "zeig"]):
        intent = "sexuell"
    elif any(kw in msg for kw in ["hey", "hi ", "hallo", "moin", "na "]):
        intent = "smalltalk"
    elif any(kw in msg for kw in ["nein", "stop", "lass", "nerv", "kein interesse", "bye"]):
        intent = "boundary"
    else:
        intent = "flirt" if user_type in ("AKTIV", "PREMIUM", "KAEUFER") else "unklar"

    # Emotion schätzen
    if intent == "boundary":
        emotion = "genervt"
    elif intent == "kauf":
        emotion = "direkt"
    elif intent == "sexuell":
        emotion = "geil"
    elif trailing > 3:
        emotion = "reserviert"
    else:
        emotion = "neugierig"

    # Antwortziel
    goal_map = {
        "frage": "klären",
        "flirt": "vertiefen",
        "kauf": "verkaufen",
        "unklar": "klären",
        "boundary": "spiegeln",
        "reengage": "zurückholen",
        "smalltalk": "vertiefen",
        "sexuell": "eskalieren",
    }
    reply_goal = goal_map.get(intent, "vertiefen")

    # Ton basierend auf User-Typ
    mode_map = {
        "NEU": "sanft",
        "KALT": "verspielt",
        "KALT_HART": "direkt",
        "AKTIV": "verspielt",
        "KAEUFER": "frech",
        "PREMIUM": "intim",
    }
    persona_mode = mode_map.get(user_type, "sanft")

    return ChatAnalysis(
        intent=intent,
        topic=last_user_msg[:80] if last_user_msg else "kein Kontext",
        emotion=emotion,
        reply_goal=reply_goal,
        persona_mode=persona_mode,
        key_detail=last_user_msg[:60] if last_user_msg and last_user_msg != "(keine)" else "nichts Konkretes",
        confidence=0.3,
    )


# ─── AI-gestützte Analyse ────────────────────────────────────────────────────

_ANALYSIS_SYSTEM = (
    "Du bist ein Chat-Analyst. Analysiere die Chat-Situation und antworte NUR mit validem JSON. "
    "Keine Erklärungen, kein Markdown, nur das JSON-Objekt."
)

_ANALYSIS_PROMPT = """Letzte 3 Nachrichten:
{recent_context}

User-Typ: {user_type} | Umsatz: ${revenue:.0f} | {trailing}x ohne Antwort

Analysiere die LETZTE User-Nachricht und antworte mit diesem JSON:
{{
  "intent": "<frage|flirt|kauf|unklar|boundary|reengage|smalltalk|sexuell>",
  "topic": "<worum geht es konkret, 1 kurzer Satz>",
  "emotion": "<neugierig|reserviert|direkt|genervt|spielerisch|geil|schüchtern>",
  "reply_goal": "<klären|vertiefen|spiegeln|eskalieren|verkaufen|zurückholen|necken>",
  "persona_mode": "<sanft|direkt|verspielt|intim|frech>",
  "key_detail": "<ein konkretes Wort/Detail aus der letzten User-Nachricht das die Antwort aufgreifen soll>"
}}

Regeln:
- intent=reengage wenn der User NICHT geantwortet hat
- intent=boundary wenn er ablehnt/genervt ist → reply_goal=spiegeln (respektieren, nicht drängeln)
- key_detail MUSS ein echtes Wort/Detail aus seiner Nachricht sein, NICHTS erfinden
- Bei leerer/keiner User-Nachricht: key_detail="nichts Konkretes"
"""


def analyze_chat(
    history: list,
    username: str,
    user_type: str,
    revenue: float,
    trailing: int,
    client: openai.OpenAI,
    model: str = TEXT_MODEL,
) -> ChatAnalysis:
    """
    Analysiert die Chat-Situation VOR dem Generieren einer Antwort.
    Gibt immer ein ChatAnalysis-Objekt zurück (AI oder Fallback).
    """
    # Letzte User-Nachricht extrahieren
    user_msgs = [m.text for m in history if m.role == "user"]
    last_user_msg = user_msgs[-1] if user_msgs else "(keine)"

    # Kontext der letzten 3 Nachrichten aufbereiten
    recent = history[-6:] if len(history) > 6 else history  # max 6 messages for context
    recent_lines = []
    for m in recent[-6:]:
        label = "Model" if m.role == "me" else "User"
        recent_lines.append(f"{label}: {m.text}")
    recent_context = "\n".join(recent_lines) if recent_lines else "(leerer Verlauf)"

    prompt = _ANALYSIS_PROMPT.format(
        recent_context=recent_context,
        user_type=user_type,
        revenue=revenue,
        trailing=trailing,
    )

    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=200,
            timeout=20,
            messages=[
                {"role": "system", "content": _ANALYSIS_SYSTEM},
                {"role": "user", "content": prompt},
            ],
        )
        raw = resp.choices[0].message.content or ""
        # Think-Tags entfernen falls vorhanden
        import re
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

        data = parse_json_from_response(raw)
        if not data:
            print(f"  [ANALYSE] JSON-Parse fehlgeschlagen → Fallback")
            return _fallback_analysis(last_user_msg, user_type, trailing)

        # Validierung: alle Felder müssen da sein
        required = ["intent", "topic", "emotion", "reply_goal", "persona_mode", "key_detail"]
        for key in required:
            if key not in data or not data[key]:
                print(f"  [ANALYSE] Feld '{key}' fehlt → Fallback")
                return _fallback_analysis(last_user_msg, user_type, trailing)

        # Intent validieren
        valid_intents = {"frage", "flirt", "kauf", "unklar", "boundary", "reengage", "smalltalk", "sexuell"}
        if data["intent"] not in valid_intents:
            data["intent"] = "unklar"

        analysis = ChatAnalysis(
            intent=data["intent"],
            topic=str(data["topic"])[:100],
            emotion=str(data["emotion"]),
            reply_goal=str(data["reply_goal"]),
            persona_mode=str(data["persona_mode"]),
            key_detail=str(data["key_detail"])[:80],
            confidence=0.8,
        )

        print(f"  [ANALYSE] {analysis.intent}/{analysis.emotion} → {analysis.reply_goal} ({analysis.persona_mode})")
        return analysis

    except Exception as e:
        print(f"  [ANALYSE] Fehler: {e} → Fallback")
        return _fallback_analysis(last_user_msg, user_type, trailing)
