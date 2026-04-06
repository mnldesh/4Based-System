"""
researcher.py — Marketing-Trends via DuckDuckGo recherchieren
und auf Hilda / Tia anpassen.

Optimierungen:
  - Parallele DDG-Suchen mit ThreadPoolExecutor + Semaphore (Rate-Limit-Schutz)
  - Parallele LLM-Calls: hilda_strategy, tia_strategy, tips gleichzeitig
"""

import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from duckduckgo_search import DDGS

from shared.ai_client import make_client, chat, parse_json_from_response
from shared.personas import PERSONAS

ROOT = Path(__file__).resolve().parents[2]

# ─── Search queries ───────────────────────────────────────────────────────────

BASE_QUERIES = [
    "OnlyFans marketing strategies {year}",
    "content creator fan engagement tips {year}",
    "subscription platform growth tactics {year}",
    "how to increase OnlyFans subscribers {year}",
    "best posting times adult content creators {year}",
    "mass message strategy OnlyFans {year}",
    "content creator caption ideas that convert {year}",
    "fan retention strategies subscription platform {year}",
]

DDG_DELAY     = 2.0   # Sekunden Basisverzögerung pro Thread
DDG_MAX_RETRY = 3     # Wiederholungen bei Rate Limit
DDG_WORKERS   = 4     # Parallele Such-Threads

# ─── Data types ───────────────────────────────────────────────────────────────

@dataclass
class ResearchResult:
    query:   str
    title:   str
    url:     str
    snippet: str


@dataclass
class MarketingInsights:
    date:           str
    raw_results:    list[ResearchResult]
    summary:        str
    hilda_strategy: str
    tia_strategy:   str
    top_tips:       list[str]

# ─── Search ───────────────────────────────────────────────────────────────────

def search_ddg(query: str, max_results: int = 5) -> list[ResearchResult]:
    """Suche mit Retry bei Rate Limit."""
    for attempt in range(DDG_MAX_RETRY):
        try:
            with DDGS() as ddgs:
                return [
                    ResearchResult(
                        query   = query,
                        title   = r.get("title", ""),
                        url     = r.get("href",  ""),
                        snippet = r.get("body",  ""),
                    )
                    for r in ddgs.text(query, max_results=max_results)
                ]
        except Exception as e:
            err = str(e).lower()
            if "ratelimit" in err or "202" in err or "429" in err:
                wait = DDG_DELAY * (2 ** attempt)
                print(f"  [DDG] Rate Limit — warte {wait:.0f}s...")
                time.sleep(wait)
            else:
                print(f"  [DDG] Fehler bei '{query}': {e}")
                break
    return []


def run_searches(year: Optional[int] = None) -> list[ResearchResult]:
    y       = year or datetime.now().year
    queries = [q.format(year=y) for q in BASE_QUERIES]
    total   = len(queries)

    # Semaphore: begrenzt gleichzeitige DDG-Verbindungen + schützt vor Rate Limit
    _sem     = threading.Semaphore(DDG_WORKERS)
    _lock    = threading.Lock()
    counter  = [0]

    def _search_one(q: str) -> list[ResearchResult]:
        with _sem:
            time.sleep(DDG_DELAY * 0.5)   # kurze Pause pro Thread-Start
            results = search_ddg(q)
            with _lock:
                counter[0] += 1
                print(f"  [{counter[0]}/{total}] {q[:55]}  ({len(results)} Treffer)")
            return results

    print(f"[RESEARCHER] {total} Suchanfragen parallel ({y})...")
    all_res: list[ResearchResult] = []
    with ThreadPoolExecutor(max_workers=DDG_WORKERS) as ex:
        for partial in ex.map(_search_one, queries):
            all_res.extend(partial)

    print(f"[RESEARCHER] {len(all_res)} Ergebnisse gesammelt")
    return all_res

# ─── AI Analysis ──────────────────────────────────────────────────────────────

SUMMARY_SYSTEM = """Du bist ein Marketing-Experte für Content Creator auf Abo-Plattformen.
Analysiere die folgenden Suchergebnisse und erstelle eine strukturierte Zusammenfassung
der wichtigsten Marketing-Strategien. Antworte auf Deutsch, klar und präzise."""

PERSONA_SYSTEM = """Du bist ein Marketing-Stratege der Persona-basierte Strategien entwickelt.
Passe die gegebenen Marketing-Insights auf die Persona an.
Antworte auf Deutsch, max 300 Wörter, als konkreter Aktionsplan."""

TIPS_SYSTEM = """Extrahiere die 8 wichtigsten universellen Marketing-Tipps aus den Insights.
Antworte NUR mit einem JSON-Array ohne weiteren Text: ["tipp1", "tipp2", ...]"""

MAX_SNIPPETS = 30   # Token-Limit für LLM


def build_snippets_text(results: list[ResearchResult]) -> str:
    lines = []
    for i, r in enumerate(results[:MAX_SNIPPETS], 1):
        lines.append(f"{i}. [{r.title}]\n{r.snippet}\n")
    return "\n".join(lines)


def _persona_strategy(persona_name: str, summary: str, client) -> str:
    p = PERSONAS[persona_name]
    result = chat(
        system = PERSONA_SYSTEM,
        user   = (
            f"Persona: {p['name']}, {p['age']}J., Stil: {p['style']}\n"
            f"Platform: {p['platform']}, Gutschein: {p['voucher_pct']}%\n\n"
            f"Marketing Insights:\n{summary}\n\n"
            f"Erstelle einen konkreten Aktionsplan für {p['name']}:"
        ),
        client     = client,
        max_tokens = 500,
    )
    return result or f"Kein Aktionsplan für {p['name']} verfügbar."


def analyze_results(results: list[ResearchResult], client=None) -> MarketingInsights:
    c        = client or make_client()
    snippets = build_snippets_text(results)

    print("[RESEARCHER] Erstelle Zusammenfassung...")
    summary = chat(
        system     = SUMMARY_SYSTEM,
        user       = f"Suchergebnisse:\n\n{snippets}\n\nFasse die wichtigsten Marketing-Strategien zusammen:",
        client     = c,
        max_tokens = 800,
    )
    if not summary:
        summary = "Keine Zusammenfassung verfügbar."

    # hilda_strategy, tia_strategy und tips parallel erzeugen (alle hängen nur von summary ab)
    print("[RESEARCHER] Erstelle Persona-Strategien + Tipps parallel...")
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_hilda = ex.submit(_persona_strategy, "hilda", summary, c)
        f_tia   = ex.submit(_persona_strategy, "tia",   summary, c)
        f_tips  = ex.submit(chat, TIPS_SYSTEM, summary, c, 400)

    hilda_strat = f_hilda.result()
    tia_strat   = f_tia.result()
    raw_tips    = f_tips.result() or ""

    tips: list[str] = []
    if raw_tips:
        data = parse_json_from_response(raw_tips)
        if isinstance(data, list):
            tips = [str(t) for t in data[:8]]
        elif isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    tips = [str(t) for t in v[:8]]
                    break

    if not tips:
        # Fallback: Zeilen aufsplitten
        tips = [
            line.strip("- 1234567890. ")
            for line in raw_tips.split("\n")
            if line.strip() and not line.startswith("{")
        ][:8]

    return MarketingInsights(
        date           = datetime.now().strftime("%Y-%m-%d"),
        raw_results    = results,
        summary        = summary,
        hilda_strategy = hilda_strat,
        tia_strategy   = tia_strat,
        top_tips       = tips,
    )

# ─── Save / Load ──────────────────────────────────────────────────────────────

def save_insights(insights: MarketingInsights, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(asdict(insights), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[RESEARCHER] Gespeichert: {output}")


def load_insights(path: Path) -> Optional[MarketingInsights]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        known_rr = {"query", "title", "url", "snippet"}
        data["raw_results"] = [
            ResearchResult(**{k: v for k, v in r.items() if k in known_rr})
            for r in data.get("raw_results", [])
        ]
        known_mi = {"date", "raw_results", "summary", "hilda_strategy", "tia_strategy", "top_tips"}
        return MarketingInsights(**{k: v for k, v in data.items() if k in known_mi})
    except Exception as e:
        print(f"[RESEARCHER] Laden fehlgeschlagen: {e}")
        return None

# ─── Main entry ───────────────────────────────────────────────────────────────

def run_research(output_dir: Optional[Path] = None) -> MarketingInsights:
    out_dir  = output_dir or ROOT / "data" / "research"
    out_file = out_dir / f"insights_{datetime.now().strftime('%Y-%m-%d')}.json"
    results  = run_searches()
    insights = analyze_results(results)
    save_insights(insights, out_file)
    return insights


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Marketing Researcher")
    ap.add_argument("--output-dir", default=None)
    args     = ap.parse_args()
    out      = Path(args.output_dir) if args.output_dir else None
    insights = run_research(out)

    print("\n" + "=" * 60)
    print("ZUSAMMENFASSUNG:")
    print(insights.summary[:500])
    print("\nHILDA STRATEGIE:")
    print(insights.hilda_strategy[:300])
    print("\nTIA STRATEGIE:")
    print(insights.tia_strategy[:300])
    print("\nTOP TIPPS:")
    for i, tip in enumerate(insights.top_tips, 1):
        print(f"  {i}. {tip}")
