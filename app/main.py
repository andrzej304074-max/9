"""Serwer HTTP backtestera: statyczny frontend + API do wczytywania danych i liczenia wyników."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Optional
from zoneinfo import available_timezones

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import BacktestConfig, ConfigError, options_payload
from .csv_loader import DataError, LoadResult, bars_to_csv, load_bars
from .engine import run_backtest
from .fetch import DEFAULT_SYMBOL, fetch_bars
from .stats import build_response

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
SAMPLE_FILE = DATA_DIR / "GBPUSD_15m_sample.csv"

MAX_UPLOAD_BYTES = 64 * 1024 * 1024

app = FastAPI(title="Backtester GBP/USD", version="1.0.0")

# surowa treść CSV per zbiór danych; parsowanie jest leniwe, bo zależy od strefy czasowej
_DATASETS: dict[str, str] = {}
_PARSE_CACHE: dict[tuple[str, str], LoadResult] = {}


class BacktestRequest(BaseModel):
    dataset_id: str
    config: dict[str, Any] = Field(default_factory=dict)


class FetchRequest(BaseModel):
    symbol: str = DEFAULT_SYMBOL
    interval: str = "15m"
    range: str = "60d"
    timezone: str = "Europe/London"


def _dataset_id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def _store(text: str) -> str:
    dataset_id = _dataset_id(text)
    _DATASETS[dataset_id] = text
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = UPLOAD_DIR / f"{dataset_id}.csv"
    if not target.exists():
        target.write_text(text, encoding="utf-8")
    return dataset_id


def _load_text(dataset_id: str) -> str:
    if dataset_id in _DATASETS:
        return _DATASETS[dataset_id]
    stored = UPLOAD_DIR / f"{dataset_id}.csv"
    if stored.exists():
        text = stored.read_text(encoding="utf-8")
        _DATASETS[dataset_id] = text
        return text
    raise HTTPException(
        status_code=404,
        detail="Nie znaleziono wczytanych danych. Wgraj plik CSV jeszcze raz.",
    )


def _parse(dataset_id: str, timezone_name: str) -> LoadResult:
    key = (dataset_id, timezone_name)
    if key not in _PARSE_CACHE:
        if len(_PARSE_CACHE) > 24:
            _PARSE_CACHE.clear()
        _PARSE_CACHE[key] = load_bars(_load_text(dataset_id), timezone_name)
    return _PARSE_CACHE[key]


def _dataset_payload(dataset_id: str, result: LoadResult, source: str, timezone_name: str) -> dict[str, Any]:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(timezone_name)
    return {
        "dataset_id": dataset_id,
        "source": source,
        "bars": len(result.bars),
        "interval_minutes": result.interval_minutes,
        "first": result.first_ts.astimezone(tz).strftime("%Y-%m-%d %H:%M") if result.first_ts else None,
        "last": result.last_ts.astimezone(tz).strftime("%Y-%m-%d %H:%M") if result.last_ts else None,
        "first_date": result.first_ts.astimezone(tz).date().isoformat() if result.first_ts else None,
        "last_date": result.last_ts.astimezone(tz).date().isoformat() if result.last_ts else None,
        "warnings": result.warnings,
        "rejected_rows": result.rejected_rows,
        "columns": result.source_columns,
        "timezone": timezone_name,
    }


@app.exception_handler(DataError)
async def _data_error_handler(_request, exc: DataError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(ConfigError)
async def _config_error_handler(_request, exc: ConfigError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/api/options")
def get_options() -> dict[str, Any]:
    """Opcje pól wyboru, ustawienia domyślne i lista popularnych stref czasowych."""
    popular = [
        "Europe/London",
        "Europe/Warsaw",
        "Europe/Berlin",
        "UTC",
        "America/New_York",
        "Asia/Tokyo",
        "Australia/Sydney",
    ]
    others = sorted(tz for tz in available_timezones() if tz not in popular)
    return {
        "options": options_payload(),
        "defaults": BacktestConfig().to_dict(),
        "timezones": popular + others,
        "sample_available": SAMPLE_FILE.exists(),
    }


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), timezone: str = "Europe/London") -> dict[str, Any]:
    raw = await file.read()
    if not raw:
        raise DataError("Wysłany plik jest pusty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise DataError("Plik jest za duży (limit 64 MB).")

    for encoding in ("utf-8-sig", "utf-8", "cp1250", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise DataError("Nie udało się rozpoznać kodowania pliku.")

    dataset_id = _store(text)
    result = _parse(dataset_id, timezone)
    return _dataset_payload(dataset_id, result, file.filename or "plik.csv", timezone)


@app.get("/api/sample")
def sample(timezone: str = "Europe/London") -> dict[str, Any]:
    if not SAMPLE_FILE.exists():
        raise DataError(
            "Brak pliku z danymi demo. Wygeneruj go poleceniem: python3 tools/make_sample_data.py"
        )
    dataset_id = _store(SAMPLE_FILE.read_text(encoding="utf-8"))
    result = _parse(dataset_id, timezone)
    payload = _dataset_payload(dataset_id, result, "dane demo (syntetyczne)", timezone)
    payload["warnings"] = payload["warnings"] + [
        "To są dane SYNTETYCZNE, wygenerowane losowo — służą wyłącznie do sprawdzenia mechaniki. "
        "Do realnych wniosków wgraj własny eksport CSV z TradingView."
    ]
    return payload


@app.post("/api/fetch")
def fetch(request: FetchRequest) -> dict[str, Any]:
    bars = fetch_bars(symbol=request.symbol, interval=request.interval, range_=request.range)
    dataset_id = _store(bars_to_csv(bars))
    result = _parse(dataset_id, request.timezone)
    payload = _dataset_payload(dataset_id, result, f"Yahoo Finance ({request.symbol})", request.timezone)
    payload["warnings"] = payload["warnings"] + [
        "Dane pochodzą z Yahoo Finance, nie z TradingView — kwotowania mogą się nieznacznie różnić."
    ]
    return payload


@app.post("/api/backtest")
def backtest(request: BacktestRequest) -> dict[str, Any]:
    cfg = BacktestConfig.from_dict(request.config)
    result = _parse(request.dataset_id, cfg.timezone)
    outcome, tz = run_backtest(result.bars, cfg)

    response = build_response(outcome, cfg, tz)
    response["dataset"] = {
        "bars": len(result.bars),
        "interval_minutes": result.interval_minutes,
        "warnings": result.warnings,
    }
    return response


if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
