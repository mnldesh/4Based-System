#!/bin/bash
# =============================================================================
# 4Based Automation System — Vollständiges Installations-Script
# Ausführen: bash install.sh
# =============================================================================

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo ""
echo "============================================================"
echo "  4Based Automation System — Installation"
echo "============================================================"
echo ""

# ─── 1. System-Pakete ─────────────────────────────────────────────────────────
info "System-Pakete installieren..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-pip python3-venv \
    ffmpeg git curl wget 2>/dev/null || true
ok "System-Pakete installiert"

# ─── 2. Python venv ───────────────────────────────────────────────────────────
info "Python Virtual Environment..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    ok "venv erstellt"
else
    ok "venv bereits vorhanden"
fi

source .venv/bin/activate

# ─── 3. Python-Pakete ─────────────────────────────────────────────────────────
info "Python-Pakete installieren..."
pip install --upgrade pip -q

pip install -q \
    openai \
    playwright \
    ddgs \
    google-api-python-client \
    google-auth \
    google-auth-oauthlib \
    Pillow \
    requests

ok "Python-Pakete installiert"

# ─── 4. Playwright Browser ────────────────────────────────────────────────────
info "Playwright Chromium installieren..."
python -m playwright install chromium
python -m playwright install-deps chromium 2>/dev/null || true
ok "Playwright installiert"

# ─── 5. Ollama ────────────────────────────────────────────────────────────────
info "Ollama prüfen..."
if ! command -v ollama &>/dev/null; then
    info "Ollama installieren..."
    curl -fsSL https://ollama.ai/install.sh | sh
    ok "Ollama installiert"
else
    ok "Ollama bereits vorhanden"
fi

# Ollama starten falls nicht aktiv
if ! curl -s http://127.0.0.1:11434/api/tags &>/dev/null; then
    info "Ollama starten..."
    ollama serve &>/dev/null &
    sleep 8
fi

# ─── 6. Ollama Modelle ────────────────────────────────────────────────────────
info "Ollama Modelle prüfen..."
warn "qwen3:14b = ~9.3 GB | llava:7b = ~4.7 GB — Download kann lang dauern!"

if ! ollama list 2>/dev/null | grep -q "qwen3:14b"; then
    info "qwen3:14b herunterladen (~9.3 GB)..."
    ollama pull qwen3:14b
    ok "qwen3:14b installiert"
else
    ok "qwen3:14b bereits vorhanden"
fi

if ! ollama list 2>/dev/null | grep -q "llava:7b"; then
    info "llava:7b herunterladen (~4.7 GB)..."
    ollama pull llava:7b
    ok "llava:7b installiert"
else
    ok "llava:7b bereits vorhanden"
fi

# ─── 7. Ordnerstruktur ────────────────────────────────────────────────────────
info "Ordnerstruktur erstellen..."
mkdir -p \
    config \
    data/drive_cache/tmp \
    data/drive_cache/media \
    data/analysis \
    data/research \
    "data/Plan für die nächsten Tage" \
    state \
    logs
ok "Ordner erstellt"

# ─── 8. Config erstellen falls nicht vorhanden ────────────────────────────────
if [ ! -f "config/orchestrator.json" ]; then
    cat > config/orchestrator.json << 'CONF'
{
  "drive_source_folder_hilda": "",
  "drive_source_folder_tia":   "",
  "drive_output_folder_id":    "",
  "min_content_score": 6,
  "posts_per_day": 11,
  "mass_msg_count": 4,
  "paid_every": 5,
  "personas": ["hilda", "tia"],
  "research_every_days": 3
}
CONF
    ok "config/orchestrator.json erstellt"
else
    ok "config/orchestrator.json bereits vorhanden"
fi

# ─── 9. Fertig ────────────────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo -e "  ${GREEN}Installation abgeschlossen!${NC}"
echo "============================================================"
echo ""
echo "NÄCHSTE SCHRITTE:"
echo ""
echo "1. Google Drive (falls noch nicht eingerichtet):"
echo "   → config/google_oauth_client.json ablegen"
echo "   → Ordner-IDs in config/orchestrator.json eintragen"
echo ""
echo "2. Testen:"
echo "   source .venv/bin/activate"
echo "   python scripts/hilda_planner.py --dry-run"
echo "   python scripts/tia_planner.py   --dry-run"
echo ""
echo "3. Produktiv:"
echo "   python scripts/hilda_runner.py"
echo "   python scripts/tia_runner.py"
echo ""
