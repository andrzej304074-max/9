"""Testy normalizacji plików CSV z TradingView."""

from datetime import datetime, timezone

import pytest

from app.csv_loader import DataError, load_bars, parse_number, parse_timestamp
from zoneinfo import ZoneInfo


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


# --- pojedyncze parsery ------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1704182400", "2024-01-02 08:00:00"),           # unix w sekundach
        ("1704182400000", "2024-01-02 08:00:00"),        # unix w milisekundach
        ("2024-01-02T08:00:00Z", "2024-01-02 08:00:00"),
        ("2024-01-02T09:00:00+01:00", "2024-01-02 08:00:00"),
        ("2024-01-02 08:00:00", "2024-01-02 08:00:00"),
        ("02.01.2024 08:00", "2024-01-02 08:00:00"),
        ("02/01/2024 08:00", "2024-01-02 08:00:00"),
    ],
)
def test_parse_timestamp_variants(raw, expected):
    assert parse_timestamp(raw, ZoneInfo("UTC")) == utc(expected)


def test_naive_timestamp_uses_selected_timezone():
    """Znacznik bez strefy interpretowany jest w strefie wybranej przez użytkownika."""
    warsaw = parse_timestamp("2024-07-01 08:00:00", ZoneInfo("Europe/Warsaw"))
    london = parse_timestamp("2024-07-01 08:00:00", ZoneInfo("Europe/London"))
    assert warsaw == utc("2024-07-01 06:00:00")  # latem UTC+2
    assert london == utc("2024-07-01 07:00:00")  # latem UTC+1


def test_parse_timestamp_rejects_garbage():
    with pytest.raises(ValueError):
        parse_timestamp("kiedyś w zeszłym tygodniu", ZoneInfo("UTC"))


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1.2345", 1.2345),
        ("1,2345", 1.2345),        # przecinek dziesiętny
        ("1 234.56", 1234.56),     # spacja jako separator tysięcy
        ("1,234.56", 1234.56),     # angielski separator tysięcy
        ("1.234,56", 1234.56),     # polski separator tysięcy
    ],
)
def test_parse_number_variants(raw, expected):
    assert parse_number(raw) == pytest.approx(expected)


# --- całe pliki --------------------------------------------------------------------


def test_standard_tradingview_export():
    text = (
        "time,open,high,low,close,Volume\n"
        "2024-01-02T08:00:00Z,1.2700,1.2720,1.2690,1.2710,1500\n"
        "2024-01-02T08:15:00Z,1.2710,1.2730,1.2700,1.2725,1600\n"
        "2024-01-02T08:30:00Z,1.2725,1.2740,1.2715,1.2735,1700\n"
    )
    result = load_bars(text)
    assert len(result.bars) == 3
    assert result.interval_minutes == 15
    assert result.bars[0].ts == utc("2024-01-02 08:00:00")
    assert result.bars[0].close == pytest.approx(1.2710)
    assert result.rejected_rows == 0


def test_semicolon_delimiter_with_comma_decimals():
    """Eksport otwarty i zapisany w polskim Excelu."""
    text = (
        "time;open;high;low;close\n"
        "2024-01-02 08:00:00;1,2700;1,2720;1,2690;1,2710\n"
        "2024-01-02 08:15:00;1,2710;1,2730;1,2700;1,2725\n"
        "2024-01-02 08:30:00;1,2725;1,2740;1,2715;1,2735\n"
    )
    result = load_bars(text, "UTC")
    assert len(result.bars) == 3
    assert result.bars[1].high == pytest.approx(1.2730)


def test_unix_timestamps_and_alternative_headers():
    text = (
        "Date,O,H,L,C\n"
        "1704182400,1.2700,1.2720,1.2690,1.2710\n"
        "1704183300,1.2710,1.2730,1.2700,1.2725\n"
    )
    result = load_bars(text)
    assert len(result.bars) == 2
    assert result.bars[0].ts == utc("2024-01-02 08:00:00")


def test_file_without_header_uses_positional_columns():
    text = (
        "2024-01-02T08:00:00Z,1.2700,1.2720,1.2690,1.2710\n"
        "2024-01-02T08:15:00Z,1.2710,1.2730,1.2700,1.2725\n"
    )
    result = load_bars(text)
    assert len(result.bars) == 2
    assert any("nagłówka" in w for w in result.warnings)


def test_inconsistent_and_duplicate_rows_are_dropped():
    text = (
        "time,open,high,low,close\n"
        "2024-01-02T08:00:00Z,1.2700,1.2720,1.2690,1.2710\n"
        "2024-01-02T08:15:00Z,1.2710,1.2600,1.2700,1.2725\n"  # high poniżej open — odrzucone
        "2024-01-02T08:30:00Z,1.2725,1.2740,1.2715,1.2735\n"
        "2024-01-02T08:30:00Z,1.2725,1.2740,1.2715,1.2739\n"  # duplikat znacznika czasu
    )
    result = load_bars(text)
    assert len(result.bars) == 2
    assert result.rejected_rows == 1
    assert result.bars[-1].close == pytest.approx(1.2739)  # wygrywa ostatni wpis
    assert any("zduplikowanych" in w for w in result.warnings)


def test_bars_are_sorted_even_if_file_is_reversed():
    text = (
        "time,open,high,low,close\n"
        "2024-01-02T08:30:00Z,1.2725,1.2740,1.2715,1.2735\n"
        "2024-01-02T08:00:00Z,1.2700,1.2720,1.2690,1.2710\n"
        "2024-01-02T08:15:00Z,1.2710,1.2730,1.2700,1.2725\n"
    )
    bars = load_bars(text).bars
    assert [b.ts for b in bars] == sorted(b.ts for b in bars)


def test_non_15m_interval_produces_warning():
    text = "time,open,high,low,close\n" + "".join(
        f"2024-01-02T{8 + i:02d}:00:00Z,1.27,1.28,1.26,1.275\n" for i in range(4)
    )
    result = load_bars(text)
    assert result.interval_minutes == 60
    assert any("60 min" in w for w in result.warnings)


def test_histdata_format():
    """HistData (M1 ASCII): średnik, brak nagłówka, czas jako RRRRMMDD GGMMSS."""
    text = (
        "20240102 080000;1.27000;1.27200;1.26900;1.27100;0\n"
        "20240102 081500;1.27100;1.27300;1.27000;1.27250;0\n"
        "20240102 083000;1.27250;1.27400;1.27150;1.27350;0\n"
    )
    result = load_bars(text, "UTC")
    assert len(result.bars) == 3
    assert result.bars[0].ts == utc("2024-01-02 08:00:00")
    assert result.bars[0].close == pytest.approx(1.27100)


def test_mt4_export_with_separate_date_and_time_columns():
    """Eksport z MT4/MT5: data i godzina w dwóch osobnych kolumnach, kropki w dacie."""
    text = (
        "2024.01.02,08:00,1.27000,1.27200,1.26900,1.27100,120\n"
        "2024.01.02,08:15,1.27100,1.27300,1.27000,1.27250,130\n"
        "2024.01.02,08:30,1.27250,1.27400,1.27150,1.27350,140\n"
    )
    result = load_bars(text, "UTC")
    assert len(result.bars) == 3
    assert result.bars[0].ts == utc("2024-01-02 08:00:00")
    assert result.bars[1].high == pytest.approx(1.27300)


def test_mt5_header_with_separate_date_and_time_columns():
    """MT5 eksportuje z nagłówkami w nawiasach ostrokątnych i tabulatorem."""
    text = (
        "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\n"
        "2024.01.02\t08:00:00\t1.27000\t1.27200\t1.26900\t1.27100\t120\n"
        "2024.01.02\t08:15:00\t1.27100\t1.27300\t1.27000\t1.27250\t130\n"
    )
    result = load_bars(text, "UTC")
    assert len(result.bars) == 2
    assert result.bars[0].ts == utc("2024-01-02 08:00:00")


def test_single_time_column_is_not_mistaken_for_split_columns():
    """Zwykły eksport z TradingView ma jedną kolumnę czasu — nie wolno jej rozbić."""
    text = (
        "time,open,high,low,close\n"
        "2024-01-02T08:00:00Z,1.2700,1.2720,1.2690,1.2710\n"
        "2024-01-02T08:15:00Z,1.2710,1.2730,1.2700,1.2725\n"
    )
    result = load_bars(text)
    assert result.bars[0].ts == utc("2024-01-02 08:00:00")
    assert result.bars[0].open == pytest.approx(1.2700)


@pytest.mark.parametrize(
    "prices,expected_pip",
    [
        ([1.2700, 1.2800], 0.0001),   # para walutowa
        ([157.20, 157.80], 0.01),     # para z jenem
        ([2650.0, 2660.0], 0.1),      # złoto
        ([95000.0, 96000.0], 1.0),    # bitcoin
    ],
)
def test_suggested_pip_size_follows_price_scale(prices, expected_pip):
    from app.csv_loader import suggest_pip_size

    rows = "".join(
        f"2024-01-02T0{8 + i}:00:00Z,{p},{p * 1.001},{p * 0.999},{p}\n"
        for i, p in enumerate(prices)
    )
    bars = load_bars("time,open,high,low,close\n" + rows).bars
    assert suggest_pip_size(bars) == expected_pip


def test_empty_file_raises():
    with pytest.raises(DataError):
        load_bars("")


def test_missing_columns_raise_with_helpful_message():
    with pytest.raises(DataError, match="brakuje kolumn"):
        load_bars("time,open,close\n2024-01-02T08:00:00Z,1.27,1.28\n")


def test_no_valid_rows_raises():
    with pytest.raises(DataError, match="żadnej świecy"):
        load_bars("time,open,high,low,close\nnie-data,a,b,c,d\n")


def test_unknown_timezone_raises():
    with pytest.raises(DataError, match="strefa czasowa"):
        load_bars("time,open,high,low,close\n2024-01-02T08:00:00Z,1.27,1.28,1.26,1.275\n", "Marte/Olympus")
