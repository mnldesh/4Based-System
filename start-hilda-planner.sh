#!/bin/bash
# ─── Planner: Hilda ───────────────────────────────────────────────────────────
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ ! -f ".venv/bin/python" ]; then
    echo "[ERROR] .venv nicht gefunden — erst: bash install.sh"
    exit 1
fi

echo "[HILDA PLANNER] Content-Plan wird erstellt..."
PYTHONPATH="$DIR/scripts" exec .venv/bin/python scripts/plan.py --persona hilda "$@"
