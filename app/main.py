"""Serwer HTTP backtestera: statyczny frontend + API do wczytywania danych i liczenia wyników."""

from __future__ import annotations

import hashlib
import threading
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Optional
from zoneinfo import available_timezones

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import BacktestConfig, ConfigError, options_payload
from .csv_loader import DataError, LoadResult, bars_to_csv, load_bars, suggest_pip_size
from .dukascopy import DEFAULT_INSTRUMENT, download_bars, instruments_payload
from .engine import run_backtest
from .fetch import DEFAULT_SYMBOL, fetch_bars
from .stats import build_response

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DUKASCOPY_CACHE = DATA_DIR / "dukascopy_cache"
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


class DukascopyRequest(BaseModel):
    instrument: str = DEFAULT_INSTRUMENT
    date_from: str
    date_to: str
    interval_minutes: int = 15
    price: str = "bid"
    timezone: str = "Europe/London"


# Pobieranie z Dukascopy idzie plik po pliku (jeden na godzinę), więc wieloletni zakres
# trwa minuty — za długo na pojedyncze żądanie HTTP. Zadania biegną w tle, a front
# odpytuje o postęp.
_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


def _job_update(job_id: str, **fields: Any) -> None:
    with _JOBS_LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update(fields)


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
        "suggested_pip_size": suggest_pip_size(result.bars),
        "median_price": sorted(b.close for b in result.bars)[len(result.bars) // 2]
        if result.bars
        else None,
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
        "dukascopy_instruments": instruments_payload(),
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


def _run_dukascopy_job(job_id: str, request: DukascopyRequest) -> None:
    def progress(done: int, total: int) -> None:
        _job_update(job_id, done=done, total=total)

    def cancelled() -> bool:
        with _JOBS_LOCK:
            return _JOBS.get(job_id, {}).get("cancel", False)

    try:
        bars = download_bars(
            instrument=request.instrument,
            start=date.fromisoformat(request.date_from),
            end=date.fromisoformat(request.date_to),
            interval_minutes=request.interval_minutes,
            price=request.price,
            cache_dir=DUKASCOPY_CACHE,
            progress=progress,
            cancelled=cancelled,
        )
        dataset_id = _store(bars_to_csv(bars))
        result = _parse(dataset_id, request.timezone)
        payload = _dataset_payload(
            dataset_id, result, f"Dukascopy ({request.instrument.upper()})", request.timezone
        )
        payload["warnings"] = payload["warnings"] + [
            f"Świece złożone z ticków Dukascopy po cenie {request.price}. "
            "To inny dostawca kwotowań niż TradingView — poziomy mogą się nieznacznie różnić."
        ]
        _job_update(job_id, state="done", dataset=payload)
    except DataError as exc:
        _job_update(job_id, state="error", error=str(exc))
    except Exception as exc:  # nieprzewidziany błąd nie może zostawić zadania w zawieszeniu
        _job_update(job_id, state="error", error=f"Nieoczekiwany błąd pobierania: {exc}")


@app.post("/api/dukascopy/start")
def dukascopy_start(request: DukascopyRequest) -> dict[str, Any]:
    """Uruchamia pobieranie w tle i natychmiast oddaje identyfikator zadania."""
    try:
        start, end = date.fromisoformat(request.date_from), date.fromisoformat(request.date_to)
    except ValueError:
        raise DataError("Daty muszą być w formacie RRRR-MM-DD.")
    if start > end:
        raise DataError("Data początkowa jest późniejsza niż końcowa.")
    if start.year < 2003:
        raise DataError("Archiwum Dukascopy sięga 2003 roku — wybierz późniejszą datę początkową.")

    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        if sum(1 for job in _JOBS.values() if job["state"] == "running") >= 2:
            raise DataError("Trwa już pobieranie. Poczekaj na jego zakończenie albo je przerwij.")
        _JOBS[job_id] = {"state": "running", "done": 0, "total": 0, "cancel": False}

    threading.Thread(target=_run_dukascopy_job, args=(job_id, request), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/dukascopy/status/{job_id}")
def dukascopy_status(job_id: str) -> dict[str, Any]:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Nie znaleziono zadania pobierania.")
        snapshot = dict(job)
    snapshot.pop("cancel", None)
    return snapshot


@app.post("/api/dukascopy/cancel/{job_id}")
def dukascopy_cancel(job_id: str) -> dict[str, Any]:
    with _JOBS_LOCK:
        if job_id not in _JOBS:
            raise HTTPException(status_code=404, detail="Nie znaleziono zadania pobierania.")
        _JOBS[job_id]["cancel"] = True
    return {"ok": True}


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
