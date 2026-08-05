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

import http.client
import lzma
import os
import struct
import threading
import time
import urllib.error
import urllib.parse
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

# Po tylu dobach z rzędu bez ani jednego pliku uznajemy, że to nie dziura w archiwum,
# tylko blokada — i oddajemy sterowanie zamiast mielić resztę zakresu na pusto.
MAX_DEAD_DAYS = 4
DEAD_DAY_PAUSE = 1.0        # sekundy oddechu, gdyby powodem był limit żądań

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


# --- gotowe świece minutowe --------------------------------------------------------
#
# Poza plikami tickowymi Dukascopy udostępnia też pliki z gotowymi świecami: jeden na
# całą dobę zamiast dwudziestu czterech godzinowych. To dwudziestoczterokrotnie mniej
# żądań, a do złożenia świecy 15-minutowej minutowe w zupełności wystarczają.
#
# Format tych plików nie jest oficjalnie udokumentowany, dlatego nie ufamy mu na słowo:
# przed użyciem porównujemy jedną dobę świec z tą samą dobą złożoną z ticków. Zgadza się
# — korzystamy; nie zgadza się — cicho wracamy do ticków. Dzięki temu ewentualna pomyłka
# w odczycie formatu nie może przemycić błędnych cen do wyników.

CANDLE_STRUCT = struct.Struct(">i5f")   # 24 B: przesunięcie w sekundach, O, C, L, H, wolumen
CANDLE_TOLERANCE = 1e-6                 # świece z obu źródeł muszą się zgadzać co do piątego miejsca


@dataclass(frozen=True)
class CandleLayout:
    """Jeden możliwy sposób odczytania rekordu świecy.

    Nie wiemy z góry, w jakiej kolejności zapisane są ceny, czy są liczbami w punktach
    czy gotowymi wartościami i w jakiej jednostce jest znacznik czasu. Zamiast obstawiać,
    wyliczamy wszystkie rozsądne warianty i sprawdzamy je względem ticków, które są pewne.
    """

    fmt: str            # układ pól rekordu
    order: str          # 'oclh' albo 'ohlc' — kolejność cen po znaczniku czasu
    scaled: bool        # czy dzielić przez skalę instrumentu
    time_divisor: int   # 1 dla sekund, 1000 dla milisekund

    @property
    def struct(self) -> struct.Struct:
        return struct.Struct(self.fmt)

    def label(self) -> str:
        jednostka = "s" if self.time_divisor == 1 else "ms"
        return f"{self.fmt} {self.order} {'w punktach' if self.scaled else 'wprost'} czas w {jednostka}"


def candle_layouts() -> list[CandleLayout]:
    """Wszystkie warianty warte sprawdzenia — 24-bajtowy rekord, dwie kolejności cen,
    dwa sposoby zapisu ceny, dwie jednostki czasu."""
    return [
        CandleLayout(fmt, order, scaled, divisor)
        for fmt in (">i5f", ">5if")          # czas + 5 zmiennoprzecinkowych albo 5 całkowitych + wolumen
        for order in ("oclh", "ohlc")
        for scaled in (True, False)
        for divisor in (1, 1000)
    ]


def day_candles_url(instrument: str, day: date, price: str = "bid") -> str:
    """Adres pliku z minutowymi świecami całej doby. Miesiąc, jak zwykle, liczony od zera."""
    side = "ASK" if price == "ask" else "BID"
    return (
        f"{BASE_URL}/{instrument.upper()}/{day.year:04d}/{day.month - 1:02d}/"
        f"{day.day:02d}/{side}_candles_min_1.bi5"
    )


def decode_candles(
    payload: bytes, day_start: datetime, scale: float, layout: Optional[CandleLayout] = None
) -> list[Bar]:
    """Rozkodowuje plik ze świecami minutowymi według zadanego układu pól."""
    layout = layout or CandleLayout(">i5f", "oclh", True, 1)
    raw = _decompress(payload)
    if not raw:
        return []

    st = layout.struct
    divisor = scale if layout.scaled else 1.0
    base = day_start.timestamp()
    usable = len(raw) - (len(raw) % st.size)
    bars: list[Bar] = []

    for record in st.iter_unpack(raw[:usable]):
        stamp, a, b, c, d = record[0], record[1], record[2], record[3], record[4]
        if layout.order == "oclh":
            o, close, low, high = a, b, c, d
        else:
            o, high, low, close = a, b, c, d
        if o <= 0 or high <= 0 or low <= 0 or close <= 0:
            continue    # minuta bez handlu bywa zapisana zerami
        bars.append(
            Bar(
                ts=datetime.fromtimestamp(base + stamp / layout.time_divisor, tz=timezone.utc),
                open=o / divisor, high=high / divisor, low=low / divisor, close=close / divisor,
                volume=0.0,
            )
        )
    return bars


def candles_look_sane(bars: list[Bar]) -> bool:
    """Strukturalny sanity-check: gdyby kolejność pól była inna, te warunki nie przejdą."""
    if not bars:
        return False
    for bar in bars[:200]:
        if not (bar.low <= bar.open <= bar.high and bar.low <= bar.close <= bar.high):
            return False
    stamps = [b.ts for b in bars]
    return stamps == sorted(stamps) and len(set(stamps)) == len(stamps)


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


class BarBuilder:
    """Składa świece OHLC wprost z surowych plików, bez materializowania ticków.

    Wcześniej pobieranie zbierało wszystkie ticki z zakresu do jednej listy i dopiero na
    końcu je sortowało i agregowało. Rok danych to około stu milionów ticków, czyli
    kilkanaście gigabajtów w pamięci — więcej, niż ma do dyspozycji cała funkcja. Tutaj
    każdy tick jest natychmiast wliczany do swojego koszyka i zapominany, więc zużycie
    pamięci zależy od liczby *świec wyjściowych*, a nie od liczby ticków.
    """

    __slots__ = ("step", "price", "_buckets")

    def __init__(self, interval_minutes: int, price: str = "bid") -> None:
        if interval_minutes <= 0:
            raise DataError("Interwał świecy musi być większy od zera.")
        if price not in ("bid", "ask", "mid"):
            raise DataError("Cena świecy musi być jedną z: bid, ask, mid.")
        self.step = interval_minutes * 60
        self.price = price
        self._buckets: dict[int, list[float]] = {}

    def add_bar(self, epoch_seconds: float, o: float, h: float, l: float, c: float) -> None:
        """Wlicza gotową świecę — używane, gdy źródłem są świece minutowe, a nie ticki."""
        key = int(epoch_seconds) // self.step
        bucket = self._buckets.get(key)
        if bucket is None:
            self._buckets[key] = [o, h, l, c]
        else:
            if h > bucket[1]:
                bucket[1] = h
            if l < bucket[2]:
                bucket[2] = l
            bucket[3] = c

    def add(self, epoch_seconds: float, value: float) -> None:
        key = int(epoch_seconds) // self.step
        bucket = self._buckets.get(key)
        if bucket is None:
            self._buckets[key] = [value, value, value, value]  # open, high, low, close
        else:
            if value > bucket[1]:
                bucket[1] = value
            elif value < bucket[2]:
                bucket[2] = value
            bucket[3] = value

    def feed_bi5(self, payload: bytes, hour_start: datetime, scale: float) -> int:
        """Dekoduje plik godzinowy i od razu wlicza go w świece. Zwraca liczbę ticków."""
        raw = _decompress(payload)
        if not raw:
            return 0

        base = hour_start.timestamp()
        want_bid = self.price == "bid"
        want_ask = self.price == "ask"
        usable = len(raw) - (len(raw) % TICK_STRUCT.size)
        count = 0

        # iter_unpack jest wyraźnie szybszy od unpack_from w pętli, a pomijanie obiektów
        # Tick oszczędza jedną alokację na każdy z milionów rekordów.
        for millis, ask_points, bid_points, _av, _bv in TICK_STRUCT.iter_unpack(raw[:usable]):
            if ask_points == 0 or bid_points == 0:
                continue
            if want_bid:
                value = bid_points / scale
            elif want_ask:
                value = ask_points / scale
            else:
                value = (bid_points + ask_points) / (2.0 * scale)
            self.add(base + millis / 1000.0, value)
            count += 1
        return count

    def feed_ticks(self, ticks: Iterable[Tick]) -> None:
        for tick in ticks:
            value = tick.bid if self.price == "bid" else (tick.ask if self.price == "ask" else tick.mid)
            self.add(tick.ts.timestamp(), value)

    def bars(self) -> list[Bar]:
        """Gotowe świece, uporządkowane. Sortujemy klucze koszyków — jest ich tyle, ile
        świec wyjściowych, a nie tyle, ile ticków."""
        out: list[Bar] = []
        for key in sorted(self._buckets):
            o, h, l, c = self._buckets[key]
            out.append(
                Bar(
                    ts=datetime.fromtimestamp(key * self.step, tz=timezone.utc),
                    open=o, high=h, low=l, close=c, volume=0.0,
                )
            )
        return out

    def __len__(self) -> int:
        return len(self._buckets)


def ticks_to_bars(ticks: Iterable[Tick], interval_minutes: int, price: str = "bid") -> list[Bar]:
    """Składa ticki w świece OHLC o zadanym interwale.

    Domyślnie po cenie bid — tak samo, jak wykresy walutowe pokazuje TradingView.
    Okresy bez ticków (weekend, przerwa w handlu) po prostu nie tworzą świec.
    """
    builder = BarBuilder(interval_minutes, price)
    # Wejście bywa nieuporządkowane (np. w testach), a otwarcie i zamknięcie świecy
    # zależą od kolejności — dlatego tutaj sortujemy. Ścieżka pobierania tego nie robi,
    # bo pliki godzinowe przychodzą chronologicznie i ticki w nich też.
    builder.feed_ticks(sorted(ticks, key=lambda t: t.ts))
    return builder.bars()


class _ConnectionPool:
    """Połączenia HTTPS wielokrotnego użytku — po jednym na wątek roboczy.

    Pobranie roku historii to ponad sześć tysięcy plików. Standardowe `urlopen` zestawia
    dla każdego z nich osobne połączenie TCP wraz z uściskiem TLS, co przy takiej liczbie
    plików sumuje się w minuty samego narzutu. Tutaj każdy wątek zestawia połączenie raz
    i korzysta z niego wielokrotnie.
    """

    def __init__(self) -> None:
        self._local = threading.local()
        self._all: list[http.client.HTTPSConnection] = []
        self._lock = threading.Lock()

    @staticmethod
    def _proxy() -> Optional[tuple[str, int]]:
        """Adres proxy, jeśli środowisko go narzuca — `http.client` nie czyta go sam."""
        raw = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if not raw:
            return None
        parts = urllib.parse.urlsplit(raw if "://" in raw else f"http://{raw}")
        if not parts.hostname:
            return None
        return parts.hostname, parts.port or 80

    def get(self) -> http.client.HTTPSConnection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn

        host = urllib.parse.urlsplit(BASE_URL).hostname or "datafeed.dukascopy.com"
        proxy = self._proxy()
        if proxy:
            conn = http.client.HTTPSConnection(proxy[0], proxy[1], timeout=TIMEOUT_SECONDS)
            conn.set_tunnel(host, 443)
        else:
            conn = http.client.HTTPSConnection(host, timeout=TIMEOUT_SECONDS)

        self._local.conn = conn
        with self._lock:
            self._all.append(conn)
        return conn

    def drop(self) -> None:
        """Porzuca połączenie tego wątku — używane, gdy okazało się nieżywe."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def close_all(self) -> None:
        with self._lock:
            for conn in self._all:
                try:
                    conn.close()
                except Exception:
                    pass
            self._all.clear()


_POOL = _ConnectionPool()


def _fetch_hour(url: str) -> Optional[bytes]:
    """Pobiera jeden plik godzinowy, korzystając z połączenia współdzielonego w wątku.

    Zwraca `b""`, gdy pliku nie ma (weekend, święto — sytuacja normalna), a `None`,
    gdy mimo ponowień nie udało się go pobrać. Rozróżnienie jest istotne, bo pobranie
    wieloletniego zakresu to dziesiątki tysięcy plików: pojedyncza wywrotka nie może
    przewracać całej roboty, ale musi zostać policzona i zgłoszona.
    """
    path = urllib.parse.urlsplit(url).path

    for attempt in range(RETRIES):
        try:
            conn = _POOL.get()
            conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Connection": "keep-alive"})
            response = conn.getresponse()
            body = response.read()          # zawsze czytamy do końca, inaczej połączenie się zablokuje
            if response.status in (404, 410):
                return b""                  # weekend albo święto — normalne
            if response.status == 200:
                return body
            _POOL.drop()                    # 5xx albo blokada — zacznijmy od świeżego połączenia
        except Exception:
            _POOL.drop()                    # zerwane albo przeterminowane połączenie
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
        # Skoro połączenie działa, od razu sprawdzamy, czy da się użyć szybszej ścieżki.
        candles = verify_candles(instrument)
        return {
            "ok": ticks > 0,
            "url": url,
            "ms": elapsed,
            "bytes": len(payload),
            "ticks": ticks,
            "candles": {"usable": candles.usable, "reason": candles.reason,
                        "compared": candles.compared, "scaled": candles.scaled},
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
class CandleSupport:
    """Werdykt, czy dla danego instrumentu wolno użyć gotowych świec zamiast ticków."""

    usable: bool = False
    layout: Optional[CandleLayout] = None
    reason: str = ""
    compared: int = 0            # ile minut udało się porównać z tickami
    checked: int = 0             # ile wariantów formatu sprawdzono

    @property
    def scaled(self) -> bool:
        return self.layout.scaled if self.layout else True


def verify_candles(
    instrument: str = DEFAULT_INSTRUMENT,
    day: Optional[date] = None,
    price: str = "bid",
    cache_dir: Optional[Path] = None,
) -> CandleSupport:
    """Rozstrzyga, czy pliki ze świecami są czytane poprawnie — przez porównanie z tickami.

    Pobiera dobę świec i jedną godzinę ticków z tej samej doby, składa ticki w świece
    minutowe i porównuje wspólne minuty. Zgodność oznacza, że format odczytujemy dobrze;
    rozbieżność albo brak plików oznacza, że zostajemy przy tickach.
    """
    day = day or date(2024, 1, 2)          # zwykły wtorek, pewny handel przez całą dobę
    scale = instrument_scale(instrument)

    payload = _fetch_hour(day_candles_url(instrument, day, price))
    if not payload:
        return CandleSupport(reason="Archiwum nie ma plików ze świecami pod tym adresem.")

    hour = datetime(day.year, day.month, day.day, 10, tzinfo=timezone.utc)
    tick_payload = _fetch_hour(hour_url(instrument, hour))
    if not tick_payload:
        return CandleSupport(reason="Nie udało się pobrać ticków do porównania.")

    reference = BarBuilder(1, price)
    reference.feed_bi5(tick_payload, hour, scale)
    wanted = {bar.ts: bar for bar in reference.bars()}
    if not wanted:
        return CandleSupport(reason="Godzina odniesienia nie zawiera ticków.")

    day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    layouts = candle_layouts()
    sane_but_wrong = 0

    for layout in layouts:
        try:
            bars = decode_candles(payload, day_start, scale, layout)
        except (struct.error, ValueError, OverflowError, OSError):
            continue          # ten układ w ogóle nie pasuje do zawartości pliku
        if not candles_look_sane(bars):
            continue
        sane_but_wrong += 1

        matched = 0
        for bar in bars:
            other = wanted.get(bar.ts)
            if other is None:
                continue
            if (abs(bar.open - other.open) > CANDLE_TOLERANCE
                    or abs(bar.close - other.close) > CANDLE_TOLERANCE
                    or abs(bar.high - other.high) > CANDLE_TOLERANCE
                    or abs(bar.low - other.low) > CANDLE_TOLERANCE):
                matched = 0
                break
            matched += 1

        if matched >= 10:      # kilkanaście zgodnych minut wystarczy, żeby wykluczyć przypadek
            return CandleSupport(
                True, layout, f"Świece zgadzają się z tickami (układ {layout.label()}).",
                matched, len(layouts),
            )

    if sane_but_wrong:
        powod = (f"Żaden z {len(layouts)} sprawdzonych układów nie dał świec zgodnych z tickami "
                 f"(struktura pasowała w {sane_but_wrong} wariantach, ale ceny się rozjeżdżają).")
    else:
        powod = (f"Plik istnieje, ale żaden z {len(layouts)} sprawdzonych układów nie daje "
                 "poprawnych świec — format jest inny, niż zakładamy.")
    return CandleSupport(reason=powod + " Pobieranie idzie z ticków.", checked=len(layouts))


def inspect_candles(
    instrument: str = DEFAULT_INSTRUMENT,
    day: Optional[date] = None,
    price: str = "bid",
) -> dict[str, object]:
    """Rozkłada plik ze świecami na czynniki pierwsze i pokazuje surowe liczby.

    Format tych plików nie jest udokumentowany. Zamiast zgadywać w nieskończoność,
    pobieramy jeden plik i wypisujemy: ile waży, na ile bajtów dzieli się jego zawartość
    i jak wyglądają pierwsze rekordy odczytane na kilka sposobów — obok wartości
    wyliczonych z ticków, które są pewne. Zestawienie jednego z drugim wystarcza,
    żeby rozpoznać właściwy układ pól.
    """
    day = day or date(2024, 1, 2)
    scale = instrument_scale(instrument)
    url = day_candles_url(instrument, day, price)

    payload = _fetch_hour(url)
    if payload is None:
        return {"url": url, "error": "Nie udało się pobrać pliku (błąd połączenia)."}
    if not payload:
        return {"url": url, "error": "Archiwum nie ma pliku pod tym adresem (404)."}

    try:
        raw = _decompress(payload)
    except DataError as exc:
        return {"url": url, "compressed_bytes": len(payload), "error": str(exc)}

    out: dict[str, object] = {
        "url": url,
        "compressed_bytes": len(payload),
        "raw_bytes": len(raw),
        "dzieli_sie_bez_reszty_przez": [n for n in (12, 16, 20, 24, 28, 32, 40) if len(raw) % n == 0],
        "pierwsze_48_bajtow_hex": raw[:48].hex(),
    }

    # Kilka prawdopodobnych układów 24-bajtowego rekordu.
    layouts = {
        ">i5f": "int32 + 5x float32",
        ">6i": "6x int32",
        ">2i4f": "2x int32 + 4x float32",
        ">i4fi": "int32 + 4x float32 + int32",
        ">5if": "5x int32 + float32",
        ">q4f": "int64 + 4x float32",
    }
    odczyty: dict[str, object] = {}
    for fmt, opis in layouts.items():
        st = struct.Struct(fmt)
        if len(raw) < st.size * 3:
            continue
        try:
            odczyty[f"{fmt} ({opis})"] = [list(st.unpack_from(raw, i * st.size)) for i in range(3)]
        except struct.error:
            continue
    out["odczyty_pierwszych_3_rekordow"] = odczyty

    # Wartości pewne — z ticków tej samej doby, do porównania z powyższymi.
    hour = datetime(day.year, day.month, day.day, 10, tzinfo=timezone.utc)
    tick_payload = _fetch_hour(hour_url(instrument, hour))
    if tick_payload:
        reference = BarBuilder(1, price)
        reference.feed_bi5(tick_payload, hour, scale)
        bars = reference.bars()[:3]
        out["swiece_z_tickow_10_00"] = [
            {"czas": b.ts.isoformat(), "o": round(b.open, 5), "h": round(b.high, 5),
             "l": round(b.low, 5), "c": round(b.close, 5)}
            for b in bars
        ]
        out["skala_instrumentu"] = scale
    return out


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
    source: str = "ticks"         # 'candles' albo 'ticks' — co ostatecznie zadziałało
    skipped_days: list[date] = field(default_factory=list)   # doby, z których nie przyszło nic

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
    use_candles: bool = True,
    tolerate_gaps: bool = False,
) -> DownloadResult:
    """Pobiera zakres dzień po dniu i mówi, dokąd zdążył.

    Idziemy chronologicznie małymi partiami dni. Po każdej partii sprawdzamy budżet czasu:
    gdy się kończy, przerywamy i zwracamy komplet dni domkniętych do tej pory. Wywołujący
    wznawia od `covered_to + 1` — bez tego wieloletni zakres nie miałby szans zmieścić się
    w limicie pojedynczego żądania.

    Awaria pojedynczego pliku godzinowego nie przerywa pobierania; jest liczona i zgłaszana.
    Dopiero gdy **nic** się nie udało, uznajemy to za problem z łączem.

    `tolerate_gaps` mówi, że dane z tego instrumentu już wcześniej przychodziły — wtedy nawet
    doba bez ani jednego pliku jest dziurą do zanotowania, a nie powodem do przerwania. Ustawia
    to wznowione pobieranie: kolejny odcinek zaczyna się od dowolnego dnia i pojedyncza martwa
    doba nie może przekreślić lat ściągniętej już historii.
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

    builder = BarBuilder(interval_minutes, price)
    attempted = 0
    dead_streak = 0        # ile dób z rzędu nie oddało ani jednego pliku

    # Jedna próba na całe pobieranie: jeśli gotowe świece są czytelne, każda doba kosztuje
    # jeden plik zamiast dwudziestu czterech.
    support = verify_candles(instrument, price=price, cache_dir=cache_dir) if use_candles else CandleSupport()
    result.source = "candles" if support.usable else "ticks"

    def load_day_candles(day: date) -> Optional[list[Bar]]:
        cached: Optional[Path] = None
        if cache_dir is not None:
            cached = (cache_dir / instrument / f"{day.year:04d}" / f"{day.month:02d}"
                      / f"{day.day:02d}" / f"candles_min1_{price}.bi5")
            if cached.exists():
                payload = cached.read_bytes()
            else:
                payload = _fetch_hour(day_candles_url(instrument, day, price))
                if payload is None:
                    return None
                try:
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    cached.write_bytes(payload)
                except OSError:
                    pass
        else:
            payload = _fetch_hour(day_candles_url(instrument, day, price))
            if payload is None:
                return None

        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        return decode_candles(payload, start, scale, support.layout)

    def load(when: datetime) -> Optional[tuple[datetime, bytes]]:
        """Zwraca surową zawartość pliku albo None, gdy godziny nie udało się pobrać.

        Dekodowanie zostaje poza wątkami roboczymi: rozpakowanie i przeliczenie ticków
        w większości nie zwalnia blokady interpretera, więc równoległość i tak by tu
        nie pomogła, a wątki mają się zajmować czekaniem na sieć.
        """
        cached: Optional[Path] = None
        if cache_dir is not None:
            cached = (
                cache_dir / instrument / f"{when.year:04d}" / f"{when.month:02d}"
                / f"{when.day:02d}" / f"{when.hour:02d}.bi5"
            )
            if cached.exists():
                return when, cached.read_bytes()

        payload = _fetch_hour(hour_url(instrument, when))
        if payload is None:
            return None
        if cached is not None:
            try:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(payload)
            except OSError:
                pass   # brak miejsca na cache nie może psuć pobierania
        return when, payload

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for index, day in enumerate(days):
            if cancelled is not None and cancelled():
                raise DataError("Pobieranie zostało przerwane.")
            # Budżet sprawdzamy przed każdą dobą, ale nigdy przed pierwszą — inaczej przy
            # ciasnym limicie nie pobralibyśmy niczego i nie było się jak posunąć dalej.
            if index and deadline is not None and time.monotonic() > deadline:
                result.stopped_early = True
                break

            hours_today = hours_in_range(day, day)
            failed_today = 0

            if support.usable:
                bars_today = load_day_candles(day)
                attempted += 1
                if bars_today is None:
                    failed_today = len(hours_today)          # doba przepadła w całości
                    result.failed_hours += 1
                else:
                    for bar in bars_today:
                        builder.add_bar(bar.ts.timestamp(), bar.open, bar.high, bar.low, bar.close)
                    result.hours_done += 1
            else:
                for outcome in pool.map(load, hours_today):
                    attempted += 1
                    if outcome is None:
                        failed_today += 1
                        result.failed_hours += 1
                    else:
                        when, payload = outcome
                        builder.feed_bi5(payload, when, scale)  # od razu w świece, bez listy ticków
                        result.hours_done += 1

            # Doba, z której nie przyszedł ani jeden plik, znaczy co innego na początku,
            # a co innego w środku wieloletniego pobierania.
            if failed_today and failed_today == len(hours_today):
                if result.hours_done == 0 and not tolerate_gaps:
                    # Nic się jeszcze nie udało — archiwum jest po prostu nieosiągalne.
                    # Nie ma sensu mielić kolejnych tysięcy godzin, żeby to potwierdzić.
                    raise DataError(
                        "Nie udało się pobrać z Dukascopy ani jednego pliku "
                        f"({failed_today} prób dla dnia {day.isoformat()}). "
                        "Użyj przycisku Sprawdź połączenie — powie, czy serwer w ogóle widzi "
                        "datafeed.dukascopy.com. W razie blokady wgraj plik CSV ręcznie."
                    )

                # Dane już wcześniej przychodziły, więc to dziura w archiwum albo chwilowa
                # blokada. Dziesięciu lat historii nie wolno wyrzucić przez jedną taką dobę:
                # notujemy ją, przesuwamy się dalej i mówimy o tym na końcu.
                result.skipped_days.append(day)
                dead_streak += 1
                result.covered_to = day
                if dead_streak >= MAX_DEAD_DAYS:
                    # Seria martwych dób to już nie dziura, tylko blokada. Oddajemy sterowanie
                    # zamiast przemielić resztę zakresu na pusto — wznowienie spróbuje ponownie.
                    result.stopped_early = True
                    break
                time.sleep(DEAD_DAY_PAUSE)      # daj archiwum odetchnąć, jeśli to limit żądań
                continue

            dead_streak = 0
            result.covered_to = day
            if progress is not None:
                progress(result.hours_done + result.failed_hours, result.hours_total)

    _POOL.close_all()   # nie zostawiamy otwartych gniazd po zakończonym pobieraniu
    result.bars = builder.bars()
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
