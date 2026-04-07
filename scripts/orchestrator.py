"""
orchestrator.py — Autonomes Content-Plan System

Täglicher Ablauf:
  1. Drive direkt scannen (Download→Analyse→Löschen pro Datei)
  2. Marketing-Recherche via DuckDuckGo
  3. Content-Plan für Hilda + Tia erstellen
  4. Top-Dateien in Feedback-Ordner in Drive kopieren
  5. Pläne lokal + in Drive speichern

Ordnerstruktur (lokal + Drive identisch):
  Plan für die nächsten Tage/
    Hilda/
      2025-01-15_Mittwoch/
        plan_hilda_2025-01-15.json
        plan_hilda_2025-01-15.txt
    Tia/
      ...

Start:
  python scripts/orchestrator.py
  python scripts/orchestrator.py --skip-research
  python scripts/orchestrator.py --skip-drive
  python scripts/orchestrator.py --persona hilda
  python scripts/orchestrator.py --dry-run
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

# sys.path so setzen dass "shared", "content", "drive" direkt importierbar sind
ROOT        = Path("/home/kali/4based-system")
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from shared.ai_client   import make_client, ensure_ollama
from content.analyzer   import (
    analyze_image, analyze_video,
    SUPPORTED_IMAGES, SUPPORTED_VIDEOS,
    save_analysis, load_analysis, ContentScore,
)
from content.researcher import run_research, load_insights
from content.planner    import (
    create_day_plan, save_plan, load_plan,
    plan_to_readable_text, print_plan, DayPlan,
)
from drive.manager      import (
    iter_drive_media,
    get_or_create_folder,
    upload_text,
    upload_feedback_files,
    DOWNLOAD_DIR,
)

DATA_DIR     = ROOT / "data"
ANALYSIS_DIR = DATA_DIR / "analysis"
RESEARCH_DIR = DATA_DIR / "research"
PLANS_ROOT   = DATA_DIR / "Plan für die nächsten Tage"

DAYS = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag","Samstag","Sonntag"]

# ─── Config ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    path    = ROOT / "config" / "orchestrator.json"
    default = {
        "drive_source_folder_id": "",
        "drive_output_folder_id": "",
        "min_content_score":      6,
        "posts_per_day":          11,
        "mass_msg_count":         4,
        "paid_every":             5,
        "personas":               ["hilda", "tia"],
        "research_every_days":    3,
    }
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(default, indent=2), encoding="utf-8")
        print(f"[CONFIG] Erstellt: {path}")
        print("[CONFIG] Bitte drive_source_folder_id und drive_output_folder_id eintragen!")
        return default
    return {**default, **json.loads(path.read_text(encoding="utf-8"))}

# ─── Step 1+2: Scan & Analyze ─────────────────────────────────────────────────

def _analyze_one(path: Path, client, min_score: int, results: list[ContentScore]) -> None:
    ext = path.suffix.lower()
    tag = "[IMG]" if ext in SUPPORTED_IMAGES else "[VID]"
    print(f"  {tag} {path.name}", end=" ... ", flush=True)

    if ext in SUPPORTED_IMAGES:
        s = analyze_image(path, client)
    elif ext in SUPPORTED_VIDEOS:
        s = analyze_video(path, client)
    else:
        print("übersprungen")
        return

    if s:
        print(f"Score {s.score}/10 [{s.placement}] [{s.content_category}]")
        if s.score >= min_score:
            results.append(s)
    else:
        print("fehlgeschlagen")


def step_scan_and_analyze(
    cfg: dict,
    use_drive: bool,
    client=None,
    persona_name: str = "",
    folder_id_override: str = "",
) -> list[ContentScore]:
    today    = datetime.now().strftime("%Y-%m-%d")
    suffix   = f"_{persona_name}" if persona_name else ""
    out_file = ANALYSIS_DIR / f"analysis{suffix}_{today}.json"

    if out_file.exists():
        print(f"[STEP 1+2] Analyse von heute vorhanden: {out_file.name}")
        return load_analysis(out_file)

    def _load_last() -> list[ContentScore]:
        pattern  = f"analysis{suffix}_*.json"
        existing = sorted(ANALYSIS_DIR.glob(pattern), reverse=True)
        if existing:
            print(f"[STEP 1+2] Lade letzte Analyse: {existing[0].name}")
            return load_analysis(existing[0])
        return []

    client    = client or make_client()
    min_score = cfg.get("min_content_score", 6)
    results:  list[ContentScore] = []

    if not use_drive:
        local = DOWNLOAD_DIR / "media"
        if not local.exists() or not any(local.iterdir()):
            print(f"[STEP 1+2] Kein lokales Material in {local}")
            return _load_last()
        files = [f for f in local.iterdir() if f.is_file()]
        print(f"[STEP 1+2] Analysiere {len(files)} lokale Dateien...")
        for f in files:
            _analyze_one(f, client, min_score, results)
    else:
        # Reihenfolge: expliziter Override → persona-spezifisch → generisch
        folder_id = (
            folder_id_override
            or (cfg.get(f"drive_source_folder_{persona_name}") if persona_name else "")
            or cfg.get("drive_source_folder_id", "")
        )
        if not folder_id:
            print(f"[STEP 1+2] Kein Drive-Ordner für '{persona_name}' → lokaler Fallback")
            return _load_last()
        print(f"[STEP 1+2] Drive scannen (Ordner: {folder_id[:20]}...)...")
        try:
            for _, tmp_path in iter_drive_media(folder_id):
                _analyze_one(tmp_path, client, min_score, results)
        except Exception as e:
            print(f"[STEP 1+2] Drive Fehler: {e}")
            return _load_last()

    if not results:
        return _load_last()

    results.sort(key=lambda x: x.score, reverse=True)
    save_analysis(results, out_file)
    print(f"[STEP 1+2] {len(results)} Dateien mit Score ≥ {min_score}")
    return results

# ─── Step 3: Marketing-Recherche ──────────────────────────────────────────────

def step_research(cfg: dict, skip: bool):
    today      = datetime.now().strftime("%Y-%m-%d")
    every_days = cfg.get("research_every_days", 3)
    existing   = sorted(RESEARCH_DIR.glob("insights_*.json"), reverse=True)

    if skip:
        print("[STEP 3] SKIP — Recherche übersprungen")
        if existing:
            return load_insights(existing[0])
        return None

    # Aktualitäts-Check
    if existing:
        last_day = existing[0].stem.replace("insights_", "")
        try:
            delta = (
                datetime.strptime(today, "%Y-%m-%d") -
                datetime.strptime(last_day, "%Y-%m-%d")
            ).days
            if delta < every_days:
                print(f"[STEP 3] Recherche noch aktuell ({delta}d alt, max {every_days}d)")
                return load_insights(existing[0])
        except ValueError:
            pass

    print("[STEP 3] Starte Marketing-Recherche...")
    return run_research(RESEARCH_DIR)

# ─── Step 4: Content-Pläne erstellen ──────────────────────────────────────────

def step_plan(
    analysis: list[ContentScore],
    insights,
    cfg:      dict,
    personas: list[str],
    client=None,
) -> dict[str, DayPlan]:
    today  = datetime.now().strftime("%Y-%m-%d")
    day    = DAYS[datetime.now().weekday()]
    plans: dict[str, DayPlan] = {}
    client = client or make_client()

    for persona in personas:
        day_folder = PLANS_ROOT / persona.capitalize() / f"{today}_{day}"
        day_folder.mkdir(parents=True, exist_ok=True)

        out_json = day_folder / f"plan_{persona}_{today}.json"
        out_txt  = day_folder / f"plan_{persona}_{today}.txt"

        if out_json.exists():
            print(f"[STEP 4] Plan für {persona} vorhanden: {day_folder.name}")
            plans[persona] = load_plan(out_json)
            continue

        if not analysis:
            print(f"[STEP 4] SKIP {persona}: Keine Analyse")
            continue

        plan = create_day_plan(
            persona_name   = persona,
            content        = analysis,
            insights       = insights,
            posts_per_day  = cfg.get("posts_per_day",  11),
            mass_msg_count = cfg.get("mass_msg_count",  4),
            paid_every     = cfg.get("paid_every",       5),
            client         = client,
        )
        save_plan(plan, out_json)
        out_txt.write_text(plan_to_readable_text(plan), encoding="utf-8")
        print(f"[STEP 4] Gespeichert: {day_folder.relative_to(ROOT)}/")
        plans[persona] = plan

    return plans

# ─── Step 5: Feedback-Ordner in Drive ─────────────────────────────────────────

def step_feedback(analysis: list[ContentScore], cfg: dict, use_drive: bool) -> None:
    if not use_drive:
        print("[STEP 5] SKIP: Drive deaktiviert")
        return

    folder_id = cfg.get("drive_output_folder_id", "")
    if not folder_id:
        print("[STEP 5] SKIP: drive_output_folder_id fehlt")
        return

    top_files = [Path(r.file) for r in analysis if r.score >= 8 and Path(r.file).exists()]
    if not top_files:
        print("[STEP 5] Keine Top-Dateien (Score ≥ 8)")
        return

    print(f"[STEP 5] {len(top_files)} Top-Dateien → Drive Feedback-Ordner...")
    upload_feedback_files(top_files, "top_content", parent_id=folder_id)

# ─── Step 6: Pläne in Drive hochladen ─────────────────────────────────────────

def step_upload_plans(plans: dict[str, DayPlan], cfg: dict, use_drive: bool) -> None:
    if not use_drive:
        print("[STEP 6] Drive deaktiviert — Pläne nur lokal gespeichert")
        for plan in plans.values():
            if plan:
                print_plan(plan)
        return

    folder_id = cfg.get("drive_output_folder_id", "")
    if not folder_id:
        print("[STEP 6] drive_output_folder_id fehlt — nur lokal gespeichert")
        return

    today     = datetime.now().strftime("%Y-%m-%d")
    day       = DAYS[datetime.now().weekday()]
    root_id   = get_or_create_folder("Plan für die nächsten Tage", folder_id)

    for persona, plan in plans.items():
        if not plan:
            continue
        print(f"[STEP 6] Lade Plan für {persona} in Drive hoch...")
        try:
            persona_id = get_or_create_folder(persona.capitalize(), root_id)
            day_id     = get_or_create_folder(f"{today}_{day}", persona_id)

            upload_text(
                json.dumps(asdict(plan), ensure_ascii=False, indent=2),
                f"plan_{persona}_{today}.json",
                day_id,
            )
            upload_text(
                plan_to_readable_text(plan),
                f"plan_{persona}_{today}.txt",
                day_id,
            )
            print(f"  → Plan für die nächsten Tage/{persona.capitalize()}/{today}_{day}/")
        except Exception as e:
            print(f"  [ERROR] {persona}: {e}")

# ─── Summary ──────────────────────────────────────────────────────────────────

def print_summary(plans: dict[str, DayPlan], analysis: list[ContentScore]) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*60}")
    print(f"ZUSAMMENFASSUNG — {now}")
    print(f"{'='*60}")
    print(f"Analysierte Inhalte: {len(analysis)}")

    for persona, plan in plans.items():
        if not plan:
            continue
        paid  = sum(1 for p in plan.posts if p.post_type == "paid")
        buyer = sum(1 for m in plan.mass_messages if m.target == "buyer")
        nonb  = sum(1 for m in plan.mass_messages if m.target == "non_buyer")
        print(f"\n  {persona.upper()}:")
        print(f"    Posts: {len(plan.posts)} ({paid} paid)")
        print(f"    Massennachrichten: Käufer={buyer} | Nicht-Käufer={nonb}")
        if plan.posts:
            print(f"    Zeitraum: {plan.posts[0].time} – {plan.posts[-1].time}")

    print(f"\nLokal gespeichert: {PLANS_ROOT}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="4Based Orchestrator — Täglicher Content-Plan")
    ap.add_argument("--skip-research", action="store_true")
    ap.add_argument("--skip-drive",    action="store_true")
    ap.add_argument("--dry-run",       action="store_true", help="Nichts in Drive hochladen")
    ap.add_argument("--persona",       choices=["hilda", "tia", "both"], default="both")
    args = ap.parse_args()

    use_drive = not args.skip_drive and not args.dry_run
    personas  = ["hilda", "tia"] if args.persona == "both" else [args.persona]

    print(f"\n[ORCHESTRATOR] Start — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Personas : {', '.join(personas)}")
    print(f"  Drive    : {'JA' if use_drive else 'NEIN'}")
    print(f"  Recherche: {'NEIN' if args.skip_research else 'JA'}")

    for d in [ANALYSIS_DIR, PLANS_ROOT, RESEARCH_DIR, DOWNLOAD_DIR / "media"]:
        d.mkdir(parents=True, exist_ok=True)

    ensure_ollama()   # Ollama starten falls nicht aktiv
    cfg    = load_config()
    client = make_client()   # einmal erzeugen, überall wiederverwenden

    # Research parallel zur Analyse starten (läuft im Hintergrund)
    _executor: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=1)
    research_future: Future = _executor.submit(step_research, cfg, args.skip_research)

    analysis = step_scan_and_analyze(cfg, use_drive, client)

    # Research-Ergebnis abholen (ist meist schon fertig)
    insights = research_future.result()
    _executor.shutdown(wait=False)

    plans    = step_plan(analysis, insights, cfg, personas, client)
    step_feedback(analysis, cfg, use_drive)
    step_upload_plans(plans, cfg, use_drive)
    print_summary(plans, analysis)

    print(f"\n[ORCHESTRATOR] Fertig — {datetime.now().strftime('%H:%M:%S')}")


if __name__ == "__main__":
    main()
