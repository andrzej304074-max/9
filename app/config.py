"""Konfiguracja backtestu — wartości domyślne, walidacja i serializacja.

Wszystkie parametry strategii są edytowalne z poziomu UI; ten moduł jest jedynym
miejscem, w którym zdefiniowane są dozwolone wartości i domyślne ustawienia.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# --- dozwolone wartości pól wyboru -------------------------------------------------

STRATEGIES = {
    "candle_direction": "Kierunek świecy sesyjnej",
    "range_breakout": "Wybicie zakresu świecy",
}

BREAKOUT_WINDOW_MODES = {
    "end_of_day": "Do końca dnia",
    "until_time": "Do określonej godziny",
    "hours_after": "Przez N godzin od świecy",
}

BREAKOUT_TRIGGERS = {
    "touch": "Dotknięcie poziomu",
    "close_beyond": "Zamknięcie świecy poza zakresem",
}

BREAKOUT_RETRY_MODES = {
    "single": "Jedna transakcja dziennie",
    "opposite": "Dopuść jeszcze wybicie w drugą stronę",
    "unlimited": "Każde kolejne wybicie",
}

BREAKOUT_BOTH_SIDES = {
    "open_proximity": "Pierwszy poziom bliższy otwarciu świecy",
    "high_first": "Zawsze najpierw górą",
    "low_first": "Zawsze najpierw dołem",
    "skip": "Pomiń — nie da się rozstrzygnąć",
}

DIRECTION_MODES = {
    "follow": "Podążaj za świecą",
    "invert": "Odwróć sygnał",
    "long_only": "Tylko long",
    "short_only": "Tylko short",
}

DOJI_MODES = {
    "skip": "Pomiń dzień",
    "long": "Zagraj long",
    "short": "Zagraj short",
}

ENTRY_MODES = {
    "next_open": "Otwarcie kolejnej świecy",
    "signal_close": "Zamknięcie świecy sygnałowej",
}

SL_METHODS = {
    "candle_range": "Zakres świecy sygnałowej",
    "fixed_pips": "Stała liczba pipsów",
    "percent": "Procent ceny wejścia",
}

TIE_BREAKS = {
    "sl_first": "Stop Loss pierwszy (konserwatywnie)",
    "tp_first": "Take Profit pierwszy (optymistycznie)",
}

POSITION_MODES = {
    "parallel": "Równolegle, bez limitu czasu",
    "close_at_time": "Zamknij o określonej godzinie",
    "replace": "Zamknij starą przy otwarciu nowej",
    "skip": "Pomiń nowy sygnał",
}

SIZING_MODES = {
    "compound": "Cała ekspozycja, z kapitalizacją",
    "fixed_notional": "Cała ekspozycja, bez kapitalizacji",
    "risk_percent": "Stałe ryzyko % na transakcję",
}

WEEKDAY_NAMES_PL = [
    "Poniedziałek",
    "Wtorek",
    "Środa",
    "Czwartek",
    "Piątek",
    "Sobota",
    "Niedziela",
]


class ConfigError(ValueError):
    """Błąd walidacji konfiguracji — komunikat trafia wprost do UI."""


@dataclass
class BacktestConfig:
    # --- dane ---
    timezone: str = "Europe/London"

    # --- wybór strategii ---
    strategy: str = "candle_direction"

    # --- świeca sygnałowa (wspólna dla obu strategii) ---
    signal_hour: int = 8
    signal_minute: int = 0
    candle_minutes: int = 15
    direction_mode: str = "follow"
    doji_mode: str = "skip"

    # --- strategia „wybicie zakresu” ---
    breakout_window_mode: str = "end_of_day"
    breakout_until_hour: int = 17
    breakout_until_minute: int = 0
    breakout_hours: float = 6.0
    breakout_trigger: str = "touch"
    breakout_buffer_pips: float = 0.0
    breakout_retry_mode: str = "single"
    breakout_max_per_day: int = 5
    breakout_both_sides: str = "open_proximity"

    # --- logika pozycji ---
    entry_mode: str = "next_open"
    rr_ratio: float = 4.0
    sl_method: str = "candle_range"
    sl_pips: float = 20.0
    sl_percent: float = 0.10
    sl_multiplier: float = 1.0
    pip_size: float = 0.0001
    spread_pips: float = 0.0
    tie_break: str = "sl_first"

    # --- zarządzanie pozycją ---
    position_mode: str = "parallel"
    close_time_hour: int = 22
    close_time_minute: int = 0
    close_after_days: int = 0

    # --- kapitał ---
    initial_capital: float = 10000.0
    leverage: float = 30.0
    sizing_mode: str = "compound"
    risk_percent: float = 1.0

    # --- zakres testu ---
    lookback_days: int = 0  # 0 = cała dostępna historia
    date_from: Optional[str] = None  # 'YYYY-MM-DD'
    date_to: Optional[str] = None
    weekdays: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])

    # -------------------------------------------------------------------------

    @property
    def spread_price(self) -> float:
        """Koszt spreadu wyrażony w jednostkach ceny."""
        return self.spread_pips * self.pip_size

    @property
    def closes_at_time(self) -> bool:
        return self.position_mode == "close_at_time"

    @property
    def is_breakout(self) -> bool:
        return self.strategy == "range_breakout"

    @property
    def breakout_buffer_price(self) -> float:
        """Bufor wybicia wyrażony w jednostkach ceny."""
        return self.breakout_buffer_pips * self.pip_size

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Optional[dict[str, Any]]) -> "BacktestConfig":
        """Buduje konfigurację z surowego JSON-a, ignorując nieznane klucze."""
        raw = raw or {}
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(raw) - known
        data = {k: v for k, v in raw.items() if k in known}
        cfg = cls(**data)
        cfg.validate()
        cfg.unknown_keys = sorted(unknown)  # type: ignore[attr-defined]
        return cfg

    def validate(self) -> None:
        def _choice(value: str, allowed: dict[str, str], label: str) -> None:
            if value not in allowed:
                raise ConfigError(
                    f"{label}: nieznana wartość '{value}'. "
                    f"Dozwolone: {', '.join(sorted(allowed))}."
                )

        _choice(self.strategy, STRATEGIES, "Strategia")
        _choice(self.breakout_window_mode, BREAKOUT_WINDOW_MODES, "Okno wybicia")
        _choice(self.breakout_trigger, BREAKOUT_TRIGGERS, "Wyzwalacz wybicia")
        _choice(self.breakout_retry_mode, BREAKOUT_RETRY_MODES, "Powtórki wybicia")
        _choice(self.breakout_both_sides, BREAKOUT_BOTH_SIDES, "Wybicie obustronne")
        _choice(self.direction_mode, DIRECTION_MODES, "Tryb kierunku")
        _choice(self.doji_mode, DOJI_MODES, "Zachowanie na doji")
        _choice(self.entry_mode, ENTRY_MODES, "Moment wejścia")
        _choice(self.sl_method, SL_METHODS, "Metoda Stop Lossa")
        _choice(self.tie_break, TIE_BREAKS, "Rozstrzyganie TP/SL w jednej świecy")
        _choice(self.position_mode, POSITION_MODES, "Tryb zarządzania pozycją")
        _choice(self.sizing_mode, SIZING_MODES, "Tryb wielkości pozycji")

        if not 0 <= self.signal_hour <= 23:
            raise ConfigError("Godzina sygnału musi mieścić się w zakresie 0–23.")
        if not 0 <= self.signal_minute <= 59:
            raise ConfigError("Minuta sygnału musi mieścić się w zakresie 0–59.")
        if not 1 <= self.candle_minutes <= 1440:
            raise ConfigError("Długość świecy musi mieścić się w zakresie 1–1440 minut.")
        if not 0 <= self.close_time_hour <= 23:
            raise ConfigError("Godzina zamknięcia musi mieścić się w zakresie 0–23.")
        if not 0 <= self.close_time_minute <= 59:
            raise ConfigError("Minuta zamknięcia musi mieścić się w zakresie 0–59.")
        if self.close_after_days < 0:
            raise ConfigError("Liczba dni do zamknięcia nie może być ujemna.")

        if self.rr_ratio <= 0:
            raise ConfigError("Stosunek zysku do ryzyka (RR) musi być większy od zera.")
        if self.sl_multiplier <= 0:
            raise ConfigError("Mnożnik Stop Lossa musi być większy od zera.")
        if self.pip_size <= 0:
            raise ConfigError("Rozmiar pipsa musi być większy od zera.")
        if self.spread_pips < 0:
            raise ConfigError("Spread nie może być ujemny.")
        if self.sl_method == "fixed_pips" and self.sl_pips <= 0:
            raise ConfigError("Przy metodzie 'stałe pipsy' Stop Loss musi być większy od zera.")
        if self.sl_method == "percent" and self.sl_percent <= 0:
            raise ConfigError("Przy metodzie 'procent ceny' Stop Loss musi być większy od zera.")

        if self.initial_capital <= 0:
            raise ConfigError("Kapitał początkowy musi być większy od zera.")
        if self.leverage <= 0:
            raise ConfigError("Dźwignia musi być większa od zera.")
        if self.sizing_mode == "risk_percent" and not 0 < self.risk_percent <= 100:
            raise ConfigError("Ryzyko na transakcję musi mieścić się w przedziale (0; 100]%.")

        if not 0 <= self.breakout_until_hour <= 23:
            raise ConfigError("Godzina końca okna wybicia musi mieścić się w zakresie 0–23.")
        if not 0 <= self.breakout_until_minute <= 59:
            raise ConfigError("Minuta końca okna wybicia musi mieścić się w zakresie 0–59.")
        if self.breakout_hours <= 0:
            raise ConfigError("Długość okna wybicia musi być większa od zera.")
        if self.breakout_buffer_pips < 0:
            raise ConfigError("Bufor wybicia nie może być ujemny.")
        if self.breakout_max_per_day < 1:
            raise ConfigError("Limit transakcji na dzień musi wynosić co najmniej 1.")

        if self.lookback_days < 0:
            raise ConfigError("Liczba dni wstecz nie może być ujemna.")

        try:
            self.weekdays = sorted({int(d) for d in self.weekdays})
        except (TypeError, ValueError):
            raise ConfigError("Lista dni tygodnia zawiera nieprawidłowe wartości.")
        if not self.weekdays:
            raise ConfigError("Wybierz co najmniej jeden dzień tygodnia.")
        if any(d < 0 or d > 6 for d in self.weekdays):
            raise ConfigError("Dni tygodnia muszą mieścić się w zakresie 0 (pon) – 6 (niedz).")

        for label, value in (("Data od", self.date_from), ("Data do", self.date_to)):
            if value:
                from datetime import date as _date

                try:
                    _date.fromisoformat(value)
                except ValueError:
                    raise ConfigError(f"{label}: oczekiwano formatu YYYY-MM-DD, otrzymano '{value}'.")

        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ConfigError("Data 'od' jest późniejsza niż data 'do'.")


def options_payload() -> dict[str, dict[str, str]]:
    """Słowniki opcji dla UI — front nie musi ich duplikować."""
    return {
        "strategy": STRATEGIES,
        "direction_mode": DIRECTION_MODES,
        "doji_mode": DOJI_MODES,
        "entry_mode": ENTRY_MODES,
        "sl_method": SL_METHODS,
        "tie_break": TIE_BREAKS,
        "position_mode": POSITION_MODES,
        "sizing_mode": SIZING_MODES,
        "breakout_window_mode": BREAKOUT_WINDOW_MODES,
        "breakout_trigger": BREAKOUT_TRIGGERS,
        "breakout_retry_mode": BREAKOUT_RETRY_MODES,
        "breakout_both_sides": BREAKOUT_BOTH_SIDES,
    }
