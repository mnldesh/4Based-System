"""
analyzer.py — Bilder & Videos analysieren und bewerten
Spezialisiert auf Erotik-Content für Abo-Plattformen (4Based, OnlyFans etc.)
"""

import json
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

FRAME_WORKERS  = 4   # Parallele Vision-Calls pro Video
FOLDER_WORKERS = 4   # Parallele Datei-Analysen im Ordner

from shared.ai_client import make_client, vision, chat, parse_json_from_response

SUPPORTED_IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
SUPPORTED_VIDEOS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

# ─── Data types ───────────────────────────────────────────────────────────────

@dataclass
class ContentScore:
    file:             str
    type:             str           # "image" | "video"
    score:            int           # 1-10 Gesamtscore
    production_score: int           # 1-10 Technische Qualität
    erotic_score:     int           # 1-10 Erotisches Potenzial
    strengths:        list[str]
    weaknesses:       list[str]
    placement:        str           # "free_teaser" | "paid" | "ppv" | "story" | "mass_msg_preview"
    best_for:         list[str]
    content_category: str           # "softcore" | "implied" | "explicit" | "bts" | "lifestyle"
    caption_idea:     str
    hook_idea:        str
    persona_fit:      dict[str, int]
    video_meta:       dict

# ─── Prompts ──────────────────────────────────────────────────────────────────

IMAGE_EVAL_PROMPT = """Du analysierst ein Bild für eine Erotik-Content-Creatorin auf einer Abo-Plattform (wie OnlyFans/4Based).

Bewerte objektiv und professionell aus Marketing-Sicht. Antworte NUR mit validem JSON:
{
  "score": <1-10 Gesamtscore>,
  "production_score": <1-10 Technische Qualität: Licht, Schärfe, Bildkomposition, Farbe>,
  "erotic_score": <1-10 Erotisches Potenzial: Spannung, Ausdruck, Körpersprache, Atmosphäre>,
  "strengths": ["<stärke1>", "<stärke2>", "<stärke3>"],
  "weaknesses": ["<schwäche1>", "<schwäche2>"],
  "placement": "<free_teaser|paid|ppv|story|mass_msg_preview>",
  "best_for": ["<post|story|teaser|paid|ppv>"],
  "content_category": "<softcore|implied|explicit|bts|lifestyle>",
  "caption_idea": "<kurze deutsche caption die neugierig macht, max 2 sätze>",
  "hook_idea": "<erster satz/teaser der zum klicken animiert>",
  "persona_fit": {
    "hilda": <1-10>,
    "tia": <1-10>
  }
}

Bewertungsrichtlinien:
- production_score: Licht (natürlich/studio), Schärfe, Hintergrund, Bildausschnitt, Farben
- erotic_score: Spannung, Blickkontakt, Körpersprache, Pose, Ausdruck, Stimmung
- placement: free_teaser=zeigt wenig aber macht neugierig | paid=guter Hauptcontent | ppv=bestes exklusiv-Material | story=kurz/spontan | mass_msg_preview=gut als Vorschau in Nachrichten
- content_category: softcore=andeutend | implied=nichts direkt gezeigt aber klar | explicit=direkt | bts=behind-the-scenes | lifestyle=alltag/person
- Persona: Hilda=edgy/dunkel/provokant/selbstbewusst | Tia=hell/süß/verspielt/herzlich"""


VIDEO_FRAME_PROMPT = """Du analysierst einen Frame aus einem Erotik-Video für eine Content-Creatorin auf einer Abo-Plattform.

Antworte NUR mit validem JSON:
{
  "production_score": <1-10>,
  "erotic_score": <1-10>,
  "lighting": "<gut|ok|schlecht>",
  "sharpness": "<scharf|ok|unscharf>",
  "composition": "<gut|ok|schlecht>",
  "mood": "<aufregend|verführerisch|verspielt|intensiv|romantisch|alltäglich>",
  "content_visible": "<softcore|implied|explicit|non_erotic>",
  "persona_fit": {
    "hilda": <1-10>,
    "tia": <1-10>
  }
}"""


VIDEO_SUMMARY_SYSTEM = """Du bist ein Content-Stratege für Erotik-Content-Creator auf Abo-Plattformen.
Analysiere die Frame-Analyse eines Videos und erstelle eine Gesamtbewertung.
Antworte NUR mit validem JSON ohne zusätzlichen Text."""

VIDEO_SUMMARY_PROMPT = """Frame-Analysen des Videos '{filename}' (Dauer: {duration:.1f}s, {n_frames} Frames):

{frames_json}

Erstelle Gesamtbewertung als JSON:
{{
  "score": <1-10 Gesamtscore>,
  "production_score": <1-10>,
  "erotic_score": <1-10>,
  "strengths": ["<stärke1>", "<stärke2>", "<stärke3>"],
  "weaknesses": ["<schwäche1>", "<schwäche2>"],
  "placement": "<free_teaser|paid|ppv|story|mass_msg_preview>",
  "best_for": ["<post|story|teaser|paid|ppv>"],
  "content_category": "<softcore|implied|explicit|bts|lifestyle>",
  "caption_idea": "<kurze deutsche caption, max 2 sätze>",
  "hook_idea": "<erster satz/teaser-text der zum kaufen animiert>",
  "persona_fit": {{"hilda": <1-10>, "tia": <1-10>}},
  "has_strong_hook": <true|false>,
  "climax_timing": "<early|mid|late|throughout>",
  "recommended_preview_length": <sekunden als int>,
  "edit_suggestions": ["<empfehlung1>"]
}}"""

# ─── Video Metadata ───────────────────────────────────────────────────────────

def get_video_info(video_path: Path) -> dict:
    """Gibt Dauer, Auflösung und Framerate zurück."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,r_frame_rate,duration",
                "-show_entries", "format=duration",
                "-of", "json",
                str(video_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        data    = json.loads(result.stdout)
        fmt_dur = float(data.get("format", {}).get("duration") or 0)
        stream  = (data.get("streams") or [{}])[0]
        s_dur   = float(stream.get("duration") or 0)
        duration = fmt_dur or s_dur

        fps_raw = stream.get("r_frame_rate", "30/1")
        try:
            num, den = fps_raw.split("/")
            fps = round(int(num) / int(den), 2)
        except Exception:
            fps = 30.0

        return {
            "duration": duration,
            "width":    int(stream.get("width")  or 0),
            "height":   int(stream.get("height") or 0),
            "fps":      fps,
        }
    except Exception as e:
        print(f"  [VIDEO INFO] {e}")
        return {"duration": 0, "width": 0, "height": 0, "fps": 0}


def extract_frames(video_path: Path, n_frames: int = 6) -> list[tuple[Path, float]]:
    """
    Extrahiert Frames intelligent:
      - Hook (erste 2%)
      - Früher Einstieg (5%)
      - Gleichmäßige Mitte
      - Kurz vor Ende (88%, 94%)
    """
    info     = get_video_info(video_path)
    duration = info["duration"]
    if duration <= 0:
        return []

    anchor_pcts = [0.02, 0.05]
    end_pcts    = [0.88, 0.94]
    middle_n    = max(0, n_frames - len(anchor_pcts) - len(end_pcts))
    middle_pcts = [
        0.10 + (0.75 * i / max(middle_n, 1))
        for i in range(middle_n)
    ]

    all_pcts   = anchor_pcts + middle_pcts + end_pcts
    timestamps = sorted({round(duration * p, 2) for p in all_pcts})

    tmpdir = Path(tempfile.mkdtemp())
    frames: list[tuple[Path, float]] = []

    def _extract_one(args: tuple[int, float]) -> Optional[tuple[Path, float]]:
        i, ts = args
        ts    = min(ts, duration - 0.5)
        out   = tmpdir / f"frame_{i:02d}.jpg"
        try:
            subprocess.run(
                ["ffmpeg", "-ss", str(ts), "-i", str(video_path),
                 "-frames:v", "1", "-q:v", "2", str(out), "-y"],
                capture_output=True, timeout=60,
            )
            return (out, ts) if out.exists() else None
        except Exception:
            return None

    try:
        # Alle Frames parallel extrahieren
        with ThreadPoolExecutor(max_workers=FRAME_WORKERS) as ex:
            results = list(ex.map(_extract_one, enumerate(timestamps)))
        frames = [r for r in results if r is not None]
        frames.sort(key=lambda x: x[1])   # Nach Timestamp sortieren
    except Exception as e:
        print(f"  [FRAMES] {e}")
        shutil.rmtree(tmpdir, ignore_errors=True)

    return frames


def _cleanup_frames(frames: list[tuple[Path, float]]) -> None:
    """Löscht alle Temp-Frames und den Temp-Ordner."""
    dirs = set()
    for path, _ in frames:
        dirs.add(path.parent)
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)

# ─── Image Analysis ───────────────────────────────────────────────────────────

def analyze_image(path: Path, client=None) -> Optional[ContentScore]:
    if path.suffix.lower() not in SUPPORTED_IMAGES:
        return None

    c   = client or make_client()
    raw = vision(path, IMAGE_EVAL_PROMPT, c)
    if not raw:
        return None

    data = parse_json_from_response(raw)
    if not data:
        print(f"  [ANALYZER] Kein JSON in Response: {raw[:80]}")
        return None

    return ContentScore(
        file             = str(path),
        type             = "image",
        score            = int(data.get("score", 5)),
        production_score = int(data.get("production_score", 5)),
        erotic_score     = int(data.get("erotic_score", 5)),
        strengths        = data.get("strengths", []),
        weaknesses       = data.get("weaknesses", []),
        placement        = data.get("placement", "paid"),
        best_for         = data.get("best_for", ["post"]),
        content_category = data.get("content_category", "softcore"),
        caption_idea     = data.get("caption_idea", ""),
        hook_idea        = data.get("hook_idea", ""),
        persona_fit      = data.get("persona_fit", {"hilda": 5, "tia": 5}),
        video_meta       = {},
    )

# ─── Video Analysis ───────────────────────────────────────────────────────────

def analyze_frame(frame_path: Path, client=None) -> Optional[dict]:
    """Analysiert einen einzelnen Video-Frame."""
    c   = client or make_client()
    raw = vision(frame_path, VIDEO_FRAME_PROMPT, c)
    if not raw:
        return None
    return parse_json_from_response(raw)


def analyze_video(path: Path, client=None) -> Optional[ContentScore]:
    if path.suffix.lower() not in SUPPORTED_VIDEOS:
        return None

    info     = get_video_info(path)
    duration = info["duration"]
    if duration <= 0:
        print(f"  [ANALYZER] Video-Dauer unlesbar: {path.name}")
        return None

    # Frame-Anzahl nach Länge
    if duration < 30:     n_frames = 4
    elif duration < 60:   n_frames = 6
    elif duration < 120:  n_frames = 8
    elif duration < 300:  n_frames = 12
    else:                 n_frames = 16   # 5min+

    mins    = int(duration // 60)
    secs    = int(duration % 60)
    dur_str = f"{mins}m{secs:02d}s" if mins > 0 else f"{secs}s"
    print(f"  ({dur_str}, {info['width']}x{info['height']}, {n_frames} frames)", end=" ", flush=True)

    frames = extract_frames(path, n_frames)
    if not frames:
        print("keine frames")
        return None

    c = client or make_client()

    def _analyze_one_frame(args: tuple[Path, float]) -> Optional[dict]:
        frame_path, ts = args
        fd = analyze_frame(frame_path, c)
        if fd:
            fd["timestamp"] = ts
        return fd

    # Alle Frames parallel analysieren
    frame_results: list[dict] = []
    with ThreadPoolExecutor(max_workers=FRAME_WORKERS) as ex:
        for fd in ex.map(_analyze_one_frame, frames):
            if fd:
                frame_results.append(fd)

    frame_results.sort(key=lambda x: x.get("timestamp", 0))
    _cleanup_frames(frames)

    if not frame_results:
        return None

    frames_json = json.dumps(frame_results, ensure_ascii=False, indent=2)
    prompt      = VIDEO_SUMMARY_PROMPT.format(
        filename    = path.name,
        duration    = duration,
        n_frames    = len(frame_results),
        frames_json = frames_json,
    )

    raw  = chat(system=VIDEO_SUMMARY_SYSTEM, user=prompt, client=c, max_tokens=600)
    data = parse_json_from_response(raw) if raw else None

    if not data:
        return _fallback_video_score(path, frame_results, info)

    return ContentScore(
        file             = str(path),
        type             = "video",
        score            = int(data.get("score", 5)),
        production_score = int(data.get("production_score", 5)),
        erotic_score     = int(data.get("erotic_score", 5)),
        strengths        = data.get("strengths", []),
        weaknesses       = data.get("weaknesses", []),
        placement        = data.get("placement", "paid"),
        best_for         = data.get("best_for", ["post"]),
        content_category = data.get("content_category", "softcore"),
        caption_idea     = data.get("caption_idea", ""),
        hook_idea        = data.get("hook_idea", ""),
        persona_fit      = data.get("persona_fit", {"hilda": 5, "tia": 5}),
        video_meta       = {
            "duration":                   duration,
            "resolution":                 f"{info['width']}x{info['height']}",
            "fps":                        info["fps"],
            "has_strong_hook":            bool(data.get("has_strong_hook", False)),
            "climax_timing":              data.get("climax_timing", "mid"),
            "recommended_preview_length": int(data.get("recommended_preview_length", 15)),
            "edit_suggestions":           data.get("edit_suggestions", []),
        },
    )


def _fallback_video_score(path: Path, frame_results: list[dict], info: dict) -> ContentScore:
    """Einfacher Durchschnitt wenn LLM-Summary fehlschlägt."""
    prod  = [f.get("production_score", 5) for f in frame_results]
    ero   = [f.get("erotic_score", 5) for f in frame_results]
    avg_p = round(sum(prod) / len(prod))
    avg_e = round(sum(ero)  / len(ero))
    score = round((avg_p + avg_e) / 2)
    hilda = round(sum(f.get("persona_fit", {}).get("hilda", 5) for f in frame_results) / len(frame_results))
    tia   = round(sum(f.get("persona_fit", {}).get("tia",   5) for f in frame_results) / len(frame_results))

    return ContentScore(
        file="str(path)", type="video", score=score,
        production_score=avg_p, erotic_score=avg_e,
        strengths=[], weaknesses=[], placement="paid",
        best_for=["paid"], content_category="softcore",
        caption_idea="", hook_idea="",
        persona_fit={"hilda": hilda, "tia": tia},
        video_meta={
            "duration":   info["duration"],
            "resolution": f"{info['width']}x{info['height']}",
            "fps":        info["fps"],
        },
    )

# ─── Folder Scanner ───────────────────────────────────────────────────────────

def analyze_folder(folder: Path, min_score: int = 6, client=None) -> list[ContentScore]:
    """
    Analysiert alle Bilder und Videos in einem Ordner.
    Gibt Dateien mit score >= min_score zurück, sortiert nach Score.
    """
    c       = client or make_client()
    results: list[ContentScore] = []

    # Einmal iterieren, dabei kategorisieren
    images: list[Path] = []
    videos: list[Path] = []
    for f in folder.iterdir():
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext in SUPPORTED_IMAGES:   images.append(f)
        elif ext in SUPPORTED_VIDEOS: videos.append(f)

    print(f"[ANALYZER] {len(images)} Bilder, {len(videos)} Videos in '{folder.name}'")

    def _process_image(f: Path) -> Optional[ContentScore]:
        print(f"  [IMG] {f.name}", end=" ... ", flush=True)
        s = analyze_image(f, c)
        if s:
            print(f"Score {s.score}/10 (prod:{s.production_score} ero:{s.erotic_score}) [{s.placement}]")
        else:
            print("übersprungen")
        return s

    def _process_video(f: Path) -> Optional[ContentScore]:
        print(f"  [VID] {f.name}", end=" ... ", flush=True)
        s = analyze_video(f, c)
        if s:
            dur     = float(s.video_meta.get("duration", 0) or 0)
            hook    = "✓hook" if s.video_meta.get("has_strong_hook") else "schwacher hook"
            mins    = int(dur // 60); secs = int(dur % 60)
            dur_str = f"{mins}m{secs:02d}s" if mins > 0 else f"{secs}s"
            print(f"Score {s.score}/10 (prod:{s.production_score} ero:{s.erotic_score}) [{s.placement}] {dur_str} {hook}")
        else:
            print("übersprungen")
        return s

    # Bilder parallel analysieren
    with ThreadPoolExecutor(max_workers=FOLDER_WORKERS) as ex:
        for s in ex.map(_process_image, images):
            if s and s.score >= min_score:
                results.append(s)

    # Videos parallel analysieren (Videos sind schwerer, eigener Pool)
    with ThreadPoolExecutor(max_workers=max(1, FOLDER_WORKERS // 2)) as ex:
        for s in ex.map(_process_video, videos):
            if s and s.score >= min_score:
                results.append(s)

    results.sort(key=lambda x: x.score, reverse=True)

    # Zusammenfassung
    placements = {}
    categories = {}
    for r in results:
        placements[r.placement]        = placements.get(r.placement, 0) + 1
        categories[r.content_category] = categories.get(r.content_category, 0) + 1

    print(f"\n[ANALYZER] {len(results)} Dateien mit Score ≥ {min_score}")
    if placements: print(f"  Platzierung: {placements}")
    if categories: print(f"  Kategorien:  {categories}")
    return results


def save_analysis(results: list[ContentScore], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[ANALYZER] Gespeichert: {output}")


def load_analysis(path: Path) -> list[ContentScore]:
    if not path.exists():
        return []
    try:
        data    = json.loads(path.read_text(encoding="utf-8"))
        results = []
        for d in data:
            # Rückwärtskompatibilität
            d.setdefault("production_score", d.get("score", 5))
            d.setdefault("erotic_score",     d.get("score", 5))
            d.setdefault("placement",        "paid")
            d.setdefault("content_category", "softcore")
            d.setdefault("hook_idea",        "")
            d.setdefault("video_meta",       {})
            # Nur bekannte Felder übergeben
            known = {f.name for f in ContentScore.__dataclass_fields__.values()}
            results.append(ContentScore(**{k: v for k, v in d.items() if k in known}))
        return results
    except Exception as e:
        print(f"[ANALYZER] Laden fehlgeschlagen: {e}")
        return []


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Erotik Content Analyzer")
    ap.add_argument("folder",      help="Ordner mit Bildern/Videos")
    ap.add_argument("--min-score", type=int, default=6)
    ap.add_argument("--output",    default=None, help="Ausgabe-Pfad (default: neben Ordner)")
    args = ap.parse_args()

    folder = Path(args.folder)
    if not folder.exists():
        raise SystemExit(f"Ordner nicht gefunden: {folder}")

    output  = Path(args.output) if args.output else folder.parent / f"analysis_{folder.name}.json"
    results = analyze_folder(folder, min_score=args.min_score)
    save_analysis(results, output)

    if results:
        print(f"\nTOP 5:")
        for r in results[:5]:
            t = f"({float(r.video_meta.get('duration', 0) or 0):.0f}s)" if r.type == "video" else ""
            print(f"  {r.score}/10 [{r.placement}] {Path(r.file).name} {t}")
            if r.caption_idea: print(f"    Caption: {r.caption_idea[:70]}")
            if r.hook_idea:    print(f"    Hook:    {r.hook_idea[:70]}")
