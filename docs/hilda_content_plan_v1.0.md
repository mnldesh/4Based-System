# Hilda Content-Plan Logik — v1.0

## Fortschritt

- [x] Schritt 1: `ai_client.py` — Chat/Plan Routing auf Claude 3.5 Sonnet / Claude 3 Opus
- [x] Schritt 2: `personas.py` — Hilda `content_style` aktualisiert
- [x] Schritt 3: `planner.py` — Hauptrefactor (Zeitfenster, Captions, Massennachrichten)
- [x] Schritt 4: `orchestrator.py` — Pfad + `save_mass_messages` + Config
- [x] Schritt 5: `orchestrator.json` — `posts_per_day=12`, `mass_msg_count=5`, `max_paid_posts=3`

---

## Context

Das bestehende Content-Planungssystem (`planner.py`, `orchestrator.py`) wurde für Hilda Valentine
spezialisiert:

1. **Tagesbogen** mit 5 Zeitfenstern statt flachem PEAK_HOURS-Pool (nur Abend/Nacht)
2. **Caption-Stil** ohne festes Hook→Teaser→CTA-Schema — emotional und stimmungsbasiert
3. **Massennachrichten** mit exakt max. 2 paid-Nachrichten (bisher zufällig)
4. **Ordnerstruktur** um `Plan für die nächsten Tage`-Unterordner ergänzt + `save_mass_messages` im Orchestrator
5. **Modell-Routing** überarbeitet: Chat → Claude 3.5 Sonnet, Planung → Claude 3 Opus

---

## Modell-Konfiguration (`scripts/shared/ai_client.py`)

| Zweck | Modell | Konstante |
|---|---|---|
| Chat / Live-Dialoge | Claude 3.5 Sonnet `claude-3-5-sonnet-20241022` | `CLAUDE_MODEL` |
| Planung / Strategie / Captions | Claude 3 Opus `claude-3-opus-20240229` | `PLAN_MODEL` |
| Ollama-Fallback (Chat) | `qwen2.5:latest` | `TEXT_MODEL` |
| Bildanalyse | `llava:7b` | `VISION_MODEL` |

Routing:
- `purpose="chat"` → `CLAUDE_MODEL` (Claude 3.5 Sonnet), Fallback: `TEXT_MODEL` (Ollama)
- `purpose="plan"` → `PLAN_MODEL` (Claude 3 Opus), Fallback: `TEXT_MODEL` (Ollama)

---

## Kritische Dateien

| Datei | Änderungstyp |
|---|---|
| `scripts/content/planner.py` | Hauptrefactor |
| `scripts/shared/personas.py` | Hilda `content_style` updaten |
| `scripts/shared/ai_client.py` | Modell-Konstanten + Routing |
| `scripts/orchestrator.py` | Pfad + `save_mass_messages` + `create_day_plan` Signatur |
| `config/orchestrator.json` | `posts_per_day=12`, `mass_msg_count=5`, `max_paid_posts=3` |

---

## Tagesbogen der Posts (12 Posts)

| Zone | Uhrzeiten | Stimmung | Posts | Paid |
|---|---|---|---|---|
| `morgen` | 08–12 Uhr | warm, sanft, BTS/Lifestyle | 3 | 0 |
| `mittag` | 14–17 Uhr | Teaser, Andeutungen, Neugier | 2 | 0 |
| `abend` | 18–21 Uhr | intensiv, verführerisch | 3 | 1 |
| `spaet_abend` | 22–23 Uhr | Höhepunkte, PPV/Gutscheine | 2 | 1 |
| `nacht` | 00–07 Uhr | sehr intensiv, sehr intim | 2 | 1 |
| **Gesamt** | | | **12** | **3** |

Paid-Posts werden ausschließlich in `abend`, `spaet_abend` und `nacht` platziert (max. 3).

---

## Massennachrichten-Plan (5 Nachrichten)

| Zeit | Zielgruppe | Paid | Dateiname | Inhalt |
|---|---|---|---|---|
| 18:00 | `non_buyer` | Nein | `mass_non_buyer_1800.txt` | warm, Bezug auf Tages-Posts |
| 20:00 | `non_buyer` | Nein | `mass_non_buyer_2000.txt` | Beziehungs-/Nähe-Nachricht |
| 22:00 | `buyer` | **Ja** (paid_1) | `mass_buyer_2200_paid_1.txt` | PPV/Stammkunden-Angebot |
| 00:00 | `non_buyer` | **Ja** (paid_2) | `mass_non_buyer_0000_paid_2.txt` | Gutschein-Angebot |
| 04:00 | `non_buyer` | Nein | `mass_non_buyer_0400.txt` | nächtlicher Flirt/Nähe |

---

## Resultierende Ordnerstruktur

```
/mnt/Arbeit/Planung/Hilda/
├── Plan für die nächsten Tage/
│   └── yyyy-mm-dd_{Wochentag}/
│       ├── plan_hilda_yyyy-mm-dd.json
│       └── plan_hilda_yyyy-mm-dd.txt
└── Massennachrichten/
    └── yyyy-mm-dd_{Wochentag}_hilda/
        ├── mass_non_buyer_1800.txt
        ├── mass_non_buyer_2000.txt
        ├── mass_buyer_2200_paid_1.txt
        ├── mass_non_buyer_0000_paid_2.txt
        └── mass_non_buyer_0400.txt
```

---

## Caption-Stil (Hilda)

Kein festes Schema. Stimmungsbasiert je Zeitfenster (`MOOD_CAPTION_STYLE` in `planner.py`):

| Zone | Ton |
|---|---|
| `morgen` | warm, natürlich, leicht schüchtern — Morgengruß an Vertrauten |
| `mittag` | neugierig, Cliffhanger — andeutet ohne aufzulösen |
| `abend` | verführerisch, persönlich — sanfter Hinweis bei paid Posts |
| `spaet_abend` | intensiv, warm — dezente Einladung zu exklusivem Content |
| `nacht` | sehr intim, flüsternd — als ob nur mit einer Person gesprochen wird |

**Verboten:** Preise nennen, festes Hook→CTA-Schema, aggressive Verkaufssprache, englische Wörter.

---

## Hilda `content_style` (personas.py)

```
Ästhetik: weich, unschuldig, natürlich. Farben: hell, warm, pastellig.
CAPTION-STIL: Schreib natürlich und mit Gefühl — kein festes Schema, kein Verkaufszwang.
Lass die Stimmung des Moments führen: manchmal ein warmer Morgengedanke, manchmal
leise Spannung, manchmal etwas sehr Intimes. Weniger Worte, mehr Herz.
TON: casual Deutsch, warm, leicht geheimnisvoll, persönlich.
NIEMALS: Preise nennen, aggressiv verkaufen, feste Strukturen aufzwingen.
IMMER: Authentizität, Nähe, das Gefühl 'diese Person meint genau mich'.
Emojis: sparsam, nur 🌸🥺💕
```

---

## Verifikation

```bash
# Test ohne Drive/Recherche
cd /home/user/ideal-memory
python scripts/orchestrator.py --skip-drive --skip-research --persona hilda

# Direkter Planner-Test (Caption + Hashtag)
python scripts/content/planner.py --test --persona hilda

# Modell-Check
python -c "from shared.ai_client import CLAUDE_MODEL, PLAN_MODEL; print(CLAUDE_MODEL, PLAN_MODEL)"
# Erwartet: claude-3-5-sonnet-20241022  claude-3-opus-20240229
```

Erwartetes Ergebnis:
- `/mnt/Arbeit/Planung/Hilda/Plan für die nächsten Tage/{today}_{day}/` erstellt
- `/mnt/Arbeit/Planung/Hilda/Massennachrichten/{today}_{day}_hilda/` mit 5 Dateien (2 mit `_paid_`)
- 12 Posts über alle 5 Zeitfenster, max. 3 paid (nur abend/spaet_abend/nacht)
