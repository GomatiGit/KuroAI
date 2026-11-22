import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Optional: zusätzlicher Pfad mit discord.py/aiohttp (z. B. aus einer venv).
_extra = os.environ.get("KURO_TEST_SITE_PACKAGES")
if _extra and _extra not in sys.path:
    sys.path.append(_extra)


def run(coro):
    """Kleine Hilfe statt pytest-asyncio."""
    return asyncio.run(coro)
