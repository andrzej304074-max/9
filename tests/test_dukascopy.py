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


def test_total_network_failure_is_reported_as_such(tmp_path, monkeypatch):
    import app.dukascopy as duka

    monkeypatch.setattr(duka, "_fetch_hour", lambda url: None)
    with pytest.raises(DataError, match="połączenie z internetem"):
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
