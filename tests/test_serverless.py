"""Testy przystosowania do wdrożenia bezserwerowego.

Sprawdzają zachowania, które na platformie w rodzaju Vercela decydują o tym, czy aplikacja
w ogóle działa: zapis poza katalogiem projektu, przyjmowanie skompresowanych plików,
sygnalizowanie utraty stanu i limit długości pobierania z Dukascopy.
"""

from __future__ import annotations

import gzip
import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_CSV = "time,open,high,low,close\n" + "\n".join(
    f"2024-01-0{d} 08:{m:02d}:00,1.2000,1.2010,1.1990,1.2005"
    for d in range(1, 4)
    for m in (0, 15, 30, 45)
)


def load_app(monkeypatch, tmp_path: Path, *, serverless: bool, **env: str):
    """Ładuje serwer od nowa z zadanym środowiskiem.

    Moduły czytają zmienne środowiskowe w chwili importu, więc trzeba je przeładować —
    inaczej test zobaczyłby ustawienia poprzedniego."""
    if serverless:
        monkeypatch.setenv("VERCEL", "1")
    else:
        monkeypatch.delenv("VERCEL", raising=False)
        monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(tmp_path))
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    for name in ("app.runtime", "app.main"):
        sys.modules.pop(name, None)
    return importlib.import_module("app.main")


@pytest.fixture
def serverless_app(monkeypatch, tmp_path):
    module = load_app(monkeypatch, tmp_path, serverless=True)
    yield module, TestClient(module.app)
    for name in ("app.runtime", "app.main"):
        sys.modules.pop(name, None)


# --- zapis poza katalogiem projektu ------------------------------------------------


def test_uploads_land_in_the_writable_directory(serverless_app, tmp_path):
    """Na Vercelu katalog projektu jest tylko do odczytu — zapis musi iść gdzie indziej."""
    _, client = serverless_app
    response = client.post("/api/upload", files={"file": ("dane.csv", SAMPLE_CSV, "text/csv")})

    assert response.status_code == 200
    assert list((tmp_path / "uploads").glob("*.csv"))
    assert not (REPO_ROOT / "data" / "uploads").exists() or not list(
        (REPO_ROOT / "data" / "uploads").glob(f"{response.json()['dataset_id']}.csv")
    )


def test_a_read_only_directory_does_not_break_the_upload(monkeypatch, tmp_path):
    """Gdyby nawet /tmp odmówił zapisu, dane w pamięci procesu wystarczą do policzenia wyniku."""
    module = load_app(monkeypatch, tmp_path, serverless=True)
    monkeypatch.setattr(
        Path, "write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only"))
    )
    client = TestClient(module.app)

    assert client.post("/api/upload", files={"file": ("d.csv", SAMPLE_CSV, "text/csv")}).status_code == 200


# --- kompresja w drodze na serwer --------------------------------------------------


def test_gzipped_upload_is_accepted(serverless_app):
    _, client = serverless_app
    packed = gzip.compress(SAMPLE_CSV.encode())
    response = client.post(
        "/api/upload",
        files={"file": ("dane.csv.gz", packed, "application/gzip")},
        data={"encoding": "gzip"},
    )
    assert response.status_code == 200
    assert response.json()["bars"] == 12


def test_gzip_is_detected_without_the_hint(serverless_app):
    """Nagłówek bywa gubiony po drodze, więc rozpoznajemy też sam podpis pliku."""
    _, client = serverless_app
    packed = gzip.compress(SAMPLE_CSV.encode())
    response = client.post("/api/upload", files={"file": ("dane.gz", packed, "application/gzip")})
    assert response.status_code == 200
    assert response.json()["bars"] == 12


def test_compressed_and_plain_uploads_give_the_same_dataset(serverless_app):
    _, client = serverless_app
    plain = client.post("/api/upload", files={"file": ("d.csv", SAMPLE_CSV, "text/csv")})
    packed = client.post(
        "/api/upload",
        files={"file": ("d.csv.gz", gzip.compress(SAMPLE_CSV.encode()), "application/gzip")},
        data={"encoding": "gzip"},
    )
    assert plain.json()["dataset_id"] == packed.json()["dataset_id"]


def test_broken_archive_is_reported_clearly(serverless_app):
    _, client = serverless_app
    response = client.post(
        "/api/upload",
        files={"file": ("d.gz", b"\x1f\x8bpopsute", "application/gzip")},
        data={"encoding": "gzip"},
    )
    assert response.status_code == 400
    assert "rozpakować" in response.json()["detail"]


def test_upload_limit_is_tighter_when_serverless(monkeypatch, tmp_path):
    module = load_app(monkeypatch, tmp_path, serverless=True)
    assert module.MAX_UPLOAD_BYTES == 4 * 1024 * 1024

    module = load_app(monkeypatch, tmp_path, serverless=False)
    assert module.MAX_UPLOAD_BYTES == 64 * 1024 * 1024


# --- utrata stanu między instancjami -----------------------------------------------


def test_unknown_dataset_returns_409_so_the_client_can_recover(serverless_app):
    """404 znaczyłoby „nie ma i nie będzie”; 409 mówi frontowi: wyślij dane jeszcze raz."""
    _, client = serverless_app
    response = client.post("/api/backtest", json={"dataset_id": "nieistniejacy", "config": {}})
    assert response.status_code == 409
    assert "jeszcze raz" in response.json()["detail"]


def test_compare_also_signals_a_lost_dataset(serverless_app):
    _, client = serverless_app
    response = client.post(
        "/api/compare",
        json={"dataset_id": "nieistniejacy", "configs": {"candle_direction": {}}},
    )
    assert response.status_code == 409


def test_dataset_survives_within_one_instance(serverless_app):
    """W obrębie tej samej instancji dane mają przetrwać — /tmp służy jako pamięć podręczna."""
    module, client = serverless_app
    dataset_id = client.post(
        "/api/upload", files={"file": ("d.csv", SAMPLE_CSV, "text/csv")}
    ).json()["dataset_id"]

    module._DATASETS.clear()          # symulacja: proces stracił dane z pamięci
    module._PARSE_CACHE.clear()

    assert client.post("/api/backtest", json={"dataset_id": dataset_id, "config": {}}).status_code == 200


# --- Dukascopy w środowisku z limitem czasu ----------------------------------------


def test_dukascopy_range_is_capped_when_serverless(serverless_app):
    _, client = serverless_app
    response = client.post(
        "/api/dukascopy/start", json={"date_from": "2020-01-01", "date_to": "2024-12-31"}
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "odcinkami" in detail           # wskazówka, jak pobrać dłuższy zakres
    assert "1827" in detail                # ile dni faktycznie wybrano


def test_the_cap_scales_with_the_time_budget(monkeypatch, tmp_path):
    module = load_app(monkeypatch, tmp_path, serverless=True, BACKTESTER_MAX_SECONDS="300")
    from app.runtime import dukascopy_day_limit

    assert dukascopy_day_limit() > 300      # plan Pro pozwala na wyraźnie więcej


def test_local_runs_have_no_day_limit(monkeypatch, tmp_path):
    load_app(monkeypatch, tmp_path, serverless=False)
    from app.runtime import dukascopy_day_limit

    assert dukascopy_day_limit() == 0


def test_short_range_passes_the_check(serverless_app, monkeypatch):
    """Sam limit nie może blokować rozsądnych zakresów — pobieranie podmieniamy atrapą."""
    module, client = serverless_app
    monkeypatch.setattr(
        module, "_run_dukascopy_job", lambda job_id, _req: module._job_update(job_id, state="done", dataset={})
    )
    response = client.post(
        "/api/dukascopy/start", json={"date_from": "2024-01-01", "date_to": "2024-01-05"}
    )
    assert response.status_code == 200
    # wynik wraca od razu, bez odpytywania o postęp
    assert response.json()["state"] == "done"


# --- pobieranie odcinkami ----------------------------------------------------------


def fake_bars(start: str, count: int):
    """Świece godzinowe od podanej daty — wystarczą, żeby sprawdzić sklejanie."""
    from datetime import datetime, timedelta, timezone as tz

    from app.csv_loader import Bar

    first = datetime.fromisoformat(start).replace(tzinfo=tz.utc)
    return [
        Bar(ts=first + timedelta(hours=i), open=1.2, high=1.21, low=1.19, close=1.205, volume=0.0)
        for i in range(count)
    ]


def test_chunk_returns_csv_with_a_header(serverless_app, monkeypatch):
    module, client = serverless_app
    monkeypatch.setattr(module, "download_bars", lambda **kw: fake_bars("2024-01-01", 3))

    response = client.post(
        "/api/dukascopy/chunk", json={"date_from": "2024-01-01", "date_to": "2024-01-02"}
    )
    assert response.status_code == 200
    assert response.json()["bars"] == 3
    assert response.json()["csv"].startswith("time,open,high,low,close")


def test_chunk_can_skip_the_header_for_continuation(serverless_app, monkeypatch):
    """Kolejne odcinki dokleja się do pierwszego, więc nagłówek może być tylko jeden."""
    module, client = serverless_app
    monkeypatch.setattr(module, "download_bars", lambda **kw: fake_bars("2024-01-03", 3))

    body = {"date_from": "2024-01-03", "date_to": "2024-01-04", "with_header": False}
    csv_text = client.post("/api/dukascopy/chunk", json=body).json()["csv"]

    assert not csv_text.startswith("time,")
    assert csv_text.startswith("2024-01-03")
    assert len(csv_text.strip().split("\n")) == 3


def test_glued_chunks_parse_as_one_dataset(serverless_app, monkeypatch):
    """Sedno pomysłu: odcinki sklejone w przeglądarce muszą dać poprawny plik."""
    module, client = serverless_app
    pieces = []
    for index, (start, day) in enumerate([("2024-01-01", "2024-01-01"), ("2024-01-02", "2024-01-02")]):
        monkeypatch.setattr(module, "download_bars", lambda _s=start, **kw: fake_bars(_s, 24))
        pieces.append(
            client.post(
                "/api/dukascopy/chunk",
                json={"date_from": day, "date_to": day, "with_header": index == 0},
            ).json()["csv"]
        )

    merged = "".join(pieces)
    uploaded = client.post("/api/upload", files={"file": ("scalone.csv", merged, "text/csv")})

    assert uploaded.status_code == 200
    assert uploaded.json()["bars"] == 48          # oba odcinki, bez zgubionych wierszy
    assert uploaded.json()["interval_minutes"] == 60


def test_empty_chunk_glues_cleanly(serverless_app, monkeypatch):
    """Weekend nie ma notowań — pusty odcinek nie może popsuć sklejenia."""
    module, client = serverless_app
    monkeypatch.setattr(module, "download_bars", lambda **kw: [])

    csv_text = client.post(
        "/api/dukascopy/chunk",
        json={"date_from": "2024-01-06", "date_to": "2024-01-07", "with_header": False},
    ).json()["csv"]
    assert csv_text == ""


def test_chunk_longer_than_the_budget_is_rejected(serverless_app):
    _, client = serverless_app
    response = client.post(
        "/api/dukascopy/chunk", json={"date_from": "2020-01-01", "date_to": "2024-12-31"}
    )
    assert response.status_code == 400
    assert "odcinek" in response.json()["detail"].lower()


def test_chunk_validates_the_date_order(serverless_app):
    _, client = serverless_app
    response = client.post(
        "/api/dukascopy/chunk", json={"date_from": "2024-03-01", "date_to": "2024-02-01"}
    )
    assert response.status_code == 400


# --- informacja dla frontu ---------------------------------------------------------


def test_options_expose_the_runtime_limits(serverless_app):
    _, client = serverless_app
    runtime = client.get("/api/options").json()["runtime"]
    assert runtime["serverless"] is True
    assert runtime["dukascopy_max_days"] > 0
    assert runtime["max_upload_bytes"] == 4 * 1024 * 1024


def test_api_responses_are_never_cached(serverless_app):
    """Odpowiedź API na CDN-ie byłaby katastrofą — każdy dostałby cudzy backtest."""
    _, client = serverless_app
    assert client.get("/api/options").headers["cache-control"] == "no-store"
