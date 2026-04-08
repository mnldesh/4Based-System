#!/bin/bash
# Runner — startet automatisch mit .venv
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ -z "$1" ]; then
    echo "[ERROR] Persona angeben: ./start.sh hilda|tia [optionen]"
    exit 1
fi

if [ ! -f ".venv/bin/python" ]; then
    echo "[ERROR] .venv nicht gefunden — erst install.sh ausführen"
    exit 1
fi

PERSONA="$1"
shift
exec .venv/bin/python scripts/runner.py --persona "$PERSONA" "$@"
