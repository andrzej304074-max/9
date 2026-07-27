"""Opcjonalne pobieranie danych 15-minutowych z publicznego API Yahoo Finance.

UWAGA: to ścieżka pomocnicza, nie podstawowa. TradingView nie udostępnia publicznego API
do pobierania historii, więc dane z Yahoo mogą się nieznacznie różnić od tych na Twoim
wykresie (inny dostawca kwotowań, inne zamknięcia świec). Do wiernego odwzorowania
wykresu z TradingView użyj eksportu CSV.

Yahoo oddaje maksymalnie ok. 60 dni historii dla interwału 15-minutowego.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .csv_loader import Bar, DataError

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
DEFAULT_SYMBOL = "GBPUSD=X"
USER_AGENT = "Mozilla/5.0 (compatible; gbpusd-backtester/1.0)"
TIMEOUT_SECONDS = 20


def fetch_bars(symbol: str = DEFAULT_SYMBOL, interval: str = "15m", range_: str = "60d") -> list[Bar]:
    """Pobiera świece z Yahoo Finance. Rzuca `DataError` z czytelnym komunikatem po polsku."""
    url = f"{YAHOO_URL.format(symbol=symbol)}?interval={interval}&range={range_}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
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
        message = (chart["error"] or {}).get("description", "nieznany błąd")
        raise DataError(f"Serwer danych zgłosił błąd: {message}")

    results = chart.get("result") or []
    if not results:
        raise DataError("Serwer danych nie zwrócił żadnych notowań dla podanego symbolu.")

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

    if not bars:
        raise DataError("Serwer danych zwrócił wyłącznie puste świece.")
    return sorted(bars, key=lambda b: b.ts)
