#!/bin/bash
# ─── Planner: Tia ─────────────────────────────────────────────────────────────
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ ! -f ".venv/bin/python" ]; then
    echo "[ERROR] .venv nicht gefunden — erst: bash install.sh"
    exit 1
fi

echo "[TIA PLANNER] Content-Plan wird erstellt..."
PYTHONPATH="$DIR/scripts" exec .venv/bin/python scripts/plan.py --persona tia "$@"
