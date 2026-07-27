#!/usr/bin/env python3
"""Generator deterministycznych danych demo GBP/USD 15 min w formacie eksportu TradingView.

Dane są SYNTETYCZNE (błądzenie losowe z ustalonym ziarnem) — służą wyłącznie do tego, żeby
aplikacja działała od razu po uruchomieniu i żeby dało się sprawdzić mechanikę backtestu.
Nie wyciągaj z nich żadnych wniosków o rynku — do realnych testów wgraj własny CSV
wyeksportowany z TradingView.

Użycie:
    python3 tools/make_sample_data.py [plik_wyjsciowy]
"""

from __future__ import annotations

import math
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SEED = 20240815
START = date(2026, 1, 1)
END = date(2026, 7, 24)
START_PRICE = 1.2650
BAR_MINUTES = 15
SUB_STEPS = 5  # ile kroków wewnątrz świecy — daje realistyczne knoty


def volatility_at(hour: int, minute: int) -> float:
    """Zmienność zależna od pory dnia (w jednostkach ceny na świecę 15 min).

    Szczyty wypadają na otwarcie Londynu (07:00 UTC) i sesję nowojorską (13:30 UTC),
    noc azjatycka jest wyraźnie spokojniejsza.
    """
    t = hour + minute / 60.0
    base = 0.00022
    london = 0.00075 * math.exp(-((t - 7.25) ** 2) / 1.6)
    newyork = 0.00060 * math.exp(-((t - 13.5) ** 2) / 2.4)
    asia = 0.00018 * math.exp(-((t - 1.0) ** 2) / 6.0)
    return base + london + newyork + asia


def generate() -> list[tuple[datetime, float, float, float, float, int]]:
    rng = random.Random(SEED)
    price = START_PRICE
    rows: list[tuple[datetime, float, float, float, float, int]] = []

    day = START
    while day <= END:
        if day.weekday() >= 5:  # rynek forex stoi w weekend
            day += timedelta(days=1)
            continue

        # lekki dzienny bias, żeby powstawały wielodniowe trendy
        daily_drift = rng.gauss(0.0, 0.00018) / (24 * 4)

        for slot in range(24 * 60 // BAR_MINUTES):
            minutes = slot * BAR_MINUTES
            hour, minute = divmod(minutes, 60)
            ts = datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)

            sigma = volatility_at(hour, minute) / math.sqrt(SUB_STEPS)
            bar_open = price
            path = [bar_open]
            for _ in range(SUB_STEPS):
                price += rng.gauss(daily_drift, sigma)
                path.append(price)

            bar_high = max(path)
            bar_low = min(path)
            bar_close = path[-1]
            volume = int(abs(rng.gauss(1400, 500)) + 120 * (sigma * 10000))
            rows.append((ts, bar_open, bar_high, bar_low, bar_close, volume))

        day += timedelta(days=1)

    return rows


def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/GBPUSD_15m_sample.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows = generate()
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("time,open,high,low,close,Volume\n")
        for ts, o, h, lo, c, v in rows:
            handle.write(
                f"{ts.strftime('%Y-%m-%dT%H:%M:%S+00:00')},"
                f"{o:.5f},{h:.5f},{lo:.5f},{c:.5f},{v}\n"
            )

    print(f"Zapisano {len(rows)} świec do {out_path}")
    print(f"Zakres: {rows[0][0].date()} – {rows[-1][0].date()}")


if __name__ == "__main__":
    main()
