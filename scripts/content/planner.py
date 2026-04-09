"""
planner.py — Autonomer Content-Plan Generator

Erstellt täglich einen vollständigen Content-Plan:
  - 10-12 Posts/Tag über 5 Zeitfenster (morgen→nacht) mit Spannungsaufbau
  - Max. 3 Paid-Posts/Tag (nur abend/spaet_abend/nacht)
  - 4-6 Massennachrichten/Tag, max. 2 paid (Käufer / Nicht-Käufer)
  - Mood-basierte Captions (kein festes Hook→CTA-Schema)
  - Basiert auf Content-Analyse + Marketing-Recherche

Optimierungen:
  - Parallele Caption + Hashtag Generierung mit ThreadPoolExecutor
  - Parallele Massennachrichten-Generierung
"""

import json
import random
import sys as _sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# Sicherstellen dass scripts/ im Suchpfad ist (auch bei direktem Aufruf)
_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in _sys.path:
    _sys.path.insert(0, str(_SCRIPTS))

from shared.ai_client import chat, parse_json_from_response, clean_reply
from shared.personas import PERSONAS
from content.analyzer import ContentScore, load_analysis
from content.researcher import MarketingInsights, load_insights

from shared.config import ROOT

# 5 Zeitfenster mit emotionalem Tagesbogen
TIME_ZONES = [
    {"name": "morgen",      "hours": [8, 9, 10, 11, 12],       "label": "warm, sanft, BTS/Lifestyle"},
    {"name": "mittag",      "hours": [14, 15, 16, 17],          "label": "Teaser, Andeutungen, Neugier"},
    {"name": "abend",       "hours": [18, 19, 20, 21],          "label": "intensiv, verführerisch"},
    {"name": "spaet_abend", "hours": [22, 23],                  "label": "Höhepunkte, PPV/Gutscheine"},
    {"name": "nacht",       "hours": [0, 1, 2, 3, 4, 5, 6, 7], "label": "sehr intensiv, sehr intim"},
]

# Post-Verteilung je Zeitfenster (für 10/11/12 Posts)
ZONE_DISTRIBUTION = {
    10: {"morgen": 2, "mittag": 2, "abend": 2, "spaet_abend": 2, "nacht": 2},
    11: {"morgen": 3, "mittag": 2, "abend": 2, "spaet_abend": 2, "nacht": 2},
    12: {"morgen": 3, "mittag": 2, "abend": 3, "spaet_abend": 2, "nacht": 2},
}

# Fester Massennachricht-Schedule: (Stunde, Zielgruppe, is_paid)
# Exakt 2 paid-Nachrichten (22 Uhr Käufer-PPV, 00 Uhr Nicht-Käufer-Gutschein)
MASS_MSG_SCHEDULE = [
    (18, "non_buyer", False),   # warm, Bezug auf Tages-Posts
    (20, "non_buyer", False),   # Beziehungs-/Nähe-Nachricht
    (22, "buyer",     True),    # paid_1: PPV/Stammkunden-Angebot
    (0,  "non_buyer", True),    # paid_2: Gutschein für Nicht-Käufer
    (4,  "non_buyer", False),   # nächtlicher Flirt/Nähe
]

POST_WORKERS = 6   # Parallele LLM-Calls für Posts
MSG_WORKERS  = 4   # Parallele LLM-Calls für Massennachrichten

# ─── Data types ───────────────────────────────────────────────────────────────

@dataclass
class Post:
    time:      str
    file:      str
    score:     int
    post_type: str          # "free" | "paid"
    caption:   str
    hashtags:  list[str]
    best_for:  list[str]
    mood:      str = ""     # "morgen"|"mittag"|"abend"|"spaet_abend"|"nacht"


@dataclass
class MassMessage:
    time:            str
    target:          str    # "buyer" | "non_buyer"
    text:            str
    include_voucher: bool


@dataclass
class DayPlan:
    date:          str
    persona:       str
    posts:         list[Post]
    mass_messages: list[MassMessage]
    notes:         str

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _build_zone_schedule(posts_per_day: int) -> list[tuple[str, str]]:
    """
    Erstellt eine sortierte Liste von (time_str, mood_name) für alle Posts.
    Verteilt Posts über 5 Zeitfenster gemäß ZONE_DISTRIBUTION.
    Sortierung: 08–23 Uhr vor 00–07 Uhr (natürlicher Tagesbogen).
    """
    dist = ZONE_DISTRIBUTION.get(posts_per_day)
    if not dist:
        # Proportionaler Fallback für andere Post-Anzahlen
        ratios = [0.25, 0.17, 0.25, 0.17, 0.17]
        counts = [max(1, round(posts_per_day * r)) for r in ratios]
        while sum(counts) > posts_per_day:
            counts[counts.index(max(counts))] -= 1
        while sum(counts) < posts_per_day:
            counts[counts.index(min(counts))] += 1
        dist = {z["name"]: c for z, c in zip(TIME_ZONES, counts)}

    result: list[tuple[str, str]] = []
    for zone in TIME_ZONES:
        count = dist.get(zone["name"], 0)
        hours = zone["hours"]
        # Stunden wählen — bei Überlauf wiederverwenden mit Minuten-Versatz
        if count <= len(hours):
            chosen = sorted(random.sample(hours, count))
        else:
            chosen = sorted((hours * ((count // len(hours)) + 1))[:count])

        hour_counts: dict[int, int] = {}
        for h in chosen:
            idx    = hour_counts.get(h, 0)
            minute = (idx * 20) % 60
            result.append((f"{h:02d}:{minute:02d}", zone["name"]))
            hour_counts[h] = idx + 1

    # Tageschronologisch sortieren: 08–23 vor 00–07
    result.sort(key=lambda x: int(x[0][:2]) if int(x[0][:2]) >= 8 else int(x[0][:2]) + 24)
    return result

# ─── Caption & Hashtag Generator ──────────────────────────────────────────────

# 12 verschiedene Blickwinkel — damit wiederholter Content nicht erkennbar ist
CAPTION_ANGLES = [
    "Mysteriöse Andeutung — Neugier wecken ohne zu viel zu zeigen",
    "Direkte Provokation — freche Challenge oder Aussage",
    "Persönliche Story — als ob du gerade etwas Privates teilst",
    "FOMO — andere verpassen das, du bist dabei",
    "Frage an Follower — zum Kommentieren animieren",
    "Exklusivität betonen — nur für die die wirklich dabei sind",
    "Humor/Selbstironie — leichter witziger Ton",
    "Emotionaler Hook — kurze ehrliche Aussage die berührt",
    "Behind-the-Scenes — Blick hinter die Kulissen",
    "Urgency/Countdown — limitiert, jetzt oder nie",
    "Community-Kompliment — Fans fühlen sich besonders",
    "Cliffhanger — Fortsetzung folgt, was passiert als nächstes",
]

HASHTAG_SYSTEM = """Generiere 5-8 relevante Hashtags für einen deutschen Content-Creator Post.
Antworte NUR mit einem JSON-Array, kein anderer Text davor oder danach.
Format: ["#tag1", "#tag2", "#tag3"]
Regeln: Mix aus nischig und mittelgroß. Keine extrem generischen wie #love #beautiful #instagood."""

# Stimmungsbasierte Caption-Anleitung je Tageszeit (kein festes Schema)
MOOD_CAPTION_STYLE: dict[str, str] = {
    "morgen": (
        "Es ist Morgen — Hilda beginnt ihren Tag. "
        "Schreib eine warme, natürliche Caption — wie ein guter Morgengruß an einen Vertrauten. "
        "Sanft, nahbar, leicht schüchtern. Kein Verkauf, kein Druck."
    ),
    "mittag": (
        "Nachmittagsstimmung — etwas baut sich auf. "
        "Schreib eine neugierig machende Caption, die andeutet ohne aufzulösen. "
        "Leichter Cliffhanger. Neugier wecken, nichts verraten."
    ),
    "abend": (
        "Abendstimmung — es wird intensiver. "
        "Schreib eine verführerische, persönliche Caption. "
        "Einladend, leicht intensiv — bei paid Posts ein sanfter Hinweis auf etwas Besonderes, ohne Preis."
    ),
    "spaet_abend": (
        "Später Abend — Höhepunkt des Tages. "
        "Schreib eine intensive, warme Caption. "
        "Bei paid Posts: dezente Einladung zu exklusivem Content — ohne Preis zu nennen."
    ),
    "nacht": (
        "Nacht — sehr intim, sehr persönlich. "
        "Schreib wie ein Flüstern. Sehr nah, sehr privat, sehr romantisch. "
        "Als würde Hilda nur mit dieser einen Person sprechen."
    ),
}


def generate_caption(
    score:        ContentScore,
    persona_name: str,
    post_type:    str,
    tip:          str,
    angle:        str = "",
    mood:         str = "",
) -> str:
    p = PERSONAS[persona_name]
    mood_guidance = MOOD_CAPTION_STYLE.get(mood, "")

    caption_system = (
        f"Du bist {p['name']}, {p['age']}J., Content-Creatorin auf 4Based.\n"
        f"Persönlichkeit: {p['style']}\n"
        f"{p['content_style']}\n"
        "SPRACHE: Antworte IMMER auf Deutsch. Niemals auf Englisch.\n"
        "AUFGABE: Schreib eine Caption die sich natürlich anfühlt — kein festes Schema, kein Verkaufsdruck.\n"
        f"{mood_guidance}\n"
        "VERBOTEN: Preise nennen, feste Strukturen wie Hook-Teaser-CTA, generische Phrasen, englische Wörter.\n"
        "Kein Markdown. Keine Präfixe. Max 3 Sätze.\n"
        "Antworte NUR mit der fertigen Caption."
    )

    angle_line = f"Blickwinkel: {angle}\n" if angle else ""
    tip_line   = f"Ansatz: {tip}\n" if tip else ""
    prompt = (
        f"Content-Typ: {score.type} | Post-Typ: {post_type} | Kategorie: {score.content_category}\n"
        f"Was macht diesen Content besonders: {', '.join(score.strengths)}\n"
        f"Inspiration: {score.hook_idea}\n"
        f"{tip_line}"
        f"{angle_line}"
        f"Schreib jetzt eine Caption als {p['name']}:"
    )
    result = clean_reply(chat(caption_system, prompt, purpose="plan", max_tokens=120))
    return result or score.caption_idea or f"Neuer Content von {p['name']} 🌸"


def generate_hashtags(score: ContentScore, persona_name: str) -> list[str]:
    import re
    p      = PERSONAS[persona_name]
    prompt = (
        f"Persona: {p['name']}, Stil: {p['style']}\n"
        f"Content-Kategorie: {score.content_category}, Typ: {score.type}\n"
        f"Stärken: {', '.join(score.strengths)}\n"
        f"Generiere 5-8 passende deutsche Hashtags als JSON-Array:"
    )
    raw  = chat(HASHTAG_SYSTEM, prompt, purpose="plan", max_tokens=150)
    data = parse_json_from_response(raw) if raw else None

    if isinstance(data, list) and data:
        return [str(t) for t in data if str(t).startswith("#")]

    # Regex-Fallback: alle #tags direkt aus dem Text ziehen
    if raw:
        tags = re.findall(r"#\w+", raw)
        if tags:
            return tags[:8]

    return []

# ─── Mass Message Generator ───────────────────────────────────────────────────

MASS_MSG_SYSTEM = """Du schreibst Massennachrichten für eine Content-Creator auf einer Abo-Plattform.
Kurz, persönlich wirkend, max 2 Sätze. Kein Markdown. Kein Username am Anfang."""


def generate_mass_message(
    persona_name:    str,
    target:          str,
    context:         str,
    include_voucher: bool,
) -> str:
    p = PERSONAS[persona_name]
    voucher_hint = (
        f" Erwähne subtil den {p['voucher_pct']}% Gutschein-Code."
        if include_voucher else ""
    )
    system = p["mass_msg"][target]
    prompt = (
        f"Kontext: {context}\n"
        f"Zielgruppe: {'bestehende Käufer' if target == 'buyer' else 'noch nicht gekauft'}\n"
        f"{voucher_hint}\n"
        f"Schreibe die Massennachricht als {p['name']}:"
    )
    result = chat(system, prompt, purpose="plan", max_tokens=80)
    return result or (
        f"Hey, schau dir meinen neuen Content an 🔥" if target == "non_buyer"
        else f"Danke für deine Unterstützung ❤️ Neues für dich!"
    )

# ─── Plan Builder ─────────────────────────────────────────────────────────────

def build_post_schedule(
    content:        list[ContentScore],
    persona_name:   str,
    tips:           list[str],
    posts_per_day:  int,
    max_paid_posts: int = 3,
) -> list[Post]:
    # Nach Persona-Fit + Score sortieren
    scored = sorted(
        content,
        key=lambda x: x.persona_fit.get(persona_name, 5) * 2 + x.score,
        reverse=True,
    )

    schedule = _build_zone_schedule(posts_per_day)

    # Paid-Slots aus abend/spaet_abend/nacht wählen (max. max_paid_posts)
    paid_zones = {"abend", "spaet_abend", "nacht"}
    eligible   = [i for i, (_, mood) in enumerate(schedule) if mood in paid_zones]
    paid_idx   = set(random.sample(eligible, min(max_paid_posts, len(eligible))))

    # Aufgaben vorbereiten — Winkel rotieren bei wiederholtem Content
    file_use_count: dict[str, int] = {}
    work_items = []
    for i, (time_str, mood) in enumerate(schedule):
        content_item = scored[i % len(scored)]
        post_type    = "paid" if i in paid_idx else "free"
        use_count    = file_use_count.get(content_item.file, 0)
        angle        = CAPTION_ANGLES[(i + use_count) % len(CAPTION_ANGLES)]
        tip_idx      = (i + use_count * 3) % len(tips) if tips else 0
        tip          = tips[tip_idx] if tips else "authentisch sein"
        file_use_count[content_item.file] = use_count + 1
        work_items.append((i, content_item, post_type, time_str, mood, tip, angle))

    def _generate_post(args) -> tuple[int, Post]:
        idx, content_item, post_type, time_str, mood, tip, angle = args
        caption  = generate_caption(content_item, persona_name, post_type, tip, angle, mood)
        hashtags = generate_hashtags(content_item, persona_name)
        print(f"  Caption [{idx+1}/{posts_per_day}] {time_str} [{mood}] ({post_type}) ✓")
        return idx, Post(
            time      = time_str,
            file      = content_item.file,
            score     = content_item.score,
            post_type = post_type,
            caption   = caption,
            hashtags  = hashtags,
            best_for  = content_item.best_for,
            mood      = mood,
        )

    # Alle Caption+Hashtag Calls parallel
    results: dict[int, Post] = {}
    with ThreadPoolExecutor(max_workers=POST_WORKERS) as ex:
        futures = {ex.submit(_generate_post, item): item[0] for item in work_items}
        for future in as_completed(futures):
            idx, post = future.result()
            results[idx] = post

    return [results[i] for i in range(posts_per_day)]


def build_mass_messages(
    persona_name: str,
    tips:         list[str],
    count:        int,
) -> list[MassMessage]:
    # Festen Schedule verwenden (bis zu count Einträge, max. 2 paid)
    schedule = MASS_MSG_SCHEDULE[:count]

    def _generate_msg(args) -> tuple[int, MassMessage]:
        idx, (hour, target, is_paid) = args
        time_str = f"{hour:02d}:00"
        context  = tips[idx % len(tips)] if tips else "neuen Content teilen"
        text     = generate_mass_message(persona_name, target, context, include_voucher=is_paid)
        return idx, MassMessage(
            time            = time_str,
            target          = target,
            text            = text,
            include_voucher = is_paid,
        )

    results: dict[int, MassMessage] = {}
    with ThreadPoolExecutor(max_workers=MSG_WORKERS) as ex:
        futures = {ex.submit(_generate_msg, (i, sched)): i for i, sched in enumerate(schedule)}
        for future in as_completed(futures):
            idx, msg = future.result()
            results[idx] = msg

    return [results[i] for i in range(len(schedule))]

# ─── Main Plan Function ───────────────────────────────────────────────────────

def create_day_plan(
    persona_name:   str,
    content:        list[ContentScore],
    insights:       Optional[MarketingInsights] = None,
    posts_per_day:  int = 12,
    mass_msg_count: int = 5,
    max_paid_posts: int = 3,
    date:           Optional[str] = None,
    **_kwargs,      # ignoriert unbekannte Parameter (z.B. client= vom Orchestrator)
) -> DayPlan:
    today = date or datetime.now().strftime("%Y-%m-%d")
    tips  = (insights.top_tips if insights else []) or ["authentisch sein", "Fragen stellen", "Exklusivität betonen"]

    # Content nach Persona filtern
    persona_content = [ct for ct in content if ct.persona_fit.get(persona_name, 5) >= 6]
    if not persona_content:
        print(f"  [WARN] Kein Content mit Persona-Fit ≥ 6 für {persona_name} — nutze alle")
        persona_content = content

    print(f"\n[PLANNER] {persona_name.upper()} — {today}")
    print(f"  {len(persona_content)} passende Inhalte | {posts_per_day} Posts | {mass_msg_count} Massennachrichten | max. {max_paid_posts} paid")

    posts         = build_post_schedule(persona_content, persona_name, tips, posts_per_day, max_paid_posts)
    mass_messages = build_mass_messages(persona_name, tips, mass_msg_count)

    strat = ""
    if insights:
        strat = (insights.hilda_strategy if persona_name == "hilda" else insights.tia_strategy)[:300]

    paid_count = sum(1 for p in posts if p.post_type == "paid")
    notes = f"{len(posts)} Posts ({paid_count} paid), {mass_msg_count} Massennachrichten. {strat}"

    return DayPlan(
        date          = today,
        persona       = persona_name,
        posts         = posts,
        mass_messages = mass_messages,
        notes         = notes,
    )

# ─── Save / Load ──────────────────────────────────────────────────────────────

def save_plan(plan: DayPlan, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(asdict(plan), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[PLANNER] Gespeichert: {output}")


def load_plan(path: Path) -> Optional[DayPlan]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["posts"]         = [Post(**{k: v for k, v in p.items() if k in Post.__dataclass_fields__})
                                  for p in data.get("posts", [])]
        data["mass_messages"] = [MassMessage(**{k: v for k, v in m.items() if k in MassMessage.__dataclass_fields__})
                                  for m in data.get("mass_messages", [])]
        return DayPlan(**{k: v for k, v in data.items() if k in DayPlan.__dataclass_fields__})
    except Exception as e:
        print(f"[PLANNER] Laden fehlgeschlagen: {e}")
        return None


def save_mass_messages(plan: DayPlan, output_dir: Path) -> None:
    """
    Speichert jede Massennachricht als eigene .txt-Datei.
    Pfad: output_dir/Massennachrichten/{plan.date}_{Wochentag}_{persona}/
    """
    from shared.constants import DAYS
    weekday  = datetime.strptime(plan.date, "%Y-%m-%d").weekday()
    day_name = DAYS[weekday]
    day_dir  = output_dir / "Massennachrichten" / f"{plan.date}_{day_name}_{plan.persona}"
    day_dir.mkdir(parents=True, exist_ok=True)

    paid_count = 0
    for msg in plan.mass_messages:
        is_paid = msg.include_voucher
        if is_paid:
            paid_count += 1
            paid_suffix = f"_paid_{paid_count}"
        else:
            paid_suffix = ""

        time_str  = msg.time.replace(":", "")
        base_name = f"mass_{msg.target}_{time_str}{paid_suffix}.txt"
        out_file  = day_dir / base_name

        lines  = [f"Zeit: {msg.time}", f"Ziel: {msg.target}"]
        if msg.include_voucher:
            lines.append("Enthält Gutschein: JA")
        lines += ["", msg.text]

        out_file.write_text("\n".join(lines), encoding="utf-8")
        print(f"  [MASS] Gespeichert: {base_name} (Paid={is_paid})")


def plan_to_readable_text(plan: DayPlan) -> str:
    lines = [
        f"CONTENT PLAN — {plan.persona.upper()} — {plan.date}",
        "=" * 60,
        f"Notizen: {plan.notes}",
        "",
        f"POSTS ({len(plan.posts)}):",
    ]
    for p in plan.posts:
        paid       = "[PAID] " if p.post_type == "paid" else "       "
        mood_label = f" [{p.mood}]" if p.mood else ""
        lines += [
            f"  {p.time}{mood_label} {paid}Score:{p.score}/10",
            f"  Caption:  {p.caption}",
            f"  Hashtags: {' '.join(p.hashtags)}",
            f"  Datei:    {Path(p.file).name}",
            "",
        ]
    lines += ["", f"MASSENNACHRICHTEN ({len(plan.mass_messages)}):"]
    for m in plan.mass_messages:
        voucher = " [VOUCHER]" if m.include_voucher else ""
        lines += [
            f"  {m.time} [{m.target.upper()}]{voucher}",
            f"  Text: {m.text}",
            "",
        ]
    return "\n".join(lines)


def print_plan(plan: DayPlan) -> None:
    print(plan_to_readable_text(plan))


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Content Planner")
    ap.add_argument("--test", action="store_true", help="Minimal-Test ohne Mediendateien")
    ap.add_argument("--persona",    choices=["hilda", "tia", "both"], default="both")
    ap.add_argument("--analysis",   default=None)
    ap.add_argument("--insights",   default=None)
    ap.add_argument("--posts",     type=int, default=12)
    ap.add_argument("--mass-msgs", type=int, default=5)
    ap.add_argument("--max-paid",  type=int, default=3, dest="max_paid")
    ap.add_argument("--output-dir", default=None)
    args = ap.parse_args()

    # ── Minimal-Test ohne Mediendateien ──────────────────────────────────────
    if args.test:
        from content.analyzer import ContentScore
        mock = ContentScore(
            file             = "test_foto.jpg",
            type             = "image",
            score            = 8,
            production_score = 7,
            erotic_score     = 5,
            strengths        = ["gute Beleuchtung", "natürlicher Look"],
            weaknesses       = [],
            placement        = "free_teaser",
            best_for         = ["NEU", "KALT"],
            content_category = "lifestyle",
            caption_idea     = "Ein entspannter Abend",
            hook_idea        = "So sehe ich aus wenn...",
            persona_fit      = {"hilda": 9, "tia": 6},
            video_meta       = {},
        )
        persona = args.persona if args.persona != "both" else "hilda"
        print(f"\n=== Caption Test ({persona}) ===")
        cap = generate_caption(mock, persona, "free", "", "", "morgen")
        print(f"  → {cap}")
        print(f"\n=== Hashtag Test ({persona}) ===")
        tags = generate_hashtags(mock, persona)
        print(f"  → {tags}")
        print("\n✓ Planner-Test fertig")
        raise SystemExit(0)
    # ─────────────────────────────────────────────────────────────────────────

    out_dir  = Path(args.output_dir) if args.output_dir else ROOT / "data" / "Plan für die nächsten Tage"
    analysis = load_analysis(Path(args.analysis)) if args.analysis else []
    insights = load_insights(Path(args.insights)) if args.insights else None

    if not analysis:
        raise SystemExit("[ERROR] Keine Content-Analyse. Zuerst analyzer.py ausführen.")

    from shared.constants import DAYS
    personas = ["hilda", "tia"] if args.persona == "both" else [args.persona]
    today    = datetime.now().strftime("%Y-%m-%d")
    day      = DAYS[datetime.now().weekday()]

    for p_name in personas:
        plan     = create_day_plan(p_name, analysis, insights, args.posts, args.mass_msgs, args.max_paid)
        out_file = out_dir / p_name.capitalize() / f"{today}_{day}" / f"plan_{p_name}_{today}.json"
        save_plan(plan, out_file)
        out_file.with_suffix(".txt").write_text(plan_to_readable_text(plan), encoding="utf-8")
        print_plan(plan)
        save_mass_messages(plan, out_dir)
