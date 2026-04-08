"""
config.py — Zentrale Konfiguration für das gesamte 4Based-System.
Alle anderen Module importieren ROOT und PLANS_ROOT von hier.

Pfade können per Umgebungsvariable überschrieben werden:
  FOURBASED_ROOT  → Projekt-Stammverzeichnis (Standard: automatisch erkannt)
  FOURBASED_PLANS → Planungs-Ausgabe (Standard: /mnt/Arbeit/Planung)
"""

import os
from pathlib import Path

# Automatisch: 2 Ebenen über dieser Datei (scripts/shared/config.py → Projekt-Root)
_DEFAULT_ROOT = Path(__file__).resolve().parents[2]

ROOT       = Path(os.environ.get("FOURBASED_ROOT", _DEFAULT_ROOT))
PLANS_ROOT = Path(os.environ.get("FOURBASED_PLANS", "/mnt/Arbeit/Planung"))
