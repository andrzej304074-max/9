"""Testy biblioteki zapisanych zbiorów danych.

Biblioteka ma jedno zadanie: raz wczytany plik ma dać się otworzyć ponownie bez
pobierania go z sieci i bez wysyłania z dysku. Testy pilnują, żeby opis zbioru był
prawdziwy, żeby nazwa nadana ręcznie przetrwała, a usunięcie faktycznie usuwało.
"""

from __future__ import annotations

import importlib
import sys

import pytest
from fastapi.testclient import TestClient

CSV = "time,open,high,low,close\n" + "\n".join(
    f"2024-01-0{d} 08:{m:02d}:00,1.2000,1.2010,1.1990,1.2005"
    for d in range(1, 4)
    for m in (0, 15, 30, 45)
)
INNY_CSV = CSV.replace("1.2005", "1.2007")


@pytest.fixture
def client(monkeypatch, tmp_path):
    """Świeża aplikacja z pustą biblioteką w katalogu tymczasowym."""
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(tmp_path))
    for name in ("app.runtime", "app.library", "app.main"):
        sys.modules.pop(name, None)
    module = importlib.import_module("app.main")
    yield TestClient(module.app)
    for name in ("app.runtime", "app.library", "app.main"):
        sys.modules.pop(name, None)


def wgraj(client, csv=CSV, nazwa="dane.csv"):
    return client.post("/api/upload", files={"file": (nazwa, csv, "text/csv")}).json()


def biblioteka(client):
    return client.get("/api/datasets").json()["datasets"]


# --- zapis i opis ------------------------------------------------------------------


def test_library_starts_empty(client):
    assert biblioteka(client) == []


def test_an_uploaded_file_lands_in_the_library(client):
    wgraj(client)
    wpisy = biblioteka(client)
    assert len(wpisy) == 1
    assert wpisy[0]["name"] == "dane.csv"


def test_the_entry_carries_a_usable_description(client):
    """Sama nazwa nie wystarcza — z listy ma być widać, co to za dane."""
    wgraj(client)
    wpis = biblioteka(client)[0]

    assert wpis["bars"] == 12
    assert wpis["interval_minutes"] == 15
    assert wpis["first_date"] == "2024-01-01"
    assert wpis["last_date"] == "2024-01-03"
    assert wpis["bytes"] > 0


def test_the_same_file_twice_is_one_entry(client):
    """Identyfikator wynika z treści, więc powtórka nie mnoży pozycji."""
    wgraj(client)
    wgraj(client)
    assert len(biblioteka(client)) == 1


def test_different_files_are_separate_entries(client):
    wgraj(client, CSV, "pierwszy.csv")
    wgraj(client, INNY_CSV, "drugi.csv")
    assert {e["name"] for e in biblioteka(client)} == {"pierwszy.csv", "drugi.csv"}


def test_demo_data_is_remembered_under_its_own_name(client):
    client.get("/api/sample")
    assert "demo" in biblioteka(client)[0]["name"]


# --- ponowne otwarcie --------------------------------------------------------------


def test_a_saved_dataset_opens_without_sending_the_file_again(client):
    """Sedno biblioteki: wracamy do danych bez ich ponownego przesyłania."""
    dataset_id = wgraj(client)["dataset_id"]
    response = client.post(f"/api/datasets/{dataset_id}/open")

    assert response.status_code == 200
    assert response.json()["bars"] == 12
    assert response.json()["dataset_id"] == dataset_id


def test_an_opened_dataset_can_be_backtested(client):
    dataset_id = wgraj(client)["dataset_id"]
    client.post(f"/api/datasets/{dataset_id}/open")
    wynik = client.post("/api/backtest", json={"dataset_id": dataset_id, "config": {}})
    assert wynik.status_code == 200


def test_opening_a_missing_dataset_says_so(client):
    assert client.post("/api/datasets/nieistniejacy/open").status_code == 404


def test_opening_moves_the_entry_to_the_top(client):
    """Lista układa się od ostatnio używanych, żeby nie szukać po całości."""
    pierwszy = wgraj(client, CSV, "pierwszy.csv")["dataset_id"]
    wgraj(client, INNY_CSV, "drugi.csv")
    assert biblioteka(client)[0]["name"] == "drugi.csv"

    client.post(f"/api/datasets/{pierwszy}/open")
    assert biblioteka(client)[0]["name"] == "pierwszy.csv"


# --- zmiana nazwy ------------------------------------------------------------------


def test_renaming_sticks(client):
    dataset_id = wgraj(client)["dataset_id"]
    assert client.patch(f"/api/datasets/{dataset_id}", json={"name": "GBPUSD 2024"}).status_code == 200
    assert biblioteka(client)[0]["name"] == "GBPUSD 2024"


def test_a_manual_name_survives_reloading_the_same_data(client):
    """Nazwa nadana ręcznie nie może zniknąć, gdy te same dane trafią ponownie."""
    dataset_id = wgraj(client)["dataset_id"]
    client.patch(f"/api/datasets/{dataset_id}", json={"name": "Moja nazwa"})
    wgraj(client)
    assert biblioteka(client)[0]["name"] == "Moja nazwa"


def test_an_empty_name_is_rejected(client):
    dataset_id = wgraj(client)["dataset_id"]
    assert client.patch(f"/api/datasets/{dataset_id}", json={"name": "   "}).status_code == 400
    assert biblioteka(client)[0]["name"] == "dane.csv"


# --- usuwanie i pobieranie ---------------------------------------------------------


def test_deleting_removes_the_entry_and_the_file(client, tmp_path):
    dataset_id = wgraj(client)["dataset_id"]
    assert client.delete(f"/api/datasets/{dataset_id}").status_code == 200

    assert biblioteka(client) == []
    assert not (tmp_path / "datasets" / f"{dataset_id}.csv").exists()


def test_a_deleted_dataset_cannot_be_opened(client):
    dataset_id = wgraj(client)["dataset_id"]
    client.delete(f"/api/datasets/{dataset_id}")
    assert client.post(f"/api/datasets/{dataset_id}/open").status_code == 404


def test_deleting_twice_reports_the_second_time(client):
    dataset_id = wgraj(client)["dataset_id"]
    client.delete(f"/api/datasets/{dataset_id}")
    assert client.delete(f"/api/datasets/{dataset_id}").status_code == 404


def test_csv_can_be_downloaded_back(client):
    """Trwałą kopię trzeba dać się wynieść — zwłaszcza przy ulotnym zapisie w chmurze."""
    dataset_id = wgraj(client)["dataset_id"]
    response = client.get(f"/api/datasets/{dataset_id}/csv")

    assert response.status_code == 200
    assert response.text == CSV
    assert "attachment" in response.headers["content-disposition"]


def test_the_download_filename_follows_the_entry_name(client):
    dataset_id = wgraj(client)["dataset_id"]
    client.patch(f"/api/datasets/{dataset_id}", json={"name": "GBPUSD 2024"})
    naglowek = client.get(f"/api/datasets/{dataset_id}/csv").headers["content-disposition"]
    assert "GBPUSD 2024.csv" in naglowek


def test_a_dangerous_name_cannot_escape_into_the_filename(client):
    """Nazwę nadaje użytkownik, więc nie może wstrzyknąć ścieżki ani cudzysłowu."""
    dataset_id = wgraj(client)["dataset_id"]
    client.patch(f"/api/datasets/{dataset_id}", json={"name": '../../etc/passwd"'})
    naglowek = client.get(f"/api/datasets/{dataset_id}/csv").headers["content-disposition"]

    assert ".." not in naglowek
    assert "/" not in naglowek.split("filename=")[1]


# --- odporność ---------------------------------------------------------------------


def test_a_corrupted_index_does_not_break_the_app(client, tmp_path):
    wgraj(client)
    (tmp_path / "datasets" / "index.json").write_text("{to nie jest JSON", encoding="utf-8")
    assert biblioteka(client) == []          # pusto zamiast wywrotki
    assert wgraj(client)["bars"] == 12       # i da się pracować dalej


def test_an_entry_without_its_file_is_hidden(client, tmp_path):
    dataset_id = wgraj(client)["dataset_id"]
    (tmp_path / "datasets" / f"{dataset_id}.csv").unlink()
    assert biblioteka(client) == []


def test_the_library_reports_whether_storage_survives(client):
    """Przy wdrożeniu bezserwerowym nie wolno obiecywać trwałego archiwum."""
    assert client.get("/api/datasets").json()["persistent"] is True


def test_serverless_storage_is_flagged_as_temporary(monkeypatch, tmp_path):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(tmp_path))
    for name in ("app.runtime", "app.library", "app.main"):
        sys.modules.pop(name, None)
    module = importlib.import_module("app.main")

    assert TestClient(module.app).get("/api/datasets").json()["persistent"] is False
    for name in ("app.runtime", "app.library", "app.main"):
        sys.modules.pop(name, None)


def test_the_compression_suffix_does_not_leak_into_the_name(client):
    """Front pakuje plik gzipem i dokleja .gz — w bibliotece ma być nazwa z dysku."""
    import gzip

    response = client.post(
        "/api/upload",
        files={"file": ("moj_eksport.csv.gz", gzip.compress(CSV.encode()), "application/gzip")},
        data={"encoding": "gzip"},
    )
    assert response.status_code == 200
    assert biblioteka(client)[0]["name"] == "moj_eksport.csv"
