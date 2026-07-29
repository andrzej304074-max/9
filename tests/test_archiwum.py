"""Testy narzędzia do masowego pobrania archiwum.

Samego pobierania nie ruszamy — archiwum podstawiamy atrapą. Sprawdzamy to, co
narzędzie robi z wynikiem: czy liczy okres poprawnie, czy każdy instrument trafia
do biblioteki jako osobna pozycja z sensowną nazwą i czy powtórka korzysta
z pamięci podręcznej zamiast sieci.
"""

from __future__ import annotations

import importlib
import lzma
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def narzedzie(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(tmp_path))
    for name in ("app.runtime", "app.library"):
        sys.modules.pop(name, None)
    sys.modules.pop("tools.pobierz_archiwum", None)
    spec = importlib.util.spec_from_file_location(
        "narzedzie_archiwum", Path(__file__).resolve().parent.parent / "tools" / "pobierz_archiwum.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    for name in ("app.runtime", "app.library"):
        sys.modules.pop(name, None)


def archiwum(baza: int = 126_350, log: list | None = None):
    """Atrapa archiwum: godziny sesji mają ticki, reszta jest pusta."""
    import app.dukascopy as duka

    def _fetch(url: str):
        if log is not None:
            log.append(url)
        if "candles" in url:
            return b""
        hour = int(url.rstrip(".bi5").split("/")[-1][:2])
        if not 8 <= hour < 17:
            return b""
        raw = b"".join(duka.TICK_STRUCT.pack(m * 60_000, baza + m, baza + m - 1, 1.0, 1.0)
                       for m in range(60))
        comp = lzma.LZMACompressor(format=lzma.FORMAT_ALONE)
        return comp.compress(raw) + comp.flush()

    return _fetch


# --- liczenie okresu ---------------------------------------------------------------


def test_range_ends_yesterday(narzedzie):
    """Dzisiejsze godziny bywają jeszcze niegotowe w archiwum."""
    _, koniec = narzedzie.lata_wstecz(1)
    assert koniec == date.today() - timedelta(days=1)


def test_range_spans_the_requested_years(narzedzie):
    poczatek, koniec = narzedzie.lata_wstecz(10)
    assert 3640 <= (koniec - poczatek).days <= 3660


def test_range_never_predates_the_archive(narzedzie):
    """Archiwum zaczyna się w 2003 — prośba o 50 lat nie może zejść niżej."""
    poczatek, _ = narzedzie.lata_wstecz(50)
    assert poczatek == date(2003, 1, 1)


# --- zapis do biblioteki -----------------------------------------------------------


def test_each_instrument_becomes_its_own_entry(narzedzie, monkeypatch):
    import app.dukascopy as duka
    from app import library

    for kod, baza in (("GBPUSD", 126_350), ("EURUSD", 108_500)):
        monkeypatch.setattr(duka, "_fetch_hour", archiwum(baza))
        assert narzedzie.pobierz_instrument(kod, date(2024, 1, 2), date(2024, 1, 5), 15, "bid")

    nazwy = [e["name"] for e in library.entries()]
    assert len(nazwy) == 2
    assert any("GBP/USD" in n for n in nazwy)
    assert any("EUR/USD" in n for n in nazwy)


def test_the_entry_name_carries_instrument_interval_and_period(narzedzie, monkeypatch):
    import app.dukascopy as duka
    from app import library

    monkeypatch.setattr(duka, "_fetch_hour", archiwum())
    narzedzie.pobierz_instrument("GBPUSD", date(2024, 1, 2), date(2024, 1, 5), 15, "bid")

    wpis = library.entries()[0]
    assert "GBP/USD" in wpis["name"]
    assert "15 min" in wpis["name"]
    assert wpis["first_date"] == "2024-01-02"
    assert wpis["interval_minutes"] == 15
    assert wpis["bars"] > 0


def test_an_empty_period_is_skipped_without_an_entry(narzedzie, monkeypatch):
    """Weekend albo święto — nie ma czego zapisywać."""
    import app.dukascopy as duka
    from app import library

    monkeypatch.setattr(duka, "_fetch_hour", lambda url: b"")
    assert not narzedzie.pobierz_instrument("GBPUSD", date(2024, 1, 6), date(2024, 1, 7), 15, "bid")
    assert library.entries() == []


def test_a_repeat_run_uses_the_cache_instead_of_the_network(narzedzie, monkeypatch):
    """Sedno wznawiania: raz pobrane godziny nie są pobierane drugi raz."""
    import app.dukascopy as duka

    pierwszy: list[str] = []
    monkeypatch.setattr(duka, "_fetch_hour", archiwum(log=pierwszy))
    narzedzie.pobierz_instrument("GBPUSD", date(2024, 1, 2), date(2024, 1, 3), 15, "bid")
    assert pierwszy

    drugi: list[str] = []

    def martwa_siec(url: str):
        drugi.append(url)
        return None                      # sieć niedostępna
    monkeypatch.setattr(duka, "_fetch_hour", martwa_siec)

    assert narzedzie.pobierz_instrument("GBPUSD", date(2024, 1, 2), date(2024, 1, 3), 15, "bid")

    # Ani jeden plik godzinowy nie poszedł do sieci — wszystkie przyszły z dysku.
    # Zostaje jedynie sonda sprawdzająca, czy da się użyć gotowych świec; jest jedna
    # na całe pobieranie i jej niepowodzenie tylko cofa nas do ticków.
    assert not [u for u in drugi if "ticks" in u]
    assert all("candles" in u for u in drugi)


def test_the_interval_choice_is_respected(narzedzie, monkeypatch):
    import app.dukascopy as duka
    from app import library

    monkeypatch.setattr(duka, "_fetch_hour", archiwum())
    narzedzie.pobierz_instrument("GBPUSD", date(2024, 1, 2), date(2024, 1, 3), 60, "bid")
    assert library.entries()[0]["interval_minutes"] == 60


# --- formatowanie dla człowieka ----------------------------------------------------


@pytest.mark.parametrize("sekundy,oczekiwane", [(45, "45 s"), (600, "10 min"), (7200, "2.0 h")])
def test_durations_are_readable(narzedzie, sekundy, oczekiwane):
    assert narzedzie.ludzko(sekundy) == oczekiwane


@pytest.mark.parametrize("bajty,fragment", [(2048, "KB"), (5 * 1024**2, "MB"), (3 * 1024**3, "GB")])
def test_sizes_are_readable(narzedzie, bajty, fragment):
    assert fragment in narzedzie.rozmiar(bajty)
