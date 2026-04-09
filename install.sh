#!/bin/bash
# =============================================================================
# 4Based Automation System — Installations-Script
# Ausführen: bash install.sh
# =============================================================================

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[ OK ]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
step()  { echo -e "\n${BLUE}══════════════════════════════════════${NC}"; echo -e "${BLUE}  $1${NC}"; echo -e "${BLUE}══════════════════════════════════════${NC}"; }

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo ""
echo "╔══════════════════════════════════════╗"
echo "║  4Based Automation System            ║"
echo "║  Installation                        ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "  Verzeichnis: $DIR"
echo ""

# ─── 1. System-Pakete ─────────────────────────────────────────────────────────
step "1. System-Pakete"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-pip python3-venv \
    ffmpeg git curl wget 2>/dev/null || true
ok "System-Pakete installiert"

# ─── 2. Python venv ───────────────────────────────────────────────────────────
step "2. Python Virtual Environment (.venv)"
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    ok ".venv erstellt"
else
    ok ".venv bereits vorhanden"
fi

# Immer aktivieren für den Rest des Scripts
source .venv/bin/activate
ok "venv aktiviert: $(python --version)"

# ─── 3. Python-Pakete ─────────────────────────────────────────────────────────
step "3. Python-Pakete (in .venv)"
pip install --upgrade pip -q

pip install -q \
    openai \
    anthropic \
    python-dotenv \
    playwright \
    ddgs \
    google-api-python-client \
    google-auth \
    google-auth-oauthlib \
    Pillow \
    requests

ok "Python-Pakete installiert"

# Versionen ausgeben
echo "  openai:      $(pip show openai      2>/dev/null | grep Version | cut -d' ' -f2)"
echo "  playwright:  $(pip show playwright  2>/dev/null | grep Version | cut -d' ' -f2)"

# ─── 4. Playwright Browser ────────────────────────────────────────────────────
step "4. Playwright Chromium"
python -m playwright install chromium
python -m playwright install-deps chromium 2>/dev/null || true
ok "Playwright Chromium installiert"

# ─── 5. Ollama ────────────────────────────────────────────────────────────────
step "5. Ollama"
if ! command -v ollama &>/dev/null; then
    info "Ollama installieren..."
    curl -fsSL https://ollama.ai/install.sh | sh
    ok "Ollama installiert"
else
    ok "Ollama bereits vorhanden: $(ollama --version 2>/dev/null || echo 'unbekannte Version')"
fi

# Ollama starten falls nicht aktiv
if ! curl -s http://127.0.0.1:11434/api/tags &>/dev/null; then
    info "Ollama starten..."
    ollama serve &>/dev/null &
    for i in $(seq 1 15); do
        sleep 1
        if curl -s http://127.0.0.1:11434/api/tags &>/dev/null; then
            ok "Ollama gestartet (${i}s)"
            break
        fi
    done
    if ! curl -s http://127.0.0.1:11434/api/tags &>/dev/null; then
        warn "Ollama nicht erreichbar — Modelle können nicht geprüft werden"
    fi
else
    ok "Ollama läuft bereits"
fi

# ─── 6. Ollama Modelle ────────────────────────────────────────────────────────
step "6. Ollama Modelle"
warn "qwen2.5:latest ≈ 4.7 GB | llava:7b ≈ 4.7 GB — Download kann dauern!"

if curl -s http://127.0.0.1:11434/api/tags &>/dev/null; then
    if ! ollama list 2>/dev/null | grep -q "qwen2.5:latest"; then
        info "qwen2.5:latest herunterladen (Chat-Fallback + Planung)..."
        ollama pull qwen2.5:latest && ok "qwen2.5:latest installiert"
    else
        ok "qwen2.5:latest bereits vorhanden"
    fi

    if ! ollama list 2>/dev/null | grep -q "llava:7b"; then
        info "llava:7b herunterladen (Video/Bild-Analyse)..."
        ollama pull llava:7b && ok "llava:7b installiert"
    else
        ok "llava:7b bereits vorhanden"
    fi
else
    warn "Ollama nicht erreichbar — Modelle übersprungen. Später manuell:"
    warn "  ollama pull qwen2.5:latest"
    warn "  ollama pull llava:7b"
fi

# ─── 7. Ordnerstruktur ────────────────────────────────────────────────────────
step "7. Ordnerstruktur"

# Lokale Ordner im Projektverzeichnis
mkdir -p \
    config \
    data/drive_cache/tmp \
    data/drive_cache/media \
    data/analysis \
    data/research \
    state \
    logs \
    references/hilda-bot \
    references/tia-bot
ok "Projektordner erstellt"

# Planungs-Zielordner (externer Mount)
PLAN_DIR="/mnt/Arbeit/Planung"
if [ -d "/mnt/Arbeit" ]; then
    mkdir -p "$PLAN_DIR/Hilda" "$PLAN_DIR/Tia"
    ok "Planungs-Ordner erstellt: $PLAN_DIR"
else
    warn "/mnt/Arbeit nicht eingehängt — $PLAN_DIR wird beim ersten Planer-Lauf erstellt"
    warn "Sicherstellen: mount /mnt/Arbeit vor dem Start von planner.py"
fi

# ─── 8. Startscripte ausführbar machen ────────────────────────────────────────
step "8. Startscripte"
for SCRIPT in start.sh start-hilda.sh start-tia.sh start-hilda-planner.sh start-tia-planner.sh start-all.sh; do
    chmod +x "$SCRIPT" 2>/dev/null && ok "$SCRIPT ausführbar" || warn "$SCRIPT nicht gefunden"
done

# ─── 9. Config erstellen falls nicht vorhanden ────────────────────────────────
step "9. Konfiguration"

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

if [ ! -f ".env" ]; then
    cat > .env << 'ENVFILE'
# Anthropic API Key — hier eintragen
# Erhältlich unter: https://console.anthropic.com/
ANTHROPIC_API_KEY=sk-ant-...
ENVFILE
    ok ".env Vorlage erstellt — ANTHROPIC_API_KEY eintragen!"
else
    ok ".env bereits vorhanden"
fi

if [ ! -f "config/blacklist.json" ]; then
    echo '["4Based"]' > config/blacklist.json
    ok "config/blacklist.json erstellt (enthält: 4Based)"
else
    ok "config/blacklist.json bereits vorhanden"
fi

# ─── 10. Installationscheck ───────────────────────────────────────────────────
step "10. Installations-Check"
ERRORS=0

# Python-Imports testen
python -c "import openai; import anthropic; import playwright; print('  openai + anthropic + playwright: OK')" || { warn "openai/anthropic/playwright Import fehlgeschlagen"; ERRORS=$((ERRORS+1)); }
python -c "import sys; sys.path.insert(0, 'scripts'); from shared.personas import PERSONAS; print('  personas: OK')" || { warn "personas Import fehlgeschlagen"; ERRORS=$((ERRORS+1)); }
python -c "import sys; sys.path.insert(0, 'scripts'); from shared.ai_client import make_client; print('  ai_client: OK')" || { warn "ai_client Import fehlgeschlagen"; ERRORS=$((ERRORS+1)); }
python -c "import sys; sys.path.insert(0, 'scripts'); from shared.base_runner import load_account; print('  base_runner: OK')" || { warn "base_runner Import fehlgeschlagen"; ERRORS=$((ERRORS+1)); }

if [ $ERRORS -eq 0 ]; then
    ok "Alle Imports erfolgreich"
else
    warn "$ERRORS Import-Fehler — bitte prüfen"
fi

# ─── Fertig ───────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════╗"
if [ $ERRORS -eq 0 ]; then
echo -e "║  ${GREEN}Installation erfolgreich!${NC}             ║"
else
echo -e "║  ${YELLOW}Installation mit Warnungen${NC}            ║"
fi
echo "╚══════════════════════════════════════╝"
echo ""
echo "NÄCHSTE SCHRITTE:"
echo ""
echo "  0. API Key eintragen:"
echo "     nano .env   →   ANTHROPIC_API_KEY=sk-ant-..."
echo ""
echo "  1. Sessions speichern (einmalig, Browser öffnet sich):"
echo "     ./start-hilda.sh --save-session"
echo "     ./start-tia.sh   --save-session"
echo ""
echo "  2. Testen (kein Versand):"
echo "     ./start-hilda.sh --dry-run --once"
echo "     ./start-tia.sh   --dry-run --once"
echo ""
echo "  3. Einzeln produktiv starten:"
echo "     ./start-hilda.sh --headless"
echo "     ./start-tia.sh   --headless"
echo ""
echo "  4. Beide gleichzeitig starten:"
echo "     ./start-all.sh"
echo "     (Logs: logs/hilda.log | logs/tia.log)"
echo ""
echo "  5. Content-Planer:"
echo "     ./start-hilda-planner.sh          # Tagesplan für Hilda"
echo "     ./start-tia-planner.sh            # Tagesplan für Tia"
echo "     ./start-hilda-planner.sh --dry-run  # Nur testen"
echo ""
echo "  6. Modell-Übersicht:"
echo "     Chat:         Claude API (claude-sonnet-4-6)"
echo "     Chat-Fallback: qwen2.5:latest"
echo "     Planung/Captions: qwen2.5:latest"
echo "     Videoanalyse: llava:7b"
echo ""
