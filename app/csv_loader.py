"""Wczytywanie i normalizacja danych świecowych wyeksportowanych z TradingView.

TradingView eksportuje CSV w kilku wariantach (różne separatory, różne formaty czasu,
przecinek albo kropka dziesiętna), dlatego parser wykrywa format sam, zamiast wymagać
od użytkownika ręcznego przygotowania pliku.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, NamedTuple, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class DataError(ValueError):
    """Błąd danych wejściowych — komunikat trafia wprost do UI."""


class Bar(NamedTuple):
    """Pojedyncza świeca. `ts` to zawsze moment otwarcia, w UTC."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class LoadResult:
    bars: list[Bar]
    interval_minutes: Optional[int]
    warnings: list[str]
    rejected_rows: int
    source_columns: list[str]

    @property
    def first_ts(self) -> Optional[datetime]:
        return self.bars[0].ts if self.bars else None

    @property
    def last_ts(self) -> Optional[datetime]:
        return self.bars[-1].ts if self.bars else None


# --- nagłówki kolumn ---------------------------------------------------------------

_TIME_KEYS = ("time", "date", "datetime", "date_time", "timestamp", "czas", "data")
_OPEN_KEYS = ("open", "o", "otwarcie")
_HIGH_KEYS = ("high", "h", "max", "najwyzszy")
_LOW_KEYS = ("low", "l", "min", "najnizszy")
_CLOSE_KEYS = ("close", "c", "last", "zamkniecie")
_VOLUME_KEYS = ("volume", "vol", "v", "wolumen")

_UNIX_RE = re.compile(r"^-?\d{9,14}$")

_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%d-%m-%Y %H:%M",
    "%Y%m%d %H:%M:%S",
    "%Y%m%d",
)


def resolve_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise DataError(
            f"Nieznana strefa czasowa '{name}'. Użyj nazwy IANA, np. 'Europe/London'."
        )


def _normalise_key(key: str) -> str:
    key = key.strip().lower().lstrip("﻿")
    key = re.sub(r"[^a-z0-9]+", "", key)
    return key


def _find_column(header: list[str], candidates: Iterable[str]) -> Optional[int]:
    normalised = [_normalise_key(h) for h in header]
    for cand in candidates:
        if cand in normalised:
            return normalised.index(cand)
    # dopuszczamy nazwy typu "close price" / "Open (GBPUSD)"
    for cand in candidates:
        for idx, name in enumerate(normalised):
            if name.startswith(cand) and len(cand) > 1:
                return idx
    return None


def _detect_delimiter(sample: str) -> str:
    first_line = next((ln for ln in sample.splitlines() if ln.strip()), "")
    counts = {d: first_line.count(d) for d in (",", ";", "\t", "|")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


def parse_number(raw: str) -> float:
    text = raw.strip().replace(" ", "").replace(" ", "").replace("'", "")
    if not text:
        raise ValueError("pusta wartość")
    if "," in text and "." in text:
        # np. "1 234,56" albo "1,234.56" — ostatni separator jest dziesiętny
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    return float(text)


def parse_timestamp(raw: str, tz: ZoneInfo) -> datetime:
    """Zamienia surowy znacznik czasu na aware-datetime w UTC.

    Znaczniki bez informacji o strefie interpretowane są w strefie `tz` — czyli tej,
    w której użytkownik ogląda wykres w TradingView.
    """
    text = raw.strip().strip('"')
    if not text:
        raise ValueError("pusty znacznik czasu")

    if _UNIX_RE.match(text):
        value = int(text)
        if abs(value) >= 10**14:  # mikrosekundy
            value //= 1_000_000
        elif abs(value) >= 10**11:  # milisekundy
            value /= 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc)

    iso_candidate = text.replace("Z", "+00:00").replace("z", "+00:00")
    dt: Optional[datetime] = None
    try:
        dt = datetime.fromisoformat(iso_candidate)
    except ValueError:
        for fmt in _DATETIME_FORMATS:
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        raise ValueError(f"nierozpoznany format czasu: '{raw}'")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def _detect_interval(bars: list[Bar]) -> Optional[int]:
    if len(bars) < 3:
        return None
    deltas = Counter()
    for prev, cur in zip(bars, bars[1:]):
        minutes = int((cur.ts - prev.ts).total_seconds() // 60)
        if minutes > 0:
            deltas[minutes] += 1
    if not deltas:
        return None
    return deltas.most_common(1)[0][0]


def load_bars(text: str, timezone_name: str = "Europe/London") -> LoadResult:
    """Parsuje treść pliku CSV do listy świec posortowanych rosnąco po czasie."""
    tz = resolve_timezone(timezone_name)
    if not text or not text.strip():
        raise DataError("Plik jest pusty.")

    text = text.lstrip("﻿")
    delimiter = _detect_delimiter(text[:8192])
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)

    try:
        rows = [row for row in reader if row and any(cell.strip() for cell in row)]
    except csv.Error as exc:
        raise DataError(f"Nie udało się odczytać pliku CSV: {exc}")

    if not rows:
        raise DataError("Plik nie zawiera żadnych danych.")

    warnings: list[str] = []
    header = rows[0]
    has_header = any(_normalise_key(cell) in _TIME_KEYS for cell in header)

    if has_header:
        idx_time = _find_column(header, _TIME_KEYS)
        idx_open = _find_column(header, _OPEN_KEYS)
        idx_high = _find_column(header, _HIGH_KEYS)
        idx_low = _find_column(header, _LOW_KEYS)
        idx_close = _find_column(header, _CLOSE_KEYS)
        idx_volume = _find_column(header, _VOLUME_KEYS)
        data_rows = rows[1:]
        source_columns = [h.strip() for h in header]
    else:
        # brak nagłówka — zakładamy kolejność czas, O, H, L, C[, V]
        if len(header) < 5:
            raise DataError(
                "Nie znaleziono nagłówka ani 5 kolumn (czas, open, high, low, close). "
                "Wyeksportuj dane z TradingView przez 'Eksportuj dane wykresu'."
            )
        idx_time, idx_open, idx_high, idx_low, idx_close = 0, 1, 2, 3, 4
        idx_volume = 5 if len(header) > 5 else None
        data_rows = rows
        source_columns = ["(bez nagłówka)"]
        warnings.append(
            "Plik nie ma nagłówka — przyjęto kolejność kolumn: czas, open, high, low, close."
        )

    missing = [
        name
        for name, idx in (
            ("czas", idx_time),
            ("open", idx_open),
            ("high", idx_high),
            ("low", idx_low),
            ("close", idx_close),
        )
        if idx is None
    ]
    if missing:
        raise DataError(
            "W pliku brakuje kolumn: " + ", ".join(missing) + ". "
            f"Znalezione kolumny: {', '.join(source_columns)}."
        )

    max_idx = max(i for i in (idx_time, idx_open, idx_high, idx_low, idx_close) if i is not None)

    seen: dict[datetime, Bar] = {}
    rejected = 0
    first_errors: list[str] = []

    for line_no, row in enumerate(data_rows, start=2 if has_header else 1):
        if len(row) <= max_idx:
            rejected += 1
            if len(first_errors) < 3:
                first_errors.append(f"wiersz {line_no}: za mało kolumn")
            continue
        try:
            ts = parse_timestamp(row[idx_time], tz)
            o = parse_number(row[idx_open])
            h = parse_number(row[idx_high])
            lo = parse_number(row[idx_low])
            c = parse_number(row[idx_close])
            v = 0.0
            if idx_volume is not None and len(row) > idx_volume:
                try:
                    v = parse_number(row[idx_volume])
                except ValueError:
                    v = 0.0
        except (ValueError, OverflowError, OSError) as exc:
            rejected += 1
            if len(first_errors) < 3:
                first_errors.append(f"wiersz {line_no}: {exc}")
            continue

        if not all(x > 0 for x in (o, h, lo, c)):
            rejected += 1
            if len(first_errors) < 3:
                first_errors.append(f"wiersz {line_no}: cena niedodatnia")
            continue
        if h < max(o, c) or lo > min(o, c) or h < lo:
            rejected += 1
            if len(first_errors) < 3:
                first_errors.append(f"wiersz {line_no}: niespójne OHLC")
            continue

        seen[ts] = Bar(ts=ts, open=o, high=h, low=lo, close=c, volume=v)

    if not seen:
        detail = ("; ".join(first_errors)) if first_errors else "brak poprawnych wierszy"
        raise DataError(f"Nie udało się odczytać żadnej świecy ({detail}).")

    bars = sorted(seen.values(), key=lambda b: b.ts)

    duplicates = len(data_rows) - rejected - len(bars)
    if duplicates > 0:
        warnings.append(f"Pominięto {duplicates} zduplikowanych znaczników czasu.")
    if rejected:
        detail = "; ".join(first_errors)
        warnings.append(f"Odrzucono {rejected} nieprawidłowych wierszy ({detail}).")

    interval = _detect_interval(bars)
    if interval is not None and interval != 15:
        warnings.append(
            f"Wykryty interwał danych to {interval} min. Strategia opisuje świecę 15-minutową — "
            "ustaw 'długość świecy sygnałowej' zgodnie z tym, co chcesz testować."
        )

    return LoadResult(
        bars=bars,
        interval_minutes=interval,
        warnings=warnings,
        rejected_rows=rejected,
        source_columns=source_columns,
    )


def bars_to_csv(bars: list[Bar]) -> str:
    """Serializuje świece do formatu zgodnego z eksportem TradingView (ISO 8601, UTC)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["time", "open", "high", "low", "close", "volume"])
    for bar in bars:
        writer.writerow(
            [
                bar.ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                f"{bar.open:.5f}",
                f"{bar.high:.5f}",
                f"{bar.low:.5f}",
                f"{bar.close:.5f}",
                f"{bar.volume:g}",
            ]
        )
    return out.getvalue()
