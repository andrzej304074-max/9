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
import tempfile
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent

# Vercel ustawia VERCEL i VERCEL_ENV, a pod spodem i tak działa Lambda ze swoimi zmiennymi.
# Sprawdzamy wszystkie tropy: pomyłka w tę stronę oznacza próbę zapisu do katalogu tylko
# do odczytu, czyli wywrotkę przy starcie funkcji.
IS_SERVERLESS = bool(
    os.environ.get("VERCEL")
    or os.environ.get("VERCEL_ENV")
    or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    or os.environ.get("LAMBDA_TASK_ROOT")
)

# Budżet czasu pojedynczego żądania. Na Vercelu to `maxDuration` z vercel.json:
# plan Hobby dopuszcza do 60 s, Pro znacznie więcej. Wartość można nadpisać zmienną
# środowiskową, gdyby limit planu się zmienił.
MAX_REQUEST_SECONDS = int(os.environ.get("BACKTESTER_MAX_SECONDS", "60" if IS_SERVERLESS else "3600"))

# Limit rozmiaru żądania. Na Vercelu ciało żądania jest twardo ograniczone do 4,5 MB,
# dlatego front kompresuje CSV gzipem przed wysłaniem.
MAX_UPLOAD_BYTES = int(
    os.environ.get("BACKTESTER_MAX_UPLOAD", str(4 * 1024 * 1024 if IS_SERVERLESS else 64 * 1024 * 1024))
)


_WYBRANY: Optional[tuple[str, Path]] = None     # (żądanie ze środowiska, katalog, który zadziałał)


def _zapisywalny(katalog: Path) -> bool:
    """Czy do tego katalogu da się naprawdę zapisać.

    Samo istnienie nie wystarcza: przy wdrożeniu bezserwerowym katalog projektu istnieje,
    ale cały system plików poza tymczasowym jest tylko do odczytu.
    """
    try:
        katalog.mkdir(parents=True, exist_ok=True)
        proba = katalog / ".proba-zapisu"
        proba.write_text("", encoding="utf-8")
        proba.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def state_dir() -> Path:
    """Katalog na dane zapisywane w trakcie pracy.

    Na Vercelu jedyne miejsce z prawem zapisu to `/tmp` — jest ulotne i lokalne dla
    instancji, ale wystarcza jako pamięć podręczna między kolejnymi żądaniami tej samej
    instancji, a to obsługuje większość ruchu.

    Wybór musi być odporny na pomyłkę w rozpoznaniu środowiska. Ten moduł wczytuje się przy
    starcie funkcji, więc wyjątek z `mkdir` położyłby całą aplikację, zanim zdążyłaby
    powiedzieć, co się stało — zamiast strony użytkownik dostałby surowy błąd platformy.
    Dlatego sprawdzamy kandydatów po kolei i bierzemy pierwszego, w którym zapis faktycznie
    działa. Wynik zapamiętujemy, ale wiążemy go z ustawieniem, z którego wynika.
    """
    global _WYBRANY
    zadany = os.environ.get("BACKTESTER_STATE_DIR", "")
    if _WYBRANY is not None and _WYBRANY[0] == zadany:
        return _WYBRANY[1]

    kandydaci = [Path(zadany)] if zadany else []
    if not IS_SERVERLESS:
        kandydaci.append(BASE_DIR / "data")
    kandydaci.append(Path(tempfile.gettempdir()) / "backtester")

    wybrany = next((k for k in kandydaci if _zapisywalny(k)), kandydaci[-1])
    _WYBRANY = (zadany, wybrany)
    return wybrany


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
