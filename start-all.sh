#!/bin/bash
# ─── Beide Runner parallel starten (Hilda + Tia) ─────────────────────────────
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ ! -f ".venv/bin/python" ]; then
    echo "[ERROR] .venv nicht gefunden — erst: bash install.sh"
    exit 1
fi

# Log-Dateien
LOG_HILDA="logs/hilda.log"
LOG_TIA="logs/tia.log"
mkdir -p logs

# Prüfen ob Sessions vorhanden
MISSING=0
if [ ! -f "hilda.storage.json" ]; then
    echo "[WARN] Keine Session für Hilda — ./start-hilda.sh --save-session"
    MISSING=$((MISSING+1))
fi
if [ ! -f "tia.storage.json" ]; then
    echo "[WARN] Keine Session für Tia — ./start-tia.sh --save-session"
    MISSING=$((MISSING+1))
fi
if [ $MISSING -gt 0 ]; then
    echo "[ERROR] Sessions fehlen. Erst einloggen, dann start-all.sh erneut starten."
    exit 1
fi

echo ""
echo "╔══════════════════════════════════════╗"
echo "║  4Based — Beide Runner starten       ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "  Hilda → logs/hilda.log"
echo "  Tia   → logs/tia.log"
echo ""
echo "  Stoppen: Ctrl+C (beendet beide)"
echo ""

# Trap für sauberes Beenden beider Prozesse
cleanup() {
    echo ""
    echo "[ALL] Stoppe Hilda + Tia..."
    kill "$PID_HILDA" "$PID_TIA" 2>/dev/null
    wait "$PID_HILDA" "$PID_TIA" 2>/dev/null
    echo "[ALL] Beendet."
    exit 0
}
trap cleanup SIGINT SIGTERM

# Runner starten
.venv/bin/python scripts/runner.py --persona hilda --headless "$@" 2>&1 | tee "$LOG_HILDA" &
PID_HILDA=$!

.venv/bin/python scripts/runner.py --persona tia --headless "$@" 2>&1 | tee "$LOG_TIA" &
PID_TIA=$!

echo "[ALL] Hilda PID: $PID_HILDA | Tia PID: $PID_TIA"
echo ""

# Warten bis einer abbricht
wait -n "$PID_HILDA" "$PID_TIA" 2>/dev/null || wait "$PID_HILDA" "$PID_TIA"
cleanup
