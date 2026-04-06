"""
hilda_planner.py — Content-Plan nur für Hilda erstellen

Datenquelle: immer Google Drive (drive_source_folder_id in config/orchestrator.json)

Ablauf:
  1. Google Drive scannen → Bilder/Videos analysieren
  2. Marketing-Recherche (DuckDuckGo) — parallel zur Analyse
  3. Tagesplan für Hilda generieren
  4. Plan lokal + in Drive speichern
"""

import argparse
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

ROOT    = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from shared.ai_client import make_client, ensure_ollama
from orchestrator import (
    load_config,
    step_scan_and_analyze,
    step_research,
    step_plan,
    step_upload_plans,
    print_summary,
    ANALYSIS_DIR, PLANS_ROOT, RESEARCH_DIR, DOWNLOAD_DIR,
)


def main() -> None:
    ap = argparse.ArgumentParser(description="Hilda — Content-Plan aus Google Drive erstellen")
    ap.add_argument("--skip-research", action="store_true", help="Keine neue Recherche (letzte nutzen)")
    ap.add_argument("--dry-run",       action="store_true", help="Plan nicht in Drive hochladen")
    args = ap.parse_args()

    print(f"\n[HILDA PLANNER] Start")
    print(f"  Datenquelle: Google Drive")
    print(f"  Recherche  : {'NEIN (letzte)' if args.skip_research else 'JA'}")
    print(f"  Drive-Upload: {'NEIN' if args.dry_run else 'JA'}")

    for d in [ANALYSIS_DIR, PLANS_ROOT, RESEARCH_DIR, DOWNLOAD_DIR / "media"]:
        d.mkdir(parents=True, exist_ok=True)

    ensure_ollama()
    cfg    = load_config()
    client = make_client()

    # Research parallel zur Analyse starten
    _ex = ThreadPoolExecutor(max_workers=1)
    research_future = _ex.submit(step_research, cfg, args.skip_research)

    analysis = step_scan_and_analyze(cfg, use_drive=True, client=client, persona_name="hilda")
    insights = research_future.result()
    _ex.shutdown(wait=True)

    plans = step_plan(analysis, insights, cfg, ["hilda"], client)
    step_upload_plans(plans, cfg, use_drive=not args.dry_run)
    print_summary(plans, analysis)


if __name__ == "__main__":
    main()
