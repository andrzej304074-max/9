"""Testy silnika backtestu — na ręcznie zbudowanych świecach o znanym wyniku."""

from datetime import datetime, timedelta, timezone

import pytest

from app.config import BacktestConfig, ConfigError
from app.engine import (
    EXIT_OPEN,
    EXIT_REPLACED,
    EXIT_SL,
    EXIT_TIME,
    EXIT_TP,
    LONG,
    SHORT,
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_SKIPPED,
    run_backtest,
)
from app.csv_loader import Bar
from app.stats import build_summary


def bar(when: str, o: float, h: float, l: float, c: float) -> Bar:
    """Świeca o czasie 'YYYY-MM-DD HH:MM' w UTC."""
    ts = datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    return Bar(ts=ts, open=o, high=h, low=l, close=c, volume=0.0)


def flat_bars(day: str, start: str, count: int, price: float, step_minutes: int = 15) -> list[Bar]:
    """Ciąg spokojnych świec o niemal zerowym zakresie — tło, które niczego nie wyzwala."""
    begin = datetime.strptime(f"{day} {start}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    out = []
    for i in range(count):
        ts = begin + timedelta(minutes=step_minutes * i)
        out.append(Bar(ts=ts, open=price, high=price + 0.00001, low=price - 0.00001, close=price))
    return out


def cfg(**overrides) -> BacktestConfig:
    base = {"timezone": "UTC", "initial_capital": 10000.0, "leverage": 1.0}
    base.update(overrides)
    config = BacktestConfig(**base)
    config.validate()
    return config


def run(bars: list[Bar], **overrides):
    outcome, _ = run_backtest(sorted(bars, key=lambda b: b.ts), cfg(**overrides))
    return outcome


# --- kierunek pozycji --------------------------------------------------------------

GREEN = bar("2024-01-02 08:00", 1.2000, 1.2010, 1.1990, 1.2005)   # zamknięcie > otwarcie
RED = bar("2024-01-02 08:00", 1.2005, 1.2010, 1.1990, 1.2000)     # zamknięcie < otwarcie
DOJI = bar("2024-01-02 08:00", 1.2000, 1.2010, 1.1990, 1.2000)


def test_green_candle_goes_long():
    trades = run([GREEN] + flat_bars("2024-01-02", "08:15", 4, 1.2005)).trades
    assert trades[0].direction == LONG


def test_red_candle_goes_short():
    trades = run([RED] + flat_bars("2024-01-02", "08:15", 4, 1.2000)).trades
    assert trades[0].direction == SHORT


def test_take_profit_is_four_times_the_stop_distance():
    trades = run([GREEN] + flat_bars("2024-01-02", "08:15", 4, 1.2005)).trades
    trade = trades[0]
    reward = trade.take_profit - trade.entry_price
    risk = trade.entry_price - trade.stop_loss
    assert reward / risk == pytest.approx(4.0)
    # stop loss siedzi dokładnie na dołku świecy sygnałowej
    assert trade.stop_loss == pytest.approx(GREEN.low)


def test_inverted_mode_flips_direction():
    trades = run([GREEN] + flat_bars("2024-01-02", "08:15", 4, 1.2005), direction_mode="invert").trades
    assert trades[0].direction == SHORT


def test_long_only_skips_short_signals():
    trades = run([RED] + flat_bars("2024-01-02", "08:15", 4, 1.2000), direction_mode="long_only").trades
    assert trades[0].status == STATUS_SKIPPED
    assert "tylko long" in trades[0].skip_reason


def test_short_only_skips_long_signals():
    trades = run([GREEN] + flat_bars("2024-01-02", "08:15", 4, 1.2005), direction_mode="short_only").trades
    assert trades[0].status == STATUS_SKIPPED


@pytest.mark.parametrize(
    "mode,expected",
    [("skip", 0), ("long", LONG), ("short", SHORT)],
)
def test_doji_handling(mode, expected):
    trades = run([DOJI] + flat_bars("2024-01-02", "08:15", 4, 1.2000), doji_mode=mode).trades
    assert trades[0].direction == expected


# --- wyjścia -----------------------------------------------------------------------


def test_take_profit_hit():
    bars = [
        GREEN,
        bar("2024-01-02 08:15", 1.2005, 1.2010, 1.2000, 1.2008),
        bar("2024-01-02 08:30", 1.2008, 1.2070, 1.2005, 1.2065),  # sięga TP 1.2065
    ]
    trade = run(bars).trades[0]
    assert trade.exit_reason == EXIT_TP
    assert trade.exit_price == pytest.approx(trade.take_profit)
    assert trade.pnl_money > 0


def test_stop_loss_hit():
    bars = [
        GREEN,
        bar("2024-01-02 08:15", 1.2005, 1.2008, 1.1985, 1.1988),  # przebija SL 1.1990
    ]
    trade = run(bars).trades[0]
    assert trade.exit_reason == EXIT_SL
    assert trade.exit_price == pytest.approx(trade.stop_loss)
    assert trade.pnl_money < 0


def test_both_levels_in_one_candle_resolve_by_setting():
    bars = [GREEN, bar("2024-01-02 08:15", 1.2005, 1.2070, 1.1985, 1.2050)]
    assert run(bars, tie_break="sl_first").trades[0].exit_reason == EXIT_SL
    assert run(bars, tie_break="tp_first").trades[0].exit_reason == EXIT_TP


def test_position_stays_open_when_data_runs_out():
    trade = run([GREEN] + flat_bars("2024-01-02", "08:15", 6, 1.2005)).trades[0]
    assert trade.status == STATUS_OPEN
    assert trade.exit_reason == EXIT_OPEN
    assert trade.exit_ts is None


def test_position_has_no_time_limit_by_default():
    """Pozycja żyje przez wiele dni, dopóki nie trafi TP albo SL."""
    bars = [GREEN]
    for day in ("2024-01-02", "2024-01-03", "2024-01-04"):
        bars += flat_bars(day, "08:15" if day == "2024-01-02" else "00:00", 40, 1.2005)
    bars.append(bar("2024-01-05 10:00", 1.2005, 1.2070, 1.2004, 1.2065))
    trade = run(bars, weekdays=[0, 1, 2, 3, 4]).trades[0]
    assert trade.exit_reason == EXIT_TP
    assert trade.hold_hours() > 24 * 3


# --- metody stop lossa -------------------------------------------------------------


def test_fixed_pips_stop_loss():
    trade = run(
        [GREEN] + flat_bars("2024-01-02", "08:15", 4, 1.2005),
        sl_method="fixed_pips",
        sl_pips=20,
    ).trades[0]
    assert trade.risk_distance == pytest.approx(0.0020)
    assert trade.stop_loss == pytest.approx(trade.entry_price - 0.0020)
    assert trade.take_profit == pytest.approx(trade.entry_price + 0.0080)


def test_percent_stop_loss():
    trade = run(
        [GREEN] + flat_bars("2024-01-02", "08:15", 4, 1.2005),
        sl_method="percent",
        sl_percent=0.5,
    ).trades[0]
    assert trade.risk_distance == pytest.approx(trade.entry_price * 0.005)


def test_sl_multiplier_widens_the_stop():
    narrow = run([GREEN] + flat_bars("2024-01-02", "08:15", 4, 1.2005)).trades[0]
    wide = run([GREEN] + flat_bars("2024-01-02", "08:15", 4, 1.2005), sl_multiplier=2.0).trades[0]
    assert wide.risk_distance == pytest.approx(narrow.risk_distance * 2)


def test_zero_risk_distance_skips_the_trade():
    """Cena wejścia dokładnie na dołku świecy — dystans stopa wychodzi zerowy."""
    signal = bar("2024-01-02 08:00", 1.2000, 1.2010, 1.2005, 1.2005)
    entry = bar("2024-01-02 08:15", 1.2005, 1.2010, 1.2000, 1.2008)
    trade = run([signal, entry]).trades[0]
    assert trade.status == STATUS_SKIPPED
    assert "Stop Lossa" in trade.skip_reason


def test_custom_rr_ratio():
    trade = run([GREEN] + flat_bars("2024-01-02", "08:15", 4, 1.2005), rr_ratio=2.5).trades[0]
    reward = trade.take_profit - trade.entry_price
    assert reward / (trade.entry_price - trade.stop_loss) == pytest.approx(2.5)


def test_spread_moves_entry_against_the_trader():
    plain = run([GREEN] + flat_bars("2024-01-02", "08:15", 4, 1.2005)).trades[0]
    spread = run([GREEN] + flat_bars("2024-01-02", "08:15", 4, 1.2005), spread_pips=2).trades[0]
    assert spread.entry_price == pytest.approx(plain.entry_price + 0.0002)


# --- wielkość pozycji --------------------------------------------------------------

TP_DAY = [
    GREEN,
    bar("2024-01-02 08:15", 1.2005, 1.2010, 1.2000, 1.2008),
    bar("2024-01-02 08:30", 1.2008, 1.2070, 1.2005, 1.2065),
]


def test_compound_sizing_uses_current_equity():
    trade = run(TP_DAY, sizing_mode="compound", leverage=10).trades[0]
    assert trade.notional == pytest.approx(10000 * 10)


def test_fixed_notional_ignores_equity_changes():
    outcome = run(TP_DAY, sizing_mode="fixed_notional", leverage=10)
    assert outcome.trades[0].notional == pytest.approx(10000 * 10)


def test_risk_percent_sizing_risks_the_declared_amount():
    trade = run(TP_DAY, sizing_mode="risk_percent", risk_percent=1.0, leverage=500).trades[0]
    risk_money = trade.units * trade.risk_distance
    assert risk_money == pytest.approx(100.0, rel=1e-6)  # 1% z 10 000
    assert not trade.size_capped


def test_risk_percent_is_capped_by_leverage():
    trade = run(TP_DAY, sizing_mode="risk_percent", risk_percent=50.0, leverage=2).trades[0]
    assert trade.size_capped
    assert trade.notional == pytest.approx(10000 * 2)


def test_leverage_scales_the_result():
    single = run(TP_DAY, leverage=1).trades[0].pnl_money
    tenfold = run(TP_DAY, leverage=10).trades[0].pnl_money
    assert tenfold == pytest.approx(single * 10)


# --- tryby zarządzania pozycją -----------------------------------------------------


def two_overlapping_days() -> list[Bar]:
    """Dzień 1 otwiera pozycję, która nie zamyka się do sygnału z dnia 2."""
    bars = [bar("2024-01-02 08:00", 1.2000, 1.2010, 1.1990, 1.2005)]      # zielona → long
    bars += flat_bars("2024-01-02", "08:15", 40, 1.2005)
    bars += flat_bars("2024-01-03", "00:00", 32, 1.2005)
    bars += [bar("2024-01-03 08:00", 1.2005, 1.2015, 1.1995, 1.2010)]     # zielona → long
    bars += flat_bars("2024-01-03", "08:15", 40, 1.2010)
    return bars


def test_parallel_mode_allows_two_positions_at_once():
    outcome = run(two_overlapping_days(), position_mode="parallel")
    executed = [t for t in outcome.trades if t.is_executed()]
    assert len(executed) == 2
    assert outcome.max_concurrent == 2


def test_skip_mode_ignores_the_second_signal():
    outcome = run(two_overlapping_days(), position_mode="skip")
    statuses = [t.status for t in outcome.trades]
    assert statuses[0] == STATUS_OPEN
    assert statuses[1] == STATUS_SKIPPED
    assert "jeszcze otwarta" in outcome.trades[1].skip_reason
    assert outcome.max_concurrent == 1


def test_replace_mode_closes_the_old_position_when_the_new_one_opens():
    outcome = run(two_overlapping_days(), position_mode="replace")
    first, second = outcome.trades[0], outcome.trades[1]
    assert first.exit_reason == EXIT_REPLACED
    assert first.exit_ts == second.entry_ts
    assert first.exit_price == pytest.approx(second.entry_price)
    assert outcome.max_concurrent == 1


def test_close_at_time_exits_the_same_day():
    bars = [GREEN] + flat_bars("2024-01-02", "08:15", 40, 1.2005)
    bars.append(bar("2024-01-02 22:00", 1.2050, 1.2051, 1.2049, 1.2050))
    trade = run(bars, position_mode="close_at_time", close_time_hour=22, close_after_days=0).trades[0]
    assert trade.exit_reason == EXIT_TIME
    assert trade.exit_price == pytest.approx(1.2050)
    assert trade.exit_ts.hour == 22


def test_close_at_time_can_wait_extra_days():
    bars = [GREEN] + flat_bars("2024-01-02", "08:15", 40, 1.2005)
    bars.append(bar("2024-01-02 22:00", 1.2050, 1.2051, 1.2049, 1.2050))
    bars += flat_bars("2024-01-03", "00:00", 40, 1.2005)
    bars.append(bar("2024-01-03 22:00", 1.2060, 1.2061, 1.2059, 1.2060))
    trades = run(bars, position_mode="close_at_time", close_time_hour=22, close_after_days=1).trades
    assert trades[0].exit_reason == EXIT_TIME
    assert trades[0].exit_ts.day == 3


def test_take_profit_wins_over_the_time_exit():
    bars = [
        GREEN,
        bar("2024-01-02 08:15", 1.2005, 1.2070, 1.2004, 1.2065),  # TP jeszcze rano
        bar("2024-01-02 22:00", 1.2050, 1.2051, 1.2049, 1.2050),
    ]
    trade = run(bars, position_mode="close_at_time", close_time_hour=22).trades[0]
    assert trade.exit_reason == EXIT_TP


# --- rozliczenie i kapitał ---------------------------------------------------------


def test_compounding_uses_equity_at_the_moment_of_entry():
    outcome = run(two_overlapping_days(), position_mode="replace", sizing_mode="compound", leverage=10)
    first, second = outcome.trades[0], outcome.trades[1]
    # druga pozycja wchodzi po zamknięciu pierwszej, więc widzi już zaktualizowany kapitał
    assert second.equity_before == pytest.approx(first.equity_after)
    assert second.notional == pytest.approx(second.equity_before * 10)


def test_cumulative_return_tracks_final_equity():
    outcome = run(TP_DAY, leverage=10)
    summary = build_summary(outcome, cfg(leverage=10))
    assert summary["return_pct"] == pytest.approx(outcome.trades[0].cumulative_return_pct)
    assert summary["final_equity"] == pytest.approx(10000 + summary["net_profit"])


def test_stop_hit_inside_the_entry_candle_is_settled_correctly():
    """Wejście i wyjście w tej samej świecy — rozliczenie nie może się pogubić w kolejności."""
    bars = [GREEN, bar("2024-01-02 08:15", 1.2005, 1.2006, 1.1980, 1.1985)]
    outcome = run(bars, leverage=10)
    trade = outcome.trades[0]
    assert trade.exit_reason == EXIT_SL
    assert trade.entry_ts == trade.exit_ts
    assert trade.equity_before == pytest.approx(10000)
    assert outcome.final_equity == pytest.approx(10000 + trade.pnl_money)


def test_wiping_out_the_account_stops_the_backtest():
    bars = [GREEN, bar("2024-01-02 08:15", 1.2005, 1.2006, 1.1985, 1.1988)]
    bars += flat_bars("2024-01-03", "00:00", 32, 1.1990)
    bars += [bar("2024-01-03 08:00", 1.1990, 1.2000, 1.1980, 1.1995)]
    bars += flat_bars("2024-01-03", "08:15", 10, 1.1995)
    outcome = run(bars, leverage=1000)  # strata ~0,125% × 1000 przekracza kapitał
    assert outcome.liquidated_at is not None
    assert outcome.final_equity == 0.0
    assert any(t.status == "liquidated" for t in outcome.trades)


# --- strefy czasowe i zakres -------------------------------------------------------


def test_timezone_selects_a_different_signal_candle():
    """Latem 8:00 w Warszawie to 06:00 UTC, a w Londynie 07:00 UTC."""
    bars = [
        bar("2024-07-01 06:00", 1.2000, 1.2010, 1.1990, 1.2005),  # zielona
        bar("2024-07-01 06:15", 1.2005, 1.2006, 1.2004, 1.2005),
        bar("2024-07-01 07:00", 1.2005, 1.2010, 1.1990, 1.2000),  # czerwona
        bar("2024-07-01 07:15", 1.2000, 1.2001, 1.1999, 1.2000),
    ] + flat_bars("2024-07-01", "07:30", 8, 1.2000)

    warsaw = run(bars, timezone="Europe/Warsaw").trades[0]
    london = run(bars, timezone="Europe/London").trades[0]
    assert warsaw.direction == LONG
    assert london.direction == SHORT


def test_lookback_days_limits_the_range():
    bars = []
    for day in range(2, 6):
        stamp = f"2024-01-{day:02d}"
        bars.append(bar(f"{stamp} 08:00", 1.2000, 1.2010, 1.1990, 1.2005))
        bars += flat_bars(stamp, "08:15", 8, 1.2005)
    assert len(run(bars).trades) == 4
    assert len(run(bars, lookback_days=2).trades) == 2


def test_weekday_filter_excludes_days():
    bars = []
    for day in range(2, 6):  # wtorek–piątek
        stamp = f"2024-01-{day:02d}"
        bars.append(bar(f"{stamp} 08:00", 1.2000, 1.2010, 1.1990, 1.2005))
        bars += flat_bars(stamp, "08:15", 8, 1.2005)
    trades = run(bars, weekdays=[0, 1]).trades  # tylko poniedziałek i wtorek
    assert len(trades) == 1
    assert trades[0].signal_date.weekday() == 1


def test_missing_signal_window_is_reported():
    bars = flat_bars("2024-01-02", "10:00", 8, 1.2000)  # brak świec o 8:00
    trades = run(bars).trades
    assert trades[0].status == STATUS_SKIPPED
    assert "oknie sygnałowym" in trades[0].skip_reason


def test_entry_candle_missing_on_the_same_day_is_skipped():
    """Świeca sygnałowa jest ostatnią danego dnia — nie ma po czym wejść."""
    bars = [GREEN] + flat_bars("2024-01-03", "08:00", 4, 1.2005)
    trade = run(bars).trades[0]
    assert trade.status == STATUS_SKIPPED
    assert "wejściowej" in trade.skip_reason


def test_signal_close_entry_mode():
    bars = [GREEN] + flat_bars("2024-01-02", "08:15", 6, 1.2005)
    trade = run(bars, entry_mode="signal_close").trades[0]
    assert trade.entry_price == pytest.approx(GREEN.close)


# --- skąd czytany jest kierunek świecy ----------------------------------------------


def signal_candle(o, h, l, c, day="2024-01-02"):
    """Jedna świeca sygnałowa 8:00–8:15 plus spokojne świece na resztę dnia."""
    def bar(hhmm, bo, bh, bl, bc):
        ts = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        return Bar(ts=ts, open=bo, high=bh, low=bl, close=bc, volume=0.0)

    bars = [bar("08:00", o, h, l, c)]
    price = c
    for i in range(1, 40):
        hh, mm = 8 + (i * 15) // 60, (i * 15) % 60
        bars.append(bar(f"{hh:02d}:{mm:02d}", price, price + 0.0002, price - 0.0002, price))
    return bars


def run_with(bars, **overrides):
    base = {"timezone": "UTC", "initial_capital": 10000.0, "leverage": 1.0}
    base.update(overrides)
    cfg = BacktestConfig(**base)
    cfg.validate()
    outcome, _ = run_backtest(bars, cfg)
    return outcome


def test_body_is_the_default_source():
    assert BacktestConfig().direction_source == "body"


def test_body_reads_open_versus_close():
    """Zielony korpus daje longa niezależnie od tego, gdzie sięgały knoty."""
    bars = signal_candle(1.2000, 1.2050, 1.1990, 1.2010)      # zamknięcie powyżej otwarcia
    trade = [t for t in run_with(bars).trades if t.is_executed()][0]
    assert trade.direction == LONG


def test_range_reads_close_against_the_midpoint():
    bars = signal_candle(1.2000, 1.2050, 1.1990, 1.2010)      # środek zakresu to 1.2020
    trade = [t for t in run_with(bars, direction_source="range").trades if t.is_executed()][0]
    assert trade.direction == SHORT      # zamknięcie poniżej środka mimo zielonego korpusu


def test_the_two_sources_can_disagree():
    """Sedno różnicy: świeca z długim górnym knotem jest zielona, ale cena została
    odrzucona od góry i zamknęła się w dolnej połowie zakresu."""
    bars = signal_candle(1.2000, 1.2080, 1.1995, 1.2005)

    z_korpusu = [t for t in run_with(bars).trades if t.is_executed()][0]
    z_zakresu = [t for t in run_with(bars, direction_source="range").trades if t.is_executed()][0]

    assert z_korpusu.direction == LONG
    assert z_zakresu.direction == SHORT


def test_the_two_sources_agree_on_a_clean_candle():
    """Świeca bez wyraźnych knotów daje ten sam kierunek w obu trybach."""
    bars = signal_candle(1.2000, 1.2042, 1.1998, 1.2040)
    assert [t for t in run_with(bars).trades if t.is_executed()][0].direction == LONG
    assert [t for t in run_with(bars, direction_source="range").trades
            if t.is_executed()][0].direction == LONG


def test_close_exactly_at_the_midpoint_counts_as_undecided():
    bars = signal_candle(1.2000, 1.2040, 1.2000, 1.2020)       # środek zakresu = zamknięcie
    outcome = run_with(bars, direction_source="range")
    assert [t for t in outcome.trades if t.is_executed()] == []
    assert "w środku zakresu" in outcome.trades[0].skip_reason


def test_undecided_candle_respects_the_doji_setting():
    bars = signal_candle(1.2000, 1.2040, 1.2000, 1.2020)
    trade = [t for t in run_with(bars, direction_source="range", doji_mode="long").trades
             if t.is_executed()][0]
    assert trade.direction == LONG


def test_range_source_still_obeys_the_direction_mode():
    """Tryb kierunku nakłada się na oba źródła tak samo."""
    bars = signal_candle(1.2000, 1.2080, 1.1995, 1.2005)      # z zakresu wychodzi short
    trade = [t for t in run_with(bars, direction_source="range", direction_mode="invert").trades
             if t.is_executed()][0]
    assert trade.direction == LONG


def test_an_unknown_source_is_rejected():
    cfg = BacktestConfig(direction_source="cos_innego")
    with pytest.raises(ConfigError, match="Źródło kierunku"):
        cfg.validate()
