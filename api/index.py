"""Punkt wejścia dla funkcji bezserwerowej Vercela.

Runtime Pythona na Vercelu szuka w tym pliku zmiennej `app` i traktuje ją jako aplikację
ASGI. Cała logika mieszka w pakiecie `app/` — tutaj zostaje wyłącznie podpięcie ścieżki
do katalogu głównego projektu, bo funkcja startuje z katalogu `api/`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402  (import musi nastąpić po ustawieniu ścieżki)

__all__ = ["app"]
