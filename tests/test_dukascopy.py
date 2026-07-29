"""Testy dekodera Dukascopy — na plikach .bi5 budowanych tutaj, bez ruszania sieci."""

import lzma
import struct
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from app.csv_loader import DataError
from app.dukascopy import (
    TICK_STRUCT,
    Tick,
    decode_bi5,
    hour_url,
    hours_in_range,
    instrument_scale,
    ticks_to_bars,
)


def make_bi5(records: list[tuple[int, int, int]], cut_tail: int = 0) -> bytes:
    """Buduje plik .bi5 z listy (milisekundy, ask w punktach, bid w punktach).

    `cut_tail` obcina końcówkę gotowego strumienia — tak wygląda plik z Dukascopy
    pozbawiony znacznika końca strumienia.
    """
    raw = b"".join(TICK_STRUCT.pack(ms, ask, bid, 1.0, 1.0) for ms, ask, bid in records)
    compressor = lzma.LZMACompressor(format=lzma.FORMAT_ALONE)
    stream = compressor.compress(raw) + compressor.flush()
    return stream[: len(stream) - cut_tail] if cut_tail else stream



class _FakeResponse:
    def __init__(self, payload): self._payload = payload
    def read(self): return self._payload
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _fake_urlopen(payload):
    def _open(*a, **k): return _FakeResponse(payload)
    return _open


HOUR = datetime(2024, 1, 2, 8, tzinfo=timezone.utc)


# --- dekodowanie pliku godzinowego -------------------------------------------------


def test_decode_single_hour():
    payload = make_bi5([
        (0, 126_350, 126_340),
        (1_500, 126_360, 126_350),
        (3_599_999, 126_300, 126_290),
    ])
    ticks = decode_bi5(payload, HOUR, scale=100_000.0)

    assert len(ticks) == 3
    assert ticks[0].ts == HOUR
    assert ticks[0].bid == pytest.approx(1.26340)
    assert ticks[0].ask == pytest.approx(1.26350)
    assert ticks[1].ts == datetime(2024, 1, 2, 8, 0, 1, 500_000, tzinfo=timezone.utc)
    assert ticks[2].ts.minute == 59


def test_decode_tolerates_missing_end_of_stream_marker():
    """Plik z Dukascopy bywa pozbawiony znacznika końca strumienia.

    Jednorazowe `lzma.decompress` rzuciłoby wtedy wyjątkiem i straciłoby całą godzinę
    danych — dlatego dekoder używa dekompresora strumieniowego.
    """
    records = [(i * 100, 126_350 + i, 126_340 + i) for i in range(200)]
    ticks = decode_bi5(make_bi5(records, cut_tail=20), HOUR, scale=100_000.0)

    assert len(ticks) >= len(records) * 0.9   # odzyskane niemal wszystko
    assert ticks[0].bid == pytest.approx(1.26340)
    assert ticks[0].ts == HOUR


def test_decode_declares_unknown_size_like_dukascopy_files():
    """Sanity-check samego formatu: nagłówek deklaruje rozmiar jako nieznany."""
    payload = make_bi5([(0, 126_350, 126_340)])
    assert payload[5:13] == b"\xff" * 8


def test_empty_hour_gives_no_ticks():
    """Weekend albo święto — plik jest pusty, to normalna sytuacja."""
    assert decode_bi5(b"", HOUR, scale=100_000.0) == []


def test_zero_prices_are_dropped():
    payload = make_bi5([(0, 0, 0), (100, 126_350, 126_340)])
    ticks = decode_bi5(payload, HOUR, scale=100_000.0)
    assert len(ticks) == 1


def test_trailing_garbage_is_ignored():
    """Niepełny rekord na końcu nie może zepsuć odczytu poprzednich."""
    raw = TICK_STRUCT.pack(0, 126_350, 126_340, 1.0, 1.0) + b"\x00\x07"
    compressor = lzma.LZMACompressor(format=lzma.FORMAT_ALONE)
    payload = compressor.compress(raw) + compressor.flush()
    assert len(decode_bi5(payload, HOUR, scale=100_000.0)) == 1


@pytest.mark.parametrize(
    "instrument,points,expected",
    [
        ("GBPUSD", 126_340, 1.26340),   # 5 miejsc po przecinku
        ("EURUSD", 108_512, 1.08512),
        ("USDJPY", 157_204, 157.204),   # para z jenem — 3 miejsca
        ("XAUUSD", 2_650_120, 2650.120),
    ],
)
def test_price_scaling_per_instrument(instrument, points, expected):
    payload = make_bi5([(0, points, points)])
    tick = decode_bi5(payload, HOUR, scale=instrument_scale(instrument))[0]
    assert tick.bid == pytest.approx(expected)


def test_unknown_instrument_raises():
    with pytest.raises(DataError, match="Nieznany instrument"):
        instrument_scale("PLNXYZ")


# --- budowa adresu -----------------------------------------------------------------


@pytest.mark.parametrize(
    "when,expected_fragment",
    [
        (datetime(2024, 1, 2, 8), "/2024/00/02/08h_ticks.bi5"),    # styczeń to miesiąc 00
        (datetime(2024, 12, 31, 23), "/2024/11/31/23h_ticks.bi5"), # grudzień to miesiąc 11
        (datetime(2003, 5, 5, 0), "/2003/04/05/00h_ticks.bi5"),
    ],
)
def test_month_is_zero_indexed_in_url(when, expected_fragment):
    url = hour_url("GBPUSD", when)
    assert url.endswith(expected_fragment)
    assert "GBPUSD" in url


def test_hours_in_range_skips_saturdays():
    # 2024-01-05 to piątek, 06 sobota, 07 niedziela, 08 poniedziałek
    hours = hours_in_range(date(2024, 1, 5), date(2024, 1, 8))
    days = sorted({h.day for h in hours})
    assert days == [5, 7, 8]           # sobota wypada
    # piątek i poniedziałek w całości, z niedzieli tylko wieczorne otwarcie rynku
    assert len(hours) == 24 + 3 + 24


def test_sunday_morning_is_not_downloaded():
    """Niedzielne przedpołudnie to gwarantowane puste pliki — przy wieloletnim
    zakresie ich pomijanie oszczędza kilkanaście procent żądań."""
    sunday = [h.hour for h in hours_in_range(date(2024, 1, 7), date(2024, 1, 7))]
    assert sunday == [21, 22, 23]


def test_hours_in_range_single_day():
    assert len(hours_in_range(date(2024, 1, 2), date(2024, 1, 2))) == 24


# --- składanie ticków w świece -----------------------------------------------------


def ticks_at(*pairs) -> list[Tick]:
    return [
        Tick(ts=datetime(2024, 1, 2, 8, m, s, tzinfo=timezone.utc), bid=bid, ask=bid + 0.0001)
        for m, s, bid in pairs
    ]


def test_ticks_are_aggregated_into_ohlc():
    bars = ticks_to_bars(
        ticks_at((0, 0, 1.2700), (5, 0, 1.2720), (9, 0, 1.2690), (14, 59, 1.2710)),
        interval_minutes=15,
    )
    assert len(bars) == 1
    bar = bars[0]
    assert bar.ts == datetime(2024, 1, 2, 8, 0, tzinfo=timezone.utc)
    assert bar.open == pytest.approx(1.2700)
    assert bar.high == pytest.approx(1.2720)
    assert bar.low == pytest.approx(1.2690)
    assert bar.close == pytest.approx(1.2710)


def test_bars_split_on_interval_boundary():
    bars = ticks_to_bars(
        ticks_at((0, 0, 1.2700), (14, 59, 1.2710), (15, 0, 1.2750), (29, 0, 1.2740)),
        interval_minutes=15,
    )
    assert len(bars) == 2
    assert bars[0].close == pytest.approx(1.2710)
    assert bars[1].open == pytest.approx(1.2750)
    assert bars[1].ts.minute == 15


def test_periods_without_ticks_produce_no_bar():
    """Przerwa w handlu nie tworzy pustych świec — dziura zostaje dziurą."""
    bars = ticks_to_bars(ticks_at((0, 0, 1.2700), (45, 0, 1.2800)), interval_minutes=15)
    assert len(bars) == 2
    assert [b.ts.minute for b in bars] == [0, 45]


def test_unsorted_ticks_are_ordered_before_aggregation():
    bars = ticks_to_bars(
        ticks_at((9, 0, 1.2690), (0, 0, 1.2700), (5, 0, 1.2720)),
        interval_minutes=15,
    )
    assert bars[0].open == pytest.approx(1.2700)   # najwcześniejszy tick otwiera świecę
    assert bars[0].close == pytest.approx(1.2690)  # najpóźniejszy ją zamyka


@pytest.mark.parametrize("price,expected", [("bid", 1.2700), ("ask", 1.2701), ("mid", 1.27005)])
def test_bar_price_source_is_selectable(price, expected):
    bars = ticks_to_bars(ticks_at((0, 0, 1.2700)), interval_minutes=15, price=price)
    assert bars[0].open == pytest.approx(expected)


def test_one_minute_bars():
    bars = ticks_to_bars(ticks_at((0, 0, 1.2700), (0, 30, 1.2705), (1, 0, 1.2710)), 1)
    assert len(bars) == 2
    assert bars[0].high == pytest.approx(1.2705)


def test_invalid_arguments_raise():
    with pytest.raises(DataError):
        ticks_to_bars([], interval_minutes=0)
    with pytest.raises(DataError):
        ticks_to_bars([], interval_minutes=15, price="last")


# --- cały łańcuch pobierania, z podstawioną warstwą sieciową -----------------------


def test_download_pipeline_end_to_end(tmp_path, monkeypatch):
    """Od pliku godzinowego po gotowe świece: pobranie, cache, dekodowanie, agregacja."""
    import app.dukascopy as duka

    served: list[str] = []

    def fake_fetch(url: str) -> bytes:
        if "candles" in url:
            return b""          # ten instrument nie ma plików ze świecami
        served.append(url)
        hour = int(url.split("/")[-1][:2])
        if hour != 8:
            return b""  # poza ósmą rano rynek w tym teście milczy
        return make_bi5([
            (0, 126_360, 126_350),
            (5 * 60_000, 126_380, 126_370),
            (9 * 60_000, 126_310, 126_300),
            (14 * 60_000, 126_340, 126_330),
            (20 * 60_000, 126_400, 126_390),
        ])

    monkeypatch.setattr(duka, "_fetch_hour", fake_fetch)

    bars = duka.download_bars(
        instrument="GBPUSD",
        start=date(2024, 1, 2),
        end=date(2024, 1, 2),
        interval_minutes=15,
        cache_dir=tmp_path,
    )

    assert len(served) == 24                       # jeden plik na godzinę doby
    assert len(bars) == 2                          # ticki wpadły w dwie świece 15-minutowe
    assert bars[0].ts == datetime(2024, 1, 2, 8, 0, tzinfo=timezone.utc)
    assert bars[0].open == pytest.approx(1.26350)
    assert bars[0].high == pytest.approx(1.26370)
    assert bars[0].low == pytest.approx(1.26300)
    assert bars[0].close == pytest.approx(1.26330)
    assert bars[1].ts.minute == 15

    # drugie pobranie idzie z pamięci podręcznej — sieć nie jest już ruszana
    served.clear()
    again = duka.download_bars(
        instrument="GBPUSD",
        start=date(2024, 1, 2),
        end=date(2024, 1, 2),
        interval_minutes=15,
        cache_dir=tmp_path,
    )
    assert served == []
    assert [b.ts for b in again] == [b.ts for b in bars]


def test_download_reports_progress(tmp_path, monkeypatch):
    import app.dukascopy as duka

    monkeypatch.setattr(duka, "_fetch_hour", lambda url: make_bi5([(0, 126_350, 126_340)]))
    seen: list[tuple[int, int]] = []
    duka.download_bars(
        start=date(2024, 1, 2), end=date(2024, 1, 3), cache_dir=tmp_path,
        progress=lambda done, total: seen.append((done, total)),
    )
    assert seen, "postęp powinien być raportowany"
    assert seen[-1][0] == seen[-1][1] == 48  # dwie doby po 24 godziny


def test_download_can_be_cancelled(tmp_path, monkeypatch):
    import app.dukascopy as duka

    monkeypatch.setattr(duka, "_fetch_hour", lambda url: make_bi5([(0, 126_350, 126_340)]))
    with pytest.raises(DataError, match="przerwane"):
        duka.download_bars(
            start=date(2024, 1, 2), end=date(2024, 1, 31),
            cache_dir=tmp_path, cancelled=lambda: True,
        )


def test_download_without_any_ticks_explains_why(tmp_path, monkeypatch):
    import app.dukascopy as duka

    monkeypatch.setattr(duka, "_fetch_hour", lambda url: b"")
    with pytest.raises(DataError, match="nie zwrócił żadnych ticków"):
        duka.download_bars(start=date(2024, 1, 2), end=date(2024, 1, 2), cache_dir=tmp_path)


def test_reversed_dates_are_rejected(tmp_path):
    import app.dukascopy as duka

    with pytest.raises(DataError, match="późniejsza"):
        duka.download_bars(start=date(2024, 2, 1), end=date(2024, 1, 1), cache_dir=tmp_path)


def test_downloaded_bars_survive_the_csv_round_trip(tmp_path, monkeypatch):
    """Świece z Dukascopy przechodzą tą samą drogą, co wgrany plik — przez CSV."""
    import app.dukascopy as duka
    from app.csv_loader import bars_to_csv, load_bars

    monkeypatch.setattr(
        duka, "_fetch_hour",
        lambda url: make_bi5([(0, 126_360, 126_350), (60_000, 126_380, 126_370)]),
    )
    bars = duka.download_bars(start=date(2024, 1, 2), end=date(2024, 1, 2), cache_dir=tmp_path)
    reloaded = load_bars(bars_to_csv(bars), "UTC").bars

    assert len(reloaded) == len(bars)
    assert reloaded[0].ts == bars[0].ts
    assert reloaded[0].close == pytest.approx(bars[0].close)


def test_aggregated_bars_pass_ohlc_validation():
    """Świece z Dukascopy muszą przejść ten sam sanity-check, co wgrany plik CSV."""
    bars = ticks_to_bars(
        ticks_at((0, 0, 1.2700), (5, 0, 1.2760), (9, 0, 1.2650), (14, 0, 1.2710)),
        interval_minutes=15,
    )
    for bar in bars:
        assert bar.high >= max(bar.open, bar.close)
        assert bar.low <= min(bar.open, bar.close)
        assert bar.high >= bar.low


# --- pobieranie wznawialne i odporne na awarie --------------------------------------


def test_deadline_stops_between_days_and_reports_how_far_it_got(tmp_path, monkeypatch):
    """Sedno poprawki na Vercelu: zamiast dać się ubić po 60 s, oddajemy komplet
    domkniętych dni i mówimy, skąd wznowić."""
    import app.dukascopy as duka

    monkeypatch.setattr(duka, "_fetch_hour", lambda url: make_bi5([(0, 126_350, 126_340)]))
    result = duka.download_window(
        start=date(2024, 1, 1), end=date(2024, 1, 31),
        cache_dir=tmp_path, deadline=time.monotonic() - 1,
    )

    assert result.stopped_early
    assert result.covered_to is not None
    assert result.covered_to < date(2024, 1, 31)      # nie zdążył do końca
    assert result.bars                                # ale coś przywiózł
    assert not result.complete


def test_resuming_from_covered_to_leaves_no_gap(tmp_path, monkeypatch):
    """Wznowienie od dnia po `covered_to` musi dać dokładnie ten sam komplet dni
    co pobranie całości za jednym razem."""
    import app.dukascopy as duka

    monkeypatch.setattr(duka, "_fetch_hour", lambda url: make_bi5([(0, 126_350, 126_340)]))
    whole = duka.download_window(start=date(2024, 1, 1), end=date(2024, 1, 12), cache_dir=tmp_path)

    first = duka.download_window(
        start=date(2024, 1, 1), end=date(2024, 1, 12),
        cache_dir=tmp_path, deadline=time.monotonic() - 1,
    )
    second = duka.download_window(
        start=first.covered_to + timedelta(days=1), end=date(2024, 1, 12), cache_dir=tmp_path
    )

    glued = sorted({b.ts for b in first.bars} | {b.ts for b in second.bars})
    assert glued == [b.ts for b in whole.bars]
    assert len(glued) == len({b.ts for b in first.bars}) + len({b.ts for b in second.bars})


def test_the_first_batch_always_runs_even_with_no_budget(tmp_path, monkeypatch):
    """Inaczej pobieranie stanęłoby w miejscu — bez ani jednego dnia nie ma jak ruszyć dalej."""
    import app.dukascopy as duka

    monkeypatch.setattr(duka, "_fetch_hour", lambda url: make_bi5([(0, 126_350, 126_340)]))
    result = duka.download_window(
        start=date(2024, 1, 1), end=date(2024, 1, 31),
        cache_dir=tmp_path, deadline=time.monotonic() - 999,
    )
    assert result.covered_to is not None
    assert result.bars


def test_a_single_broken_hour_does_not_sink_the_whole_download(tmp_path, monkeypatch):
    """Przy tysiącach plików jedna wywrotka jest nieunikniona i nie może kasować reszty."""
    import app.dukascopy as duka

    def flaky(url: str):
        if "candles" in url:
            return b""
        return None if url.endswith("03h_ticks.bi5") else make_bi5([(0, 126_350, 126_340)])

    monkeypatch.setattr(duka, "_fetch_hour", flaky)
    result = duka.download_window(start=date(2024, 1, 2), end=date(2024, 1, 3), cache_dir=tmp_path)

    assert result.failed_hours == 2          # po jednej feralnej godzinie na dobę
    assert result.hours_done == 46
    assert result.bars


def test_a_failed_hour_is_not_cached_as_empty(tmp_path, monkeypatch):
    """Zapisanie pustki po nieudanym pobraniu utrwaliłoby dziurę na zawsze."""
    import app.dukascopy as duka

    monkeypatch.setattr(duka, "_fetch_hour", lambda url: None)
    with pytest.raises(DataError, match="ani jednego pliku"):
        duka.download_window(start=date(2024, 1, 2), end=date(2024, 1, 2), cache_dir=tmp_path)
    assert list(tmp_path.rglob("*.bi5")) == []


def test_total_network_failure_points_at_the_connection_check(tmp_path, monkeypatch):
    """Komunikat ma kierować do diagnostyki, a nie zostawiać z samym 'nie udało się'."""
    import app.dukascopy as duka

    monkeypatch.setattr(duka, "_fetch_hour", lambda url: None)
    with pytest.raises(DataError, match="Sprawdź połączenie"):
        duka.download_window(start=date(2024, 1, 2), end=date(2024, 1, 3), cache_dir=tmp_path)


def test_missing_files_are_not_treated_as_failures(tmp_path, monkeypatch):
    """404 to weekend albo święto — normalny stan, nie awaria."""
    import app.dukascopy as duka

    monkeypatch.setattr(duka, "_fetch_hour", lambda url: b"")
    result = duka.download_window(start=date(2024, 1, 2), end=date(2024, 1, 2), cache_dir=tmp_path)

    assert result.failed_hours == 0
    assert result.bars == []
    assert result.covered_to == date(2024, 1, 2)


def test_covered_to_only_advances_on_fully_finished_days(tmp_path, monkeypatch):
    import app.dukascopy as duka

    monkeypatch.setattr(duka, "_fetch_hour", lambda url: make_bi5([(0, 126_350, 126_340)]))
    result = duka.download_window(start=date(2024, 1, 2), end=date(2024, 1, 4), cache_dir=tmp_path)

    assert result.covered_to == date(2024, 1, 4)
    assert result.complete


# --- diagnostyka połączenia ---------------------------------------------------------


def test_probe_reports_success_with_timing(monkeypatch):
    import app.dukascopy as duka

    payload = make_bi5([(0, 126_350, 126_340), (1000, 126_360, 126_350)])
    monkeypatch.setattr(duka.urllib.request, "urlopen", _fake_urlopen(payload))
    r = duka.probe("GBPUSD")

    assert r["ok"] is True
    assert r["ticks"] == 2
    assert r["bytes"] == len(payload)
    assert isinstance(r["ms"], int)
    assert "/2024/00/02/10h_ticks.bi5" in r["url"]


def test_probe_explains_a_blocked_connection(monkeypatch):
    import app.dukascopy as duka

    def boom(*a, **k):
        raise duka.urllib.error.URLError("Connection refused")

    monkeypatch.setattr(duka.urllib.request, "urlopen", boom)
    r = duka.probe()

    assert r["ok"] is False
    assert "datafeed.dukascopy.com" in r["error"]


def test_probe_flags_a_provider_block(monkeypatch):
    """403 pod adresem, który na pewno istnieje, znaczy zwykle blokadę ruchu z serwerowni."""
    import app.dukascopy as duka

    def forbidden(*a, **k):
        raise duka.urllib.error.HTTPError("u", 403, "Forbidden", {}, None)

    monkeypatch.setattr(duka.urllib.request, "urlopen", forbidden)
    r = duka.probe()

    assert r["ok"] is False
    assert r["status"] == 403
    assert "blokuje" in r["error"]


def test_probe_never_raises(monkeypatch):
    """Diagnostyka, która sama się wywraca, jest bezużyteczna."""
    import app.dukascopy as duka

    def nasty(*a, **k):
        raise OSError("cokolwiek")

    monkeypatch.setattr(duka.urllib.request, "urlopen", nasty)
    assert duka.probe()["ok"] is False


# --- szybkie przerwanie przy systemowej awarii --------------------------------------


def test_a_completely_dead_day_aborts_immediately(tmp_path, monkeypatch):
    """Bez tego pobieranie roku mieliłoby tysiące nieudanych żądań, zanim się podda."""
    import app.dukascopy as duka

    tried = []

    def dead(url: str):
        tried.append(url)
        return None

    monkeypatch.setattr(duka, "_fetch_hour", dead)
    with pytest.raises(DataError, match="ani jednego pliku"):
        duka.download_window(start=date(2024, 1, 2), end=date(2024, 12, 31), cache_dir=tmp_path)

    # jedna sonda na pliki ze świecami plus doba plików godzinowych — i koniec,
    # zamiast mielenia kilkudziesięciu tysięcy żądań przez cały rok
    assert len(tried) == 25
    assert sum(1 for u in tried if "candles" in u) == 1


def test_partial_failures_do_not_abort(tmp_path, monkeypatch):
    """Połowa godzin pada, ale doba coś przywiozła — to nie jest awaria systemowa."""
    import app.dukascopy as duka

    def half(url: str):
        if "candles" in url:
            return b""
        hour = int(url.split("/")[-1][:2])
        return None if hour % 2 else make_bi5([(0, 126_350, 126_340)])

    monkeypatch.setattr(duka, "_fetch_hour", half)
    result = duka.download_window(start=date(2024, 1, 2), end=date(2024, 1, 3), cache_dir=tmp_path)

    assert result.failed_hours == 24
    assert result.bars


# --- współdzielenie połączeń --------------------------------------------------------


class _FakeConn:
    """Atrapa połączenia: liczy żądania i pozwala udawać zerwanie."""

    def __init__(self, status=200, body=b"dane", boom_after=None):
        self.status, self.body, self.boom_after = status, body, boom_after
        self.requests, self.closed = 0, False

    def request(self, method, path, headers=None):
        self.requests += 1
        if self.boom_after is not None and self.requests > self.boom_after:
            raise ConnectionResetError("połączenie zerwane")

    def getresponse(self):
        outer = self

        class _R:
            status = outer.status
            def read(self): return outer.body
        return _R()

    def close(self):
        self.closed = True


def use_pool(monkeypatch, conns):
    """Podstawia pulę oddającą kolejne atrapy połączeń."""
    import app.dukascopy as duka

    pool = duka._ConnectionPool()
    it = iter(conns)
    current = {"c": None}

    monkeypatch.setattr(pool, "get", lambda: current["c"] or current.__setitem__("c", next(it)) or current["c"])
    monkeypatch.setattr(pool, "drop", lambda: current.__setitem__("c", None))
    monkeypatch.setattr(duka, "_POOL", pool)
    return pool


def test_many_files_share_one_connection(monkeypatch):
    """Sedno poprawki: rok historii to tysiące plików, a uścisk TLS ma nastąpić raz."""
    import app.dukascopy as duka

    conn = _FakeConn(body=b"zawartosc")
    use_pool(monkeypatch, [conn, _FakeConn()])

    for i in range(25):
        assert duka._fetch_hour(f"https://x/{i}.bi5") == b"zawartosc"
    assert conn.requests == 25          # wszystko poszło jednym połączeniem


def test_missing_file_is_still_an_empty_result(monkeypatch):
    import app.dukascopy as duka

    use_pool(monkeypatch, [_FakeConn(status=404)])
    assert duka._fetch_hour("https://x/a.bi5") == b""


def test_a_dropped_connection_is_replaced_and_the_file_still_arrives(monkeypatch):
    """Połączenie trzymane godzinami bywa zamykane przez drugą stronę — to nie może
    kosztować pliku."""
    import app.dukascopy as duka

    martwe = _FakeConn(boom_after=0)
    swieze = _FakeConn(body=b"udalo sie")
    use_pool(monkeypatch, [martwe, swieze])

    assert duka._fetch_hour("https://x/a.bi5") == b"udalo sie"
    assert martwe.closed is False        # zamknięciem zajmuje się drop(), tu podmieniony


def test_server_error_gives_up_after_retries(monkeypatch):
    import app.dukascopy as duka

    use_pool(monkeypatch, [_FakeConn(status=500) for _ in range(5)])
    assert duka._fetch_hour("https://x/a.bi5") is None


def test_proxy_is_honoured_when_the_environment_sets_one(monkeypatch):
    """http.client, w odróżnieniu od urllib, nie czyta zmiennych proxy sam."""
    import app.dukascopy as duka

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    assert duka._ConnectionPool._proxy() == ("proxy.local", 3128)

    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    assert duka._ConnectionPool._proxy() is None


def test_proxy_without_scheme_is_understood(monkeypatch):
    import app.dukascopy as duka

    monkeypatch.setenv("HTTPS_PROXY", "proxy.local:8080")
    assert duka._ConnectionPool._proxy() == ("proxy.local", 8080)


# --- składanie świec w locie --------------------------------------------------------


def test_streaming_matches_the_tick_based_aggregation():
    """Nowa ścieżka musi dawać co do bitu to samo, co stara przez listę ticków."""
    from app.dukascopy import BarBuilder

    payload = make_bi5([(m * 60_000 + s * 1000, 126_350 + m, 126_340 + m)
                        for m in range(60) for s in (0, 30)])
    hour = datetime(2024, 1, 2, 8, tzinfo=timezone.utc)

    stara = ticks_to_bars(decode_bi5(payload, hour, 100_000.0), 15)
    builder = BarBuilder(15)
    builder.feed_bi5(payload, hour, 100_000.0)

    assert [(b.ts, b.open, b.high, b.low, b.close) for b in builder.bars()] \
        == [(b.ts, b.open, b.high, b.low, b.close) for b in stara]


def test_builder_holds_candles_not_ticks():
    """Zużycie pamięci ma zależeć od liczby świec, nie od liczby ticków."""
    from app.dukascopy import BarBuilder

    payload = make_bi5([(i * 100, 126_350, 126_340) for i in range(30_000)])
    b = BarBuilder(15)
    b.feed_bi5(payload, datetime(2024, 1, 2, 8, tzinfo=timezone.utc), 100_000.0)

    assert len(b) <= 4          # godzina to najwyżej cztery świece 15-minutowe
    assert len(b.bars()) <= 4


@pytest.mark.parametrize("price,expected", [("bid", 1.26340), ("ask", 1.26350), ("mid", 1.26345)])
def test_builder_respects_the_price_source(price, expected):
    from app.dukascopy import BarBuilder

    b = BarBuilder(15, price)
    b.feed_bi5(make_bi5([(0, 126_350, 126_340)]), datetime(2024, 1, 2, 8, tzinfo=timezone.utc), 100_000.0)
    assert b.bars()[0].open == pytest.approx(expected)


# --- gotowe świece zamiast ticków ---------------------------------------------------


def make_candles(records, scaled=True, scale=100_000.0) -> bytes:
    """Buduje plik ze świecami minutowymi z listy (sekundy_od_polnocy, o, h, l, c)."""
    from app.dukascopy import CANDLE_STRUCT

    mul = scale if scaled else 1.0
    raw = b"".join(
        CANDLE_STRUCT.pack(t, o * mul, c * mul, l * mul, h * mul, 1.0)
        for t, o, h, l, c in records
    )
    comp = lzma.LZMACompressor(format=lzma.FORMAT_ALONE)
    return comp.compress(raw) + comp.flush()


def test_candle_url_uses_zero_indexed_month_and_price_side():
    from app.dukascopy import day_candles_url

    url = day_candles_url("GBPUSD", date(2024, 1, 2), "bid")
    assert url.endswith("/2024/00/02/BID_candles_min_1.bi5")
    assert day_candles_url("GBPUSD", date(2024, 1, 2), "ask").endswith("ASK_candles_min_1.bi5")


def test_candles_decode_into_bars():
    from app.dukascopy import decode_candles

    payload = make_candles([(600, 1.2630, 1.2640, 1.2625, 1.2635),
                            (660, 1.2635, 1.2650, 1.2630, 1.2645)])
    bars = decode_candles(payload, datetime(2024, 1, 2, tzinfo=timezone.utc), 100_000.0)

    assert len(bars) == 2
    assert bars[0].ts == datetime(2024, 1, 2, 0, 10, tzinfo=timezone.utc)
    assert bars[0].open == pytest.approx(1.2630)
    assert bars[0].high == pytest.approx(1.2640)
    assert bars[0].low == pytest.approx(1.2625)
    assert bars[0].close == pytest.approx(1.2635)


def test_sanity_check_rejects_a_wrong_field_order():
    """Gdyby kolejność pól w rekordzie była inna, ceny przestaja spelniac zaleznosci OHLC."""
    from app.dukascopy import CANDLE_STRUCT, candles_look_sane, decode_candles

    # zapisujemy pola w zlej kolejnosci: high tam, gdzie oczekiwany jest low
    raw = CANDLE_STRUCT.pack(0, 1.2630e5, 1.2635e5, 1.2650e5, 1.2625e5, 1.0)
    comp = lzma.LZMACompressor(format=lzma.FORMAT_ALONE)
    payload = comp.compress(raw) + comp.flush()

    bars = decode_candles(payload, datetime(2024, 1, 2, tzinfo=timezone.utc), 100_000.0)
    assert not candles_look_sane(bars)


def test_empty_minutes_are_skipped():
    from app.dukascopy import decode_candles

    payload = make_candles([(0, 0, 0, 0, 0), (60, 1.2630, 1.2640, 1.2625, 1.2635)])
    bars = decode_candles(payload, datetime(2024, 1, 2, tzinfo=timezone.utc), 100_000.0)
    assert len(bars) == 1


def _matching_sources(scaled=True):
    """Ticki i świece opisujące te same minuty — tak wygląda archiwum, gdy format czytamy dobrze."""
    minutes = [(m, 1.2630 + m / 10000, 1.2640 + m / 10000, 1.2620 + m / 10000, 1.2635 + m / 10000)
               for m in range(30)]
    ticks = []
    for m, o, h, l, c in minutes:
        base = (10 * 60 + m) * 60_000        # godzina 10:00 tej doby
        ticks += [(base, round(o * 1e5), round(o * 1e5)),
                  (base + 10_000, round(h * 1e5), round(h * 1e5)),
                  (base + 20_000, round(l * 1e5), round(l * 1e5)),
                  (base + 50_000, round(c * 1e5), round(c * 1e5))]
    hour_payload = make_bi5([(t - 10 * 3_600_000, a, b) for t, a, b in ticks])
    candle_payload = make_candles([((10 * 60 + m) * 60, o, h, l, c) for m, o, h, l, c in minutes],
                                  scaled=scaled)
    return candle_payload, hour_payload


def test_verification_accepts_candles_that_match_the_ticks(monkeypatch):
    """Sedno zabezpieczenia: świec używamy dopiero, gdy zgodzą się z tickami."""
    import app.dukascopy as duka

    candles, ticks = _matching_sources()
    monkeypatch.setattr(duka, "_fetch_hour",
                        lambda url: candles if "candles" in url else ticks)

    verdict = duka.verify_candles("GBPUSD")
    assert verdict.usable
    assert verdict.scaled is True
    assert verdict.compared >= 10


def test_verification_detects_unscaled_prices(monkeypatch):
    """Gdyby ceny w pliku były zapisane wprost, a nie w punktach — też to rozpoznamy."""
    import app.dukascopy as duka

    candles, ticks = _matching_sources(scaled=False)
    monkeypatch.setattr(duka, "_fetch_hour",
                        lambda url: candles if "candles" in url else ticks)

    verdict = duka.verify_candles("GBPUSD")
    assert verdict.usable and verdict.scaled is False


def test_verification_rejects_candles_that_disagree(monkeypatch):
    """Zły odczyt formatu nie ma prawa przemycić fałszywych cen do wyników."""
    import app.dukascopy as duka

    _, ticks = _matching_sources()
    zle = make_candles([((10 * 60 + m) * 60, 9.9, 9.9, 9.9, 9.9) for m in range(30)])
    monkeypatch.setattr(duka, "_fetch_hour", lambda url: zle if "candles" in url else ticks)

    verdict = duka.verify_candles("GBPUSD")
    assert not verdict.usable
    assert verdict.layout is None
    assert "zgodnych z tickami" in verdict.reason
    assert verdict.checked >= 8          # sprawdzono cały wachlarz układów, nie jeden


def test_missing_candle_files_fall_back_to_ticks(monkeypatch):
    import app.dukascopy as duka

    monkeypatch.setattr(duka, "_fetch_hour", lambda url: b"" if "candles" in url else make_bi5([(0, 1, 1)]))
    verdict = duka.verify_candles("GBPUSD")
    assert not verdict.usable
    assert "nie ma plików" in verdict.reason


def test_download_uses_one_file_per_day_when_candles_work(tmp_path, monkeypatch):
    """Cała korzyść: doba kosztuje jeden plik zamiast dwudziestu czterech."""
    import app.dukascopy as duka

    candles, ticks = _matching_sources()
    pobrane = []

    def fetch(url):
        pobrane.append(url)
        return candles if "candles" in url else ticks

    monkeypatch.setattr(duka, "_fetch_hour", fetch)
    wynik = duka.download_window(start=date(2024, 1, 2), end=date(2024, 1, 4), cache_dir=tmp_path)

    swiecowe = [u for u in pobrane if "candles" in u]
    tickowe = [u for u in pobrane if "ticks" in u]
    assert wynik.source == "candles"
    assert len(swiecowe) == 1 + 3        # sonda weryfikacyjna + trzy doby
    assert len(tickowe) == 1             # tylko godzina odniesienia z weryfikacji
    assert wynik.bars


def test_download_falls_back_to_ticks_without_candles(tmp_path, monkeypatch):
    import app.dukascopy as duka

    monkeypatch.setattr(duka, "_fetch_hour",
                        lambda url: b"" if "candles" in url else make_bi5([(0, 126_350, 126_340)]))
    wynik = duka.download_window(start=date(2024, 1, 2), end=date(2024, 1, 2), cache_dir=tmp_path)

    assert wynik.source == "ticks"
    assert wynik.bars


def test_candles_can_be_switched_off(tmp_path, monkeypatch):
    import app.dukascopy as duka

    candles, ticks = _matching_sources()
    monkeypatch.setattr(duka, "_fetch_hour", lambda url: candles if "candles" in url else ticks)
    wynik = duka.download_window(start=date(2024, 1, 2), end=date(2024, 1, 2),
                                 cache_dir=tmp_path, use_candles=False)
    assert wynik.source == "ticks"


def test_verification_finds_the_layout_whatever_it_is(monkeypatch):
    """Nie zgadujemy jednego układu — przeszukujemy wachlarz i wybieramy ten,
    który zgadza się z tickami. Tu plik jest zapisany 'na odwrót' względem domyślnego."""
    import app.dukascopy as duka
    from app.dukascopy import CANDLE_STRUCT

    minutes = [(m, 1.2630 + m / 10000, 1.2640 + m / 10000, 1.2620 + m / 10000, 1.2635 + m / 10000)
               for m in range(30)]
    ticks = []
    for m, o, h, l, c in minutes:
        base = (10 * 60 + m) * 60_000
        ticks += [(base, round(o * 1e5), round(o * 1e5)),
                  (base + 10_000, round(h * 1e5), round(h * 1e5)),
                  (base + 20_000, round(l * 1e5), round(l * 1e5)),
                  (base + 50_000, round(c * 1e5), round(c * 1e5))]
    tick_payload = make_bi5([(t - 10 * 3_600_000, a, b) for t, a, b in ticks])

    # kolejność OHLC (nie OCLH), ceny wprost, czas w milisekundach
    raw = b"".join(
        CANDLE_STRUCT.pack((10 * 60 + m) * 60 * 1000, o, h, l, c, 1.0)
        for m, o, h, l, c in minutes
    )
    comp = lzma.LZMACompressor(format=lzma.FORMAT_ALONE)
    candle_payload = comp.compress(raw) + comp.flush()

    monkeypatch.setattr(duka, "_fetch_hour",
                        lambda url: candle_payload if "candles" in url else tick_payload)

    verdict = duka.verify_candles("GBPUSD")
    assert verdict.usable
    assert verdict.layout.order == "ohlc"
    assert verdict.layout.scaled is False
    assert verdict.layout.time_divisor == 1000


def test_every_layout_variant_is_reachable():
    """Wachlarz ma pokrywać obie kolejności cen, oba zapisy ceny i obie jednostki czasu."""
    from app.dukascopy import candle_layouts

    layouts = candle_layouts()
    assert len({l.order for l in layouts}) == 2
    assert len({l.scaled for l in layouts}) == 2
    assert len({l.time_divisor for l in layouts}) == 2
    assert len(layouts) == len(set(layouts))      # bez duplikatów


def test_inspector_reports_raw_bytes_and_readings(monkeypatch):
    """Gdy nic nie pasuje, inspektor ma pokazać surowe bajty do ręcznego rozpoznania."""
    import app.dukascopy as duka

    payload = make_candles([(600, 1.2630, 1.2640, 1.2625, 1.2635)])
    monkeypatch.setattr(duka, "_fetch_hour",
                        lambda url: payload if "candles" in url else make_bi5([(0, 126_350, 126_340)]))

    raport = duka.inspect_candles("GBPUSD")
    assert raport["compressed_bytes"] > 0
    assert raport["raw_bytes"] == 24
    assert 24 in raport["dzieli_sie_bez_reszty_przez"]
    assert len(raport["pierwsze_48_bajtow_hex"]) > 0
    assert raport["odczyty_pierwszych_3_rekordow"] == {} or isinstance(
        raport["odczyty_pierwszych_3_rekordow"], dict)


def test_inspector_survives_a_missing_file(monkeypatch):
    import app.dukascopy as duka

    monkeypatch.setattr(duka, "_fetch_hour", lambda url: b"")
    assert "404" in duka.inspect_candles("GBPUSD")["error"]
