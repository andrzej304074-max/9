"""Serwer HTTP backtestera: statyczny frontend + API do wczytywania danych i liczenia wyników."""

from __future__ import annotations

import gzip
import hashlib
import threading
import time
import uuid
import zlib
from datetime import date
from pathlib import Path
from typing import Any, Optional
from zoneinfo import available_timezones

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import BacktestConfig, ConfigError, options_payload
from .csv_loader import DataError, LoadResult, bars_to_csv, load_bars, suggest_pip_size
from .dukascopy import (
    DEFAULT_INSTRUMENT,
    download_bars,
    download_window,
    inspect_candles,
    instruments_payload,
    probe as probe_dukascopy,
)
from .engine import run_backtest
from . import library, storage
from .fetch import DEFAULT_SYMBOL, fetch_bars, intervals_payload, max_history_days
from .runtime import (
    BASE_DIR,
    IS_SERVERLESS,
    MAX_REQUEST_SECONDS,
    MAX_UPLOAD_BYTES,
    describe as describe_runtime,
    dukascopy_day_limit,
    state_dir,
)
from .stats import build_response

WEB_DIR = BASE_DIR / "web"
SAMPLE_FILE = BASE_DIR / "data" / "GBPUSD_15m_sample.csv"

# Katalogi zapisywalne wskazuje warstwa środowiska — lokalnie `data/`, na Vercelu `/tmp`.
UPLOAD_DIR = state_dir() / "uploads"
DUKASCOPY_CACHE = state_dir() / "dukascopy_cache"

app = FastAPI(title="Backtester GBP/USD", version="1.1.0")

# surowa treść CSV per zbiór danych; parsowanie jest leniwe, bo zależy od strefy czasowej
_DATASETS: dict[str, str] = {}
_PARSE_CACHE: dict[tuple[str, str], LoadResult] = {}


class BacktestRequest(BaseModel):
    dataset_id: str
    config: dict[str, Any] = Field(default_factory=dict)


class CompareRequest(BaseModel):
    dataset_id: str
    configs: dict[str, dict[str, Any]] = Field(default_factory=dict)


class FetchRequest(BaseModel):
    symbol: str = DEFAULT_SYMBOL
    interval: str = "15m"
    days: int = 60
    timezone: str = "Europe/London"


class DukascopyRequest(BaseModel):
    instrument: str = DEFAULT_INSTRUMENT
    date_from: str
    date_to: str
    interval_minutes: int = 15
    price: str = "bid"
    timezone: str = "Europe/London"


class DukascopyChunkRequest(BaseModel):
    """Jeden odcinek pobierania — na tyle krótki, żeby zmieścić się w limicie czasu żądania."""

    instrument: str = DEFAULT_INSTRUMENT
    date_from: str
    date_to: str
    interval_minutes: int = 15
    price: str = "bid"
    with_header: bool = True


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
    return library.dataset_id_for(text)


def _store(text: str, name: str = "", source: str = "", meta: Optional[dict[str, Any]] = None) -> str:
    """Zapamiętuje zbiór w pamięci procesu i w bibliotece na dysku."""
    dataset_id = _dataset_id(text)
    _DATASETS[dataset_id] = text
    library.save(dataset_id, text, name, source, meta)
    return dataset_id


def _load_text(dataset_id: str) -> str:
    if dataset_id in _DATASETS:
        return _DATASETS[dataset_id]
    text = library.get_text(dataset_id)
    if text is not None:
        _DATASETS[dataset_id] = text
        return text
    # Kod 409 zamiast 404: front rozpoznaje go jako „instancja nie zna tych danych”
    # i sam wysyła CSV jeszcze raz, bez pokazywania błędu użytkownikowi.
    raise HTTPException(
        status_code=409,
        detail="Serwer nie ma już tych danych w pamięci. Wyślij plik CSV jeszcze raz.",
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
    payload = {
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

    # Każde źródło danych przechodzi przez to miejsce, więc tu opisujemy zbiór w bibliotece.
    library.describe(
        dataset_id,
        name=source,
        source=source,
        meta={key: payload[key] for key in
              ("bars", "interval_minutes", "first_date", "last_date", "rejected_rows")},
    )
    return payload


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
        "fetch_intervals": intervals_payload(),
        "fetch_history_days": {key: max_history_days(key) for key in intervals_payload()},
        "runtime": describe_runtime(),
    }


@app.post("/api/upload")
async def upload(
    file: UploadFile = File(...),
    timezone: str = "Europe/London",
    encoding_hint: str = Form("plain", alias="encoding"),
) -> dict[str, Any]:
    raw = await file.read()
    if not raw:
        raise DataError("Wysłany plik jest pusty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        limit_mb = MAX_UPLOAD_BYTES / (1024 * 1024)
        raise DataError(
            f"Plik jest za duży (limit {limit_mb:.1f} MB)."
            + (" Na Vercelu ciało żądania jest ograniczone przez platformę." if IS_SERVERLESS else "")
        )

    # Front pakuje CSV gzipem, żeby zmieścić się w limicie ciała żądania.
    if encoding_hint == "gzip" or raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        # OSError to zły nagłówek, EOFError — plik urwany w transmisji, zlib.error — uszkodzony strumień
        except (OSError, EOFError, zlib.error) as exc:
            raise DataError(
                f"Nie udało się rozpakować przesłanego pliku ({exc}). "
                "Spróbuj wysłać go jeszcze raz."
            )

    for encoding in ("utf-8-sig", "utf-8", "cp1250", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise DataError("Nie udało się rozpoznać kodowania pliku.")

    # Przeglądarka dokleja „.gz” przy kompresji — w bibliotece ma się pokazać nazwa,
    # którą użytkownik zna ze swojego dysku.
    filename = (file.filename or "plik.csv").removesuffix(".gz")

    dataset_id = _store(text)
    result = _parse(dataset_id, timezone)
    return _dataset_payload(dataset_id, result, filename, timezone)


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
    """Kompletuje żądany okres, cofając się oknami — jedno żądanie do dostawcy nie wystarcza."""
    # Zostawiamy zapas na złożenie CSV i sparsowanie go po pobraniu.
    deadline = time.monotonic() + MAX_REQUEST_SECONDS * 0.7 if IS_SERVERLESS else None

    outcome = fetch_bars(
        symbol=request.symbol,
        interval=request.interval,
        days=request.days,
        deadline=deadline,
    )
    dataset_id = _store(bars_to_csv(outcome.bars))
    result = _parse(dataset_id, request.timezone)
    payload = _dataset_payload(
        dataset_id, result, f"Yahoo Finance ({request.symbol})", request.timezone
    )
    payload["warnings"] = payload["warnings"] + outcome.notes + [
        "Dane pochodzą z Yahoo Finance, nie z TradingView — kwotowania mogą się nieznacznie różnić."
    ]
    payload["fetch"] = {
        "requested_days": outcome.requested_days,
        "covered_days": outcome.covered_days,
        "windows": outcome.windows,
        "stopped_early": outcome.stopped_early,
    }
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
    """Uruchamia pobieranie i oddaje identyfikator zadania.

    Lokalnie zadanie biegnie w wątku w tle, a front odpytuje o postęp. Na platformie
    bezserwerowej wątek zginąłby razem z odpowiedzią, a kolejne odpytanie i tak mogłoby
    trafić na inną instancję — dlatego tam pobieranie wykonuje się w tym samym żądaniu,
    a gotowy wynik wraca od razu.
    """
    try:
        start, end = date.fromisoformat(request.date_from), date.fromisoformat(request.date_to)
    except ValueError:
        raise DataError("Daty muszą być w formacie RRRR-MM-DD.")
    if start > end:
        raise DataError("Data początkowa jest późniejsza niż końcowa.")
    if start.year < 2003:
        raise DataError("Archiwum Dukascopy sięga 2003 roku — wybierz późniejszą datę początkową.")

    day_limit = dukascopy_day_limit()
    if day_limit and (end - start).days + 1 > day_limit:
        raise DataError(
            f"Pojedyncze żądanie ma limit czasu, więc obejmuje najwyżej {day_limit} dni, "
            f"a wybrano {(end - start).days + 1}. Dłuższe zakresy pobiera się odcinkami "
            f"przez /api/dukascopy/chunk — interfejs robi to automatycznie."
        )

    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        if not IS_SERVERLESS and sum(1 for job in _JOBS.values() if job["state"] == "running") >= 2:
            raise DataError("Trwa już pobieranie. Poczekaj na jego zakończenie albo je przerwij.")
        _JOBS[job_id] = {"state": "running", "done": 0, "total": 0, "cancel": False}

    if IS_SERVERLESS:
        _run_dukascopy_job(job_id, request)
        with _JOBS_LOCK:
            snapshot = dict(_JOBS[job_id])
        snapshot.pop("cancel", None)
        # Front widzi gotowy stan i pomija odpytywanie o postęp.
        return {"job_id": job_id, **snapshot}

    threading.Thread(target=_run_dukascopy_job, args=(job_id, request), daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/dukascopy/chunk")
def dukascopy_chunk(request: DukascopyChunkRequest) -> dict[str, Any]:
    """Pobiera jeden odcinek zakresu i oddaje surowy CSV.

    Przy wdrożeniu bezserwerowym długiej historii nie da się pobrać w jednym żądaniu,
    bo obowiązuje limit czasu. Przeglądarka dzieli więc zakres na odcinki, prosi o nie
    po kolei i skleja wyniki u siebie — dopiero komplet trafia na serwer jako jeden
    plik. Dzięki temu żadne pojedyncze żądanie nie przekracza budżetu, a scalanie nie
    zależy od tego, która instancja obsłużyła który odcinek.
    """
    try:
        start, end = date.fromisoformat(request.date_from), date.fromisoformat(request.date_to)
    except ValueError:
        raise DataError("Daty muszą być w formacie RRRR-MM-DD.")
    if start > end:
        raise DataError("Data początkowa jest późniejsza niż końcowa.")

    # Nie ograniczamy tu długości zakresu: pobieranie i tak zatrzyma się na granicy
    # budżetu czasu i odda komplet dni domkniętych do tej pory.
    # Ile zdążymy, zależy od prędkości łącza do archiwum, a tej nie da się z góry zgadnąć.
    # Dlatego pobieramy do wyczerpania budżetu i mówimy, dokąd doszliśmy — front wznowi
    # od następnego dnia. Zapas zostawiamy na złożenie CSV i odesłanie odpowiedzi.
    deadline = time.monotonic() + MAX_REQUEST_SECONDS * 0.65 if IS_SERVERLESS else None

    outcome = download_window(
        instrument=request.instrument,
        start=start,
        end=end,
        interval_minutes=request.interval_minutes,
        price=request.price,
        cache_dir=DUKASCOPY_CACHE,
        deadline=deadline,
    )
    text = bars_to_csv(outcome.bars)
    if not request.with_header:
        text = text.split("\n", 1)[1] if "\n" in text else ""
    return {
        "csv": text,
        "bars": len(outcome.bars),
        # dzień domknięty w całości — stąd front zaczyna kolejne żądanie
        "covered_to": outcome.covered_to.isoformat() if outcome.covered_to else None,
        "complete": outcome.complete,
        "failed_hours": outcome.failed_hours,
    }


@app.get("/api/dukascopy/probe")
def dukascopy_probe(instrument: str = DEFAULT_INSTRUMENT) -> dict[str, Any]:
    """Sprawdza jednym żądaniem, czy archiwum Dukascopy jest osiągalne z tego serwera.

    Odpowiada na najtrudniejsze do zdiagnozowania zgłoszenie: „klikam i nic się nie dzieje".
    """
    return probe_dukascopy(instrument)


@app.get("/api/dukascopy/inspect")
def dukascopy_inspect(instrument: str = DEFAULT_INSTRUMENT, price: str = "bid") -> dict[str, Any]:
    """Surowy podgląd pliku ze świecami — do rozpoznania nieudokumentowanego formatu."""
    return inspect_candles(instrument, price=price)


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


class RenameRequest(BaseModel):
    name: str


@app.get("/api/datasets")
def list_datasets() -> dict[str, Any]:
    """Zbiory, które już przez aplikację przeszły — do ponownego użycia bez pobierania."""
    usage = library.usage()
    return {
        "datasets": library.entries(),
        "usage": usage,
        # O trwałości decyduje magazyn, nie samo środowisko: bezserwerowe wdrożenie
        # z podpiętym magazynem obiektów przechowuje dane na stałe.
        "persistent": bool(usage.get("persistent")),
    }


@app.get("/api/storage/probe")
def storage_probe() -> dict[str, Any]:
    """Sprawdza pełnym cyklem, czy zapis biblioteki jest trwały na tym wdrożeniu."""
    return storage.probe()


@app.post("/api/datasets/{dataset_id}/open")
def open_dataset(dataset_id: str, timezone: str = "Europe/London") -> dict[str, Any]:
    """Wczytuje zapisany zbiór ponownie — bez sieci i bez wysyłania pliku."""
    entry = next((e for e in library.entries() if e["id"] == dataset_id), None)
    text = library.get_text(dataset_id)
    if text is None:
        raise HTTPException(status_code=404, detail="Nie ma już takiego zbioru w bibliotece.")

    _DATASETS[dataset_id] = text
    result = _parse(dataset_id, timezone)
    label = (entry or {}).get("name") or (entry or {}).get("source") or "zapisany zbiór"
    return _dataset_payload(dataset_id, result, label, timezone)


@app.patch("/api/datasets/{dataset_id}")
def rename_dataset(dataset_id: str, request: RenameRequest) -> dict[str, Any]:
    if not library.rename(dataset_id, request.name):
        raise DataError("Nazwa nie może być pusta, a zbiór musi istnieć w bibliotece.")
    return {"ok": True}


@app.delete("/api/datasets/{dataset_id}")
def delete_dataset(dataset_id: str) -> dict[str, Any]:
    if not library.remove(dataset_id):
        raise HTTPException(status_code=404, detail="Nie ma już takiego zbioru w bibliotece.")
    _DATASETS.pop(dataset_id, None)
    for key in [k for k in _PARSE_CACHE if k[0] == dataset_id]:
        _PARSE_CACHE.pop(key, None)
    return {"ok": True}


@app.get("/api/datasets/{dataset_id}/csv")
def download_dataset(dataset_id: str) -> Response:
    """Oddaje zapisany zbiór jako plik CSV — do obejrzenia albo poprawienia u siebie."""
    text = library.get_text(dataset_id)
    if text is None:
        raise HTTPException(status_code=404, detail="Nie ma już takiego zbioru w bibliotece.")
    entry = next((e for e in library.entries() if e["id"] == dataset_id), {})
    safe = "".join(c for c in str(entry.get("name", dataset_id)) if c.isalnum() or c in " -_")[:60]
    return Response(
        content=text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe or dataset_id}.csv"'},
    )


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


@app.post("/api/compare")
def compare(request: CompareRequest) -> dict[str, Any]:
    """Liczy obie strategie na tych samych świecach, każdą z jej własnymi ustawieniami."""
    if not request.configs:
        raise DataError("Nie podano żadnej konfiguracji do porównania.")

    results: dict[str, Any] = {}
    for strategy, raw in request.configs.items():
        cfg = BacktestConfig.from_dict({**raw, "strategy": strategy})
        result = _parse(request.dataset_id, cfg.timezone)
        outcome, tz = run_backtest(result.bars, cfg)
        results[strategy] = build_response(outcome, cfg, tz)

    return {"results": results}


@app.middleware("http")
async def _cache_static(request, call_next):
    """Pliki frontu trafiają na CDN, odpowiedzi API nigdy.

    Na Vercelu wszystkie ścieżki przechodzą przez tę funkcję, więc bez nagłówka każdy
    styl i skrypt budziłby ją na nowo. Statyka jest wersjonowana wdrożeniem, ale HTML
    trzymamy krótko, żeby po wdrożeniu nie zostać ze starą stroną.
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif response.status_code == 200:
        if path.endswith((".css", ".js")):
            response.headers["Cache-Control"] = "public, max-age=300, s-maxage=86400"
        elif path in ("/", "") or path.endswith(".html"):
            response.headers["Cache-Control"] = "public, max-age=0, s-maxage=60, must-revalidate"
    return response


if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
