"""
planner.py — Autonomer Content-Plan Generator

Erstellt täglich einen vollständigen Content-Plan:
  - 10-12 Posts/Tag (alle 3-7 Posts ein Paid-Post)
  - 3-6 Massennachrichten/Tag (getrennt: Käufer / Nicht-Käufer)
  - Passende Captions, Uhrzeiten, Hashtags
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

# Peak-Posting-Zeiten: 18:00–04:00 (Abend/Nacht) + 04:00–08:00 (früh morgens)
PEAK_HOURS     = [18, 19, 20, 21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7]
MASS_MSG_HOURS = [18, 21, 0, 4]
POST_WORKERS   = 6   # Parallele LLM-Calls für Posts
MSG_WORKERS    = 4   # Parallele LLM-Calls für Massennachrichten

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

def _spread_times(hours_pool: list[int], count: int) -> list[str]:
    """
    Wählt `count` Uhrzeiten aus dem Pool.
    Wenn count > len(pool): erlaubte Stunden wiederverwenden mit kleinem Versatz.
    Gibt sortierte "HH:MM" Strings zurück.
    """
    pool = list(hours_pool)
    if count <= len(pool):
        chosen = sorted(random.sample(pool, count))
    else:
        # Zu viele Posts → Stunden wiederverwenden, unterschiedliche Minuten
        chosen = sorted((pool * ((count // len(pool)) + 1))[:count])

    result = []
    hour_counts: dict[int, int] = {}
    for h in chosen:
        idx    = hour_counts.get(h, 0)
        minute = (idx * 20) % 60   # 0, 20, 40, dann wieder 0
        result.append(f"{h:02d}:{minute:02d}")
        hour_counts[h] = idx + 1

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

CAPTION_SYSTEM = """Du bist ein Social-Media-Texter für Content-Creator auf Abo-Plattformen.
SPRACHE: Antworte IMMER auf Deutsch. Niemals auf Englisch oder einer anderen Sprache.
Schreibe eine kurze, konvertierende Caption im Stil der Persona.
Max 2 Sätze. Kein Markdown. Keine generischen Phrasen. Keine Präfixe wie "Caption:".
Antworte NUR mit der fertigen Caption."""

HASHTAG_SYSTEM = """Generiere 5-8 relevante Hashtags für einen deutschen Content-Creator Post.
Antworte NUR mit einem JSON-Array, kein anderer Text davor oder danach.
Format: ["#tag1", "#tag2", "#tag3"]
Regeln: Mix aus nischig und mittelgroß. Keine extrem generischen wie #love #beautiful #instagood."""


def generate_caption(
    score:        ContentScore,
    persona_name: str,
    post_type:    str,
    tip:          str,
    angle:        str = "",
) -> str:
    p          = PERSONAS[persona_name]
    angle_line = f"Blickwinkel: {angle}\n" if angle else ""
    tip_line   = f"Marketing-Tipp einbauen: {tip}\n" if tip else ""
    prompt = (
        f"Persona: {p['name']}, {p['age']}J., Stil: {p['style']}\n"
        f"Content-Typ: {score.type} | Post-Typ: {post_type}\n"
        f"Content-Stärken: {', '.join(score.strengths)}\n"
        f"Persona-Stil: {p['content_style']}\n"
        f"{tip_line}"
        f"{angle_line}"
        f"Caption-Idee als Basis: {score.caption_idea}\n\n"
        f"Schreibe die finale Caption als {p['name']} auf Deutsch:"
    )
    result = clean_reply(chat(CAPTION_SYSTEM, prompt, purpose="plan", max_tokens=100))
    return result or score.caption_idea or f"Neuer Content von {p['name']} 🔥"


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
    content:       list[ContentScore],
    persona_name:  str,
    tips:          list[str],
    posts_per_day: int,
    paid_every:    int,
) -> list[Post]:
    # Nach Persona-Fit + Score sortieren
    scored = sorted(
        content,
        key=lambda x: x.persona_fit.get(persona_name, 5) * 2 + x.score,
        reverse=True,
    )

    times = _spread_times(PEAK_HOURS, posts_per_day)

    # Alle Aufgaben vorbereiten — Winkel rotieren bei wiederholtem Content
    file_use_count: dict[str, int] = {}
    work_items = []
    for i in range(posts_per_day):
        content_item = scored[i % len(scored)]
        post_type    = "paid" if (i + 1) % paid_every == 0 else "free"
        use_count    = file_use_count.get(content_item.file, 0)
        angle        = CAPTION_ANGLES[use_count % len(CAPTION_ANGLES)]
        # Tip versetzt: beim 2. Einsatz derselben Datei anderen Tip nehmen
        tip_idx      = (i + use_count * 3) % len(tips) if tips else 0
        tip          = tips[tip_idx] if tips else "authentisch sein"
        file_use_count[content_item.file] = use_count + 1
        work_items.append((i, content_item, post_type, times[i], tip, angle))

    def _generate_post(args) -> tuple[int, Post]:
        idx, content_item, post_type, time_str, tip, angle = args
        caption  = generate_caption(content_item, persona_name, post_type, tip, angle)
        hashtags = generate_hashtags(content_item, persona_name)
        print(f"  Caption [{idx+1}/{posts_per_day}] {time_str} ({post_type}) ✓")
        return idx, Post(
            time      = time_str,
            file      = content_item.file,
            score     = content_item.score,
            post_type = post_type,
            caption   = caption,
            hashtags  = hashtags,
            best_for  = content_item.best_for,
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
    times = _spread_times(MASS_MSG_HOURS, count)

    # Zielgruppen + Voucher-Flags vorab bestimmen (nicht thread-abhängig)
    assignments = []
    for i in range(count):
        target          = "buyer" if random.random() < 0.4 else "non_buyer"
        include_voucher = (target == "non_buyer") and (random.random() < 0.4)
        tip             = tips[i % len(tips)] if tips else "neuen Content teilen"
        assignments.append((i, times[i], target, include_voucher, tip))

    def _generate_msg(args) -> tuple[int, MassMessage]:
        idx, time_str, target, include_voucher, tip = args
        text = generate_mass_message(persona_name, target, tip, include_voucher)
        return idx, MassMessage(
            time            = time_str,
            target          = target,
            text            = text,
            include_voucher = include_voucher,
        )

    results: dict[int, MassMessage] = {}
    with ThreadPoolExecutor(max_workers=MSG_WORKERS) as ex:
        futures = {ex.submit(_generate_msg, a): a[0] for a in assignments}
        for future in as_completed(futures):
            idx, msg = future.result()
            results[idx] = msg

    return [results[i] for i in range(count)]

# ─── Main Plan Function ───────────────────────────────────────────────────────

def create_day_plan(
    persona_name:   str,
    content:        list[ContentScore],
    insights:       Optional[MarketingInsights] = None,
    posts_per_day:  int = 11,
    mass_msg_count: int = 4,
    paid_every:     int = 5,
    date:           Optional[str] = None,
) -> DayPlan:
    today = date or datetime.now().strftime("%Y-%m-%d")
    tips  = (insights.top_tips if insights else []) or ["authentisch sein", "Fragen stellen", "Exklusivität betonen"]

    # Content nach Persona filtern
    persona_content = [ct for ct in content if ct.persona_fit.get(persona_name, 5) >= 6]
    if not persona_content:
        print(f"  [WARN] Kein Content mit Persona-Fit ≥ 6 für {persona_name} — nutze alle")
        persona_content = content

    print(f"\n[PLANNER] {persona_name.upper()} — {today}")
    print(f"  {len(persona_content)} passende Inhalte | {posts_per_day} Posts | {mass_msg_count} Massennachrichten")

    posts         = build_post_schedule(persona_content, persona_name, tips, posts_per_day, paid_every)
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


def plan_to_readable_text(plan: DayPlan) -> str:
    lines = [
        f"CONTENT PLAN — {plan.persona.upper()} — {plan.date}",
        "=" * 60,
        f"Notizen: {plan.notes}",
        "",
        f"POSTS ({len(plan.posts)}):",
    ]
    for p in plan.posts:
        paid = "[PAID] " if p.post_type == "paid" else "       "
        lines += [
            f"  {p.time} {paid}Score:{p.score}/10",
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
    ap.add_argument("--posts",      type=int, default=11)
    ap.add_argument("--mass-msgs",  type=int, default=4)
    ap.add_argument("--paid-every", type=int, default=5, dest="paid_every")
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
        cap = generate_caption(mock, persona, "free", None, None)
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
        plan     = create_day_plan(p_name, analysis, insights, args.posts, args.mass_msgs, args.paid_every)
        out_file = out_dir / p_name.capitalize() / f"{today}_{day}" / f"plan_{p_name}_{today}.json"
        save_plan(plan, out_file)
        out_file.with_suffix(".txt").write_text(plan_to_readable_text(plan), encoding="utf-8")
        print_plan(plan)
