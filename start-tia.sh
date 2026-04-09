#!/bin/bash
# ─── Runner: Tia ──────────────────────────────────────────────────────────────
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ ! -f ".venv/bin/python" ]; then
    echo "[ERROR] .venv nicht gefunden — erst: bash install.sh"
    exit 1
fi

if [[ ! " $* " =~ " --save-session " ]] && [ ! -f "state/tia.storage.json" ]; then
    echo "[ERROR] Keine Session für Tia — erst einloggen:"
    echo "  ./start-tia.sh --save-session"
    exit 1
fi

echo "[TIA] Runner startet..."
exec .venv/bin/python scripts/runner.py --persona tia "$@"
