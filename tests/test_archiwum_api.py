"""Testy masowego pobrania archiwum z poziomu aplikacji.

Sieci nie ruszamy — archiwum podstawiamy atrapą. Sprawdzane jest to, co odróżnia tę funkcję
od zwykłego pobierania: że praca dzieli się na kroki mieszczące się w limicie żądania, że
kolejny krok podejmuje ją dokładnie tam, gdzie skończył poprzedni (także po „przeniesieniu"
na inną instancję), i że dopiero domknięty instrument trafia do biblioteki.
"""

from __future__ import annotations

import importlib
import lzma
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


MODULY = ("app.runtime", "app.storage", "app.library", "app.archiwum", "app.main")


def przeladuj():
    """Wczytuje moduły aplikacji od nowa, tak żeby wszystkie widziały ten sam komplet.

    Samo usunięcie z `sys.modules` nie wystarcza: `from . import storage` bierze atrybut
    pakietu, jeśli tam został, więc `app.main` dostawałby stary moduł, a test — nowy.
    Dlatego importujemy je jawnie, w kolejności zależności.
    """
    for nazwa in MODULY:
        sys.modules.pop(nazwa, None)
    return [importlib.import_module(nazwa) for nazwa in MODULY][-1]


@pytest.fixture
def app_modul(monkeypatch, tmp_path):
    """Świeża aplikacja z pustym magazynem w katalogu tymczasowym."""
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(tmp_path))
    module = przeladuj()
    sys.modules["app.storage"].reset()
    yield module
    for nazwa in MODULY:
        sys.modules.pop(nazwa, None)


@pytest.fixture
def client(app_modul, monkeypatch):
    """Klient z krótkim zakresem i zerowym budżetem kroku.

    Zakres skracamy do kilku dni, bo atrapa archiwum i tak nie sprawdza objętości, a rok
    to ponad osiem tysięcy plików na instrument. Zerowy budżet daje krok o znanej wielkości:
    pobieranie zawsze domyka jedną dobę, więc „wznawianie od kursora" da się sprawdzić
    bez zgadywania, gdzie akurat wypadnie granica.
    """
    archiwum = sys.modules["app.archiwum"]
    monkeypatch.setattr(archiwum, "zakres_lat", lambda lata: (date(2024, 1, 1), date(2024, 1, 5)))
    monkeypatch.setattr(app_modul, "_archive_budget", lambda: 0.0)
    return TestClient(app_modul.app)


DNI_W_ZAKRESIE = 5


def archiwum_atrapa(log: list | None = None, martwe: bool = False):
    """Godziny sesji mają ticki, reszta jest pusta."""
    import app.dukascopy as duka

    def _fetch(url: str):
        if log is not None:
            log.append(url)
        if martwe or "candles" in url:
            return b"" if not martwe else None
        godzina = int(url.rstrip(".bi5").split("/")[-1][:2])
        if not 8 <= godzina < 17:
            return b""
        # Każdy instrument musi mieć własne ceny: identyfikator zbioru wynika z treści,
        # więc dwa instrumenty o identycznych kwotowaniach byłyby jedną pozycją w bibliotece.
        baza = 100_000 + sum(url.split("/")[4].encode()) * 10
        raw = b"".join(duka.TICK_STRUCT.pack(m * 60_000, baza + m, baza + m - 1, 1.0, 1.0)
                       for m in range(60))
        comp = lzma.LZMACompressor(format=lzma.FORMAT_ALONE)
        return comp.compress(raw) + comp.flush()

    return _fetch


@pytest.fixture
def siec(monkeypatch):
    import app.dukascopy as duka
    monkeypatch.setattr(duka, "_fetch_hour", archiwum_atrapa())


def zacznij(client, **kwargs):
    body = {"instruments": ["GBPUSD"], "years": 1, "interval_minutes": 15, "price": "bid"}
    body.update(kwargs)
    return client.post("/api/archive/start", json=body)


def dokoncz(client, kroki: int = 40) -> dict:
    """Powtarza krok tak, jak robi to przeglądarka — aż plan przestanie biec."""
    for _ in range(kroki):
        dane = client.post("/api/archive/step").json()
        if dane["plan"]["state"] != "running":
            return dane
    raise AssertionError("plan nie domknął się w rozsądnej liczbie kroków")


# --- oszacowanie przed startem ---------------------------------------------------------


def test_the_estimate_costs_nothing_and_downloads_nothing(client, monkeypatch):
    import app.dukascopy as duka
    monkeypatch.setattr(duka, "_fetch_hour", lambda url: pytest.fail("oszacowanie nie pobiera"))

    dane = client.get("/api/archive/estimate?instruments=GBPUSD,EURUSD&years=10").json()
    assert dane["instruments"] == 2
    assert dane["files_total"] == dane["files_per_instrument"] * 2
    assert dane["seconds_slow"] > dane["seconds_fast"] > 0


def test_the_range_covers_the_requested_years(app_modul):
    archiwum = sys.modules["app.archiwum"]
    poczatek, koniec = archiwum.zakres_lat(10)
    assert 3640 <= (koniec - poczatek).days <= 3660


def test_the_range_never_predates_the_archive(app_modul):
    """Archiwum Dukascopy zaczyna się w 2003 — prośba o 50 lat nie może zejść niżej."""
    archiwum = sys.modules["app.archiwum"]
    assert archiwum.zakres_lat(50)[0] == date(2003, 1, 1)


def test_the_range_ends_yesterday(app_modul):
    """Dzisiejsze godziny bywają w archiwum jeszcze niegotowe."""
    archiwum = sys.modules["app.archiwum"]
    assert archiwum.zakres_lat(1)[1] == date.today() - timedelta(days=1)


# --- plan --------------------------------------------------------------------------


def test_nothing_is_running_before_the_first_start(client):
    assert client.get("/api/archive/status").json()["plan"] is None


def test_starting_lays_out_every_chosen_instrument(client):
    plan = zacznij(client, instruments=["GBPUSD", "EURUSD"]).json()["plan"]
    assert [p["code"] for p in plan["instruments"]] == ["GBPUSD", "EURUSD"]
    assert all(p["state"] == "pending" for p in plan["instruments"])
    assert plan["days_total"] == sum(p["days_total"] for p in plan["instruments"])


def test_an_unknown_instrument_is_refused_before_any_download(client):
    odp = zacznij(client, instruments=["GBPUSD", "NIEISTNIEJE"])
    assert odp.status_code == 400
    assert "NIEISTNIEJE" in odp.json()["detail"]
    assert client.get("/api/archive/status").json()["plan"] is None


@pytest.mark.parametrize("pole,wartosc", [
    ("years", 0), ("years", 99), ("interval_minutes", 7), ("price", "srednia"), ("instruments", []),
])
def test_nonsense_settings_are_refused(client, pole, wartosc):
    assert zacznij(client, **{pole: wartosc}).status_code == 400


def test_a_second_start_does_not_trample_a_running_plan(client, siec):
    zacznij(client)
    odp = zacznij(client)
    assert odp.status_code == 400
    assert "już trwa" in odp.json()["detail"]


def test_the_plan_survives_a_fresh_instance(client, app_modul, siec):
    """Sedno całej konstrukcji: kolejny krok może trafić na inną instancję."""
    zacznij(client)
    client.post("/api/archive/step")
    kursor = client.get("/api/archive/status").json()["plan"]["instruments"][0]["cursor"]

    swiezy = TestClient(importlib.reload(app_modul).app)     # pamięć procesu pusta
    assert swiezy.get("/api/archive/status").json()["plan"]["instruments"][0]["cursor"] == kursor


# --- wykonanie krokami --------------------------------------------------------------


def test_a_step_makes_progress_without_finishing_everything_at_once(client, siec):
    """Przy wyczerpanym budżecie krok domyka jedną dobę i oddaje sterowanie — dokładnie po to,
    żeby żadne pojedyncze żądanie nie przekroczyło limitu czasu platformy."""
    zacznij(client, years=1)
    dane = client.post("/api/archive/step").json()
    assert dane["plan"]["state"] == "running"
    assert 0 < dane["progress"]["days_done"] < dane["progress"]["days_total"]


def test_repeated_steps_finish_the_job(client, siec):
    zacznij(client, years=1)
    dane = dokoncz(client)
    assert dane["plan"]["state"] == "done"
    assert dane["progress"]["fraction"] == 1.0


def test_each_step_resumes_where_the_previous_one_stopped(client, siec):
    """Bez tego wieloletni zakres pobierałby w kółko ten sam początek."""
    zacznij(client, years=1)
    kursory = []
    for _ in range(3):
        dane = client.post("/api/archive/step").json()
        kursory.append(dane["plan"]["instruments"][0]["cursor"])
        if dane["plan"]["state"] != "running":
            break
    assert kursory == sorted(kursory)
    assert len(set(kursory)) == len(kursory)


def test_a_finished_instrument_lands_in_the_library(client, siec):
    library = sys.modules["app.library"]

    zacznij(client, years=1)
    dokoncz(client)

    wpisy = library.entries()
    assert len(wpisy) == 1
    assert "GBP/USD" in wpisy[0]["name"]
    assert "15 min" in wpisy[0]["name"]
    assert wpisy[0]["bars"] > 0
    assert wpisy[0]["interval_minutes"] == 15


def test_each_instrument_becomes_its_own_entry(client, siec):
    library = sys.modules["app.library"]

    zacznij(client, instruments=["GBPUSD", "EURUSD"], years=1)
    dokoncz(client, kroki=80)

    nazwy = [e["name"] for e in library.entries()]
    assert len(nazwy) == 2
    assert any("GBP/USD" in n for n in nazwy) and any("EUR/USD" in n for n in nazwy)


def test_the_saved_file_has_one_header_and_stays_in_order(client, siec):
    """Zbiór powstaje ze sklejonych kawałków — łatwo tu o powtórzony nagłówek albo bałagan."""
    library = sys.modules["app.library"]

    zacznij(client, years=1)
    dokoncz(client)

    tekst = library.get_text(library.entries()[0]["id"])
    wiersze = tekst.strip().split("\n")
    assert wiersze[0].startswith("time,")
    assert not [w for w in wiersze[1:] if w.startswith("time,")]
    czasy = [w.split(",")[0] for w in wiersze[1:]]
    assert czasy == sorted(czasy)


def test_the_scaffolding_is_cleaned_up_after_a_finished_instrument(client, siec, tmp_path):
    zacznij(client, years=1)
    dokoncz(client)
    assert not list((tmp_path / "datasets" / "archiwum").rglob("*.csv"))


def test_an_interval_other_than_the_default_is_respected(client, siec):
    library = sys.modules["app.library"]

    zacznij(client, years=1, interval_minutes=60)
    dokoncz(client)
    assert library.entries()[0]["interval_minutes"] == 60


# --- przerwanie i awarie -------------------------------------------------------------


def test_cancelling_stops_the_plan(client, siec):
    zacznij(client, years=1)
    client.post("/api/archive/step")
    assert client.post("/api/archive/cancel").json()["plan"]["state"] == "cancelled"
    assert client.post("/api/archive/step").json()["plan"]["state"] == "cancelled"


def test_cancelling_keeps_what_already_reached_the_library(client, siec):
    """Przerwanie ma zatrzymać dalszą pracę, a nie odebrać to, co się udało."""
    library = sys.modules["app.library"]

    zacznij(client, instruments=["GBPUSD", "EURUSD"], years=1)
    for _ in range(40):
        dane = client.post("/api/archive/step").json()
        if dane["plan"]["instruments"][0]["state"] == "done":
            break
    client.post("/api/archive/cancel")
    assert len(library.entries()) == 1


def test_cancelling_clears_the_unfinished_scaffolding(client, siec, tmp_path):
    zacznij(client, years=1)
    client.post("/api/archive/step")
    client.post("/api/archive/cancel")
    assert not list((tmp_path / "datasets" / "archiwum").rglob("*.csv"))


def test_cancelling_during_a_step_is_not_overwritten_by_that_step(client, siec):
    """Przerwanie prawie zawsze trafia w środek kroku — krok kończy się już po nim i nie może
    przywrócić planu do „w trakcie", bo pobieranie ruszyłoby dalej wbrew decyzji."""
    archiwum = sys.modules["app.archiwum"]
    zacznij(client, years=1)

    prawdziwy = archiwum._kawalek

    def kawalek_z_przerwaniem(plan, pozycja, koniec, cache_dir):
        prawdziwy(plan, pozycja, koniec, cache_dir)
        archiwum.przerwij()                 # użytkownik klika „Przerwij" w trakcie pobierania

    archiwum._kawalek = kawalek_z_przerwaniem
    try:
        dane = client.post("/api/archive/step").json()
    finally:
        archiwum._kawalek = prawdziwy

    assert dane["plan"]["state"] == "cancelled"
    assert client.get("/api/archive/status").json()["plan"]["state"] == "cancelled"


def test_cancelling_during_a_step_cleans_up_what_that_step_downloaded(client, siec, tmp_path):
    archiwum = sys.modules["app.archiwum"]
    zacznij(client, years=1)

    prawdziwy = archiwum._kawalek

    def kawalek_z_przerwaniem(plan, pozycja, koniec, cache_dir):
        prawdziwy(plan, pozycja, koniec, cache_dir)
        archiwum.przerwij()

    archiwum._kawalek = kawalek_z_przerwaniem
    try:
        client.post("/api/archive/step")
    finally:
        archiwum._kawalek = prawdziwy

    assert not list((tmp_path / "datasets" / "archiwum").rglob("*.csv"))


def test_a_new_plan_can_start_after_cancelling(client, siec):
    zacznij(client, years=1)
    client.post("/api/archive/cancel")
    assert zacznij(client, years=1).status_code == 200


def test_a_dead_archive_is_reported_instead_of_grinding_on(client, monkeypatch):
    import app.dukascopy as duka
    monkeypatch.setattr(duka, "_fetch_hour", archiwum_atrapa(martwe=True))

    zacznij(client, years=1)
    dane = client.post("/api/archive/step").json()
    pozycja = dane["plan"]["instruments"][0]
    assert pozycja["state"] == "error"
    assert pozycja["note"]
    assert dane["plan"]["state"] == "done"      # nie ma na czym stać w miejscu


def test_a_dead_archive_leaves_the_library_empty(client, monkeypatch):
    library = sys.modules["app.library"]
    import app.dukascopy as duka
    monkeypatch.setattr(duka, "_fetch_hour", archiwum_atrapa(martwe=True))

    zacznij(client, years=1)
    client.post("/api/archive/step")
    assert library.entries() == []


def test_forgetting_the_plan_starts_from_a_clean_slate(client, siec):
    zacznij(client, years=1)
    client.delete("/api/archive")
    assert client.get("/api/archive/status").json()["plan"] is None


def test_a_step_without_a_plan_says_so_instead_of_crashing(client):
    odp = client.post("/api/archive/step")
    assert odp.status_code == 400
    assert "Nie ma rozpoczętego" in odp.json()["detail"]


# --- postęp dla interfejsu -------------------------------------------------------------


def test_progress_reports_the_share_of_days_covered(client, siec):
    zacznij(client, instruments=["GBPUSD", "EURUSD"], years=1)
    dane = client.post("/api/archive/step").json()
    postep = dane["progress"]
    assert postep["instruments_total"] == 2
    assert 0 < postep["fraction"] < 1
    assert postep["days_total"] == sum(p["days_total"] for p in dane["plan"]["instruments"])


def test_the_repeat_run_reuses_the_cache_instead_of_the_network(client, siec, monkeypatch):
    """Powtórka po przerwaniu nie może pobierać drugi raz tego samego."""
    import app.dukascopy as duka

    zacznij(client, years=1)
    dokoncz(client)
    client.delete("/api/archive")

    drugi: list[str] = []
    monkeypatch.setattr(duka, "_fetch_hour", archiwum_atrapa(log=drugi))
    zacznij(client, years=1)
    dokoncz(client)

    assert not [u for u in drugi if "ticks" in u]
