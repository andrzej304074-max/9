"""Metryki podsumowujące backtest oraz rozbicie wyników na dni tygodnia."""

from __future__ import annotations

from typing import Any, Optional
from zoneinfo import ZoneInfo

from .config import BacktestConfig, WEEKDAY_NAMES_PL
from .engine import (
    EXIT_REPLACED,
    EXIT_SL,
    EXIT_TIME,
    EXIT_TP,
    STATUS_CLOSED,
    STATUS_LIQUIDATED,
    STATUS_OPEN,
    STATUS_SKIPPED,
    BacktestOutcome,
    Trade,
)


def _max_drawdown(equities: list[float]) -> tuple[float, float]:
    """Zwraca (maksymalne obsunięcie w %, maksymalne obsunięcie w pieniądzu)."""
    peak = float("-inf")
    max_pct = 0.0
    max_money = 0.0
    for value in equities:
        peak = max(peak, value)
        if peak > 0:
            drop = peak - value
            max_money = max(max_money, drop)
            max_pct = max(max_pct, drop / peak * 100.0)
    return max_pct, max_money


def _safe_div(a: float, b: float) -> Optional[float]:
    return a / b if b else None


def build_summary(outcome: BacktestOutcome, cfg: BacktestConfig) -> dict[str, Any]:
    trades = outcome.trades
    closed = [t for t in trades if t.status == STATUS_CLOSED]
    still_open = [t for t in trades if t.status == STATUS_OPEN]
    skipped = [t for t in trades if t.status == STATUS_SKIPPED]
    liquidated = [t for t in trades if t.status == STATUS_LIQUIDATED]

    wins = [t for t in closed if t.pnl_money > 0]
    losses = [t for t in closed if t.pnl_money < 0]
    gross_profit = sum(t.pnl_money for t in wins)
    gross_loss = abs(sum(t.pnl_money for t in losses))

    equities = [cfg.initial_capital] + [p.equity for p in outcome.equity_curve]
    dd_pct, dd_money = _max_drawdown(equities)

    net_profit = outcome.final_equity - cfg.initial_capital
    best = max(closed, key=lambda t: t.pnl_money, default=None)
    worst = min(closed, key=lambda t: t.pnl_money, default=None)
    holds = [t.hold_hours() for t in closed if t.hold_hours() is not None]

    return {
        "initial_capital": cfg.initial_capital,
        "final_equity": outcome.final_equity,
        "net_profit": net_profit,
        "return_pct": net_profit / cfg.initial_capital * 100.0,
        "unrealised_pnl": outcome.unrealised_pnl,
        "equity_with_open": outcome.final_equity + outcome.unrealised_pnl,
        "signal_days": len(trades),
        "trades_closed": len(closed),
        "trades_open": len(still_open),
        "trades_skipped": len(skipped),
        "trades_liquidated": len(liquidated),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": _safe_div(len(wins) * 100.0, len(closed)),
        "profit_factor": _safe_div(gross_profit, gross_loss),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "avg_trade": _safe_div(sum(t.pnl_money for t in closed), len(closed)),
        "avg_win": _safe_div(gross_profit, len(wins)),
        "avg_loss": _safe_div(-gross_loss, len(losses)),
        "expectancy_pct": _safe_div(sum(t.pnl_pct for t in closed), len(closed)),
        "max_drawdown_pct": dd_pct,
        "max_drawdown_money": dd_money,
        "days_positive": len(wins),
        "days_negative": len(losses),
        "exit_tp": sum(1 for t in closed if t.exit_reason == EXIT_TP),
        "exit_sl": sum(1 for t in closed if t.exit_reason == EXIT_SL),
        "exit_time": sum(1 for t in closed if t.exit_reason == EXIT_TIME),
        "exit_replaced": sum(1 for t in closed if t.exit_reason == EXIT_REPLACED),
        "best_day": {"date": best.signal_date.isoformat(), "pnl": best.pnl_money} if best else None,
        "worst_day": {"date": worst.signal_date.isoformat(), "pnl": worst.pnl_money} if worst else None,
        "avg_hold_hours": _safe_div(sum(holds), len(holds)),
        "max_concurrent": outcome.max_concurrent,
        "peak_leverage": outcome.peak_leverage,
        "liquidated": outcome.liquidated_at is not None,
        "liquidated_at": outcome.liquidated_at.isoformat() if outcome.liquidated_at else None,
        "notes": outcome.notes,
    }


def build_weekday_breakdown(trades: list[Trade], cfg: BacktestConfig) -> list[dict[str, Any]]:
    """Rozbicie wyniku na dni tygodnia — pokazuje, który dzień faktycznie zarabia."""
    rows: list[dict[str, Any]] = []
    for weekday in range(7):
        same_day = [t for t in trades if t.weekday == weekday]
        if not same_day:
            continue
        closed = [t for t in same_day if t.status == STATUS_CLOSED]
        wins = [t for t in closed if t.pnl_money > 0]
        pnl = sum(t.pnl_money for t in closed)
        rows.append(
            {
                "weekday": weekday,
                "weekday_name": WEEKDAY_NAMES_PL[weekday],
                "signals": len(same_day),
                "trades": len(closed),
                "wins": len(wins),
                "losses": len(closed) - len(wins),
                "win_rate": _safe_div(len(wins) * 100.0, len(closed)),
                "pnl_money": pnl,
                "pnl_pct_of_capital": pnl / cfg.initial_capital * 100.0,
            }
        )
    return rows


def build_equity_curve(outcome: BacktestOutcome, cfg: BacktestConfig, tz: ZoneInfo) -> list[dict[str, Any]]:
    return [
        {
            "time": point.ts.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
            "date": point.ts.astimezone(tz).date().isoformat(),
            "equity": point.equity,
            "cumulative_return_pct": point.cumulative_return_pct,
        }
        for point in outcome.equity_curve
    ]


def build_response(outcome: BacktestOutcome, cfg: BacktestConfig, tz: ZoneInfo) -> dict[str, Any]:
    return {
        "summary": build_summary(outcome, cfg),
        "weekday_breakdown": build_weekday_breakdown(outcome.trades, cfg),
        "equity_curve": build_equity_curve(outcome, cfg, tz),
        "trades": [t.to_dict(tz) for t in outcome.trades],
        "config": cfg.to_dict(),
    }
