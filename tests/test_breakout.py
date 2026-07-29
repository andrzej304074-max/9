"""Testy strategii „wybicie zakresu świecy sesyjnej”.

Zakres świecy 8:00–8:15 to w każdym teście 1,1990 – 1,2010 (szerokość 20 pipsów),
więc dystans ryzyka po wybiciu wynosi 0,0020, a take profit leży 0,0080 od wejścia.
"""

from datetime import datetime, timezone

import pytest

from app.config import BacktestConfig
from app.csv_loader import Bar
from app.engine import (
    EXIT_SL,
    EXIT_TP,
    LONG,
    SHORT,
    STATUS_CLOSED,
    STATUS_SKIPPED,
    run_backtest,
)

DAY = "2024-01-02"
RANGE_HIGH = 1.2010
RANGE_LOW = 1.1990


def bar(hhmm: str, o: float, h: float, l: float, c: float, day: str = DAY) -> Bar:
    ts = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    return Bar(ts=ts, open=o, high=h, low=l, close=c, volume=0.0)


def quiet(hhmm: str, price: float = 1.2000, day: str = DAY) -> Bar:
    """Świeca całkowicie wewnątrz zakresu — niczego nie wyzwala."""
    return bar(hhmm, price, price + 0.0001, price - 0.0001, price, day)


def range_candle(day: str = DAY) -> Bar:
    return bar("08:00", 1.2000, RANGE_HIGH, RANGE_LOW, 1.2005, day)


def cfg(**overrides) -> BacktestConfig:
    base = {
        "strategy": "range_breakout",
        "timezone": "UTC",
        "initial_capital": 10000.0,
        "leverage": 1.0,
    }
    base.update(overrides)
    config = BacktestConfig(**base)
    config.validate()
    return config


def run(bars: list[Bar], **overrides):
    outcome, _ = run_backtest(sorted(bars, key=lambda b: b.ts), cfg(**overrides))
    return outcome


def played(outcome) -> list:
    return [t for t in outcome.trades if t.is_executed()]


# --- kierunek i poziomy ------------------------------------------------------------


def test_break_upwards_goes_long_with_stop_on_the_opposite_edge():
    bars = [
        range_candle(),
        quiet("08:15"),
        bar("08:30", 1.2006, 1.2015, 1.2004, 1.2012),  # przebija szczyt zakresu
        quiet("08:45", 1.2012),
    ]
    trade = played(run(bars))[0]

    assert trade.direction == LONG
    assert trade.breakout_side == "up"
    assert trade.entry_price == pytest.approx(RANGE_HIGH)
    assert trade.stop_loss == pytest.approx(RANGE_LOW)          # przeciwna strona świecy
    assert trade.risk_distance == pytest.approx(0.0020)
    assert trade.take_profit == pytest.approx(RANGE_HIGH + 0.0080)


def test_break_downwards_goes_short_with_stop_on_the_opposite_edge():
    bars = [
        range_candle(),
        quiet("08:15"),
        bar("08:30", 1.1995, 1.1996, 1.1985, 1.1988),  # przebija dołek zakresu
        quiet("08:45", 1.1988),
    ]
    trade = played(run(bars))[0]

    assert trade.direction == SHORT
    assert trade.breakout_side == "down"
    assert trade.entry_price == pytest.approx(RANGE_LOW)
    assert trade.stop_loss == pytest.approx(RANGE_HIGH)
    assert trade.take_profit == pytest.approx(RANGE_LOW - 0.0080)


def test_take_profit_is_four_times_the_range():
    trade = played(run([
        range_candle(), quiet("08:15"),
        bar("08:30", 1.2006, 1.2015, 1.2004, 1.2012),
        quiet("08:45", 1.2012),
    ]))[0]
    reward = trade.take_profit - trade.entry_price
    risk = trade.entry_price - trade.stop_loss
    assert reward / risk == pytest.approx(4.0)


def test_custom_rr_ratio_moves_only_the_target():
    trade = played(run([
        range_candle(), quiet("08:15"),
        bar("08:30", 1.2006, 1.2015, 1.2004, 1.2012),
        quiet("08:45", 1.2012),
    ], rr_ratio=2.0))[0]
    assert trade.stop_loss == pytest.approx(RANGE_LOW)
    assert trade.take_profit == pytest.approx(RANGE_HIGH + 0.0040)


def test_invert_mode_fades_the_breakout():
    trade = played(run([
        range_candle(), quiet("08:15"),
        bar("08:30", 1.2006, 1.2015, 1.2004, 1.2012),
        quiet("08:45", 1.2012),
    ], direction_mode="invert"))[0]
    assert trade.direction == SHORT


def test_long_only_skips_a_downward_break():
    outcome = run([
        range_candle(), quiet("08:15"),
        bar("08:30", 1.1995, 1.1996, 1.1985, 1.1988),
        quiet("08:45", 1.1988),
    ], direction_mode="long_only")
    assert played(outcome) == []
    assert "tylko long" in outcome.trades[0].skip_reason


def test_rejected_side_is_reported_once_not_on_every_candle():
    """Odrzucona strona nie może zaśmiecać tabeli jednym wierszem na świecę."""
    bars = [range_candle(), quiet("08:15")]
    for i in range(10):  # dziesięć kolejnych świec poniżej zakresu
        bars.append(bar(f"{8 + (30 + 15 * i) // 60:02d}:{(30 + 15 * i) % 60:02d}",
                        1.1985, 1.1986, 1.1980, 1.1984))
    outcome = run(bars, direction_mode="long_only")
    assert len(outcome.trades) == 1


# --- wyjścia z pozycji -------------------------------------------------------------


def test_take_profit_hit_after_breakout():
    bars = [
        range_candle(), quiet("08:15"),
        bar("08:30", 1.2006, 1.2015, 1.2004, 1.2012),
        bar("08:45", 1.2012, 1.2095, 1.2011, 1.2090),  # sięga TP 1.2090
    ]
    trade = played(run(bars))[0]
    assert trade.exit_reason == EXIT_TP
    assert trade.pnl_money > 0


def test_stop_loss_hit_after_breakout():
    bars = [
        range_candle(), quiet("08:15"),
        bar("08:30", 1.2006, 1.2015, 1.2004, 1.2012),
        bar("08:45", 1.2012, 1.2013, 1.1985, 1.1987),  # wraca pod dołek zakresu
    ]
    trade = played(run(bars))[0]
    assert trade.exit_reason == EXIT_SL
    assert trade.exit_price == pytest.approx(RANGE_LOW)


def test_breakout_candle_can_stop_out_immediately():
    """Świeca wybicia bywa tak szeroka, że sięga też stop lossa po drugiej stronie."""
    bars = [
        range_candle(), quiet("08:15"),
        bar("08:30", 1.2000, 1.2015, 1.1985, 1.1990),
        quiet("08:45", 1.1990),
    ]
    trade = played(run(bars, breakout_both_sides="high_first"))[0]
    assert trade.exit_reason == EXIT_SL
    assert trade.entry_ts == trade.exit_ts


# --- brak wybicia i okno czasowe ---------------------------------------------------


def test_day_without_a_breakout_is_reported():
    bars = [range_candle()] + [quiet(f"{h:02d}:00") for h in range(9, 20)]
    outcome = run(bars)
    assert played(outcome) == []
    assert outcome.trades[0].status == STATUS_SKIPPED
    assert "nie został przebity" in outcome.trades[0].skip_reason


def test_window_end_of_day_accepts_a_late_breakout():
    bars = [range_candle(), quiet("08:15"),
            bar("20:00", 1.2006, 1.2015, 1.2004, 1.2012), quiet("20:15", 1.2012)]
    assert len(played(run(bars, breakout_window_mode="end_of_day"))) == 1


def test_window_until_time_rejects_a_late_breakout():
    bars = [range_candle(), quiet("08:15"),
            bar("20:00", 1.2006, 1.2015, 1.2004, 1.2012), quiet("20:15", 1.2012)]
    outcome = run(bars, breakout_window_mode="until_time", breakout_until_hour=17)
    assert played(outcome) == []
    assert "nie został przebity" in outcome.trades[0].skip_reason


def test_window_hours_after_limits_the_watch_period():
    bars = [range_candle(), quiet("08:15"),
            bar("10:00", 1.2006, 1.2015, 1.2004, 1.2012), quiet("10:15", 1.2012)]
    assert played(run(bars, breakout_window_mode="hours_after", breakout_hours=6)) != []
    assert played(run(bars, breakout_window_mode="hours_after", breakout_hours=1)) == []


def test_breakout_inside_the_range_candle_itself_is_not_counted():
    """Wypatrujemy wybicia dopiero PO zamknięciu świecy zakresowej."""
    bars = [range_candle(), quiet("08:15"), quiet("08:30")]
    assert played(run(bars)) == []


# --- wyzwalacz: dotknięcie kontra zamknięcie ---------------------------------------

WICK_ONLY = [
    range_candle(), quiet("08:15"),
    bar("08:30", 1.2005, 1.2015, 1.2004, 1.2007),  # knot ponad zakresem, zamknięcie w środku
    quiet("08:45", 1.2007),
]


def test_touch_trigger_fires_on_a_wick():
    trades = played(run(WICK_ONLY, breakout_trigger="touch"))
    assert len(trades) == 1
    assert trades[0].entry_price == pytest.approx(RANGE_HIGH)


def test_close_beyond_trigger_ignores_a_wick():
    assert played(run(WICK_ONLY, breakout_trigger="close_beyond")) == []


def test_close_beyond_enters_at_the_closing_price():
    bars = [
        range_candle(), quiet("08:15"),
        bar("08:30", 1.2005, 1.2020, 1.2004, 1.2018),  # zamyka się ponad zakresem
        quiet("08:45", 1.2018),
    ]
    trade = played(run(bars, breakout_trigger="close_beyond"))[0]
    assert trade.entry_price == pytest.approx(1.2018)
    assert trade.stop_loss == pytest.approx(RANGE_LOW)   # stop nadal na granicy zakresu


def test_gap_above_the_level_enters_at_the_open():
    """Przy luce otwarcia ponad poziomem zlecenie stop wykona się po cenie otwarcia."""
    bars = [
        range_candle(), quiet("08:15"),
        bar("08:30", 1.2030, 1.2040, 1.2029, 1.2035),  # otwarcie już ponad zakresem
        quiet("08:45", 1.2035),
    ]
    trade = played(run(bars))[0]
    assert trade.entry_price == pytest.approx(1.2030)   # nie 1.2010


# --- bufor -------------------------------------------------------------------------


def test_buffer_filters_out_a_shallow_poke():
    bars = [
        range_candle(), quiet("08:15"),
        bar("08:30", 1.2005, 1.2012, 1.2004, 1.2008),  # tylko 2 pipsy ponad zakresem
        quiet("08:45", 1.2008),
    ]
    assert played(run(bars, breakout_buffer_pips=0)) != []
    assert played(run(bars, breakout_buffer_pips=5)) == []


def test_buffer_moves_the_entry_but_not_the_stop():
    bars = [
        range_candle(), quiet("08:15"),
        bar("08:30", 1.2005, 1.2030, 1.2004, 1.2025),
        quiet("08:45", 1.2025),
    ]
    trade = played(run(bars, breakout_buffer_pips=5))[0]
    assert trade.entry_price == pytest.approx(RANGE_HIGH + 0.0005)
    assert trade.stop_loss == pytest.approx(RANGE_LOW)


# --- świeca przebijająca obie granice ----------------------------------------------

OUTSIDE_BAR = [
    range_candle(), quiet("08:15"),
    # otwarcie 1.2008 jest bliżej szczytu (0,0002) niż dołka (0,0018)
    bar("08:30", 1.2008, 1.2020, 1.1980, 1.2000),
    quiet("08:45", 1.2000),
]


@pytest.mark.parametrize(
    "rule,expected",
    [("open_proximity", "up"), ("high_first", "up"), ("low_first", "down")],
)
def test_two_sided_break_resolution(rule, expected):
    trade = played(run(OUTSIDE_BAR, breakout_both_sides=rule))[0]
    assert trade.breakout_side == expected


def test_two_sided_break_can_be_skipped_as_unresolvable():
    outcome = run(OUTSIDE_BAR, breakout_both_sides="skip")
    assert played(outcome) == []
    assert "nie został przebity" in outcome.trades[0].skip_reason


def test_open_proximity_picks_the_nearer_edge():
    bars = [
        range_candle(), quiet("08:15"),
        # otwarcie 1.1992 leży bliżej dołka zakresu
        bar("08:30", 1.1992, 1.2020, 1.1980, 1.2000),
        quiet("08:45", 1.2000),
    ]
    assert played(run(bars, breakout_both_sides="open_proximity"))[0].breakout_side == "down"


# --- powtórki w obrębie dnia -------------------------------------------------------

def two_breakouts_same_day() -> list[Bar]:
    """Wybicie górą kończy się stopem, potem cena przebija zakres dołem."""
    return [
        range_candle(), quiet("08:15"),
        bar("08:30", 1.2006, 1.2015, 1.2004, 1.2012),   # wybicie górą, wejście 1.2010
        bar("08:45", 1.2012, 1.2013, 1.1988, 1.1992),   # stop na 1.1990
        bar("09:00", 1.1992, 1.1994, 1.1985, 1.1986),   # przebicie dołem
        quiet("09:15", 1.1986),
    ]


def test_single_mode_plays_one_trade_per_day():
    trades = played(run(two_breakouts_same_day(), breakout_retry_mode="single"))
    assert len(trades) == 1
    assert trades[0].breakout_side == "up"


def test_opposite_mode_allows_the_other_edge():
    trades = played(run(two_breakouts_same_day(), breakout_retry_mode="opposite"))
    assert len(trades) == 2
    assert [t.breakout_side for t in trades] == ["up", "down"]
    assert [t.attempt for t in trades] == [1, 2]


def test_opposite_mode_does_not_replay_the_same_edge():
    bars = [
        range_candle(), quiet("08:15"),
        bar("08:30", 1.2006, 1.2015, 1.2004, 1.2012),
        bar("08:45", 1.2012, 1.2013, 1.1988, 1.1992),   # stop
        bar("09:00", 1.2006, 1.2016, 1.2004, 1.2013),   # znów wybicie GÓRĄ
        quiet("09:15", 1.2013),
    ]
    trades = played(run(bars, breakout_retry_mode="opposite"))
    assert len(trades) == 1


def test_unlimited_mode_respects_the_daily_cap():
    bars = [range_candle(), quiet("08:15")]
    minute = 30
    for _ in range(8):  # osiem par: wybicie górą, potem stop
        hh, mm = 8 + minute // 60, minute % 60
        bars.append(bar(f"{hh:02d}:{mm:02d}", 1.2006, 1.2015, 1.2004, 1.2012))
        minute += 15
        hh, mm = 8 + minute // 60, minute % 60
        bars.append(bar(f"{hh:02d}:{mm:02d}", 1.2012, 1.2013, 1.1988, 1.1992))
        minute += 15
    trades = played(run(bars, breakout_retry_mode="unlimited", breakout_max_per_day=3))
    assert len(trades) == 3
    assert [t.attempt for t in trades] == [1, 2, 3]


def test_next_attempt_never_starts_before_the_previous_one_closes():
    trades = played(run(two_breakouts_same_day(), breakout_retry_mode="opposite"))
    first, second = trades
    assert first.exit_ts is not None
    assert second.entry_ts > first.exit_ts


# --- wspólna maszyneria działa tak samo na tej strategii ---------------------------


def multi_day_breakouts() -> list[Bar]:
    """Dzień 1 otwiera pozycję, która przeżywa cały dzień 2.

    Zakres drugiego dnia leży wewnątrz przedziału SL–TP pozycji z pierwszego dnia
    (1,1990 – 1,2090), więc jej nie domyka i obie mogą istnieć równolegle.
    """
    bars = [
        range_candle("2024-01-02"),                                   # zakres 1,1990–1,2010
        quiet("08:15", 1.2000, "2024-01-02"),
        bar("08:30", 1.2006, 1.2015, 1.2004, 1.2012, "2024-01-02"),   # wejście 1,2010
    ]
    bars += [quiet(f"{h:02d}:00", 1.2012, "2024-01-02") for h in range(9, 24)]

    bars += [
        bar("08:00", 1.2035, 1.2050, 1.2030, 1.2040, "2024-01-03"),   # zakres 1,2030–1,2050
        quiet("08:15", 1.2040, "2024-01-03"),
        bar("08:30", 1.2045, 1.2055, 1.2043, 1.2052, "2024-01-03"),   # wejście 1,2050
    ]
    bars += [quiet(f"{h:02d}:00", 1.2052, "2024-01-03") for h in range(9, 24)]
    return bars


def test_position_modes_apply_to_the_breakout_strategy():
    bars = multi_day_breakouts()
    assert run(bars, position_mode="parallel").max_concurrent == 2
    assert run(bars, position_mode="skip").max_concurrent == 1
    replaced = run(bars, position_mode="replace")
    assert replaced.max_concurrent == 1
    assert replaced.trades[0].exit_reason == "ZASTĄPIONA"


@pytest.mark.parametrize("mode", ["compound", "fixed_notional", "risk_percent"])
def test_sizing_modes_apply_to_the_breakout_strategy(mode):
    trade = played(run([
        range_candle(), quiet("08:15"),
        bar("08:30", 1.2006, 1.2015, 1.2004, 1.2012),
        bar("08:45", 1.2012, 1.2095, 1.2011, 1.2090),
    ], sizing_mode=mode, leverage=10, risk_percent=1.0))[0]
    assert trade.notional > 0
    assert trade.pnl_money > 0


def test_time_based_close_applies_to_the_breakout_strategy():
    bars = [
        range_candle(), quiet("08:15"),
        bar("08:30", 1.2006, 1.2015, 1.2004, 1.2012),
    ] + [quiet(f"{h:02d}:00", 1.2012) for h in range(9, 22)] + [
        bar("22:00", 1.2050, 1.2051, 1.2049, 1.2050),
    ]
    trade = played(run(bars, position_mode="close_at_time", close_time_hour=22))[0]
    assert trade.exit_reason == "CZAS"
    assert trade.exit_price == pytest.approx(1.2050)


def test_weekday_and_date_filters_apply():
    bars = multi_day_breakouts()
    assert len(run(bars).trades) == 2
    assert len(run(bars, date_from="2024-01-03").trades) == 1


# --- rozdział obu strategii --------------------------------------------------------


def test_default_strategy_is_the_original_one():
    assert BacktestConfig().strategy == "candle_direction"
    assert not BacktestConfig().is_breakout


def test_both_strategies_read_the_same_candle_but_trade_differently():
    """Świeca 8:00 jest wzrostowa, ale cena wybija zakres dołem — strategie idą w przeciwne strony."""
    bars = [
        range_candle(),                                  # zamknięcie 1.2005 > otwarcie 1.2000
        quiet("08:15", 1.2005),
        bar("08:30", 1.1995, 1.1996, 1.1985, 1.1988),    # wybicie DOŁEM
        quiet("08:45", 1.1988),
    ]
    outcome, _ = run_backtest(bars, cfg(strategy="candle_direction"))
    direction_trade = [t for t in outcome.trades if t.is_executed()][0]
    breakout_trade = played(run(bars))[0]

    assert direction_trade.direction == LONG    # zielona świeca
    assert breakout_trade.direction == SHORT    # ale wybicie poszło dołem
    assert direction_trade.entry_ts != breakout_trade.entry_ts


# --- co cena ma przebić: zakres czy korpus ------------------------------------------


def body_vs_range_bars() -> list[Bar]:
    """Świeca z długimi knotami: korpus 1,2000–1,2010, pełny zakres 1,1980–1,2030."""
    return [
        bar("08:00", 1.2000, 1.2030, 1.1980, 1.2010),
        quiet("08:15", 1.2010),
        bar("08:30", 1.2011, 1.2018, 1.2009, 1.2016),   # ponad korpusem, poniżej szczytu
        quiet("08:45", 1.2016),
    ]


def test_body_levels_trigger_where_the_full_range_would_not():
    """Korpus leży bliżej ceny, więc wybicie pada tam, gdzie pełny zakres jeszcze milczy."""
    bars = body_vs_range_bars()
    assert played(run(bars, breakout_levels="range")) == []
    trades = played(run(bars, breakout_levels="body"))
    assert len(trades) == 1
    assert trades[0].entry_price == pytest.approx(1.2010)   # kraniec korpusu, nie szczyt


def test_body_levels_give_a_tighter_stop():
    trade = played(run(body_vs_range_bars(), breakout_levels="body"))[0]
    assert trade.stop_loss == pytest.approx(1.2000)         # otwarcie, nie dołek 1,1980
    assert trade.risk_distance == pytest.approx(0.0010)     # zamiast 0,0050


def test_range_is_the_default():
    assert BacktestConfig().breakout_levels == "range"


def test_body_levels_work_downwards_too():
    bars = [
        bar("08:00", 1.2010, 1.2030, 1.1980, 1.2000),      # korpus 1,2000–1,2010
        quiet("08:15", 1.2000),
        bar("08:30", 1.1999, 1.2001, 1.1995, 1.1996),      # poniżej korpusu, powyżej dołka
        quiet("08:45", 1.1996),
    ]
    assert played(run(bars, breakout_levels="range")) == []
    trade = played(run(bars, breakout_levels="body"))[0]
    assert trade.direction == SHORT
    assert trade.entry_price == pytest.approx(1.2000)
    assert trade.stop_loss == pytest.approx(1.2010)


def test_buffer_applies_to_body_levels_as_well():
    bars = body_vs_range_bars()
    assert played(run(bars, breakout_levels="body", breakout_buffer_pips=0)) != []
    assert played(run(bars, breakout_levels="body", breakout_buffer_pips=20)) == []


def test_an_unknown_level_choice_is_rejected():
    from app.config import ConfigError

    with pytest.raises(ConfigError, match="Granice wybicia"):
        BacktestConfig(breakout_levels="cos_innego").validate()


# --- stop odczepiony od granicy wybicia ---------------------------------------------


def wide_wicks() -> list[Bar]:
    """Korpus 1,2000–1,2010, pełne wychylenia 1,1980–1,2030 — knoty są szerokie."""
    return [
        bar("08:00", 1.2000, 1.2030, 1.1980, 1.2010),
        quiet("08:15", 1.2010),
        bar("08:30", 1.2025, 1.2040, 1.2024, 1.2035),   # przebija szczyt wychylenia
        quiet("08:45", 1.2035),
    ]


def test_stop_follows_the_broken_boundary_by_default():
    trade = played(run(wide_wicks()))[0]
    assert trade.entry_price == pytest.approx(1.2030)    # wejście na szczycie wychylenia
    assert trade.stop_loss == pytest.approx(1.1980)      # stop na dołku wychylenia


def test_stop_can_sit_on_the_body_edge_while_entry_uses_the_full_swing():
    """Wejście na pełnym wychyleniu, ale stop ciasno przy korpusie."""
    trade = played(run(wide_wicks(), breakout_stop_levels="body"))[0]
    assert trade.entry_price == pytest.approx(1.2030)
    assert trade.stop_loss == pytest.approx(1.2000)      # otwarcie, nie dołek knota
    assert trade.risk_distance == pytest.approx(0.0030)  # zamiast 0,0050


def test_stop_can_sit_behind_the_full_swing_while_entry_uses_the_body():
    """Odwrotnie: wejście wcześnie na korpusie, stop dopiero za knotem."""
    bars = [
        bar("08:00", 1.2000, 1.2030, 1.1980, 1.2010),
        quiet("08:15", 1.2010),
        bar("08:30", 1.2011, 1.2018, 1.2009, 1.2016),   # ponad korpusem, poniżej szczytu
        quiet("08:45", 1.2016),
    ]
    trade = played(run(bars, breakout_levels="body", breakout_stop_levels="range"))[0]
    assert trade.entry_price == pytest.approx(1.2010)    # kraniec korpusu
    assert trade.stop_loss == pytest.approx(1.1980)      # dołek wychylenia
    assert trade.risk_distance == pytest.approx(0.0030)


def test_tighter_stop_pulls_the_target_closer():
    """Przy stałym RR ciaśniejszy stop oznacza bliższy take profit."""
    szeroki = played(run(wide_wicks()))[0]
    ciasny = played(run(wide_wicks(), breakout_stop_levels="body"))[0]
    assert ciasny.take_profit < szeroki.take_profit
    assert (ciasny.take_profit - ciasny.entry_price) / ciasny.risk_distance == pytest.approx(4.0)


def test_stop_level_choice_applies_downwards_too():
    bars = [
        bar("08:00", 1.2010, 1.2030, 1.1980, 1.2000),
        quiet("08:15", 1.2000),
        bar("08:30", 1.1979, 1.1981, 1.1970, 1.1975),   # przebija dołek wychylenia
        quiet("08:45", 1.1975),
    ]
    trade = played(run(bars, breakout_stop_levels="body"))[0]
    assert trade.direction == SHORT
    assert trade.stop_loss == pytest.approx(1.2010)      # górny kraniec korpusu
