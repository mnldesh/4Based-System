#!/usr/bin/env bash
# =============================================================================
#  4based-system — Vollständiges Installations-Script
#  Für Ubuntu/Debian (auch WSL auf Windows)
#
#  Ausführen:
#    chmod +x install.sh && bash install.sh
# =============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC}  $*"; }
err()  { echo -e "${RED}✗${NC} $*"; }
info() { echo -e "${BLUE}→${NC} $*"; }
head() { echo -e "\n${BOLD}$*${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

clear
echo -e "${BOLD}"
echo "  ╔══════════════════════════════════════╗"
echo "  ║       4based-system — Setup          ║"
echo "  ╚══════════════════════════════════════╝"
echo -e "${NC}"

# ─── Root-Check ───────────────────────────────────────────────────────────────
if [ "$EUID" -eq 0 ]; then
    warn "Läuft als root. Empfohlen: normaler User mit sudo-Rechten."
fi

# ─── OS prüfen ────────────────────────────────────────────────────────────────
head "[ 1 / 9 ]  System-Pakete"

if ! command -v apt-get &>/dev/null; then
    err "apt-get nicht gefunden. Dieses Script funktioniert nur auf Ubuntu/Debian."
    echo "  macOS-User: brew install python ffmpeg und dann manuell weiter."
    exit 1
fi

info "Paketliste aktualisieren..."
sudo apt-get update -qq

info "Installiere Basis-Pakete..."
sudo apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    ffmpeg \
    git \
    curl \
    wget \
    ca-certificates \
    gnupg \
    lsb-release \
    2>/dev/null

ok "System-Pakete installiert"

# ─── Python-Version prüfen ────────────────────────────────────────────────────
head "[ 2 / 9 ]  Python"

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
info "Python $PY_VER gefunden"

if python3 -c "import sys; exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
    ok "Python $PY_VER ≥ 3.10"
else
    warn "Python $PY_VER < 3.10 — installiere Python 3.11..."
    sudo apt-get install -y -qq software-properties-common 2>/dev/null || true
    sudo add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3.11 python3.11-venv python3.11-dev 2>/dev/null
    # Standardmäßig python3.11 nutzen
    sudo update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 2>/dev/null || true
    ok "Python 3.11 installiert"
fi

# ─── Virtuelle Umgebung ───────────────────────────────────────────────────────
head "[ 3 / 9 ]  Virtuelle Python-Umgebung"

if [ ! -d "$SCRIPT_DIR/.venv" ]; then
    info "Erstelle .venv..."
    python3 -m venv "$SCRIPT_DIR/.venv"
    ok ".venv erstellt"
else
    ok ".venv bereits vorhanden"
fi

# shellcheck disable=SC1091
source "$SCRIPT_DIR/.venv/bin/activate"

# ─── Python-Pakete ────────────────────────────────────────────────────────────
head "[ 4 / 9 ]  Python-Pakete"

info "pip upgraden..."
pip install --quiet --upgrade pip

info "Pakete installieren..."
pip install --quiet \
    openai \
    playwright \
    "duckduckgo-search>=6.0" \
    google-api-python-client \
    google-auth \
    google-auth-httplib2

ok "Python-Pakete installiert"

# ─── Playwright Browser ───────────────────────────────────────────────────────
head "[ 5 / 9 ]  Playwright Browser (Chromium)"

info "Installiere Chromium + Abhängigkeiten..."
python3 -m playwright install chromium --with-deps 2>&1 | tail -5
ok "Playwright Chromium bereit"

# ─── ffmpeg prüfen ────────────────────────────────────────────────────────────
head "[ 6 / 9 ]  ffmpeg"

if command -v ffmpeg &>/dev/null && command -v ffprobe &>/dev/null; then
    FFVER=$(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')
    ok "ffmpeg $FFVER + ffprobe vorhanden"
else
    err "ffmpeg fehlt trotz Installation — bitte manuell prüfen: sudo apt install ffmpeg"
fi

# ─── Ollama installieren ───────────────────────────────────────────────────────
head "[ 7 / 9 ]  Ollama (Lokale KI)"

if command -v ollama &>/dev/null; then
    OLLVER=$(ollama --version 2>/dev/null | head -1)
    ok "Ollama bereits installiert: $OLLVER"
else
    info "Installiere Ollama..."
    curl -fsSL https://ollama.ai/install.sh | sh
    ok "Ollama installiert"
fi

# Ollama im Hintergrund starten falls nicht läuft
if ! pgrep -x ollama &>/dev/null; then
    info "Starte Ollama-Dienst..."
    ollama serve &>/dev/null &
    OLLAMA_PID=$!
    sleep 3
    ok "Ollama gestartet (PID $OLLAMA_PID)"
else
    ok "Ollama läuft bereits"
fi

# Modelle laden (dauert je nach Internet 5–20 min)
echo ""
echo -e "  ${YELLOW}Ollama-Modelle werden jetzt geladen (~16 GB gesamt).${NC}"
echo -e "  ${YELLOW}Das kann 5–20 Minuten dauern je nach Internetgeschwindigkeit.${NC}"
echo ""

info "Lade qwen3:14b (Text-Modell, ~8 GB)..."
if ollama list 2>/dev/null | grep -q "qwen3:14b"; then
    ok "qwen3:14b bereits vorhanden"
else
    ollama pull qwen3:14b
    ok "qwen3:14b geladen"
fi

info "Lade llava:13b (Bild/Video-Modell, ~8 GB)..."
if ollama list 2>/dev/null | grep -q "llava:13b"; then
    ok "llava:13b bereits vorhanden"
else
    ollama pull llava:13b
    ok "llava:13b geladen"
fi

# ─── Ordnerstruktur ───────────────────────────────────────────────────────────
head "[ 8 / 9 ]  Ordner & Konfiguration"

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

# Config-Dateien erstellen (nur wenn noch nicht vorhanden)
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
    warn "config/orchestrator.json erstellt — Drive Folder-IDs noch leer"
else
    ok "config/orchestrator.json vorhanden"
fi

for PERSONA in hilda tia; do
    CFG="$SCRIPT_DIR/config/${PERSONA}.json"
    if [ ! -f "$CFG" ]; then
        cat > "$CFG" << EOF
{
  "username": "",
  "password": "",
  "headless": true
}
EOF
        warn "config/${PERSONA}.json erstellt — Username/Passwort noch leer"
    else
        ok "config/${PERSONA}.json vorhanden"
    fi
done

# ─── 4based.com Login-Sessions speichern ──────────────────────────────────────
head "[ 9 / 9 ]  4based.com Login-Sessions"

echo ""
echo -e "  Jetzt werden die Browser-Sessions für Hilda und Tia gespeichert."
echo -e "  ${YELLOW}Der Browser öffnet sich — logge dich ein und drücke ENTER.${NC}"
echo ""

save_session() {
    local NAME="$1"
    local OUT="$SCRIPT_DIR/state/${NAME}.storage.json"

    if [ -f "$OUT" ]; then
        echo -e "  ${GREEN}✓${NC} state/${NAME}.storage.json bereits vorhanden — überspringe"
        return
    fi

    echo -e "  ${BLUE}→${NC} Öffne Browser für ${BOLD}${NAME}${NC}..."
    python3 - "$NAME" "$OUT" << 'PYEOF'
import asyncio, sys
from playwright.async_api import async_playwright

async def main():
    name, out = sys.argv[1], sys.argv[2]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto("https://4based.com")
        print(f"\n  ➤  Logge dich jetzt als {name.upper()} bei 4based.com ein.")
        print(f"     Wenn du eingeloggt bist, drücke ENTER in diesem Terminal.\n")
        input("  [ENTER drücken wenn eingeloggt] ")
        await ctx.storage_state(path=out)
        await browser.close()
        print(f"  Session gespeichert: {out}\n")

asyncio.run(main())
PYEOF
}

save_session "hilda"
save_session "tia"

# ─── Abschluss ────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}"
echo "  ╔══════════════════════════════════════╗"
echo "  ║       Installation abgeschlossen!    ║"
echo "  ╚══════════════════════════════════════╝"
echo -e "${NC}"

echo "  Was als nächstes zu tun ist:"
echo ""
echo -e "  ${YELLOW}1.${NC} Aktiviere die Umgebung:"
echo "       source .venv/bin/activate"
echo ""
echo -e "  ${YELLOW}2.${NC} Trage 4based.com Login-Daten ein (falls noch nicht passiert):"
echo "       nano config/hilda.json"
echo "       nano config/tia.json"
echo ""
echo -e "  ${YELLOW}3.${NC} (Optional) Google Drive:"
echo "       config/google_service_account.json → Service Account JSON dort ablegen"
echo "       config/orchestrator.json → drive_source_folder_id eintragen"
echo ""
echo -e "  ${YELLOW}4.${NC} System starten:"
echo "       python scripts/orchestrator.py          # Tagesplan erstellen"
echo "       python scripts/hilda_runner.py          # Hilda DM-Bot"
echo "       python scripts/tia_runner.py            # Tia DM-Bot"
echo ""
echo "  Details: ANLEITUNG.md"
echo ""
