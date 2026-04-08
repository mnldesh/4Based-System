"""
runner.py — Persona Runner (Hilda / Tia)
"""

import argparse
import asyncio
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shared.base_runner import load_account, run_account, save_session


def main() -> None:
    ap = argparse.ArgumentParser(description="4Based Runner")
    ap.add_argument("--persona",       required=True, choices=["hilda", "tia"], help="Persona auswählen")
    ap.add_argument("--dry-run",       action="store_true", help="Keine Nachrichten senden")
    ap.add_argument("--once",          action="store_true", help="Nur einen Zyklus")
    ap.add_argument("--headless",      action="store_true", help="Browser unsichtbar (Server-Modus)")
    ap.add_argument("--save-session",  action="store_true", help="Einmalig einloggen und Session speichern")
    args = ap.parse_args()

    persona = args.persona
    storage = f"{persona}.storage.json"
    label = persona.upper()

    if args.save_session:
        asyncio.run(save_session(persona, storage))
        return

    account = load_account(persona, storage)

    stop_event = asyncio.Event()

    def shutdown(*_):
        print(f"\n[{label}] Beende...")
        stop_event.set()

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    async def _run():
        task = asyncio.create_task(
            run_account(account, args.dry_run, args.once, headless=args.headless)
        )
        done, _ = await asyncio.wait(
            [task, asyncio.create_task(stop_event.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())


if __name__ == "__main__":
    main()
