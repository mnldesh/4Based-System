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
warn "qwen3:14b ≈ 9.3 GB | llava:7b ≈ 4.7 GB — Download kann dauern!"

if curl -s http://127.0.0.1:11434/api/tags &>/dev/null; then
    if ! ollama list 2>/dev/null | grep -q "qwen3:14b"; then
        info "qwen3:14b herunterladen..."
        ollama pull qwen3:14b && ok "qwen3:14b installiert"
    else
        ok "qwen3:14b bereits vorhanden"
    fi

    if ! ollama list 2>/dev/null | grep -q "llava:7b"; then
        info "llava:7b herunterladen..."
        ollama pull llava:7b && ok "llava:7b installiert"
    else
        ok "llava:7b bereits vorhanden"
    fi
else
    warn "Ollama nicht erreichbar — Modelle übersprungen. Später manuell: ollama pull qwen3:14b && ollama pull llava:7b"
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
chmod +x start.sh 2>/dev/null && ok "start.sh ausführbar" || true

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
python -c "import openai; import playwright; print('  openai + playwright: OK')" || { warn "openai/playwright Import fehlgeschlagen"; ERRORS=$((ERRORS+1)); }
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
echo "  1. Sessions speichern (einmalig):"
echo "     ./start.sh hilda --save-session"
echo "     ./start.sh tia   --save-session"
echo ""
echo "  2. Testen (ohne Nachrichten zu senden):"
echo "     ./start.sh hilda --dry-run --once"
echo "     ./start.sh tia   --dry-run --once"
echo ""
echo "  3. Produktiv starten:"
echo "     ./start.sh hilda --headless"
echo "     ./start.sh tia   --headless"
echo ""
echo "  4. Planer (Pläne → /mnt/Arbeit/Planung):"
echo "     .venv/bin/python scripts/planner.py --persona hilda --dry-run"
echo "     .venv/bin/python scripts/planner.py --persona tia   --dry-run"
echo ""
echo "  5. Referenz-Docs eintragen:"
echo "     references/hilda-bot/  → SOUL.md, hilda-system.md, PLAYBOOK.md, MEMORY.md"
echo "     references/tia-bot/    → SOUL.md, tia-system.md,   PLAYBOOK.md, MEMORY.md"
echo ""
