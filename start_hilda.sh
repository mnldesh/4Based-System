#!/bin/bash
# Hilda Runner — startet automatisch mit .venv
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ ! -f ".venv/bin/python" ]; then
    echo "[ERROR] .venv nicht gefunden — erst install.sh ausführen"
    exit 1
fi

exec .venv/bin/python scripts/hilda_runner.py "$@"
