#!/usr/bin/env bash
# =============================================================================
# 4based-system — Install Script
# =============================================================================
# Einmalig ausführen um alle Abhängigkeiten zu installieren.
# Danach: bash install.sh
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*"; }
info() { echo -e "${BLUE}[INFO]${NC} $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ""
echo "=============================================="
echo "  4based-system — Setup"
echo "=============================================="
echo ""

# ─── 1. Python prüfen ─────────────────────────────────────────────────────────
info "Prüfe Python..."
if ! command -v python3 &>/dev/null; then
    err "Python 3 nicht gefunden. Bitte installieren: sudo apt install python3 python3-pip"
    exit 1
fi
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
info "Python $PY_VER gefunden"
if python3 -c "import sys; exit(0 if sys.version_info >= (3,10) else 1)"; then
    ok "Python Version OK (≥ 3.10)"
else
    warn "Python < 3.10 erkannt. Empfohlen: 3.10 oder neuer."
fi

# ─── 2. pip / venv ────────────────────────────────────────────────────────────
info "Erstelle virtuelle Umgebung (.venv)..."
if [ ! -d "$SCRIPT_DIR/.venv" ]; then
    python3 -m venv "$SCRIPT_DIR/.venv"
    ok "Virtuelle Umgebung erstellt"
else
    ok "Virtuelle Umgebung bereits vorhanden"
fi

# Aktivieren
source "$SCRIPT_DIR/.venv/bin/activate"

# ─── 3. Python-Pakete ─────────────────────────────────────────────────────────
info "Installiere Python-Pakete..."
pip install --quiet --upgrade pip
pip install --quiet \
    openai \
    playwright \
    duckduckgo-search \
    google-api-python-client \
    google-auth
ok "Python-Pakete installiert"

# ─── 4. Playwright Browser ────────────────────────────────────────────────────
info "Installiere Playwright-Browser (Chromium)..."
python3 -m playwright install chromium
ok "Playwright Chromium installiert"

# ─── 5. ffmpeg prüfen ─────────────────────────────────────────────────────────
info "Prüfe ffmpeg..."
if command -v ffmpeg &>/dev/null && command -v ffprobe &>/dev/null; then
    ok "ffmpeg + ffprobe gefunden"
else
    warn "ffmpeg nicht gefunden — Video-Analyse wird nicht funktionieren"
    echo "  → Ubuntu/Debian:  sudo apt install ffmpeg"
    echo "  → macOS:          brew install ffmpeg"
fi

# ─── 6. Ollama prüfen ─────────────────────────────────────────────────────────
info "Prüfe Ollama..."
if command -v ollama &>/dev/null; then
    ok "Ollama gefunden"
    # Modelle prüfen
    if ollama list 2>/dev/null | grep -q "qwen3:14b"; then
        ok "Modell qwen3:14b vorhanden"
    else
        warn "Modell qwen3:14b nicht geladen"
        echo "  → ollama pull qwen3:14b"
    fi
    if ollama list 2>/dev/null | grep -q "llava:13b"; then
        ok "Modell llava:13b vorhanden"
    else
        warn "Modell llava:13b nicht geladen"
        echo "  → ollama pull llava:13b"
    fi
else
    warn "Ollama nicht gefunden"
    echo "  → https://ollama.ai installieren, dann:"
    echo "    ollama pull qwen3:14b"
    echo "    ollama pull llava:13b"
fi

# ─── 7. Ordnerstruktur erstellen ──────────────────────────────────────────────
info "Erstelle Ordnerstruktur..."
mkdir -p \
    "$SCRIPT_DIR/config" \
    "$SCRIPT_DIR/data/analysis" \
    "$SCRIPT_DIR/data/research" \
    "$SCRIPT_DIR/data/drive_cache/media" \
    "$SCRIPT_DIR/data/Plan für die nächsten Tage/Hilda" \
    "$SCRIPT_DIR/data/Plan für die nächsten Tage/Tia" \
    "$SCRIPT_DIR/logs" \
    "$SCRIPT_DIR/state"
ok "Ordner erstellt"

# ─── 8. Config-Dateien erstellen (falls nicht vorhanden) ──────────────────────
info "Erstelle Konfigurationsdateien..."

ORCH_CFG="$SCRIPT_DIR/config/orchestrator.json"
if [ ! -f "$ORCH_CFG" ]; then
    cat > "$ORCH_CFG" << 'EOF'
{
  "drive_source_folder_id": "",
  "drive_output_folder_id": "",
  "min_content_score": 6,
  "posts_per_day": 11,
  "mass_msg_count": 4,
  "paid_every": 5,
  "personas": ["hilda", "tia"],
  "research_every_days": 3
}
EOF
    warn "config/orchestrator.json erstellt — bitte Google Drive Folder-IDs eintragen!"
else
    ok "config/orchestrator.json bereits vorhanden"
fi

HILDA_CFG="$SCRIPT_DIR/config/hilda.json"
if [ ! -f "$HILDA_CFG" ]; then
    cat > "$HILDA_CFG" << 'EOF'
{
  "username": "",
  "password": "",
  "headless": true,
  "loop_interval_minutes": 30,
  "max_new_per_run": 5
}
EOF
    warn "config/hilda.json erstellt — bitte Username + Password eintragen!"
else
    ok "config/hilda.json bereits vorhanden"
fi

TIA_CFG="$SCRIPT_DIR/config/tia.json"
if [ ! -f "$TIA_CFG" ]; then
    cat > "$TIA_CFG" << 'EOF'
{
  "username": "",
  "password": "",
  "headless": true,
  "loop_interval_minutes": 30,
  "max_new_per_run": 5
}
EOF
    warn "config/tia.json erstellt — bitte Username + Password eintragen!"
else
    ok "config/tia.json bereits vorhanden"
fi

GKEY="$SCRIPT_DIR/config/google_service_account.json"
if [ ! -f "$GKEY" ]; then
    warn "config/google_service_account.json fehlt"
    echo "  → Google Cloud Console → Service Account → JSON-Key downloadladen"
    echo "    und nach config/google_service_account.json kopieren"
else
    ok "config/google_service_account.json vorhanden"
fi

# ─── 9. Zusammenfassung ───────────────────────────────────────────────────────
echo ""
echo "=============================================="
echo "  Setup abgeschlossen"
echo "=============================================="
echo ""
echo "Nächste Schritte:"
echo ""
echo "  1. Aktiviere die virtuelle Umgebung:"
echo "       source .venv/bin/activate"
echo ""
echo "  2. Fülle config/hilda.json + config/tia.json aus (Username/Passwort)"
echo ""
echo "  3. (Optional) Google Drive:"
echo "       - Service Account Key → config/google_service_account.json"
echo "       - Folder-IDs → config/orchestrator.json"
echo ""
echo "  4. Lade Ollama-Modelle (falls noch nicht vorhanden):"
echo "       ollama pull qwen3:14b"
echo "       ollama pull llava:13b"
echo ""
echo "  5. Starte das System:"
echo "       python scripts/orchestrator.py"
echo "       python scripts/hilda_runner.py"
echo "       python scripts/tia_runner.py"
echo ""
echo "  Details: siehe ANLEITUNG.md"
echo ""
