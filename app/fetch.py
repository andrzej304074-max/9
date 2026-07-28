"""Pobieranie notowań z publicznego API Yahoo Finance — oknami wstecz.

UWAGA: to ścieżka pomocnicza, nie podstawowa. TradingView nie udostępnia publicznego API
do pobierania historii, więc dane z Yahoo mogą się nieznacznie różnić od tych na Twoim
wykresie (inny dostawca kwotowań, inne zamknięcia świec).

Jedno żądanie oddaje ograniczony wycinek, dlatego dłuższy okres kompletujemy krok po kroku:
prosimy o najświeższe okno, potem o wcześniejsze, i tak aż do żądanej daty początkowej albo
do momentu, w którym dostawca przestaje oddawać starsze notowania.

Zasięg archiwum jest po stronie Yahoo twardo ograniczony i zależy od interwału — świec
15-minutowych nie da się dostać starszych niż około 60 dni, niezależnie od sposobu pytania.
Po wieloletnią historię minutową trzeba sięgnąć do Dukascopy (`app/dukascopy.py`), które
sięga 2003 roku.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from .csv_loader import Bar, DataError

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
DEFAULT_SYMBOL = "GBPUSD=X"
USER_AGENT = "Mozilla/5.0 (compatible; gbpusd-backtester/1.0)"
TIMEOUT_SECONDS = 20

# Zabezpieczenie przed zapętleniem, gdyby dostawca zaczął oddawać w kółko to samo okno.
MAX_WINDOWS = 60


@dataclass(frozen=True)
class IntervalLimits:
    """Ograniczenia narzucone przez dostawcę dla danego interwału."""

    window_days: int   # ile dni obejmuje jedno żądanie
    history_days: int  # jak głęboko w przeszłość sięga archiwum
    minutes: int       # długość świecy, potrzebna przy przesuwaniu okna


# Wartości wynikają z zasad Yahoo, nie z naszego kodu — stąd komentarze przy nietypowych.
INTERVAL_LIMITS: dict[str, IntervalLimits] = {
    "1m": IntervalLimits(7, 30, 1),        # najkrótsza świeca ma i najpłytsze archiwum
    "2m": IntervalLimits(60, 60, 2),
    "5m": IntervalLimits(60, 60, 5),
    "15m": IntervalLimits(60, 60, 15),
    "30m": IntervalLimits(60, 60, 30),
    "60m": IntervalLimits(730, 730, 60),
    "1h": IntervalLimits(730, 730, 60),
    "1d": IntervalLimits(3650, 36500, 1440),
}

INTERVAL_LABELS = {
    "1m": "1 minuta",
    "5m": "5 minut",
    "15m": "15 minut",
    "30m": "30 minut",
    "60m": "1 godzina",
    "1d": "1 dzień",
}


@dataclass
class FetchResult:
    """Świece razem z informacją, ile z żądanego okresu udało się faktycznie pobrać."""

    bars: list[Bar]
    requested_days: int
    windows: int = 0
    stopped_early: bool = False       # dostawca skończył dane przed żądaną datą
    ran_out_of_time: bool = False     # przerwał budżet czasu żądania
    notes: list[str] = field(default_factory=list)

    @property
    def covered_days(self) -> int:
        if not self.bars:
            return 0
        return max(1, (self.bars[-1].ts - self.bars[0].ts).days + 1)


def interval_limits(interval: str) -> IntervalLimits:
    limits = INTERVAL_LIMITS.get(interval)
    if limits is None:
        allowed = ", ".join(sorted(INTERVAL_LIMITS))
        raise DataError(f"Nieznany interwał '{interval}'. Dostępne: {allowed}.")
    return limits


def max_history_days(interval: str) -> int:
    """Ile dni wstecz da się w ogóle pobrać dla tego interwału."""
    return interval_limits(interval).history_days


def fetch_bars(
    symbol: str = DEFAULT_SYMBOL,
    interval: str = "15m",
    days: int = 60,
    progress: Optional[Callable[[int, int, Optional[datetime]], None]] = None,
    deadline: Optional[float] = None,
) -> FetchResult:
    """Kompletuje żądany okres, cofając się oknami aż do jego początku.

    `deadline` to znacznik z `time.monotonic()`, po którym przerywamy i oddajemy to, co już
    mamy — przy wdrożeniu bezserwerowym żądanie ma twardy limit czasu i lepiej zwrócić
    niepełne dane z adnotacją niż dać się ubić w połowie.
    """
    if days < 1:
        raise DataError("Liczba dni do pobrania musi być dodatnia.")

    limits = interval_limits(interval)
    result = FetchResult(bars=[], requested_days=days)

    if days > limits.history_days:
        result.notes.append(
            f"Dostawca udostępnia dla interwału {INTERVAL_LABELS.get(interval, interval)} "
            f"najwyżej {limits.history_days} dni historii, a poproszono o {days}. "
            f"Pobrano tyle, ile się dało."
        )

    collected: dict[datetime, Bar] = {}
    now = datetime.now(timezone.utc)
    target_start = now - timedelta(days=min(days, limits.history_days))
    window_end = now
    step = timedelta(minutes=limits.minutes)

    while window_end > target_start and result.windows < MAX_WINDOWS:
        # Budżet sprawdzamy dopiero po pierwszym oknie: lepiej oddać krótki okres
        # niż odesłać użytkownika z pustymi rękami.
        if result.windows and deadline is not None and time.monotonic() > deadline:
            result.ran_out_of_time = True
            result.notes.append(
                "Przerwano po wyczerpaniu budżetu czasu żądania — zwrócono okres pobrany do tej pory. "
                "Powtórz z krótszym zakresem albo rzadszym interwałem."
            )
            break

        window_start = max(target_start, window_end - timedelta(days=limits.window_days))
        batch = _request_window(symbol, interval, window_start, window_end)
        result.windows += 1

        fresh = [bar for bar in batch if bar.ts not in collected]
        if not fresh:
            # Dostawca nie ma nic starszego — dalsze cofanie się nic nie wniesie.
            result.stopped_early = window_start > target_start
            break

        for bar in fresh:
            collected[bar.ts] = bar

        oldest = min(bar.ts for bar in batch)
        if progress is not None:
            progress(result.windows, len(collected), oldest)

        if oldest >= window_end:
            break  # brak postępu wstecz — zabezpieczenie przed pętlą w nieskończoność
        window_end = oldest - step

    if not collected:
        if result.ran_out_of_time:
            raise DataError(
                "Nie udało się pobrać nic w limicie czasu żądania. "
                "Spróbuj krótszego zakresu albo rzadszego interwału."
            )
        raise DataError(
            "Dostawca nie zwrócił żadnych notowań dla podanego symbolu. "
            "Sprawdź pisownię symbolu (np. GBPUSD=X) albo wgraj plik CSV z TradingView."
        )

    result.bars = sorted(collected.values(), key=lambda b: b.ts)

    covered = result.covered_days
    if covered < min(days, limits.history_days) * 0.9:
        result.stopped_early = True
    if result.stopped_early and not result.ran_out_of_time:
        result.notes.append(
            f"Dostawca oddał {covered} dni z żądanych {days} — jego archiwum na tym się kończy. "
            f"Po głębszą historię użyj pobierania z Dukascopy (sięga 2003 roku) "
            f"albo wgraj plik CSV."
        )
    return result


def _request_window(symbol: str, interval: str, start: datetime, end: datetime) -> list[Bar]:
    """Jedno okno czasowe. Puste okno nie jest błędem — po prostu nie ma tam notowań."""
    query = urllib.parse.urlencode(
        {
            "interval": interval,
            "period1": int(start.timestamp()),
            "period2": int(end.timestamp()),
            "includePrePost": "false",
        }
    )
    url = f"{YAHOO_URL.format(symbol=urllib.parse.quote(symbol))}?{query}"
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise DataError(
                f"Dostawca nie zna symbolu '{symbol}'. Dla par walutowych używa się zapisu "
                "z sufiksem =X, na przykład GBPUSD=X."
            )
        raise DataError(
            f"Serwer danych odpowiedział błędem HTTP {exc.code}. "
            "Jeśli to 403 lub 429 — dostawca ogranicza automatyczne pobieranie. "
            "Użyj wgrania pliku CSV wyeksportowanego z TradingView."
        )
    except urllib.error.URLError as exc:
        raise DataError(
            f"Brak połączenia z serwerem danych ({exc.reason}). "
            "Sprawdź internet/proxy albo wgraj plik CSV z TradingView."
        )
    except (TimeoutError, OSError) as exc:
        raise DataError(
            f"Połączenie z serwerem danych nie powiodło się ({exc}). "
            "Wgraj plik CSV wyeksportowany z TradingView."
        )
    except json.JSONDecodeError:
        raise DataError("Serwer danych zwrócił odpowiedź, której nie da się odczytać jako JSON.")

    return _parse_yahoo(payload)


def _parse_yahoo(payload: dict[str, Any]) -> list[Bar]:
    chart = payload.get("chart") or {}
    if chart.get("error"):
        error = chart["error"] or {}
        message = error.get("description") or error.get("code") or "nieznany błąd"
        # Wyjście poza zasięg archiwum nie jest awarią — to sygnał, żeby przestać się cofać.
        if "period" in str(message).lower() or "range" in str(message).lower():
            return []
        raise DataError(f"Serwer danych zgłosił błąd: {message}")

    results = chart.get("result") or []
    if not results:
        return []

    result = results[0]
    stamps = result.get("timestamp") or []
    quote_blocks = ((result.get("indicators") or {}).get("quote")) or [{}]
    quote = quote_blocks[0]

    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    bars: list[Bar] = []
    for i, stamp in enumerate(stamps):
        try:
            o, h, lo, c = opens[i], highs[i], lows[i], closes[i]
        except IndexError:
            continue
        if None in (o, h, lo, c):
            continue  # Yahoo wstawia null-e w luki rynkowe
        volume = volumes[i] if i < len(volumes) and volumes[i] is not None else 0
        bars.append(
            Bar(
                ts=datetime.fromtimestamp(stamp, tz=timezone.utc),
                open=float(o),
                high=float(h),
                low=float(lo),
                close=float(c),
                volume=float(volume),
            )
        )
    return bars


def human_depth(days: int) -> str:
    """Zasięg archiwum słowami. Dla świec dziennych Yahoo nie podaje twardej granicy,
    więc nie udajemy, że znamy liczbę — „100 lat” brzmiałoby jak obietnica."""
    if days >= 10000:
        return "pełna dostępna historia"
    if days >= 365:
        return f"{round(days / 365)} lat wstecz"
    return f"{days} dni wstecz"


def intervals_payload() -> dict[str, str]:
    """Lista interwałów dla interfejsu, z zasięgiem archiwum w opisie."""
    return {
        key: f"{label} — {human_depth(INTERVAL_LIMITS[key].history_days)}"
        for key, label in INTERVAL_LABELS.items()
    }
