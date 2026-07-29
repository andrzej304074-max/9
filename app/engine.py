"""Silnik backtestu strategii świecy sesyjnej (domyślnie 8:00–8:15) z RR 4:1.

Przebieg jest dwufazowy:

* **Faza 1** (`_build_trades`) idzie dzień po dniu, buduje świecę sygnałową, wyznacza
  kierunek, poziomy SL/TP i symuluje wyjście po kolejnych świecach. Wszystko tutaj jest
  czysto cenowo-czasowe — wielkość pozycji nie ma na to żadnego wpływu.
* **Faza 2** (`_settle`) przechodzi powstałe wejścia i wyjścia **chronologicznie** i dopiero
  wtedy nadaje pozycjom wielkość oraz księguje wynik. Dzięki temu kapitalizacja przy
  nakładających się pozycjach liczona jest poprawnie, a nie „z przyszłości”.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .config import BacktestConfig, WEEKDAY_NAMES_PL
from .csv_loader import Bar, DataError, resolve_timezone

LONG, SHORT = 1, -1

# statusy transakcji
STATUS_CLOSED = "closed"
STATUS_OPEN = "open"
STATUS_SKIPPED = "skipped"
STATUS_LIQUIDATED = "liquidated"

# powody wyjścia
EXIT_TP = "TP"
EXIT_SL = "SL"
EXIT_TIME = "CZAS"
EXIT_REPLACED = "ZASTĄPIONA"
EXIT_OPEN = "OTWARTA"


@dataclass
class Trade:
    signal_date: date
    weekday: int

    # świeca sygnałowa
    signal_open: float = 0.0
    signal_high: float = 0.0
    signal_low: float = 0.0
    signal_close: float = 0.0

    direction: int = 0
    status: str = STATUS_SKIPPED
    skip_reason: Optional[str] = None

    # tylko dla strategii wybicia: która granica zakresu puściła i która to próba w tym dniu
    breakout_side: Optional[str] = None  # 'up' | 'down'
    attempt: int = 1

    entry_ts: Optional[datetime] = None
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    risk_distance: float = 0.0

    exit_ts: Optional[datetime] = None
    exit_price: float = 0.0
    exit_reason: Optional[str] = None
    bars_held: int = 0

    # uzupełniane w fazie 2
    notional: float = 0.0
    units: float = 0.0
    equity_before: float = 0.0
    equity_after: float = 0.0
    pnl_money: float = 0.0
    pnl_pct: float = 0.0
    cumulative_return_pct: float = 0.0
    size_capped: bool = False

    def is_executed(self) -> bool:
        return self.status in (STATUS_CLOSED, STATUS_OPEN)

    def is_open_at(self, ts: datetime) -> bool:
        if not self.is_executed():
            return False
        return self.exit_ts is None or self.exit_ts > ts

    def hold_hours(self) -> Optional[float]:
        if self.entry_ts is None or self.exit_ts is None:
            return None
        return (self.exit_ts - self.entry_ts).total_seconds() / 3600.0

    def to_dict(self, tz: ZoneInfo) -> dict[str, Any]:
        def _local(ts: Optional[datetime]) -> Optional[str]:
            return ts.astimezone(tz).strftime("%Y-%m-%d %H:%M") if ts else None

        return {
            "date": self.signal_date.isoformat(),
            "weekday": self.weekday,
            "weekday_name": WEEKDAY_NAMES_PL[self.weekday],
            "signal_open": self.signal_open,
            "signal_high": self.signal_high,
            "signal_low": self.signal_low,
            "signal_close": self.signal_close,
            "signal_bullish": self.signal_close > self.signal_open,
            "direction": self.direction,
            "direction_label": "LONG" if self.direction == LONG else ("SHORT" if self.direction == SHORT else "—"),
            "status": self.status,
            "skip_reason": self.skip_reason,
            "breakout_side": self.breakout_side,
            "breakout_label": {"up": "górą", "down": "dołem"}.get(self.breakout_side or "", "—"),
            "attempt": self.attempt,
            "entry_time": _local(self.entry_ts),
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "risk_distance": self.risk_distance,
            "exit_time": _local(self.exit_ts),
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "bars_held": self.bars_held,
            "hold_hours": self.hold_hours(),
            "notional": self.notional,
            "units": self.units,
            "equity_before": self.equity_before,
            "equity_after": self.equity_after,
            "pnl_money": self.pnl_money,
            "pnl_pct": self.pnl_pct,
            "cumulative_return_pct": self.cumulative_return_pct,
            "size_capped": self.size_capped,
        }


@dataclass
class EquityPoint:
    ts: datetime
    equity: float
    cumulative_return_pct: float


@dataclass
class BacktestOutcome:
    trades: list[Trade]
    equity_curve: list[EquityPoint]
    final_equity: float
    unrealised_pnl: float
    max_concurrent: int
    peak_leverage: float
    liquidated_at: Optional[datetime] = None
    notes: list[str] = field(default_factory=list)


# --- faza 1: wyznaczanie transakcji ------------------------------------------------


def _aggregate(window: list[Bar]) -> tuple[float, float, float, float]:
    return (
        window[0].open,
        max(b.high for b in window),
        min(b.low for b in window),
        window[-1].close,
    )


def _apply_direction_mode(cfg: BacktestConfig, base: int) -> tuple[int, Optional[str]]:
    """Nakłada tryb kierunku na surowy sygnał. Wspólne dla obu strategii."""
    if cfg.direction_mode == "follow":
        return base, None
    if cfg.direction_mode == "invert":
        return -base, None
    if cfg.direction_mode == "long_only":
        return (LONG, None) if base == LONG else (0, "sygnał krótki pominięty (tryb: tylko long)")
    return (SHORT, None) if base == SHORT else (0, "sygnał długi pominięty (tryb: tylko short)")


def _resolve_direction(
    cfg: BacktestConfig, candle_open: float, candle_high: float,
    candle_low: float, candle_close: float,
) -> tuple[int, Optional[str]]:
    """Ustala kierunek sygnału ze świecy — z korpusu albo z całego zakresu.

    **Korpus** patrzy wyłącznie na otwarcie i zamknięcie, czyli na kolorową część świecy;
    knoty są pomijane. To klasyczna definicja „zielona / czerwona”.

    **Cały zakres** porównuje zamknięcie ze środkiem między szczytem a dołkiem. Świeca
    z długim górnym knotem i zamknięciem przy dole bywa formalnie zielona, choć cena
    została odrzucona od góry — ten tryb potraktuje ją jako spadkową.
    """
    if cfg.direction_source == "range":
        midpoint = (candle_high + candle_low) / 2.0
        higher, lower, tie = candle_close > midpoint, candle_close < midpoint, "w środku zakresu"
    else:
        higher, lower, tie = candle_close > candle_open, candle_close < candle_open, "bez zmiany (doji)"

    if higher:
        base = LONG
    elif lower:
        base = SHORT
    else:
        if cfg.doji_mode == "skip":
            return 0, f"świeca sygnałowa {tie}"
        base = LONG if cfg.doji_mode == "long" else SHORT

    return _apply_direction_mode(cfg, base)


def _risk_distance(cfg: BacktestConfig, direction: int, entry: float, low: float, high: float) -> float:
    if cfg.sl_method == "candle_range":
        raw = (entry - low) if direction == LONG else (high - entry)
    elif cfg.sl_method == "fixed_pips":
        raw = cfg.sl_pips * cfg.pip_size
    else:  # percent
        raw = entry * cfg.sl_percent / 100.0
    return raw * cfg.sl_multiplier


def _candidate_days(bars: list[Bar], cfg: BacktestConfig, tz: ZoneInfo) -> list[date]:
    days = sorted({bar.ts.astimezone(tz).date() for bar in bars})
    if not days:
        return []

    if cfg.lookback_days > 0:
        cutoff = days[-1] - timedelta(days=cfg.lookback_days - 1)
        days = [d for d in days if d >= cutoff]
    if cfg.date_from:
        start = date.fromisoformat(cfg.date_from)
        days = [d for d in days if d >= start]
    if cfg.date_to:
        end = date.fromisoformat(cfg.date_to)
        days = [d for d in days if d <= end]

    allowed = set(cfg.weekdays)
    return [d for d in days if d.weekday() in allowed]


def _time_exit_target(cfg: BacktestConfig, entry_ts: datetime, tz: ZoneInfo) -> Optional[datetime]:
    """Moment opcjonalnego zamknięcia pozycji o określonej godzinie."""
    if not cfg.closes_at_time:
        return None
    close_time = time(cfg.close_time_hour, cfg.close_time_minute)
    entry_local = entry_ts.astimezone(tz)
    target_local = datetime.combine(
        entry_local.date() + timedelta(days=cfg.close_after_days), close_time, tzinfo=tz
    )
    if target_local <= entry_local:
        target_local = datetime.combine(target_local.date() + timedelta(days=1), close_time, tzinfo=tz)
    return target_local.astimezone(timezone.utc)


def _build_trades(bars: list[Bar], cfg: BacktestConfig, tz: ZoneInfo) -> list[Trade]:
    """Dyspozytor strategii. Wszystko poniżej — wyjścia, nakładanie się pozycji,
    rozliczenie — jest od strategii niezależne i wspólne dla obu."""
    builder = _build_trades_breakout if cfg.is_breakout else _build_trades_direction
    trades = builder(bars, cfg, tz)

    if cfg.position_mode in ("skip", "replace"):
        _apply_overlap_rules(trades, cfg)
    return trades


def _build_trades_direction(bars: list[Bar], cfg: BacktestConfig, tz: ZoneInfo) -> list[Trade]:
    """Strategia 1: kierunek świecy sesyjnej decyduje o stronie pozycji."""
    ts_index = [bar.ts for bar in bars]
    n = len(bars)
    signal_time = time(cfg.signal_hour, cfg.signal_minute)
    trades: list[Trade] = []

    for day in _candidate_days(bars, cfg, tz):
        trade = Trade(signal_date=day, weekday=day.weekday())
        trades.append(trade)

        start_local = datetime.combine(day, signal_time, tzinfo=tz)
        end_local = start_local + timedelta(minutes=cfg.candle_minutes)
        i0 = bisect_left(ts_index, start_local.astimezone(timezone.utc))
        i1 = bisect_left(ts_index, end_local.astimezone(timezone.utc))

        window = bars[i0:i1]
        if not window:
            trade.skip_reason = "brak świec w oknie sygnałowym"
            continue

        s_open, s_high, s_low, s_close = _aggregate(window)
        trade.signal_open, trade.signal_high = s_open, s_high
        trade.signal_low, trade.signal_close = s_low, s_close

        direction, reason = _resolve_direction(cfg, s_open, s_high, s_low, s_close)
        if direction == 0:
            trade.skip_reason = reason
            continue

        # --- wejście ---
        if cfg.entry_mode == "next_open":
            if i1 >= n:
                trade.skip_reason = "brak świecy wejściowej (koniec danych)"
                continue
            entry_bar = bars[i1]
            if entry_bar.ts.astimezone(tz).date() != day:
                trade.skip_reason = "brak świecy wejściowej tego dnia (luka w danych)"
                continue
            entry_raw = entry_bar.open
            entry_ts = entry_bar.ts
        else:  # signal_close
            entry_raw = s_close
            entry_ts = end_local.astimezone(timezone.utc)
        sim_start = i1

        entry_price = entry_raw + direction * cfg.spread_price
        risk = _risk_distance(cfg, direction, entry_price, s_low, s_high)
        if risk <= 0:
            trade.skip_reason = "dystans Stop Lossa wyszedł zerowy lub ujemny"
            continue

        trade.direction = direction
        trade.status = STATUS_OPEN
        trade.entry_ts = entry_ts
        trade.entry_price = entry_price
        trade.risk_distance = risk
        trade.stop_loss = entry_price - direction * risk
        trade.take_profit = entry_price + direction * risk * cfg.rr_ratio

        _simulate_exit(trade, bars, sim_start, cfg, _time_exit_target(cfg, entry_ts, tz))

    return trades


# --- strategia 2: wybicie zakresu świecy sesyjnej ----------------------------------


def _breakout_window_end(cfg: BacktestConfig, day: date, range_end_local: datetime, tz: ZoneInfo) -> datetime:
    """Do kiedy wypatrujemy przebicia zakresu."""
    if cfg.breakout_window_mode == "until_time":
        end_local = datetime.combine(
            day, time(cfg.breakout_until_hour, cfg.breakout_until_minute), tzinfo=tz
        )
    elif cfg.breakout_window_mode == "hours_after":
        end_local = range_end_local + timedelta(hours=cfg.breakout_hours)
    else:  # end_of_day
        end_local = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=tz)
    return end_local.astimezone(timezone.utc)


def _detect_breakout(
    bar: Bar, upper: float, lower: float, cfg: BacktestConfig
) -> tuple[Optional[str], float]:
    """Sprawdza, czy świeca przebiła zakres. Zwraca (strona, cena wejścia)."""
    if cfg.breakout_trigger == "close_beyond":
        hit_up, hit_down = bar.close > upper, bar.close < lower
        entry_up = entry_down = bar.close
    else:  # touch — zlecenie stop wykonuje się na poziomie, a przy luce po cenie otwarcia
        hit_up, hit_down = bar.high >= upper, bar.low <= lower
        entry_up = max(upper, bar.open)
        entry_down = min(lower, bar.open)

    if hit_up and hit_down:
        # z samego OHLC nie wynika, który poziom padł pierwszy
        rule = cfg.breakout_both_sides
        if rule == "skip":
            return None, 0.0
        if rule == "high_first":
            return "up", entry_up
        if rule == "low_first":
            return "down", entry_down
        # open_proximity: cena rusza od otwarcia, więc bliższy poziom pada wcześniej
        return ("up", entry_up) if abs(upper - bar.open) <= abs(bar.open - lower) else ("down", entry_down)

    if hit_up:
        return "up", entry_up
    if hit_down:
        return "down", entry_down
    return None, 0.0


def _breakout_risk_distance(
    cfg: BacktestConfig, side: str, entry: float, r_low: float, r_high: float
) -> float:
    """Dystans ryzyka dla wybicia — mierzony do przeciwnej granicy zakresu.

    Kotwicą jest **strona wybicia**, a nie kierunek pozycji. Ma to znaczenie w trybie
    odwróconym: gdy gramy przeciw wybiciu górą, wejście leży dokładnie na szczycie zakresu,
    więc liczenie od kierunku pozycji dałoby dystans zerowy. Przy grze zgodnej z wybiciem
    stop ląduje dokładnie na przeciwnej granicy świecy, a przy grze przeciwnej — tyle samo
    po drugiej stronie wejścia.
    """
    if cfg.sl_method == "candle_range":
        raw = (entry - r_low) if side == "up" else (r_high - entry)
    elif cfg.sl_method == "fixed_pips":
        raw = cfg.sl_pips * cfg.pip_size
    else:  # percent
        raw = entry * cfg.sl_percent / 100.0
    return raw * cfg.sl_multiplier


def _build_trades_breakout(bars: list[Bar], cfg: BacktestConfig, tz: ZoneInfo) -> list[Trade]:
    """Strategia 2: notujemy zakres świecy sesyjnej i czekamy, aż cena go przebije.

    Wejście w stronę wybicia, stop loss po przeciwnej stronie zakresu, take profit
    `rr_ratio` razy dalej. Kolejna próba w danym dniu startuje dopiero po zamknięciu
    poprzedniej pozycji.
    """
    ts_index = [bar.ts for bar in bars]
    n = len(bars)
    signal_time = time(cfg.signal_hour, cfg.signal_minute)
    buffer_price = cfg.breakout_buffer_price
    trades: list[Trade] = []

    for day in _candidate_days(bars, cfg, tz):
        start_local = datetime.combine(day, signal_time, tzinfo=tz)
        range_end_local = start_local + timedelta(minutes=cfg.candle_minutes)
        i0 = bisect_left(ts_index, start_local.astimezone(timezone.utc))
        i1 = bisect_left(ts_index, range_end_local.astimezone(timezone.utc))

        window = bars[i0:i1]
        if not window:
            trades.append(
                Trade(
                    signal_date=day,
                    weekday=day.weekday(),
                    skip_reason="brak świec w oknie sygnałowym",
                )
            )
            continue

        r_open, r_high, r_low, r_close = _aggregate(window)
        upper = r_high + buffer_price
        lower = r_low - buffer_price
        window_end = _breakout_window_end(cfg, day, range_end_local, tz)

        limit = {"single": 1, "opposite": 2}.get(
            cfg.breakout_retry_mode, cfg.breakout_max_per_day
        )
        traded = 0
        emitted = False
        # strona raz odrzucona (przez tryb kierunku albo zerowy dystans SL) jest odrzucona
        # na cały dzień — inaczej ten sam powód powtarzałby się na każdej kolejnej świecy
        blocked: set[str] = set()
        idx = i1

        while idx < n and bars[idx].ts < window_end and traded < limit:
            side, entry_raw = _detect_breakout(bars[idx], upper, lower, cfg)
            if side is None or side in blocked:
                idx += 1
                continue

            trade = Trade(
                signal_date=day,
                weekday=day.weekday(),
                signal_open=r_open,
                signal_high=r_high,
                signal_low=r_low,
                signal_close=r_close,
                breakout_side=side,
                attempt=traded + 1,
            )
            trades.append(trade)
            emitted = True

            direction, reason = _apply_direction_mode(cfg, LONG if side == "up" else SHORT)
            if direction == 0:
                trade.skip_reason = reason
                blocked.add(side)
                idx += 1
                continue

            entry_price = entry_raw + direction * cfg.spread_price
            risk = _breakout_risk_distance(cfg, side, entry_price, r_low, r_high)
            if risk <= 0:
                trade.skip_reason = "dystans Stop Lossa wyszedł zerowy lub ujemny"
                blocked.add(side)
                idx += 1
                continue

            trade.direction = direction
            trade.status = STATUS_OPEN
            trade.entry_ts = bars[idx].ts
            trade.entry_price = entry_price
            trade.risk_distance = risk
            trade.stop_loss = entry_price - direction * risk
            trade.take_profit = entry_price + direction * risk * cfg.rr_ratio

            _simulate_exit(trade, bars, idx, cfg, _time_exit_target(cfg, bars[idx].ts, tz))

            traded += 1
            if cfg.breakout_retry_mode == "opposite":
                blocked.add(side)  # druga próba tylko na przeciwnej granicy
            if trade.exit_ts is None:
                break  # pozycja dożyła końca danych
            idx = bisect_right(ts_index, trade.exit_ts)  # kolejna próba po jej zamknięciu

        if not emitted:
            trades.append(
                Trade(
                    signal_date=day,
                    weekday=day.weekday(),
                    signal_open=r_open,
                    signal_high=r_high,
                    signal_low=r_low,
                    signal_close=r_close,
                    skip_reason="zakres nie został przebity w oknie czasowym",
                )
            )

    return trades


def _simulate_exit(
    trade: Trade,
    bars: list[Bar],
    start_idx: int,
    cfg: BacktestConfig,
    target_utc: Optional[datetime],
) -> None:
    """Idzie po świecach od wejścia aż do trafienia SL/TP lub zamknięcia czasowego."""
    direction = trade.direction
    sl, tp = trade.stop_loss, trade.take_profit

    for idx in range(start_idx, len(bars)):
        bar = bars[idx]

        if target_utc is not None and bar.ts >= target_utc:
            trade.exit_ts = bar.ts
            trade.exit_price = bar.open
            trade.exit_reason = EXIT_TIME
            trade.status = STATUS_CLOSED
            trade.bars_held = idx - start_idx
            return

        if direction == LONG:
            hit_sl = bar.low <= sl
            hit_tp = bar.high >= tp
        else:
            hit_sl = bar.high >= sl
            hit_tp = bar.low <= tp

        trade.bars_held = idx - start_idx + 1

        if hit_sl and hit_tp:
            take_sl = cfg.tie_break == "sl_first"
            trade.exit_price = sl if take_sl else tp
            trade.exit_reason = EXIT_SL if take_sl else EXIT_TP
        elif hit_sl:
            trade.exit_price = sl
            trade.exit_reason = EXIT_SL
        elif hit_tp:
            trade.exit_price = tp
            trade.exit_reason = EXIT_TP
        else:
            continue

        trade.exit_ts = bar.ts
        trade.status = STATUS_CLOSED
        return

    # dane się skończyły — pozycja zostaje otwarta, wyceniona po ostatnim zamknięciu
    trade.status = STATUS_OPEN
    trade.exit_reason = EXIT_OPEN
    if bars:
        trade.exit_price = bars[-1].close


def _apply_overlap_rules(trades: list[Trade], cfg: BacktestConfig) -> None:
    """Tryby 'pomiń nowy sygnał' i 'zamknij starą przy otwarciu nowej'.

    Transakcje są już w pełni zasymulowane, więc wystarczy nadpisać te, które kolidują.
    """
    active: list[Trade] = []
    for trade in trades:
        if not trade.is_executed() or trade.entry_ts is None:
            continue
        entry_ts = trade.entry_ts
        overlapping = [t for t in active if t.is_open_at(entry_ts)]

        if not overlapping:
            active.append(trade)
            continue

        if cfg.position_mode == "skip":
            trade.status = STATUS_SKIPPED
            trade.skip_reason = "pozycja z wcześniejszego dnia była jeszcze otwarta"
            trade.direction = 0
            trade.entry_ts = None
            trade.exit_ts = None
            trade.exit_reason = None
            trade.stop_loss = trade.take_profit = trade.entry_price = 0.0
            trade.risk_distance = 0.0
            trade.bars_held = 0
        else:  # replace
            for old in overlapping:
                old.exit_ts = entry_ts
                # stara pozycja wychodzi po tej samej cenie, po której wchodzi nowa
                old.exit_price = trade.entry_price - trade.direction * cfg.spread_price
                old.exit_reason = EXIT_REPLACED
                old.status = STATUS_CLOSED
            active.append(trade)


# --- faza 2: rozliczenie chronologiczne --------------------------------------------


def _position_notional(
    cfg: BacktestConfig, equity: float, entry_price: float, risk_distance: float
) -> tuple[float, bool]:
    if cfg.sizing_mode == "compound":
        notional = equity * cfg.leverage
    elif cfg.sizing_mode == "fixed_notional":
        notional = cfg.initial_capital * cfg.leverage
    else:  # risk_percent
        risk_fraction = risk_distance / entry_price
        notional = (equity * cfg.risk_percent / 100.0) / risk_fraction if risk_fraction > 0 else 0.0

    cap = equity * cfg.leverage
    if notional > cap:
        return max(cap, 0.0), True
    return max(notional, 0.0), False


def _settle(trades: list[Trade], bars: list[Bar], cfg: BacktestConfig) -> BacktestOutcome:
    executed = [t for t in trades if t.is_executed() and t.entry_ts is not None]
    last_ts = bars[-1].ts if bars else datetime.now(timezone.utc)

    events: list[tuple[datetime, int, int]] = []
    for idx, trade in enumerate(executed):
        events.append((trade.entry_ts, 1, idx))  # type: ignore[arg-type]
        events.append((trade.exit_ts or last_ts, 0, idx))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    equity = cfg.initial_capital
    open_notional = 0.0
    open_count = 0
    max_concurrent = 0
    peak_leverage = 0.0
    unrealised = 0.0
    liquidated_at: Optional[datetime] = None
    notes: list[str] = []

    opened: set[int] = set()
    settled: set[int] = set()
    curve: list[EquityPoint] = []
    if executed:
        curve.append(EquityPoint(ts=executed[0].entry_ts, equity=equity, cumulative_return_pct=0.0))  # type: ignore[arg-type]

    def _open(idx: int) -> None:
        nonlocal open_notional, open_count, max_concurrent, peak_leverage
        trade = executed[idx]
        trade.equity_before = equity
        notional, capped = _position_notional(cfg, equity, trade.entry_price, trade.risk_distance)
        trade.notional = notional
        trade.units = notional / trade.entry_price if trade.entry_price > 0 else 0.0
        trade.size_capped = capped
        open_notional += notional
        open_count += 1
        max_concurrent = max(max_concurrent, open_count)
        if equity > 0:
            peak_leverage = max(peak_leverage, open_notional / equity)
        opened.add(idx)

    def _close(idx: int) -> None:
        nonlocal equity, open_notional, open_count, unrealised, liquidated_at
        trade = executed[idx]
        pnl = trade.units * (trade.exit_price - trade.entry_price) * trade.direction
        open_notional -= trade.notional
        open_count -= 1
        settled.add(idx)

        if trade.status == STATUS_OPEN:
            # pozycja wciąż otwarta na koniec danych — wycena rynkowa, bez wpływu na kapitał
            trade.pnl_money = pnl
            trade.pnl_pct = (pnl / trade.equity_before * 100.0) if trade.equity_before > 0 else 0.0
            trade.equity_after = equity
            trade.cumulative_return_pct = (equity - cfg.initial_capital) / cfg.initial_capital * 100.0
            unrealised += pnl
            return

        equity += pnl
        trade.pnl_money = pnl
        trade.pnl_pct = (pnl / trade.equity_before * 100.0) if trade.equity_before > 0 else 0.0
        trade.equity_after = equity
        trade.cumulative_return_pct = (equity - cfg.initial_capital) / cfg.initial_capital * 100.0
        curve.append(
            EquityPoint(
                ts=trade.exit_ts or last_ts,
                equity=equity,
                cumulative_return_pct=trade.cumulative_return_pct,
            )
        )

        if equity <= 0 and liquidated_at is None:
            liquidated_at = trade.exit_ts or last_ts

    for ts, kind, idx in events:
        if liquidated_at is not None:
            break
        if kind == 0:
            if idx in settled:
                continue
            if idx not in opened:
                # TP/SL trafiony w tej samej świecy, w której nastąpiło wejście
                _open(idx)
            _close(idx)
        else:
            if idx not in opened:
                _open(idx)

    if liquidated_at is not None:
        equity = max(equity, 0.0)
        for idx, trade in enumerate(executed):
            if idx not in settled:
                trade.status = STATUS_LIQUIDATED
                trade.skip_reason = "kapitał wyzerowany wcześniejszą stratą"
                trade.pnl_money = 0.0
                trade.pnl_pct = 0.0
        notes.append(
            "Kapitał został wyzerowany — backtest zatrzymano, pozostałe sygnały nie zostały zagrane."
        )

    return BacktestOutcome(
        trades=trades,
        equity_curve=curve,
        final_equity=equity,
        unrealised_pnl=unrealised,
        max_concurrent=max_concurrent,
        peak_leverage=peak_leverage,
        liquidated_at=liquidated_at,
        notes=notes,
    )


def run_backtest(bars: list[Bar], cfg: BacktestConfig) -> tuple[BacktestOutcome, ZoneInfo]:
    if not bars:
        raise DataError("Brak danych do przetestowania.")
    tz = resolve_timezone(cfg.timezone)
    trades = _build_trades(bars, cfg, tz)
    outcome = _settle(trades, bars, cfg)
    return outcome, tz
