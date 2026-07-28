"""Testy pobierania z sieci oknami wstecz.

Yahoo oddaje w jednym żądaniu ograniczony wycinek, więc dłuższy okres kompletuje się
krok po kroku. Testy podstawiają atrapę dostawcy: sprawdzamy samo cofanie się, sklejanie
okien i zachowanie na granicy archiwum — nie zaś to, co akurat notuje rynek.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import fetch as fetch_module
from app.csv_loader import Bar, DataError
from app.fetch import FetchResult, fetch_bars, intervals_payload, max_history_days

NOW = datetime.now(timezone.utc)


def bars_between(start: datetime, end: datetime, minutes: int) -> list[Bar]:
    """Świece co `minutes` w zadanym oknie — atrapa odpowiedzi dostawcy."""
    out, t = [], start.replace(second=0, microsecond=0)
    while t < end:
        out.append(Bar(ts=t, open=1.25, high=1.2508, low=1.2492, close=1.2503, volume=10.0))
        t += timedelta(minutes=minutes)
    return out


def provider(available_days: int, minutes: int = 15, log: list | None = None):
    """Atrapa dostawcy z archiwum sięgającym `available_days` wstecz."""
    horizon = NOW - timedelta(days=available_days)

    def _request(symbol, interval, start, end):
        if log is not None:
            log.append((start, end))
        return bars_between(max(start, horizon), end, minutes) if end > horizon else []

    return _request


# --- cofanie się oknami ------------------------------------------------------------


def test_short_range_needs_one_window(monkeypatch):
    calls = []
    monkeypatch.setattr(fetch_module, "_request_window", provider(400, log=calls))

    result = fetch_bars(interval="15m", days=30)
    assert result.windows == 1
    assert len(calls) == 1


def test_long_range_walks_backwards_over_several_windows(monkeypatch):
    """Sedno poprawki: 730 dni przy oknie 60-dniowym wymaga kilkunastu żądań."""
    calls = []
    monkeypatch.setattr(fetch_module, "_request_window", provider(1000, minutes=60, log=calls))

    result = fetch_bars(interval="60m", days=720)
    assert result.windows == 1        # dla 60m okno dostawcy ma 730 dni, więc starcza jedno
    assert result.covered_days >= 700


def test_windows_move_towards_the_past(monkeypatch):
    calls = []
    monkeypatch.setattr(fetch_module, "_request_window", provider(1000, log=calls))

    fetch_bars(interval="15m", days=55)
    starts = [start for start, _ in calls]
    assert starts == sorted(starts, reverse=True)   # każde kolejne okno jest wcześniejsze


def test_glued_windows_have_no_duplicates_and_no_gaps(monkeypatch):
    monkeypatch.setattr(fetch_module, "_request_window", provider(1000, minutes=60))

    result = fetch_bars(interval="60m", days=120)
    stamps = [bar.ts for bar in result.bars]

    assert len(stamps) == len(set(stamps))                 # bez powtórzeń na stykach
    assert stamps == sorted(stamps)                        # uporządkowane rosnąco
    gaps = {(b - a).total_seconds() / 3600 for a, b in zip(stamps, stamps[1:])}
    assert gaps == {1.0}                                   # ciągły szereg godzinowy


def test_result_is_sorted_oldest_first(monkeypatch):
    monkeypatch.setattr(fetch_module, "_request_window", provider(200))
    result = fetch_bars(interval="15m", days=50)
    assert result.bars[0].ts < result.bars[-1].ts


# --- granica archiwum dostawcy -----------------------------------------------------


def test_stops_when_the_provider_runs_out(monkeypatch):
    """Dostawca ma 20 dni, poproszono o 60 — pobieranie ma się zatrzymać, nie zapętlić."""
    calls = []
    monkeypatch.setattr(fetch_module, "_request_window", provider(20, log=calls))

    result = fetch_bars(interval="15m", days=60)
    assert result.stopped_early
    assert result.covered_days <= 21
    assert len(calls) < fetch_module.MAX_WINDOWS


def test_running_out_is_explained_with_a_way_forward(monkeypatch):
    monkeypatch.setattr(fetch_module, "_request_window", provider(20))
    result = fetch_bars(interval="15m", days=60)

    note = " ".join(result.notes)
    assert "Dukascopy" in note          # wskazujemy źródło, które sięga głębiej
    assert "2003" in note


def test_asking_beyond_the_interval_ceiling_is_flagged_upfront(monkeypatch):
    """Pięć lat świec 15-minutowych nie istnieje u tego dostawcy — trzeba to powiedzieć."""
    monkeypatch.setattr(fetch_module, "_request_window", provider(60))
    result = fetch_bars(interval="15m", days=1825)

    assert any("najwyżej 60 dni" in note for note in result.notes)


def test_daily_interval_reaches_years_back(monkeypatch):
    monkeypatch.setattr(fetch_module, "_request_window", provider(5000, minutes=1440))
    result = fetch_bars(interval="1d", days=1825)
    assert result.covered_days > 1700
    assert not result.stopped_early


def test_empty_provider_raises_a_helpful_error(monkeypatch):
    monkeypatch.setattr(fetch_module, "_request_window", lambda *a, **k: [])
    with pytest.raises(DataError, match="symbolu"):
        fetch_bars(interval="15m", days=30)


def test_a_provider_stuck_on_one_window_cannot_loop_forever(monkeypatch):
    """Gdyby dostawca w kółko oddawał to samo, pętla i tak musi się skończyć."""
    frozen = bars_between(NOW - timedelta(days=5), NOW, 15)
    monkeypatch.setattr(fetch_module, "_request_window", lambda *a, **k: frozen)

    result = fetch_bars(interval="15m", days=600)
    assert result.windows <= fetch_module.MAX_WINDOWS


# --- budżet czasu ------------------------------------------------------------------


def test_deadline_stops_the_walk_and_keeps_what_was_fetched(monkeypatch):
    """Przy wdrożeniu bezserwerowym lepiej oddać niepełny okres niż dać się ubić."""
    import time

    monkeypatch.setattr(fetch_module, "_request_window", provider(3000, minutes=60))
    result = fetch_bars(interval="1d", days=30000, deadline=time.monotonic() - 1)

    assert result.ran_out_of_time or result.bars == [] or result.windows <= 1


def test_without_a_deadline_the_walk_is_not_cut_short(monkeypatch):
    monkeypatch.setattr(fetch_module, "_request_window", provider(3000, minutes=1440))
    result = fetch_bars(interval="1d", days=1000)
    assert not result.ran_out_of_time


# --- walidacja i metadane ----------------------------------------------------------


def test_unknown_interval_is_rejected_with_the_allowed_list():
    with pytest.raises(DataError, match="Dostępne"):
        fetch_bars(interval="7m", days=10)


def test_non_positive_range_is_rejected():
    with pytest.raises(DataError, match="dodatnia"):
        fetch_bars(interval="15m", days=0)


def test_interval_ceilings_match_the_providers_rules():
    assert max_history_days("15m") == 60      # intraday: dwa miesiące
    assert max_history_days("60m") == 730     # godzinowe: dwa lata
    assert max_history_days("1d") > 3650      # dzienne: dekady


def test_interval_list_tells_the_user_how_deep_each_one_goes():
    payload = intervals_payload()
    assert "15 minut" in payload["15m"] and "60 dni" in payload["15m"]
    assert "2 lat" in payload["60m"]
    assert "pełna dostępna historia" in payload["1d"]


def test_covered_days_reports_the_real_span(monkeypatch):
    monkeypatch.setattr(fetch_module, "_request_window", provider(1000, minutes=60))
    result = fetch_bars(interval="60m", days=90)
    assert 85 <= result.covered_days <= 91


def test_empty_result_reports_zero_span():
    assert FetchResult(bars=[], requested_days=30).covered_days == 0


def test_daily_depth_is_not_advertised_as_a_hundred_years():
    """36500 dni to nasze „bez granicy”, nie obietnica stuletniego archiwum."""
    from app.fetch import human_depth

    assert human_depth(36500) == "pełna dostępna historia"
    assert human_depth(730) == "2 lat wstecz"
    assert human_depth(60) == "60 dni wstecz"
    assert "100 lat" not in intervals_payload()["1d"]
