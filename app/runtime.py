"""Rozpoznanie środowiska uruchomieniowego.

Lokalnie aplikacja jest zwykłym procesem: ma zapisywalny katalog `data/`, trzyma dane
w pamięci między żądaniami i może pobierać z Dukascopy w wątku w tle. Na platformie
bezserwerowej (Vercel) żadne z tych trzech założeń nie obowiązuje:

* system plików jest tylko do odczytu poza `/tmp`,
* każde żądanie może trafić na inną instancję, więc pamięć procesu bywa pusta,
* wątek w tle ginie w chwili zwrócenia odpowiedzi.

Ten moduł zbiera decyzje wynikające z tych różnic w jednym miejscu, żeby reszta kodu
nie musiała pytać o środowisko.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Vercel ustawia VERCEL=1, a pod spodem i tak działa Lambda — sprawdzamy oba tropy.
IS_SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))

# Budżet czasu pojedynczego żądania. Na Vercelu to `maxDuration` z vercel.json:
# plan Hobby dopuszcza do 60 s, Pro znacznie więcej. Wartość można nadpisać zmienną
# środowiskową, gdyby limit planu się zmienił.
MAX_REQUEST_SECONDS = int(os.environ.get("BACKTESTER_MAX_SECONDS", "60" if IS_SERVERLESS else "3600"))

# Limit rozmiaru żądania. Na Vercelu ciało żądania jest twardo ograniczone do 4,5 MB,
# dlatego front kompresuje CSV gzipem przed wysłaniem.
MAX_UPLOAD_BYTES = int(
    os.environ.get("BACKTESTER_MAX_UPLOAD", str(4 * 1024 * 1024 if IS_SERVERLESS else 64 * 1024 * 1024))
)


def state_dir() -> Path:
    """Katalog na dane zapisywane w trakcie pracy.

    Na Vercelu jedyne miejsce z prawem zapisu to `/tmp` — jest ulotne i lokalne dla
    instancji, ale wystarcza jako pamięć podręczna między kolejnymi żądaniami tej samej
    instancji, a to obsługuje większość ruchu.
    """
    root = Path(os.environ.get("BACKTESTER_STATE_DIR", "/tmp/backtester" if IS_SERVERLESS else str(BASE_DIR / "data")))
    root.mkdir(parents=True, exist_ok=True)
    return root


def describe() -> dict[str, object]:
    """Informacje o środowisku dla frontu — na ich podstawie dostosowuje podpowiedzi."""
    return {
        "serverless": IS_SERVERLESS,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "max_request_seconds": MAX_REQUEST_SECONDS,
        "dukascopy_max_days": dukascopy_day_limit(),
    }


def dukascopy_day_limit() -> int:
    """Ile dni historii da się pobrać z Dukascopy w jednym żądaniu.

    Archiwum jest cięte na pliki godzinowe, więc jeden dzień to 24 pobrania. Przy ośmiu
    równoległych połączeniach wychodzi grubo licząc pół sekundy na dzień; zostawiamy
    zapas i zjadamy najwyżej 60 % budżetu, bo dochodzi jeszcze składanie świec i parsowanie.
    """
    if not IS_SERVERLESS:
        return 0  # bez limitu — lokalnie pobieranie biegnie w tle
    return max(7, int(MAX_REQUEST_SECONDS * 0.6 / 0.5))
