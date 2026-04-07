# 4Based System — Vollständige Systemzusammenfassung

## Zweck

Automatisiertes Creator-Management-System für die Plattform 4Based.com.
Betreibt zwei Personas (Hilda Valentine, 24J + Tia, 22J) mit automatischen Chat-Antworten,
Content-Planung, Marketing-Recherche und Google-Drive-Integration.

---

## Architektur-Überblick

```
┌─────────────────────────────────────────────────────────────┐
│                     4Based System                            │
├──────────────┬──────────────┬───────────────────────────────┤
│  RUNNER      │  ORCHESTRATOR │  SUPPORT                      │
│  (Live-Chat) │  (Tagesplan)  │  (Infrastruktur)              │
├──────────────┼──────────────┼───────────────────────────────┤
│ hilda_runner │ orchestrator  │ ai_client      (Ollama API)   │
│ tia_runner   │ planner       │ personas       (Hilda/Tia)    │
│ base_runner  │ analyzer      │ config         (Pfade)        │
│              │ researcher    │ intent_analyzer (Analyse)      │
│              │ drive/manager │ install.sh     (Setup)        │
└──────────────┴──────────────┴───────────────────────────────┘
         │              │               │
         ▼              ▼               ▼
    Playwright      Google Drive     Ollama (lokal)
    (Browser)       (Cloud)          qwen3:14b + llava:7b
```

---

## Modul-für-Modul

### 1. Einstiegspunkte

| Datei | Zweck |
|---|---|
| `start_hilda.sh` | Shell-Wrapper → `.venv/bin/python scripts/hilda_runner.py "$@"` |
| `start_tia.sh` | Shell-Wrapper → `.venv/bin/python scripts/tia_runner.py "$@"` |
| `install.sh` | Einmal-Setup: apt-Pakete, venv, pip, Playwright, Ollama, Modelle, Ordnerstruktur, Configs |

### 2. Runner-System (Live-Chat)

**`scripts/hilda_runner.py` / `scripts/tia_runner.py`**
- Dünne Wrapper: parsen Args (`--dry-run`, `--once`, `--headless`, `--save-session`)
- Laden Account via `load_account("hilda", "hilda.storage.json")`
- Rufen `run_account()` auf

**`scripts/shared/base_runner.py`** — das Herzstück (1100+ Zeilen)

| Funktion | Was sie tut |
|---|---|
| `run_account()` | Hauptloop: Browser starten, Login prüfen, Inbox scannen, Pässe fahren |
| `process_chat()` | Einzelnen Chat verarbeiten: Blacklist → Cooldown → History → Analyse → Reply → Send |
| `analyze_chat()` | Intent-Analyse VOR dem Antworten (importiert aus intent_analyzer) |
| `get_ai_reply()` | AI-Antwort generieren, gesteuert durch ChatAnalysis |
| `_get_reengage_reply()` | Opener-System für User ohne Nachricht |
| `get_offer_reply()` | Angebots-Nachricht bei Kaufabsicht |
| `detect_content_intent()` | Kaufabsicht erkennen (Keyword + AI) |
| `offer_fits_context()` | Prüft ob Angebot zum Verlauf passt |
| `classify_user()` | User-Segment: NEU/KALT/KALT_HART/AKTIV/KAEUFER/PREMIUM |
| `extract_user_prefs()` | AI extrahiert Vorlieben aus Verlauf |
| `get_history()` | Playwright: Chat-Bubbles auslesen |
| `send_message()` | Playwright: Nachricht senden |
| `read_snapshot()` | Top-30 Chats scannen |
| `find_next_unprocessed()` | Nächsten unbearbeiteten Chat finden |

**Flow eines einzelnen Chats:**
```
process_chat()
  → Blacklist-Check
  → Cooldown-Check (5h)
  → History laden (20 oder 100 Msgs)
  → Trailing-Check (max 10x ohne Antwort)
  → User-Prefs extrahieren (wenn ≥2 User-Msgs)
  → analyze_chat() → ChatAnalysis {intent, emotion, goal, tone, key_detail}
  → Falls intent=kauf → get_offer_reply()
  → Falls intent=boundary → get_ai_reply(analysis) mit Respekt-Modus
  → Falls intent=reengage + keine User-Msg → _get_reengage_reply() (Opener)
  → Sonst → get_ai_reply(analysis)
  → clean_reply() → is_reply_usable() → Banned-Start-Check
  → send_message() oder [DRY] Log
```

### 3. Intent-Analyse

**`scripts/shared/intent_analyzer.py`**

| Funktion | Was sie tut |
|---|---|
| `analyze_chat()` | AI-Call (~200 Tokens) → JSON mit intent/topic/emotion/reply_goal/persona_mode/key_detail |
| `_fallback_analysis()` | Regelbasiert wenn AI fehlschlägt (Keyword-Matching) |
| `ChatAnalysis` | Dataclass mit `to_prompt_block()` für den Reply-Prompt |

**Intent-Typen:** `frage` / `flirt` / `kauf` / `unklar` / `boundary` / `reengage` / `smalltalk` / `sexuell`

**Emotionen:** `neugierig` / `reserviert` / `direkt` / `genervt` / `spielerisch` / `geil` / `schüchtern`

**Reply-Ziele:** `klären` / `vertiefen` / `spiegeln` / `eskalieren` / `verkaufen` / `zurückholen` / `necken`

**Persona-Modi:** `sanft` / `direkt` / `verspielt` / `intim` / `frech`

### 4. AI-Client

**`scripts/shared/ai_client.py`**

- Ollama auf `127.0.0.1:11434` via OpenAI-kompatible API
- Text: `qwen3:14b` | Vision: `llava:7b`
- `chat()` — Text-Completion mit System+User Prompt
- `vision()` — Bild-Analyse mit Base64-Encoding
- `clean_reply()` — Entfernt: `<think>`-Tags, Markdown-Bold, Speaker-Prefix, CJK-Zeichen, Wortmonster
- `is_reply_usable()` — Prüft: min 10 Zeichen, min 5 Buchstaben
- `parse_json_from_response()` — Robuster JSON-Parser mit Repair
- `ensure_ollama()` — Startet Ollama automatisch wenn nicht aktiv
- Retry mit exponential Backoff (3 Versuche)

### 5. Personas

**`scripts/shared/personas.py`** — Single Source of Truth

| Persona | Alter | Stil | Gutschein |
|---|---|---|---|
| Hilda Valentine | 24 | schüchtern, unschuldig, innerlich sinnlich | 35% |
| Tia | 22 | verspielt, direkt, sexy, selbstbewusst | 30% |

Jede Persona hat:
- `system` — System-Prompt für die AI (Segment-spezifische Anweisungen)
- `content_style` — Ästhetik-Beschreibung für Content-Planung
- `fallback_msg` — Notfall-Nachricht wenn AI komplett versagt
- `mass_msg` — Templates für Massen-Nachrichten (buyer/non_buyer)

### 6. Orchestrator (Tagesplan)

**`scripts/orchestrator.py`** — 6-Schritte Pipeline:

```
Step 1+2: Drive scannen → Medien analysieren (Score 1-10, Placement, Kategorie)
Step 3:   Marketing-Recherche via DuckDuckGo (parallel zu Step 1+2)
Step 4:   Content-Pläne erstellen (11 Posts/Tag + 4 Massennachrichten)
Step 5:   Top-Dateien (Score ≥8) → Drive Feedback-Ordner
Step 6:   Pläne in Drive hochladen
```

**`scripts/content/analyzer.py`** — Bild/Video-Bewertung:
- Vision-Model (llava:7b) bewertet: Score, Produktion, Erotik-Potenzial
- Placement: free_teaser / paid / ppv / story / mass_msg_preview
- Kategorie: softcore / implied / explicit / bts / lifestyle
- Videos: Frame-Extraction + parallele Analyse

**`scripts/content/researcher.py`** — Marketing-Intelligence:
- 11+ DuckDuckGo-Queries (OnlyFans Marketing, Engagement, PPV-Pricing...)
- AI fasst Ergebnisse zusammen → persona-spezifische Strategien
- Caching: nur alle N Tage neu recherchieren

**`scripts/content/planner.py`** — Plan-Generator:
- Verteilt Content auf Peak-Zeiten (18:00-04:00)
- Abwechslung: free + paid Posts, Paid alle N Posts
- AI-generierte Captions + Hashtags (parallel mit ThreadPool)
- Massennachrichten: getrennt für Käufer/Nicht-Käufer

### 7. Google Drive

**`scripts/drive/manager.py`**
- OAuth 2.0 oder Service Account
- Download: `iter_drive_media()` — streamt Dateien direkt
- Upload: Pläne, Feedback-Dateien
- Ordner: automatisches Erstellen der Tages-Struktur

---

## Konfiguration

| Datei | Inhalt |
|---|---|
| `config/orchestrator.json` | Drive-Folder-IDs, Posts/Tag, Recherche-Intervall |
| `config/hilda.json` / `tia.json` | Login-Credentials, Headless-Modus |
| `config/hilda.env` / `tia.env` | EMAIL= / PASSWORD= für Auto-Login |
| `config/blacklist.json` | Usernames die nie angeschrieben werden |
| `config/google_service_account.json` | Google Drive Service-Account-Credentials |
| `shared/config.py` | `ROOT = /home/kali/4based-system`, `PLANS_ROOT = /mnt/Arbeit/Planung` |

## State & Logs

| Datei | Inhalt |
|---|---|
| `state/hilda.storage.json` | Playwright Browser-Session (Cookies) |
| `state/tia.storage.json` | Playwright Browser-Session (Cookies) |
| `state/{name}_users.json` | Persistente User-Klassifizierung + Prefs + last_sent_at |
| `state/{name}_progress.json` | Fortschritt im aktuellen Pass (Crash-Recovery) |
| `logs/runner.jsonl` | Event-Log: drafts, sent, skips, crashes, pass_complete |

---

## User-Klassifizierung

| Segment | Kriterium | Strategie |
|---|---|---|
| NEU | Kein Verlauf | Opener aus Template |
| KALT | >=1x eigene Msg ohne Antwort | Neugier wecken |
| KALT_HART | >=5x ohne Antwort | Überraschende persönliche Frage |
| AKTIV | User hat geantwortet, $0 | Connection aufbauen |
| KAEUFER | Revenue > $0 | An Content anknüpfen |
| PREMIUM | Revenue > $20 | Intim, sehr persönlich |

---

## Technologie-Stack

| Komponente | Technologie |
|---|---|
| AI (Text) | Ollama → qwen3:14b (lokal, ~8GB VRAM) |
| AI (Vision) | Ollama → llava:7b (lokal, ~5GB VRAM) |
| Browser | Playwright (Chromium) |
| Cloud | Google Drive API |
| Recherche | DuckDuckGo Search |
| OS | Kali Linux |
| Python | 3.12 in venv |

---

## Konstanten

| Name | Wert | Beschreibung |
|---|---|---|
| MAX_TRAILING | 10 | Skip wenn >10x ohne Antwort |
| COOLDOWN_HOURS | 5 | Min. Stunden zwischen eigenen Nachrichten |
| PASS_INTERVAL | 5400s (90min) | Pause zwischen Inbox-Pässen |
| AI_RETRIES | 3 | Retry-Versuche bei AI-Fehler |
| SNAPSHOT_SIZE | 30 | Top-N Chats im periodischen Check |
| DEFAULT_HISTORY_LIMIT | 20 | Chat-History Tiefe (normal) |
| PREMIUM_HISTORY_LIMIT | 100 | Chat-History Tiefe (Premium) |
| API_TIMEOUT | 180s | Timeout für Ollama-Calls |

---

## Bekannte Einschränkungen

1. **qwen3:14b** leakt gelegentlich Chinesisch → `clean_reply()` schneidet CJK ab
2. **Garbled-Word-Filter** (`_RE_GARBLED`, >15 Buchstaben) kann lange deutsche Komposita fälschlicherweise entfernen
3. **Reengage-Opener** variiert Templates per AI → Variation manchmal minimal
4. **offer_fits_context()** wird auch aufgerufen wenn analyse.intent != "kauf" → 2 extra AI-Calls pro Chat
