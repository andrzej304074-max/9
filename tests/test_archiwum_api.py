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
    """Nieosiągalne archiwum musi się skończyć błędem — ale dopiero po serii odmów,
    a nie przy pierwszej. Pojedyncza zła doba to co innego niż awaria."""
    import app.dukascopy as duka
    monkeypatch.setattr(duka, "_fetch_hour", archiwum_atrapa(martwe=True))

    zacznij(client, years=1)
    dane = dokoncz(client)
    pozycja = dane["plan"]["instruments"][0]
    assert pozycja["state"] == "error"
    assert pozycja["note"]
    assert dane["plan"]["state"] == "done"      # nie ma na czym stać w miejscu


def test_one_refused_day_does_not_kill_the_instrument(client, monkeypatch):
    """Zgłoszenie z wdrożenia: pobieranie przewracało się na jednej dobie i zostawiało
    instrument w stanie błędu na zawsze — łącznie z komunikatem zapisanym w planie.

    Archiwum bywa niedostępne przez jedną dobę i to normalne. Taka doba ma zostać pominięta,
    a reszta zakresu pobrana.
    """
    library = sys.modules["app.library"]
    import app.dukascopy as duka

    dziala = archiwum_atrapa()

    def kapryśne(url: str):
        # 3 stycznia archiwum odmawia całkowicie — również dla plików ze świecami
        return None if "/2024/00/03/" in url else dziala(url)

    monkeypatch.setattr(duka, "_fetch_hour", kapryśne)
    monkeypatch.setattr(duka, "DEAD_DAY_PAUSE", 0)

    zacznij(client, years=1)
    dane = dokoncz(client)

    pozycja = dane["plan"]["instruments"][0]
    assert pozycja["state"] == "done"
    assert pozycja["skipped_days"] >= 1
    assert library.entries()[0]["bars"] > 0
    # Notatka mówi o ominiętej dobie, a nie o awarii pobierania — surowy komunikat błędu
    # zostaje wyczyszczony, gdy dane znów popłyną.
    assert "Pominięto 1 dobę" in pozycja["note"]
    assert "ani jednego pliku" not in pozycja["note"]


def test_a_weekend_at_the_end_of_the_range_is_not_a_failure(client, monkeypatch):
    """Zakres kończący się w sobotę nie ma na końcu ani jednej godziny handlu. Archiwum
    odmawia takiego okna, ale to koniec roboty, a nie awaria — instrument ma się domknąć
    z kompletnym licznikiem dni, bez ani jednej „pominiętej" doby.
    """
    archiwum = sys.modules["app.archiwum"]
    import app.dukascopy as duka

    monkeypatch.setattr(duka, "_fetch_hour", archiwum_atrapa())
    # 6 stycznia 2024 to sobota — ostatni dzień zakresu nie ma godzin handlowych.
    monkeypatch.setattr(archiwum, "zakres_lat",
                        lambda lata: (date(2024, 1, 1), date(2024, 1, 6)))

    zacznij(client, years=1)
    dane = dokoncz(client)

    pozycja = dane["plan"]["instruments"][0]
    assert pozycja["state"] == "done"
    assert pozycja["skipped_days"] == 0
    assert not pozycja["note"]
    assert pozycja["days_done"] == pozycja["days_total"]     # pasek dochodzi do 100%
    assert dane["progress"]["days_done"] == dane["progress"]["days_total"]


def martwe_doby(martwe: set[str]):
    """Archiwum, w którym wskazane doby nie oddają ani jednego pliku."""
    dziala = archiwum_atrapa()

    def _fetch(url: str):
        if "candles" in url:
            return b""
        czesci = url.split("/")
        rok, miesiac, dzien = czesci[-4], czesci[-3], czesci[-2]
        if f"{rok}-{int(miesiac) + 1:02d}-{dzien}" in martwe:
            return None
        return dziala(url)
    return _fetch


def test_a_dead_day_in_the_middle_does_not_kill_the_instrument(client, monkeypatch):
    """Zgłoszenie z wdrożenia: pobieranie dziesięciu lat przewracało się na jednej dobie.

    Odcinek zaczyna się od dowolnego dnia, więc martwa doba wypadała na jego początku —
    a wtedy „nic się nie udało" znaczyło „archiwum nieosiągalne", mimo lat już ściągniętych.
    """
    library = sys.modules["app.library"]
    import app.dukascopy as duka

    monkeypatch.setattr(duka, "_fetch_hour", martwe_doby({"2024-01-03"}))
    monkeypatch.setattr(duka, "DEAD_DAY_PAUSE", 0)

    zacznij(client, years=1)
    dane = dokoncz(client)

    pozycja = dane["plan"]["instruments"][0]
    assert pozycja["state"] == "done"
    assert pozycja["skipped_days"] == 1
    assert "Pominięto 1 dobę" in pozycja["note"]
    assert library.entries()[0]["bars"] > 0


def test_the_gap_note_names_why_the_files_did_not_come(client, monkeypatch):
    """„Pominięto 1 dobę" bez powodu nie da się na nic zamienić: limit żądań mija sam,
    blokada adresu wymaga pobrania danych lokalnie. Powód ma stać przy dziurze."""
    import app.dukascopy as duka

    braki = martwe_doby({"2024-01-03"})

    def z_powodem(url: str):
        wynik = braki(url)
        if wynik is None:
            duka.POWODY.zglos("odpowiedź HTTP 429")
        return wynik

    monkeypatch.setattr(duka, "_fetch_hour", z_powodem)
    monkeypatch.setattr(duka, "DEAD_DAY_PAUSE", 0)

    zacznij(client, years=1)
    pozycja = dokoncz(client)["plan"]["instruments"][0]
    assert pozycja["state"] == "done"
    assert "HTTP 429" in pozycja["note"]


def test_a_gap_is_named_in_the_saved_entry(client, monkeypatch):
    """Zbiór z dziurą trafia do biblioteki, ale użytkownik ma o niej wiedzieć."""
    import app.dukascopy as duka

    monkeypatch.setattr(duka, "_fetch_hour", martwe_doby({"2024-01-03", "2024-01-04"}))
    monkeypatch.setattr(duka, "DEAD_DAY_PAUSE", 0)

    zacznij(client, years=1)
    pozycja = dokoncz(client)["plan"]["instruments"][0]
    assert pozycja["skipped_days"] == 2
    assert "Powtórz pobranie" in pozycja["note"]


def test_an_archive_that_stops_responding_ends_the_instrument_with_a_reason(client, monkeypatch):
    """Blokada w trakcie to nie powód, żeby mielić przez lata na pusto."""
    import app.dukascopy as duka

    monkeypatch.setattr(duka, "_fetch_hour", martwe_doby({f"2024-01-{d:02d}" for d in range(3, 6)}))
    monkeypatch.setattr(duka, "DEAD_DAY_PAUSE", 0)
    monkeypatch.setattr(sys.modules["app.archiwum"], "MAX_BEZOWOCNYCH", 1)

    zacznij(client, years=1)
    pozycja = dokoncz(client)["plan"]["instruments"][0]
    assert pozycja["state"] == "error"
    assert "limit żądań" in pozycja["note"]


def test_a_dead_archive_leaves_the_library_empty(client, monkeypatch):
    library = sys.modules["app.library"]
    import app.dukascopy as duka
    monkeypatch.setattr(duka, "_fetch_hour", archiwum_atrapa(martwe=True))

    zacznij(client, years=1)
    client.post("/api/archive/step")
    assert library.entries() == []


# --- powtórka po nieudanym podejściu ---------------------------------------------------


def zepsuj_plan(client, wersja=None):
    """Plan z jednym instrumentem w stanie błędu, tak jak zostawiłby go starszy kod."""
    import json

    storage = sys.modules["app.storage"]
    archiwum = sys.modules["app.archiwum"]

    plan = json.loads(storage.active().read(archiwum.PLAN_KEY))
    plan["state"] = "done"
    plan["instruments"][0].update(
        state="error",
        note="Nie udało się pobrać z Dukascopy ani jednego pliku (3 prób dla dnia 2016-08-07).",
    )
    if wersja is None:
        plan.pop("wersja", None)
    else:
        plan["wersja"] = wersja
    storage.active().write(archiwum.PLAN_KEY, json.dumps(plan))
    return plan


def test_an_error_recorded_by_an_older_build_gets_a_second_chance(client, siec):
    """Sedno zgłoszenia z wdrożenia: plan przeżywa wdrożenie, a jego notatka o błędzie
    pochodzi z kodu, który przerywał po trzech nieudanych plikach — czyli ginął na niedzieli
    (ma w archiwum tylko trzy godziny). Nowe zasady mają dać takiemu instrumentowi drugą
    szansę same z siebie, bez klikania czegokolwiek.
    """
    zacznij(client, years=1)
    zepsuj_plan(client)

    plan = client.get("/api/archive/status").json()["plan"]
    assert plan["instruments"][0]["state"] == "pending"
    assert not plan["instruments"][0]["note"]
    assert plan["state"] == "running"       # przeglądarka podejmie pracę po odświeżeniu
    assert plan["wersja"] == sys.modules["app.archiwum"].WERSJA_PLANU


def test_a_plan_from_the_current_build_is_left_alone(client, siec):
    """Błąd zapisany przez ten sam kod jest prawdziwym wynikiem — nie kasujemy go po cichu."""
    archiwum = sys.modules["app.archiwum"]
    zacznij(client, years=1)
    zepsuj_plan(client, wersja=archiwum.WERSJA_PLANU)

    plan = client.get("/api/archive/status").json()["plan"]
    assert plan["instruments"][0]["state"] == "error"
    assert plan["state"] == "done"


def test_a_cancelled_plan_is_not_resurrected_by_a_deployment(client, siec):
    """Przerwanie to decyzja użytkownika — wdrożenie nie może jej cofnąć."""
    import json

    storage = sys.modules["app.storage"]
    archiwum = sys.modules["app.archiwum"]

    zacznij(client, years=1)
    zepsuj_plan(client)
    plan = json.loads(storage.active().read(archiwum.PLAN_KEY))
    plan["state"] = "cancelled"
    plan.pop("wersja", None)
    storage.active().write(archiwum.PLAN_KEY, json.dumps(plan))

    assert client.get("/api/archive/status").json()["plan"]["state"] == "cancelled"


def test_retrying_resumes_the_failed_instrument_from_its_cursor(client, monkeypatch):
    """„Ponów nieudane" nie zaczyna od zera — kursor pamięta pierwszy niepobrany dzień."""
    import app.dukascopy as duka
    archiwum = sys.modules["app.archiwum"]
    library = sys.modules["app.library"]

    monkeypatch.setattr(duka, "_fetch_hour", martwe_doby({f"2024-01-{d:02d}" for d in range(3, 6)}))
    monkeypatch.setattr(duka, "DEAD_DAY_PAUSE", 0)
    monkeypatch.setattr(archiwum, "MAX_BEZOWOCNYCH", 1)

    zacznij(client, years=1)
    pozycja = dokoncz(client)["plan"]["instruments"][0]
    assert pozycja["state"] == "error"
    kursor, dni = pozycja["cursor"], pozycja["days_done"]

    # Archiwum wraca do życia — powtórka ma dociągnąć resztę i zapisać zbiór.
    monkeypatch.setattr(duka, "_fetch_hour", archiwum_atrapa())
    wznowiony = client.post("/api/archive/retry").json()["plan"]["instruments"][0]
    assert wznowiony["state"] == "pending"
    assert wznowiony["cursor"] == kursor            # bez cofania się do początku
    assert wznowiony["days_done"] == dni

    pozycja = dokoncz(client)["plan"]["instruments"][0]
    assert pozycja["state"] == "done"
    assert library.entries()[0]["bars"] > 0


def test_retrying_an_instrument_that_yielded_nothing_starts_over(client, monkeypatch):
    """Instrument, który przeszedł cały zakres bez ani jednej świecy, nie ma czego wznawiać
    od kursora — powtórka musi ruszyć od początku zakresu."""
    import app.dukascopy as duka
    monkeypatch.setattr(duka, "_fetch_hour", archiwum_atrapa(martwe=True))

    zacznij(client, years=1)
    plan = dokoncz(client)["plan"]
    assert plan["instruments"][0]["state"] == "error"

    monkeypatch.setattr(duka, "_fetch_hour", archiwum_atrapa())
    wznowiony = client.post("/api/archive/retry").json()["plan"]["instruments"][0]
    assert wznowiony["cursor"] == plan["date_from"]
    assert wznowiony["days_done"] == 0
    assert dokoncz(client)["plan"]["instruments"][0]["state"] == "done"


def test_retrying_does_not_touch_what_already_landed_in_the_library(client, monkeypatch):
    import app.dukascopy as duka
    archiwum = sys.modules["app.archiwum"]

    dziala = archiwum_atrapa()
    # EUR/USD nie oddaje nic; GBP/USD pobiera się normalnie.
    monkeypatch.setattr(duka, "_fetch_hour",
                        lambda url: (None if "EURUSD" in url else dziala(url)))
    monkeypatch.setattr(duka, "DEAD_DAY_PAUSE", 0)
    monkeypatch.setattr(archiwum, "MAX_ODMOW", 1)

    zacznij(client, instruments=["GBPUSD", "EURUSD"], years=1)
    plan = dokoncz(client)["plan"]
    gotowy = next(p for p in plan["instruments"] if p["code"] == "GBPUSD")
    assert gotowy["state"] == "done"

    plan = client.post("/api/archive/retry").json()["plan"]
    assert next(p for p in plan["instruments"] if p["code"] == "GBPUSD") == gotowy
    assert next(p for p in plan["instruments"] if p["code"] == "EURUSD")["state"] == "pending"


def test_retrying_a_plan_without_failures_says_so(client, siec):
    zacznij(client, years=1)
    dokoncz(client)
    odp = client.post("/api/archive/retry")
    assert odp.status_code == 400
    assert "nieudanych" in odp.json()["detail"]


def test_a_running_plan_can_be_replaced_on_purpose(client, siec):
    """Zgłoszenie: „nie da się kliknąć pobierz, jak coś się odpaliło wcześniej, i nie zmienia
    się to, co pobieramy". Trwający plan nie może zamieniać przycisku w ślepy zaułek —
    świadome „przerwij tamto i zacznij to" musi przechodzić, razem z nowymi ustawieniami.
    """
    zacznij(client, instruments=["GBPUSD"], years=1)
    assert zacznij(client, instruments=["EURUSD"], years=1).status_code == 400

    odp = zacznij(client, instruments=["EURUSD"], years=1, replace=True)
    assert odp.status_code == 200
    plan = odp.json()["plan"]
    assert [p["code"] for p in plan["instruments"]] == ["EURUSD"]     # nowy wybór wszedł w życie
    assert plan["state"] == "running"


def test_replacing_a_plan_keeps_what_already_reached_the_library(client, siec):
    """Zastąpienie planu nie może odbierać instrumentów domkniętych wcześniej."""
    library = sys.modules["app.library"]

    zacznij(client, instruments=["GBPUSD"], years=1)
    dokoncz(client)
    ile = len(library.entries())
    assert ile == 1

    zacznij(client, instruments=["EURUSD"], years=1, replace=True)
    assert len(library.entries()) == ile


def test_a_step_in_flight_does_not_overwrite_a_freshly_started_plan(client, siec, tmp_path):
    """Krok trwa kilkadziesiąt sekund, a przez ten czas użytkownik może zacząć nowy plan.
    Krok trzyma w ręku wersję sprzed pobierania — zapisanie jej z powrotem cofnęłoby
    zastąpienie i w tabeli dalej stałby stary wybór instrumentów.
    """
    archiwum = sys.modules["app.archiwum"]

    zacznij(client, instruments=["GBPUSD"], years=1)
    stary = client.get("/api/archive/status").json()["plan"]

    prawdziwy = archiwum._kawalek

    def zastap_w_trakcie(plan, pozycja, koniec_budzetu, cache_dir):
        prawdziwy(plan, pozycja, koniec_budzetu, cache_dir)
        if archiwum._kawalek is zastap_w_trakcie:          # tylko raz
            archiwum._kawalek = prawdziwy
            zacznij(client, instruments=["EURUSD"], years=1, replace=True)

    archiwum._kawalek = zastap_w_trakcie
    try:
        dane = client.post("/api/archive/step").json()
    finally:
        archiwum._kawalek = prawdziwy

    assert [p["code"] for p in dane["plan"]["instruments"]] == ["EURUSD"]
    assert dane["plan"]["id"] != stary["id"]
    # Kawałki starego planu nie mogą zostać w magazynie jako sieroty.
    kawalki = list((tmp_path / "datasets" / "archiwum").rglob("*.csv"))
    assert all(stary["id"] not in str(p) for p in kawalki)


def test_replacing_leaves_no_orphaned_chunks(client, siec, tmp_path):
    """Kawałki porzuconego planu to śmieci — po zastąpieniu nie mogą zostać w magazynie."""
    zacznij(client, instruments=["GBPUSD"], years=1)
    client.post("/api/archive/step")            # zdąży powstać przynajmniej jeden kawałek

    zacznij(client, instruments=["GBPUSD"], years=1, replace=True)
    biezacy = client.get("/api/archive/status").json()["plan"]["id"]
    kawalki = list((tmp_path / "datasets" / "archiwum").rglob("*.csv"))
    assert all(biezacy in str(p) for p in kawalki)


def test_an_abandoned_running_plan_does_not_block_a_new_download(client, siec, monkeypatch):
    """Karta zamknięta w połowie zostawia plan w stanie „w trakcie". Kroki idą z przeglądarki,
    więc taki plan nie posunie się już nigdy — i nie może blokować startu na zawsze.
    """
    archiwum = sys.modules["app.archiwum"]

    zacznij(client, years=1)
    assert zacznij(client, years=1).status_code == 400      # świeży plan naprawdę biegnie

    monkeypatch.setattr(archiwum, "PORZUCONY_PO", 0.0)
    assert zacznij(client, years=1).status_code == 200


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
