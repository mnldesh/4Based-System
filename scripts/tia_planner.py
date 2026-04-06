"""
tia_planner.py — Content-Plan nur für Tia erstellen

Führt aus:
  1. Content-Analyse (lokaler Ordner oder Google Drive)
  2. Marketing-Recherche (DuckDuckGo)
  3. Tagesplan für Tia generieren
  4. Plan speichern (lokal + optional Drive)
"""

import argparse
import sys
from pathlib import Path

ROOT       = Path(__file__).resolve().parent.parent
SCRIPTS    = Path(__file__).resolve().parent
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

from concurrent.futures import ThreadPoolExecutor, Future


def main() -> None:
    ap = argparse.ArgumentParser(description="Tia — Content-Plan erstellen")
    ap.add_argument("--skip-research", action="store_true", help="Keine neue Recherche")
    ap.add_argument("--skip-drive",    action="store_true", help="Kein Google Drive")
    ap.add_argument("--dry-run",       action="store_true", help="Nicht hochladen")
    args = ap.parse_args()

    use_drive = not args.skip_drive and not args.dry_run

    print(f"\n[TIA PLANNER] Start")
    print(f"  Drive    : {'JA' if use_drive else 'NEIN'}")
    print(f"  Recherche: {'NEIN' if args.skip_research else 'JA'}")

    for d in [ANALYSIS_DIR, PLANS_ROOT, RESEARCH_DIR, DOWNLOAD_DIR / "media"]:
        d.mkdir(parents=True, exist_ok=True)

    ensure_ollama()
    cfg    = load_config()
    client = make_client()

    # Research + Analyse parallel
    _ex: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=1)
    research_future: Future = _ex.submit(step_research, cfg, args.skip_research)

    analysis = step_scan_and_analyze(cfg, use_drive, client)
    insights = research_future.result()
    _ex.shutdown(wait=False)

    plans = step_plan(analysis, insights, cfg, ["tia"], client)

    if use_drive:
        step_upload_plans(plans, cfg)

    print_summary(analysis, plans)


if __name__ == "__main__":
    main()
