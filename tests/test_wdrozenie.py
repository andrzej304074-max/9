"""Testy integralności wdrożenia.

Powstały po zgłoszeniu „na adresie z Vercela zamiast aplikacji widzę surowy tekst".
Przyczyną był brak plików frontu w paczce funkcji: runtime Pythona pakuje tylko to, co
wyśledzi po importach, a `web/index.html` modułem Pythona nie jest. Bez katalogu `web/`
statyka nie montuje się wcale i `GET /` kończy się gołym `{"detail":"Not Found"}`.

Testy pilnują trzech rzeczy naraz: że konfiguracja wdrożenia nadal dokłada te pliki,
że ich brak tłumaczy się sam zamiast udawać awarię całej aplikacji, i że aplikacja
w ogóle wstaje na systemie plików tylko do odczytu.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

MODULY = ("app.runtime", "app.storage", "app.library", "app.archiwum", "app.main")


def zaladuj(monkeypatch, katalog_stanu: Path, korzen: Path | None = None):
    """Ładuje serwer od nowa, opcjonalnie z podstawionym katalogiem głównym projektu.

    Podmiana korzenia to jedyny sposób, żeby odtworzyć wdrożenie bez plików frontu —
    decyzja o zamontowaniu statyki zapada przy imporcie, a nie przy żądaniu.
    """
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(katalog_stanu))
    for nazwa in MODULY:
        sys.modules.pop(nazwa, None)

    runtime = importlib.import_module("app.runtime")
    if korzen is not None:
        monkeypatch.setattr(runtime, "BASE_DIR", korzen)
    return [importlib.import_module(n) for n in MODULY][-1]


@pytest.fixture(autouse=True)
def sprzataj():
    yield
    for nazwa in MODULY:
        sys.modules.pop(nazwa, None)


# --- konfiguracja wdrożenia ----------------------------------------------------------


@pytest.fixture(scope="module")
def wdrozenie() -> dict:
    return json.loads((REPO / "vercel.json").read_text(encoding="utf-8"))


def test_the_frontend_is_served_as_static_files(wdrozenie):
    """Strona nie może zależeć od tego, czy funkcja Pythona wstanie.

    Wcześniej wszystko szło przez funkcję, więc dowolny jej problem — brak pliku w paczce,
    wyjątek przy imporcie — kończył się nie stroną z błędem, tylko surowym tekstem zamiast
    aplikacji. Statyka idzie teraz z CDN-u, niezależnie od Pythona.
    """
    assert wdrozenie["outputDirectory"] == "web"


def test_only_the_api_goes_through_the_function(wdrozenie):
    assert wdrozenie["rewrites"] == [{"source": "/api/(.*)", "destination": "/api/index"}]


def test_the_function_still_carries_the_files_it_needs(wdrozenie):
    """Runtime Pythona pakuje tylko to, co wyśledzi po importach — reszta musi być jawnie.

    `app/` bywa niewidoczne dla śledzenia, bo import idzie po ręcznym dopisaniu ścieżki,
    a `data/` z danymi demo nie jest kodem. Front zostaje w paczce jako druga droga do
    działającej strony, gdyby statyka z jakiegokolwiek powodu nie zadziałała.
    """
    wzorzec = wdrozenie["functions"]["api/index.py"]["includeFiles"]
    for katalog in ("web", "data", "app"):
        assert katalog in wzorzec, katalog


def test_the_frontend_files_are_actually_in_the_repository():
    """Najlepsza konfiguracja nie pomoże, jeśli plików nie ma czego dołożyć."""
    for nazwa in ("index.html", "style.css", "app.js"):
        assert (REPO / "web" / nazwa).is_file(), nazwa


def test_the_entry_point_can_find_the_application_package():
    """Funkcja startuje z katalogu `api/`, więc korzeń projektu musi trafić na ścieżkę
    importu — inaczej cold start kończy się `ModuleNotFoundError` i błędem platformy."""
    zrodlo = (REPO / "api" / "index.py").read_text(encoding="utf-8")
    assert "sys.path.insert" in zrodlo
    assert zrodlo.index("sys.path.insert") < zrodlo.index("from app.main import app")


def test_the_frontend_asks_for_the_api_relative_to_the_page():
    """Adresy bezwzględne rozjechałyby się przy serwowaniu strony z CDN-u."""
    zrodlo = (REPO / "web" / "app.js").read_text(encoding="utf-8")
    assert "'/api/" not in zrodlo and '"/api/' not in zrodlo


# --- brak plików frontu --------------------------------------------------------------


def test_a_deployment_without_the_frontend_explains_itself(monkeypatch, tmp_path):
    """Zamiast `{"detail":"Not Found"}` — zdanie o tym, co się stało i co z tym zrobić."""
    from fastapi.testclient import TestClient

    module = zaladuj(monkeypatch, tmp_path / "stan", korzen=tmp_path / "bez-frontu")
    odp = TestClient(module.app).get("/")

    assert odp.status_code == 503
    assert "text/html" in odp.headers["content-type"]
    assert "includeFiles" in odp.text
    assert "web/index.html" in odp.text


def test_the_api_keeps_working_without_the_frontend(monkeypatch, tmp_path):
    """Brakuje interfejsu, nie aplikacji — i tak ma to wynikać z zachowania serwera."""
    from fastapi.testclient import TestClient

    module = zaladuj(monkeypatch, tmp_path / "stan", korzen=tmp_path / "bez-frontu")
    assert TestClient(module.app).get("/api/options").status_code == 200


def test_diagnostics_names_the_missing_frontend(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    module = zaladuj(monkeypatch, tmp_path / "stan", korzen=tmp_path / "bez-frontu")
    dane = TestClient(module.app).get("/api/diagnostics").json()

    assert dane["web_dir_present"] is False
    assert dane["frontend_ready"] is False
    assert dane["web_files"] == []


def test_diagnostics_confirms_a_complete_deployment(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    module = zaladuj(monkeypatch, tmp_path / "stan")
    dane = TestClient(module.app).get("/api/diagnostics").json()

    assert dane["web_dir_present"] is True
    assert dane["frontend_ready"] is True
    assert dane["python"].startswith("3.")
    assert dane["storage"]["backend"]


def test_a_complete_deployment_serves_the_page(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    module = zaladuj(monkeypatch, tmp_path / "stan")
    odp = TestClient(module.app).get("/")

    assert odp.status_code == 200
    assert "text/html" in odp.headers["content-type"]
    assert "Backtester" in odp.text


# --- system plików tylko do odczytu ---------------------------------------------------


def nie_do_zapisu(tmp_path: Path) -> Path:
    """Ścieżka, w której zapis nie ma prawa się udać.

    Nie używamy do tego uprawnień: testy potrafią biec z konta roota, które kontrolę
    uprawnień omija, więc `chmod` niczego by nie udowodnił. Katalog „wewnątrz pliku"
    jest niemożliwy dla każdego użytkownika tak samo.
    """
    blokada = tmp_path / "to-jest-plik"
    blokada.write_text("nie katalog", encoding="utf-8")
    return blokada / "dane"


def test_a_read_only_project_directory_does_not_stop_the_app(monkeypatch, tmp_path):
    """Gdyby rozpoznanie środowiska zawiodło, zapis poszedłby do katalogu projektu.

    Na platformie bezserwerowej jest on tylko do odczytu, a moduł wczytuje się przy starcie
    funkcji — wyjątek położyłby całą aplikację, zanim zdążyłaby cokolwiek powiedzieć.
    """
    from fastapi.testclient import TestClient

    korzen = nie_do_zapisu(tmp_path).parent          # katalog projektu, w którym nic nie powstanie
    monkeypatch.delenv("BACKTESTER_STATE_DIR", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    for nazwa in MODULY:
        sys.modules.pop(nazwa, None)

    runtime = importlib.import_module("app.runtime")
    monkeypatch.setattr(runtime, "BASE_DIR", korzen)
    module = [importlib.import_module(n) for n in MODULY][-1]

    assert TestClient(module.app).get("/api/options").status_code == 200
    assert runtime.state_dir() != korzen / "data"
    assert runtime.state_dir().is_dir()


def test_a_directory_that_cannot_be_written_is_not_chosen_for_state(monkeypatch, tmp_path):
    niemozliwy = nie_do_zapisu(tmp_path)
    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(niemozliwy))
    sys.modules.pop("app.runtime", None)
    runtime = importlib.import_module("app.runtime")

    wybrany = runtime.state_dir()
    assert wybrany != niemozliwy
    assert wybrany.is_dir()


def test_the_requested_directory_wins_when_it_works(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(tmp_path / "moj-katalog"))
    sys.modules.pop("app.runtime", None)
    runtime = importlib.import_module("app.runtime")
    assert runtime.state_dir() == tmp_path / "moj-katalog"


def test_the_choice_follows_the_setting_changing(monkeypatch, tmp_path):
    """Wynik jest zapamiętywany, ale związany z ustawieniem, z którego wynika."""
    sys.modules.pop("app.runtime", None)
    runtime = importlib.import_module("app.runtime")

    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(tmp_path / "pierwszy"))
    assert runtime.state_dir() == tmp_path / "pierwszy"
    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(tmp_path / "drugi"))
    assert runtime.state_dir() == tmp_path / "drugi"


@pytest.mark.parametrize("zmienna", ["VERCEL", "VERCEL_ENV", "AWS_LAMBDA_FUNCTION_NAME", "LAMBDA_TASK_ROOT"])
def test_every_serverless_marker_is_recognised(monkeypatch, zmienna):
    """Pomyłka w tę stronę oznacza próbę zapisu tam, gdzie się nie da."""
    for inna in ("VERCEL", "VERCEL_ENV", "AWS_LAMBDA_FUNCTION_NAME", "LAMBDA_TASK_ROOT"):
        monkeypatch.delenv(inna, raising=False)
    monkeypatch.setenv(zmienna, "1")
    sys.modules.pop("app.runtime", None)
    assert importlib.import_module("app.runtime").IS_SERVERLESS is True
