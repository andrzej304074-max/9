"""Pobieranie danych historycznych z darmowego feedu Dukascopy.

Dukascopy udostępnia publicznie dane tickowe sięgające 2003 roku, bez konta i bez limitów
związanych z planem — w przeciwieństwie do eksportu z TradingView, gdzie ograniczeniem jest
to, ile świec wczyta wykres. To dodatkowe źródło obok wgrywania pliku CSV; nie zastępuje go.

Format: jeden plik na godzinę, spakowany LZMA, w środku ciąg 20-bajtowych rekordów
big-endian: czas w milisekundach od początku godziny (uint32), cena ask (uint32),
cena bid (uint32), wolumen ask (float32), wolumen bid (float32). Ceny są liczbami
całkowitymi w punktach — dzieli się je przez skalę zależną od instrumentu.

Uwaga na pułapkę w adresie URL: **miesiąc jest indeksowany od zera** (styczeń = 00).
"""

from __future__ import annotations

import lzma
import struct
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, NamedTuple, Optional

from .csv_loader import Bar, DataError

BASE_URL = "https://datafeed.dukascopy.com/datafeed"
USER_AGENT = "Mozilla/5.0 (compatible; gbpusd-backtester/1.0)"
TICK_STRUCT = struct.Struct(">3I2f")  # czas, ask, bid, wolumen ask, wolumen bid
# Plik godzinowy to kilkadziesiąt kilobajtów ze zwykłego CDN-u — jeśli nie odpowie w kilka
# sekund, to znaczy, że nie odpowie w ogóle. Długi timeout przy niedostępnym archiwum
# zamieniał pobieranie w wielominutowe zawieszenie, zamiast w szybki, czytelny błąd.
TIMEOUT_SECONDS = 8
MAX_WORKERS = 24
RETRIES = 2

# Niedzielny handel zaczyna się dopiero wieczorem — wcześniejsze pliki są zawsze puste.
SUNDAY_OPEN_HOUR = 21

# Doba to 24 pliki, czyli jedna runda przy tylu wątkach — dzięki temu budżet czasu
# sprawdzamy często, a `covered_to` zawsze wskazuje dzień domknięty w całości.

# Skala cen: liczba miejsc po przecinku w kwotowaniu danego instrumentu.
INSTRUMENTS: dict[str, dict[str, object]] = {
    "GBPUSD": {"label": "GBP/USD", "scale": 100_000.0, "since": 2003},
    "EURUSD": {"label": "EUR/USD", "scale": 100_000.0, "since": 2003},
    "USDJPY": {"label": "USD/JPY", "scale": 1_000.0, "since": 2003},
    "EURGBP": {"label": "EUR/GBP", "scale": 100_000.0, "since": 2003},
    "AUDUSD": {"label": "AUD/USD", "scale": 100_000.0, "since": 2003},
    "USDCHF": {"label": "USD/CHF", "scale": 100_000.0, "since": 2003},
    "USDCAD": {"label": "USD/CAD", "scale": 100_000.0, "since": 2003},
    "XAUUSD": {"label": "Złoto (XAU/USD)", "scale": 1_000.0, "since": 2003},
}

DEFAULT_INSTRUMENT = "GBPUSD"


class Tick(NamedTuple):
    ts: datetime
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


def instrument_scale(instrument: str) -> float:
    entry = INSTRUMENTS.get(instrument.upper())
    if entry is None:
        raise DataError(
            f"Nieznany instrument '{instrument}'. Dostępne: {', '.join(sorted(INSTRUMENTS))}."
        )
    return float(entry["scale"])  # type: ignore[arg-type]


def hour_url(instrument: str, when: datetime) -> str:
    """Buduje adres pliku godzinowego. Miesiąc jest indeksowany od zera."""
    return (
        f"{BASE_URL}/{instrument.upper()}/{when.year:04d}/{when.month - 1:02d}/"
        f"{when.day:02d}/{when.hour:02d}h_ticks.bi5"
    )


def _decompress(payload: bytes) -> bytes:
    """Rozpakowuje LZMA. Pliki Dukascopy bywają bez znacznika końca strumienia,
    dlatego używamy dekompresora strumieniowego, a nie jednorazowego `lzma.decompress`."""
    if not payload:
        return b""
    last_error: Optional[Exception] = None
    for fmt in (lzma.FORMAT_AUTO, lzma.FORMAT_ALONE):
        try:
            return lzma.LZMADecompressor(format=fmt).decompress(payload)
        except lzma.LZMAError as exc:
            last_error = exc
    raise DataError(f"Nie udało się rozpakować pliku Dukascopy: {last_error}")


def decode_bi5(payload: bytes, hour_start: datetime, scale: float) -> list[Tick]:
    """Zamienia zawartość jednego pliku godzinowego na listę ticków."""
    raw = _decompress(payload)
    if not raw:
        return []

    ticks: list[Tick] = []
    usable = len(raw) - (len(raw) % TICK_STRUCT.size)
    for offset in range(0, usable, TICK_STRUCT.size):
        millis, ask_points, bid_points, _ask_vol, _bid_vol = TICK_STRUCT.unpack_from(raw, offset)
        if ask_points == 0 or bid_points == 0:
            continue
        ticks.append(
            Tick(
                ts=hour_start + timedelta(milliseconds=millis),
                bid=bid_points / scale,
                ask=ask_points / scale,
            )
        )
    return ticks


def ticks_to_bars(ticks: Iterable[Tick], interval_minutes: int, price: str = "bid") -> list[Bar]:
    """Składa ticki w świece OHLC o zadanym interwale.

    Domyślnie po cenie bid — tak samo, jak wykresy walutowe pokazuje TradingView.
    Okresy bez ticków (weekend, przerwa w handlu) po prostu nie tworzą świec.
    """
    if interval_minutes <= 0:
        raise DataError("Interwał świecy musi być większy od zera.")
    if price not in ("bid", "ask", "mid"):
        raise DataError("Cena świecy musi być jedną z: bid, ask, mid.")

    step = interval_minutes * 60
    buckets: dict[int, list[float]] = {}
    order: list[int] = []

    for tick in sorted(ticks, key=lambda t: t.ts):
        value = tick.bid if price == "bid" else (tick.ask if price == "ask" else tick.mid)
        key = int(tick.ts.timestamp()) // step
        bucket = buckets.get(key)
        if bucket is None:
            buckets[key] = [value, value, value, value]  # open, high, low, close
            order.append(key)
        else:
            bucket[1] = max(bucket[1], value)
            bucket[2] = min(bucket[2], value)
            bucket[3] = value

    bars: list[Bar] = []
    for key in order:
        o, h, l, c = buckets[key]
        bars.append(
            Bar(
                ts=datetime.fromtimestamp(key * step, tz=timezone.utc),
                open=o,
                high=h,
                low=l,
                close=c,
                volume=0.0,
            )
        )
    return sorted(bars, key=lambda b: b.ts)


def _fetch_hour(url: str) -> Optional[bytes]:
    """Pobiera jeden plik godzinowy.

    Zwraca `b""`, gdy pliku nie ma (weekend, święto — sytuacja normalna), a `None`,
    gdy mimo ponowień nie udało się go pobrać. Rozróżnienie jest istotne, bo pobranie
    wieloletniego zakresu to dziesiątki tysięcy plików: pojedyncza wywrotka nie może
    przewracać całej roboty, ale musi zostać policzona i zgłoszona.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                return b""  # weekend albo święto — normalne
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        if attempt < RETRIES - 1:
            time.sleep(0.3)

    return None


def probe(instrument: str = DEFAULT_INSTRUMENT) -> dict[str, object]:
    """Pobiera jeden znany plik godzinowy i opisuje, co się stało.

    Służy do rozstrzygania sytuacji „kliknąłem i nic się nie dzieje": mówi wprost, czy
    archiwum jest w ogóle osiągalne z tego środowiska, jak szybko odpowiada i co zwraca.
    Nigdy nie rzuca wyjątkiem — diagnostyka, która sama się wywraca, jest bezużyteczna.
    """
    # Wtorek, środek sesji londyńskiej — godzina, która na pewno ma notowania.
    when = datetime(2024, 1, 2, 10, tzinfo=timezone.utc)
    url = hour_url(instrument, when)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    started = time.monotonic()

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = response.read()
        elapsed = round((time.monotonic() - started) * 1000)
        try:
            ticks = len(decode_bi5(payload, when, instrument_scale(instrument)))
        except DataError as exc:
            return {"ok": False, "url": url, "ms": elapsed, "bytes": len(payload),
                    "error": f"Plik pobrany, ale nie daje się rozpakować: {exc}"}
        return {
            "ok": ticks > 0,
            "url": url,
            "ms": elapsed,
            "bytes": len(payload),
            "ticks": ticks,
            "error": None if ticks else "Plik pobrany, ale pusty — archiwum odpowiada inaczej niż zwykle.",
        }
    except urllib.error.HTTPError as exc:
        return {"ok": False, "url": url, "ms": round((time.monotonic() - started) * 1000),
                "status": exc.code,
                "error": f"Archiwum odpowiedziało kodem HTTP {exc.code}."
                         + (" Ten adres powinien istnieć, więc prawdopodobnie dostawca blokuje "
                            "ruch z tego serwera." if exc.code in (403, 429) else "")}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "url": url, "ms": round((time.monotonic() - started) * 1000),
                "error": f"Brak połączenia z archiwum ({exc}). "
                         "Środowisko może blokować ruch wychodzący do datafeed.dukascopy.com."}


def hours_in_range(start: date, end: date) -> list[datetime]:
    """Wszystkie godziny w zakresie dat (włącznie), pomijając czas bez notowań.

    Rynek walutowy działa od niedzieli ~22:00 UTC do piątku ~22:00 UTC. Soboty pomijamy
    w całości, a z niedziel bierzemy tylko wieczór — pozostałe niedzielne godziny to
    gwarantowane puste pliki, a przy wieloletnim zakresie stanowią kilkanaście procent
    wszystkich żądań.
    """
    hours: list[datetime] = []
    day = start
    while day <= end:
        weekday = day.weekday()
        if weekday != 5:  # sobota nie ma notowań
            first = SUNDAY_OPEN_HOUR if weekday == 6 else 0
            for hour in range(first, 24):
                hours.append(datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc))
        day += timedelta(days=1)
    return hours


@dataclass
class DownloadResult:
    """Wynik pobierania razem z informacją, dokąd faktycznie doszło.

    `covered_to` to ostatni dzień pobrany **w całości**. Dzięki temu przerwane pobieranie
    da się wznowić dokładnie od następnego dnia, bez zgadywania i bez dziur.
    """

    bars: list[Bar] = field(default_factory=list)
    covered_to: Optional[date] = None
    days_total: int = 0
    hours_done: int = 0
    hours_total: int = 0
    failed_hours: int = 0
    stopped_early: bool = False   # przerwane budżetem czasu, nie brakiem danych

    @property
    def complete(self) -> bool:
        return not self.stopped_early


def download_window(
    instrument: str = DEFAULT_INSTRUMENT,
    start: Optional[date] = None,
    end: Optional[date] = None,
    interval_minutes: int = 15,
    price: str = "bid",
    cache_dir: Optional[Path] = None,
    progress: Optional[Callable[[int, int], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
    deadline: Optional[float] = None,
) -> DownloadResult:
    """Pobiera zakres dzień po dniu i mówi, dokąd zdążył.

    Idziemy chronologicznie małymi partiami dni. Po każdej partii sprawdzamy budżet czasu:
    gdy się kończy, przerywamy i zwracamy komplet dni domkniętych do tej pory. Wywołujący
    wznawia od `covered_to + 1` — bez tego wieloletni zakres nie miałby szans zmieścić się
    w limicie pojedynczego żądania.

    Awaria pojedynczego pliku godzinowego nie przerywa pobierania; jest liczona i zgłaszana.
    Dopiero gdy **nic** się nie udało, uznajemy to za problem z łączem.
    """
    instrument = instrument.upper()
    scale = instrument_scale(instrument)
    end = end or datetime.now(timezone.utc).date() - timedelta(days=1)
    start = start or end - timedelta(days=30)
    if start > end:
        raise DataError("Data początkowa jest późniejsza niż końcowa.")

    days = [d for d in _days_in_range(start, end) if hours_in_range(d, d)]
    result = DownloadResult(days_total=len(days))
    result.hours_total = len(hours_in_range(start, end))
    if not days:
        raise DataError("Wybrany zakres nie zawiera żadnego dnia handlowego.")

    ticks: list[Tick] = []
    attempted = 0

    def load(when: datetime) -> Optional[list[Tick]]:
        """Zwraca ticki albo None, gdy godziny nie udało się pobrać."""
        cached: Optional[Path] = None
        if cache_dir is not None:
            cached = (
                cache_dir / instrument / f"{when.year:04d}" / f"{when.month:02d}"
                / f"{when.day:02d}" / f"{when.hour:02d}.bi5"
            )
            if cached.exists():
                return decode_bi5(cached.read_bytes(), when, scale)

        payload = _fetch_hour(hour_url(instrument, when))
        if payload is None:
            return None
        if cached is not None:
            try:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(payload)
            except OSError:
                pass   # brak miejsca na cache nie może psuć pobierania
        return decode_bi5(payload, when, scale)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for index, day in enumerate(days):
            if cancelled is not None and cancelled():
                raise DataError("Pobieranie zostało przerwane.")
            # Budżet sprawdzamy przed każdą dobą, ale nigdy przed pierwszą — inaczej przy
            # ciasnym limicie nie pobralibyśmy niczego i nie było się jak posunąć dalej.
            if index and deadline is not None and time.monotonic() > deadline:
                result.stopped_early = True
                break

            failed_today = 0
            for outcome in pool.map(load, hours_in_range(day, day)):
                attempted += 1
                if outcome is None:
                    failed_today += 1
                    result.failed_hours += 1
                else:
                    ticks.extend(outcome)
                    result.hours_done += 1

            # Doba, z której nie przyszedł ani jeden plik, oznacza problem systemowy,
            # a nie pecha. Nie ma sensu mielić kolejnych tysięcy godzin, żeby to potwierdzić.
            if failed_today and failed_today == len(hours_in_range(day, day)):
                raise DataError(
                    "Nie udało się pobrać z Dukascopy ani jednego pliku "
                    f"({failed_today} prób dla dnia {day.isoformat()}). "
                    "Użyj przycisku Sprawdź połączenie — powie, czy serwer w ogóle widzi "
                    "datafeed.dukascopy.com. W razie blokady wgraj plik CSV ręcznie."
                )

            result.covered_to = day
            if progress is not None:
                progress(result.hours_done + result.failed_hours, result.hours_total)

    result.bars = ticks_to_bars(ticks, interval_minutes, price)
    return result


def download_bars(
    instrument: str = DEFAULT_INSTRUMENT,
    start: Optional[date] = None,
    end: Optional[date] = None,
    interval_minutes: int = 15,
    price: str = "bid",
    cache_dir: Optional[Path] = None,
    progress: Optional[Callable[[int, int], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> list[Bar]:
    """Pobiera cały zakres i zwraca same świece — wariant bez limitu czasu.

    Używany lokalnie, gdzie pobieranie biegnie w tle i nikt go nie ubija.
    """
    result = download_window(
        instrument=instrument,
        start=start,
        end=end,
        interval_minutes=interval_minutes,
        price=price,
        cache_dir=cache_dir,
        progress=progress,
        cancelled=cancelled,
    )
    if not result.bars:
        raise DataError(
            "Dukascopy nie zwrócił żadnych ticków w tym zakresie. "
            "Sprawdź daty — dane sięgają 2003 roku, a najświeższe godziny bywają niedostępne."
        )
    return result.bars


def _days_in_range(start: date, end: date) -> list[date]:
    days, day = [], start
    while day <= end:
        days.append(day)
        day += timedelta(days=1)
    return days


def instruments_payload() -> dict[str, str]:
    return {code: str(meta["label"]) for code, meta in INSTRUMENTS.items()}
