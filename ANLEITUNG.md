# 4based-system — Vollständige Anleitung

## Übersicht

Das System automatisiert den täglichen Workflow für zwei Creator-Profile (Hilda + Tia):

| Komponente | Funktion |
|---|---|
| `orchestrator.py` | Tagesplan erstellen (Analyse → Recherche → Pläne) |
| `runner.py` | Profil auf 4based.com bedienen (--persona hilda\|tia) |
| `content/analyzer.py` | Bilder + Videos analysieren (Score, Kategorie, Caption-Idee) |
| `content/researcher.py` | Marketing-Trends recherchieren (DuckDuckGo) |
| `content/planner.py` | Content-Plan generieren (Posts + Massennachrichten) |
| `drive/manager.py` | Google Drive Integration |

---

## 1. Installation

```bash
# Repository klonen / entpacken
cd /pfad/zum/projekt

# Einmalig Setup-Script ausführen
bash install.sh
```

Das Script installiert automatisch:
- Python-Pakete (openai, playwright, duckduckgo-search, google-api-python-client)
- Playwright Chromium Browser
- Erstellt alle Ordner und Basis-Configs

**Danach virtuelle Umgebung aktivieren:**
```bash
source .venv/bin/activate
```

---

## 2. Konfiguration

### 2.1 Profile einrichten (`config/hilda.json`, `config/tia.json`)

```json
{
  "username": "dein_4based_username",
  "password": "dein_passwort",
  "headless": true,
  "loop_interval_minutes": 30,
  "max_new_per_run": 5
}
```

| Feld | Bedeutung |
|---|---|
| `username` | 4based.com Login |
| `password` | 4based.com Passwort |
| `headless` | `true` = kein Browser-Fenster (Server), `false` = sichtbar (Debugging) |
| `loop_interval_minutes` | Pause zwischen Durchläufen (Empfehlung: 30) |
| `max_new_per_run` | Max. neue User pro Durchlauf |

### 2.2 Orchestrator (`config/orchestrator.json`)

```json
{
  "drive_source_folder_id": "GOOGLE_DRIVE_ORDNER_ID",
  "drive_output_folder_id": "GOOGLE_DRIVE_ORDNER_ID",
  "min_content_score": 6,
  "posts_per_day": 11,
  "mass_msg_count": 4,
  "paid_every": 5,
  "personas": ["hilda", "tia"],
  "research_every_days": 3
}
```

| Feld | Bedeutung |
|---|---|
| `drive_source_folder_id` | Drive-Ordner mit deinen Medien (Bilder/Videos) |
| `drive_output_folder_id` | Drive-Ordner für Pläne + Feedback |
| `min_content_score` | Mindest-Score für Content (1–10, Standard: 6) |
| `posts_per_day` | Anzahl Posts täglich (Standard: 11) |
| `mass_msg_count` | Massennachrichten pro Tag (Standard: 4) |
| `paid_every` | Jeder N-te Post ist Paid-Post (Standard: 5) |
| `research_every_days` | Recherche nur alle N Tage neu (Standard: 3) |

### 2.3 Google Drive (optional)

1. Google Cloud Console → Neues Projekt
2. **Google Drive API** aktivieren
3. **Service Account** erstellen → JSON-Key downloaden
4. Key nach `config/google_service_account.json` kopieren
5. Service-Account-Email im Drive-Ordner als **Editor** hinzufügen
6. Folder-IDs aus der Drive-URL kopieren:
   `https://drive.google.com/drive/folders/`**`DIESE_ID`**

### 2.4 Ollama (AI-Modelle)

```bash
# Ollama installieren
curl -fsSL https://ollama.ai/install.sh | sh

# Modelle laden (einmalig, braucht ~15GB Speicher)
ollama pull qwen3:14b   # Text-Modell (~8GB)
ollama pull llava:13b   # Vision-Modell (~8GB)

# Testen ob Ollama läuft
ollama list
```

---

## 3. Tagesablauf — Orchestrator

```bash
# Vollständiger Tagesablauf (empfohlen)
python scripts/orchestrator.py

# Optionen:
python scripts/orchestrator.py --skip-drive        # Ohne Google Drive (lokal)
python scripts/orchestrator.py --skip-research     # Keine neue Recherche
python scripts/orchestrator.py --persona hilda     # Nur Hilda
python scripts/orchestrator.py --dry-run           # Nichts hochladen
```

### Was passiert beim Start:

```
[ORCHESTRATOR] Start — 2025-01-15 08:00:00
  Personas : hilda, tia
  Drive    : JA

[STEP 1+2] Drive scannen (parallel)...
  [IMG] foto_001.jpg ... Score 8/10 [paid] [softcore]
  [VID] video_002.mp4 ... Score 9/10 [ppv] [explicit]
  ✓ 12 Dateien analysiert

[STEP 3] Starte Marketing-Recherche (parallel)...
  [1/8] OnlyFans marketing strategies 2025 (5 Treffer)
  ...
  ✓ 40 Ergebnisse, Strategien erstellt

[STEP 4] Content-Pläne erstellen...
  [HILDA] 11 Posts + 4 Massennachrichten (parallel)
  [TIA]   11 Posts + 4 Massennachrichten (parallel)

[STEP 5] Top-Dateien → Drive Feedback-Ordner
[STEP 6] Pläne in Drive hochladen
```

### Pläne werden gespeichert unter:

```
data/Plan für die nächsten Tage/
  Hilda/
    2025-01-15_Mittwoch/
      plan_hilda_2025-01-15.json    ← Maschinenlesbar
      plan_hilda_2025-01-15.txt     ← Lesbar
  Tia/
    2025-01-15_Mittwoch/
      plan_tia_2025-01-15.json
      plan_tia_2025-01-15.txt
```

---

## 4. Automatische Antworten — Runner

```bash
# Hilda-Profil starten
python scripts/runner.py --persona hilda

# Tia-Profil starten
python scripts/runner.py --persona tia

# Optionen:
python scripts/runner.py --persona hilda --headless          # Kein Browser-Fenster
```

### Was die Runner machen:

- Loggen sich bei 4based.com ein
- Scannen neue Nachrichten alle 30 Minuten
- Klassifizieren User (NEU / KALT / AKTIV / KÄUFER / PREMIUM)
- Generieren personalisierte Antworten per KI
- Senden Antworten automatisch
- Protokollieren alles in `logs/runner.jsonl`

### User-Klassifizierung:

| Typ | Kriterium | Strategie |
|---|---|---|
| `NEU` | Erste Nachricht | Warm, neugierig machen |
| `KALT` | Schreibt selten | Re-engagen, Angebot |
| `KALT_HART` | Lange inaktiv | Starkes Angebot, Voucher |
| `AKTIV` | Schreibt regelmäßig | Relationship aufbauen |
| `KAEUFER` | Hat gekauft | Premium-Behandlung |
| `PREMIUM` | Viel ausgegeben | VIP-Behandlung |

---

## 5. Content-Analyse (manuell)

```bash
# Einzelnen Ordner analysieren
python scripts/content/analyzer.py /pfad/zu/medien

# Mit Mindest-Score
python scripts/content/analyzer.py /pfad/zu/medien --min-score 7

# Nur Bilder
python scripts/content/analyzer.py /pfad/zu/bilder --type images
```

### Score-Bedeutung:

| Score | Bedeutung | Empfehlung |
|---|---|---|
| 9–10 | Herausragend | PPV / Paid-Post |
| 7–8 | Sehr gut | Paid-Post oder guter Free-Post |
| 5–6 | Gut | Free-Post |
| 3–4 | Durchschnitt | Story / Mass-Message Preview |
| 1–2 | Schwach | Nicht verwenden |

### Placement-Typen:

- `free_teaser` — kostenloser Vorschau-Post
- `paid` — Paid-Post (Standard-Abo)
- `ppv` — Pay-Per-View (Einzelkauf)
- `story` — Story/temporärer Content
- `mass_msg_preview` — Vorschau in Massennachricht

---

## 6. Marketing-Recherche (manuell)

```bash
# Neue Recherche starten
python scripts/content/researcher.py

# Mit eigenem Output-Ordner
python scripts/content/researcher.py --output-dir /pfad/zu/ordner
```

Ergebnisse in `data/research/insights_DATUM.json`

---

## 7. Ordnerstruktur

```
4based-system/
├── config/
│   ├── hilda.json                    ← Hilda Login + Einstellungen
│   ├── tia.json                      ← Tia Login + Einstellungen
│   ├── orchestrator.json             ← Tagesplan-Einstellungen
│   └── google_service_account.json   ← Google Drive Key (manuell hinzufügen)
├── data/
│   ├── analysis/                     ← Analyse-Ergebnisse (JSON)
│   ├── research/                     ← Marketing-Insights (JSON)
│   ├── drive_cache/media/            ← Temporäre Downloads aus Drive
│   └── Plan für die nächsten Tage/   ← Content-Pläne
│       ├── Hilda/
│       └── Tia/
├── logs/
│   └── runner.jsonl                  ← Event-Log beider Runner
├── state/
│   ├── hilda_state.json              ← User-Daten Hilda
│   └── tia_state.json                ← User-Daten Tia
├── scripts/
│   ├── orchestrator.py
│   ├── runner.py
│   ├── planner.py
│   ├── shared/
│   │   ├── ai_client.py              ← Ollama API Client
│   │   ├── base_runner.py            ← Runner-Basis-Klasse
│   │   └── personas.py               ← Hilda + Tia Personas
│   ├── content/
│   │   ├── analyzer.py               ← Bild/Video Analyse
│   │   ├── researcher.py             ← DuckDuckGo Recherche
│   │   └── planner.py                ← Content-Plan Generator
│   └── drive/
│       └── manager.py                ← Google Drive API
├── install.sh                        ← Einmalig ausführen
└── ANLEITUNG.md                      ← Diese Datei
```

---

## 8. Empfohlener Tagesablauf (Cron)

```bash
# crontab -e
# Jeden Tag um 07:00 Uhr Tagesplan erstellen
0 7 * * * cd /pfad/zu/4based-system && .venv/bin/python scripts/orchestrator.py >> logs/orchestrator.log 2>&1

# Hilda + Tia Runner dauerhaft im Hintergrund
# (pm2 oder screen/tmux empfohlen)
```

### Mit PM2 (empfohlen für Server):

```bash
npm install -g pm2

pm2 start "python scripts/runner.py --persona hilda"  --name hilda --interpreter .venv/bin/python
pm2 start "python scripts/runner.py --persona tia"    --name tia   --interpreter .venv/bin/python
pm2 start "python scripts/orchestrator.py" --name orchestrator --cron "0 7 * * *"

pm2 save
pm2 startup    # Autostart nach Reboot
```

---

## 9. Troubleshooting

### Ollama antwortet nicht
```bash
ollama serve          # Manuell starten
curl http://localhost:11434/v1/models   # Testen
```

### Google Drive Fehler
```bash
# Verbindung testen
python scripts/drive/manager.py test
```

### Playwright Login schlägt fehl
```bash
# Sichtbaren Browser zum Debuggen
python scripts/runner.py --persona hilda --headless false
```

### Analyse schlägt fehl (kein ffmpeg)
```bash
sudo apt install ffmpeg     # Ubuntu/Debian
brew install ffmpeg         # macOS
```

### Logs prüfen
```bash
tail -f logs/runner.jsonl | python3 -c "import sys,json; [print(json.dumps(json.loads(l), indent=2, ensure_ascii=False)) for l in sys.stdin]"
```

---

## 10. Systemanforderungen

| Komponente | Minimum | Empfohlen |
|---|---|---|
| RAM | 8 GB | 16 GB |
| Speicher | 20 GB | 40 GB |
| Python | 3.10 | 3.11+ |
| OS | Ubuntu 20.04 | Ubuntu 22.04 |
| GPU | — | NVIDIA (für Ollama) |

**Ollama Modell-Größen:**
- `qwen3:14b` — ~8 GB RAM/VRAM
- `llava:13b` — ~8 GB RAM/VRAM
- Beide gleichzeitig: ~16 GB empfohlen
