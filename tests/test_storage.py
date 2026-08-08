"""Testy warstwy zapisu biblioteki.

Magazyn obiektów podstawiamy atrapą wpiętą w `urllib`, więc sprawdzany jest cały tor:
adres, metoda, nagłówki i rozbiór odpowiedzi — a nie tylko interfejs klasy.

Uczciwe zastrzeżenie: atrapa odtwarza API Vercel Blob tak, jak je tu rozumiemy. Ruchu
wychodzącego nie dało się sprawdzić na żywo w środowisku, w którym projekt powstawał,
więc te testy pilnują naszej strony umowy, a nie tego, że druga strona wygląda tak samo.
Rozstrzyga to dopiero `/api/storage/probe` uruchomiony na wdrożeniu.
"""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import storage       # noqa: E402


PUBLIC = "https://przyklad.public.blob.vercel-storage.com"


class AtrapaBlob:
    """Magazyn obiektów w pamięci, rozmawiający tak jak API Vercel Blob."""

    def __init__(self) -> None:
        self.obiekty: dict[str, bytes] = {}      # pathname -> treść
        self.wywolania: list[tuple[str, str]] = []
        self.zapis_dziala = True
        self.awaria = False                      # sieć całkiem odmawia
        self.naglowki_ostatniego: dict[str, str] = {}

    # --- podpięcie pod urllib ---------------------------------------------------

    def urlopen(self, request, timeout=None):
        url, metoda = request.full_url, request.get_method()
        self.wywolania.append((metoda, url))
        if self.awaria:
            raise urllib.error.URLError("sieć niedostępna")

        naglowki = {k.lower(): v for k, v in request.headers.items()}
        self.naglowki_ostatniego = naglowki
        autoryzacja = naglowki.get("authorization", "")
        if not autoryzacja.startswith("Bearer ") or not autoryzacja[7:].strip():
            raise urllib.error.HTTPError(url, 403, "brak tokenu", None, None)

        if url.startswith(PUBLIC):
            tresc = self.obiekty.get(url[len(PUBLIC) + 1:])
            if tresc is None:
                raise urllib.error.HTTPError(url, 404, "nie ma", None, None)
            return _odpowiedz(tresc)

        if metoda == "PUT":
            if not self.zapis_dziala:
                raise urllib.error.HTTPError(url, 500, "magazyn padł", None, None)
            # Ścieżka idzie w parametrze `pathname`, nie jako segment adresu.
            zapytanie = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            if "pathname" not in zapytanie:
                raise urllib.error.HTTPError(url, 400, "brak parametru pathname", None, None)
            pathname = zapytanie["pathname"][0]
            if naglowki.get("access") != "public":
                raise urllib.error.HTTPError(url, 400, "brak nagłówka access", None, None)
            if naglowki.get("x-api-version") != storage.BLOB_API_VERSION:
                raise urllib.error.HTTPError(url, 400, "zła wersja API", None, None)
            # Nadpisanie istniejącego obiektu wymaga wyraźnej zgody.
            if pathname in self.obiekty and naglowki.get("x-allow-overwrite") not in ("1", "true"):
                raise urllib.error.HTTPError(url, 409, "obiekt już istnieje", None, None)
            self.obiekty[pathname] = request.data
            return _odpowiedz(json.dumps({"url": f"{PUBLIC}/{pathname}"}).encode())

        if metoda == "POST" and url.endswith("/delete"):
            for adres in json.loads(request.data.decode()).get("urls", []):
                self.obiekty.pop(adres[len(PUBLIC) + 1:], None)
            return _odpowiedz(b"{}")

        prefix = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("prefix", [""])[0]
        blobs = [{"pathname": p, "url": f"{PUBLIC}/{p}"} for p in self.obiekty if p.startswith(prefix)]
        return _odpowiedz(json.dumps({"blobs": blobs}).encode())

    # --- pomoc dla testów -------------------------------------------------------

    def listowania(self) -> int:
        return len([1 for m, u in self.wywolania if m == "GET" and "?" in u])


def _odpowiedz(tresc: bytes):
    strumien = io.BytesIO(tresc)
    strumien.__enter__ = lambda: strumien
    strumien.__exit__ = lambda *a: False
    return strumien


@pytest.fixture
def blob(monkeypatch, tmp_path):
    """Świeży magazyn obiektów plus token, żeby `active()` po niego sięgnął."""
    atrapa = AtrapaBlob()
    monkeypatch.setattr(urllib.request, "urlopen", atrapa.urlopen)
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "vercel_blob_rw_TEST")
    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(tmp_path))
    storage.reset()
    yield atrapa
    storage.reset()


@pytest.fixture
def dysk(monkeypatch, tmp_path):
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(tmp_path))
    storage.reset()
    yield tmp_path
    storage.reset()


# --- dysk --------------------------------------------------------------------------


def test_disk_stores_and_returns_what_it_was_given(dysk):
    magazyn = storage.LocalStorage(dysk)
    assert magazyn.write("a.csv", "treść zażółć")
    assert magazyn.read("a.csv") == "treść zażółć"
    assert magazyn.exists("a.csv")
    assert magazyn.delete("a.csv") and not magazyn.exists("a.csv")


def test_reading_a_missing_key_is_empty_not_an_error(dysk):
    assert storage.LocalStorage(dysk).read("nie_ma.csv") is None


def test_deleting_a_missing_key_still_counts_as_done(dysk):
    """Kasowanie ma być idempotentne — biblioteka wywołuje je i po nieistniejącym pliku."""
    assert storage.LocalStorage(dysk).delete("nie_ma.csv")


def test_the_system_temp_directory_does_not_survive_a_restart():
    """Sedno całej sprawy: `/tmp` na Vercelu znika razem z instancją."""
    import tempfile
    assert not storage.przetrwa_restart(Path(tempfile.gettempdir()) / "backtester")
    assert not storage.przetrwa_restart(Path(tempfile.gettempdir()))


def test_a_directory_outside_temp_survives_a_restart():
    assert storage.przetrwa_restart(Path("/home/uzytkownik/projekt/data"))


# --- magazyn obiektów ---------------------------------------------------------------


def test_the_object_store_stores_and_returns_what_it_was_given(blob):
    magazyn = storage.BlobStorage("vercel_blob_rw_TEST")
    assert magazyn.write("a.csv", "treść zażółć")
    assert magazyn.read("a.csv") == "treść zażółć"
    assert magazyn.exists("a.csv")
    assert magazyn.delete("a.csv") and not magazyn.exists("a.csv")


def test_keys_stay_predictable_instead_of_getting_a_random_suffix(blob):
    """Bez wyłączenia przyrostka zapisany plik trafiałby pod adres, którego nie umiemy odgadnąć."""
    storage.BlobStorage("vercel_blob_rw_TEST").write("a.csv", "x")
    assert list(blob.obiekty) == [storage.BLOB_PREFIX + "a.csv"]


def test_everything_lands_under_one_prefix(blob):
    """Magazyn bywa dzielony z innymi zastosowaniami — nie zaśmiecamy jego korzenia."""
    storage.BlobStorage("vercel_blob_rw_TEST").write("index.json", "{}")
    assert all(p.startswith(storage.BLOB_PREFIX) for p in blob.obiekty)


def test_a_key_written_by_another_instance_is_found(blob):
    """Druga instancja zapisała plik już po tym, jak zapamiętaliśmy listę — i tak go widzimy."""
    magazyn = storage.BlobStorage("vercel_blob_rw_TEST")
    magazyn.read("cokolwiek.csv")                       # pierwsze listowanie
    blob.obiekty[storage.BLOB_PREFIX + "obcy.csv"] = b"z innej instancji"
    assert magazyn.read("obcy.csv") == "z innej instancji"


def test_the_listing_is_reused_instead_of_asked_for_every_time(blob):
    magazyn = storage.BlobStorage("vercel_blob_rw_TEST")
    magazyn.write("a.csv", "x")
    for _ in range(5):
        assert magazyn.exists("a.csv")
    assert blob.listowania() <= 1


def test_a_dead_network_is_a_refusal_not_a_crash(blob):
    magazyn = storage.BlobStorage("vercel_blob_rw_TEST")
    blob.awaria = True
    assert magazyn.write("a.csv", "x") is False
    assert magazyn.read("a.csv") is None
    assert magazyn.exists("a.csv") is False


def test_a_bad_token_is_a_refusal_not_a_crash(blob):
    assert storage.BlobStorage("").write("a.csv", "x") is False


# --- magazyn z odwrotem na dysk ------------------------------------------------------


def test_data_saved_before_the_store_was_connected_is_still_visible(blob, tmp_path):
    """Ktoś używał aplikacji lokalnie, potem podpiął magazyn — biblioteka nie może zniknąć."""
    lokalny = storage.LocalStorage(tmp_path / "dane")
    lokalny.write("stary.csv", "sprzed magazynu")
    magazyn = storage.FallbackStorage(storage.BlobStorage("vercel_blob_rw_TEST"), lokalny)
    assert magazyn.read("stary.csv") == "sprzed magazynu"


def test_a_write_goes_to_both_places(blob, tmp_path):
    lokalny = storage.LocalStorage(tmp_path / "dane")
    magazyn = storage.FallbackStorage(storage.BlobStorage("vercel_blob_rw_TEST"), lokalny)
    magazyn.write("a.csv", "x")
    assert lokalny.read("a.csv") == "x"
    assert blob.obiekty[storage.BLOB_PREFIX + "a.csv"] == b"x"


def test_a_refused_store_write_keeps_working_but_stops_promising_persistence(blob, tmp_path):
    lokalny = storage.LocalStorage(tmp_path / "dane")
    magazyn = storage.FallbackStorage(storage.BlobStorage("vercel_blob_rw_TEST"), lokalny)
    blob.zapis_dziala = False

    assert magazyn.write("a.csv", "x")       # dane nie giną…
    assert magazyn.read("a.csv") == "x"
    assert magazyn.persistent is False       # …ale nie udajemy, że przetrwają


def test_the_store_recovers_the_promise_after_a_successful_write(blob, tmp_path):
    magazyn = storage.FallbackStorage(
        storage.BlobStorage("vercel_blob_rw_TEST"), storage.LocalStorage(tmp_path / "dane"))
    blob.zapis_dziala = False
    magazyn.write("a.csv", "x")
    blob.zapis_dziala = True
    magazyn.write("b.csv", "y")
    assert magazyn.persistent is True


def test_deleting_removes_the_copy_from_both_places(blob, tmp_path):
    lokalny = storage.LocalStorage(tmp_path / "dane")
    magazyn = storage.FallbackStorage(storage.BlobStorage("vercel_blob_rw_TEST"), lokalny)
    magazyn.write("a.csv", "x")
    magazyn.delete("a.csv")
    assert not magazyn.exists("a.csv")
    assert not lokalny.exists("a.csv")
    assert storage.BLOB_PREFIX + "a.csv" not in blob.obiekty


# --- wybór magazynu ------------------------------------------------------------------


def test_without_a_token_the_choice_is_plain_disk(dysk):
    assert isinstance(storage.active(), storage.LocalStorage)


def test_with_a_token_the_choice_is_the_object_store(blob):
    assert isinstance(storage.active(), storage.FallbackStorage)


def test_the_choice_follows_the_token_appearing(monkeypatch, tmp_path):
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(tmp_path))
    storage.reset()
    assert isinstance(storage.active(), storage.LocalStorage)

    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "vercel_blob_rw_TEST")
    assert isinstance(storage.active(), storage.FallbackStorage)
    storage.reset()


def test_the_choice_follows_the_directory_changing(monkeypatch, tmp_path):
    """Bez tego test zmieniający katalog dostawał magazyn wskazujący na poprzedni."""
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(tmp_path / "pierwszy"))
    storage.reset()
    storage.active().write("a.csv", "x")

    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(tmp_path / "drugi"))
    assert not storage.active().exists("a.csv")
    storage.reset()


# --- sonda -----------------------------------------------------------------------------


def test_the_probe_confirms_a_working_object_store(blob):
    wynik = storage.probe()
    assert wynik["ok"] is True
    assert wynik["persistent"] is True
    assert "trwały" in wynik["hint"]


def test_the_probe_leaves_nothing_behind(blob):
    storage.probe()
    assert blob.obiekty == {}


def test_the_probe_reports_a_working_but_ephemeral_disk(dysk):
    """Najważniejszy przypadek: wszystko się udało, a mimo to dane nie przetrwają."""
    wynik = storage.probe()
    assert wynik["ok"] is True
    assert wynik["persistent"] is False
    assert wynik["token_present"] is False
    assert "dysk instancji" in wynik["hint"]
    assert "DEPLOY.md" in wynik["hint"]


def test_the_probe_names_the_store_as_the_problem_when_the_token_is_set(blob, tmp_path):
    blob.zapis_dziala = False
    wynik = storage.probe()
    assert wynik["persistent"] is False
    assert wynik["token_present"] is True
    # Odpowiedź magazynu kończy zgadywanie: widać kod i powód odmowy, a nie samo „nie udało się".
    assert "Magazyn odpowiedział: PUT 500" in wynik["hint"]
    assert wynik["last_error"].startswith("PUT 500")


def test_the_probe_lists_every_step_of_the_cycle(dysk):
    kroki = [k["krok"] for k in storage.probe()["steps"]]
    assert kroki == ["zapis", "odczyt", "zgodność treści", "usunięcie", "po usunięciu pusto"]


def test_the_probe_survives_a_store_that_explodes(dysk, monkeypatch):
    """Diagnostyka, która sama się wywraca, jest gorsza niż jej brak."""
    class Wybuchowy(storage.LocalStorage):
        def write(self, key, text):
            raise RuntimeError("dysk pełen")

    monkeypatch.setattr(storage, "active", lambda: Wybuchowy(dysk))
    wynik = storage.probe()
    assert wynik["ok"] is False
    assert any("dysk pełen" in str(k["szczegol"]) for k in wynik["steps"])


# --- biblioteka na magazynie -------------------------------------------------------------


def test_the_library_survives_the_instance_it_was_written_on(blob):
    """Sedno prośby: po uśpieniu instancji zbiory mają nadal być w bibliotece."""
    from app import library

    library.save("abc123", "data,open\n2024-01-01,1.0\n", "Zbiór testowy", "dukascopy")
    assert [e["id"] for e in library.entries()] == ["abc123"]

    # Nowa instancja: pamięć procesu pusta, dysk pusty, zostaje sam magazyn obiektów.
    storage.reset()
    for sciezka in Path(storage.active().backup._root).glob("*"):
        sciezka.unlink()

    assert [e["id"] for e in library.entries()] == ["abc123"]
    assert library.get_text("abc123").startswith("data,open")


def test_the_library_on_plain_disk_reports_that_it_is_not_persistent(dysk):
    from app import library

    assert library.usage()["persistent"] is False
    assert library.usage()["backend"] == "dysk"


# --- rozpoznanie tokenu w środowisku ------------------------------------------------------


def czysto(monkeypatch):
    """Środowisko bez żadnych śladów magazynu — testy nie mogą zależeć od tego, co stoi wokół."""
    for nazwa in list(os.environ):
        if "BLOB" in nazwa.upper() or nazwa.endswith(storage.TOKEN_SUFFIX):
            monkeypatch.delenv(nazwa, raising=False)
        elif os.environ[nazwa].strip().startswith(storage.TOKEN_PREFIX):
            monkeypatch.delenv(nazwa, raising=False)


def test_the_default_variable_name_is_found(monkeypatch):
    czysto(monkeypatch)
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "vercel_blob_rw_ABC")
    assert storage.find_token() == ("BLOB_READ_WRITE_TOKEN", "vercel_blob_rw_ABC")


def test_a_named_store_uses_its_own_prefix(monkeypatch):
    """Sedno zgłoszenia „podpiąłem magazyn, a dalej nie wykrywa".

    Vercel nazywa zmienną `BLOB_READ_WRITE_TOKEN` tylko przy domyślnym przedrostku.
    Nazwany magazyn albo drugi w projekcie dostaje własny — i magazyn stawał się niewidzialny.
    """
    czysto(monkeypatch)
    monkeypatch.setenv("MOJE_DANE_READ_WRITE_TOKEN", "vercel_blob_rw_XYZ")
    assert storage.find_token() == ("MOJE_DANE_READ_WRITE_TOKEN", "vercel_blob_rw_XYZ")


def test_the_default_name_wins_when_both_are_present(monkeypatch):
    """Przy dwóch magazynach domyślny jest tym, o który chodziło."""
    czysto(monkeypatch)
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "vercel_blob_rw_DOMYSLNY")
    monkeypatch.setenv("INNY_READ_WRITE_TOKEN", "vercel_blob_rw_INNY")
    assert storage.find_token()[1] == "vercel_blob_rw_DOMYSLNY"


def test_a_completely_custom_name_is_still_recognised_by_its_value(monkeypatch):
    """Ostatnia deska ratunku: zmienna nazwana po swojemu, ale z tokenem w wartości."""
    czysto(monkeypatch)
    monkeypatch.setenv("MAGAZYN", "vercel_blob_rw_ZZZ")
    assert storage.find_token() == ("MAGAZYN", "vercel_blob_rw_ZZZ")


def test_an_empty_variable_does_not_count_as_a_token(monkeypatch):
    czysto(monkeypatch)
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "   ")
    assert storage.find_token() == ("", "")


def test_nothing_is_found_in_a_clean_environment(monkeypatch):
    czysto(monkeypatch)
    assert storage.find_token() == ("", "")


def test_the_store_is_used_when_the_name_is_not_the_default_one(monkeypatch, tmp_path):
    czysto(monkeypatch)
    monkeypatch.setenv("MOJE_DANE_READ_WRITE_TOKEN", "vercel_blob_rw_XYZ")
    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(tmp_path))
    storage.reset()
    try:
        assert isinstance(storage.active(), storage.FallbackStorage)
    finally:
        storage.reset()


def test_candidates_list_names_but_never_values(monkeypatch):
    """Nazwa zmiennej nie jest tajemnicą, wartość owszem — a bez nazw nie da się tego
    zdiagnozować zdalnie."""
    czysto(monkeypatch)
    monkeypatch.setenv("MOJE_DANE_READ_WRITE_TOKEN", "vercel_blob_rw_TAJNE")
    monkeypatch.setenv("BLOB_STORE_ID", "store_123")

    kandydaci = storage.token_candidates()
    assert "MOJE_DANE_READ_WRITE_TOKEN" in kandydaci
    assert "BLOB_STORE_ID" in kandydaci
    assert not any("vercel_blob_rw_TAJNE" in k for k in kandydaci)


def test_the_probe_says_which_variable_the_token_came_from(monkeypatch, tmp_path):
    czysto(monkeypatch)
    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(tmp_path))
    storage.reset()
    try:
        assert storage.probe()["token_env"] == ""
        monkeypatch.setenv("MOJE_DANE_READ_WRITE_TOKEN", "vercel_blob_rw_XYZ")
        storage.reset()
        assert storage.probe()["token_env"] == "MOJE_DANE_READ_WRITE_TOKEN"
    finally:
        storage.reset()


def test_the_hint_points_at_a_stale_deployment_when_variables_exist(monkeypatch, tmp_path):
    """Zmienne są, tokenu nie ma — prawie zawsze znaczy to wdrożenie sprzed ich dodania."""
    czysto(monkeypatch)
    monkeypatch.setenv("BLOB_STORE_ID", "store_123")
    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(tmp_path))
    storage.reset()
    try:
        wynik = storage.probe()
        assert wynik["token_present"] is False
        assert "BLOB_STORE_ID" in wynik["hint"]
        assert "Redeploy" in wynik["hint"]
    finally:
        storage.reset()


def test_a_connected_store_without_a_token_gets_the_exact_next_step(monkeypatch, tmp_path):
    """Odcisk palca z wdrożenia: magazyn podpięty, tokenu do zapisu brak.

    Podpięcie istniejącego magazynu uwierzytelnia przez OIDC i dokłada tylko `BLOB_STORE_ID`
    oraz klucz webhooków — statyczny token powstaje przy *tworzeniu* magazynu. Sama informacja
    „nie widzę tokenu" prowadziła wtedy donikąd: użytkownik widział podpięty magazyn i słyszał,
    że go nie ma. Komunikat musi podać konkretny następny ruch.
    """
    czysto(monkeypatch)
    monkeypatch.setenv("BLOB_STORE_ID", "store_abc123")
    monkeypatch.setenv("BLOB_WEBHOOK_PUBLIC_KEY", "klucz")
    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(tmp_path))
    storage.reset()
    try:
        wynik = storage.probe()
        assert wynik["token_present"] is False
        assert "BLOB_STORE_ID" in wynik["hint"]
        assert ".env.local" in wynik["hint"]
        assert "Environment Variables" in wynik["hint"]
    finally:
        storage.reset()


# --- kontrakt API magazynu ---------------------------------------------------------------


def test_the_path_goes_in_the_pathname_parameter(blob):
    """Adres zapisu był zgadnięty źle: ścieżka jako segment adresu, a nie parametr.

    Magazyn nie rozpoznawał wtedy żądania w ogóle, więc zapis cicho przepadał, a biblioteka
    zostawała na ulotnym dysku instancji — przy pozornie poprawnie podpiętym magazynie.
    """
    magazyn = storage.BlobStorage("vercel_blob_rw_TEST")
    assert magazyn.write("a.csv", "x")
    assert magazyn.last_error == ""
    assert storage.BLOB_PREFIX + "a.csv" in blob.obiekty


def test_writing_the_same_key_twice_is_allowed(blob):
    """Indeks biblioteki zmienia się przy każdym dodaniu zbioru — bez zgody na nadpisanie
    magazyn odrzucałby każdy zapis poza pierwszym."""
    magazyn = storage.BlobStorage("vercel_blob_rw_TEST")
    assert magazyn.write("index.json", "{}")
    assert magazyn.write("index.json", '{"a": 1}')
    assert magazyn.read("index.json") == '{"a": 1}'


def test_a_refusal_is_reported_with_its_status_and_reason(blob):
    """Bez treści odmowy każda awaria magazynu wyglądała tak samo — i nie dało się jej naprawić."""
    magazyn = storage.BlobStorage("vercel_blob_rw_TEST")
    blob.zapis_dziala = False
    assert magazyn.write("a.csv", "x") is False
    assert "500" in magazyn.last_error
    assert "magazyn padł" in magazyn.last_error


def test_a_dead_connection_is_reported_as_such(blob):
    magazyn = storage.BlobStorage("vercel_blob_rw_TEST")
    blob.awaria = True
    assert magazyn.write("a.csv", "x") is False
    assert "brak połączenia" in magazyn.last_error


def test_the_error_never_carries_the_token(blob):
    """Diagnostyka trafia na ekran — token nie może się w niej znaleźć."""
    magazyn = storage.BlobStorage("vercel_blob_rw_TAJNY_TOKEN")
    blob.zapis_dziala = False
    magazyn.write("a.csv", "x")
    assert "TAJNY_TOKEN" not in magazyn.last_error


# --- pomylona wartość w zmiennej ------------------------------------------------------------

KLUCZ_PUBLICZNY = ("-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQ\n-----END PUBLIC KEY-----")


def test_a_public_key_pasted_instead_of_the_token_is_named_as_such(monkeypatch, tmp_path):
    """Realna pomyłka z wdrożenia: obok tokenu leży w panelu klucz publiczny webhooków.

    Magazyn odpowiada wtedy „Cannot get store id from token or header", bo identyfikator
    magazynu jest zaszyty w samym tokenie. Z tego komunikatu nie sposób wywnioskować,
    że pomyliły się wartości — aplikacja musi to powiedzieć wprost.
    """
    czysto(monkeypatch)
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", KLUCZ_PUBLICZNY)
    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(tmp_path))
    storage.reset()
    try:
        wynik = storage.probe()
        assert wynik["token_present"] is True
        assert wynik["token_shape_ok"] is False
        assert storage.TOKEN_PREFIX in wynik["hint"]
        assert "BLOB_WEBHOOK_PUBLIC_KEY" in wynik["hint"]
    finally:
        storage.reset()


def test_a_real_token_elsewhere_wins_over_a_wrong_value_under_the_right_name(monkeypatch):
    """Wartość wklejona pod właściwą nazwą, ale nie ta co trzeba, nie może przesłaniać
    prawdziwego tokenu leżącego gdzie indziej."""
    czysto(monkeypatch)
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", KLUCZ_PUBLICZNY)
    monkeypatch.setenv("MAGAZYN_READ_WRITE_TOKEN", "vercel_blob_rw_PRAWDZIWY")
    assert storage.find_token() == ("MAGAZYN_READ_WRITE_TOKEN", "vercel_blob_rw_PRAWDZIWY")


def test_a_wrong_value_is_still_reported_rather_than_ignored(monkeypatch):
    """Udawanie, że zmiennej nie ma, wysłałoby użytkownika po token, który już wpisał."""
    czysto(monkeypatch)
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", KLUCZ_PUBLICZNY)
    nazwa, wartosc = storage.find_token()
    assert nazwa == "BLOB_READ_WRITE_TOKEN"
    assert wartosc == KLUCZ_PUBLICZNY
    assert storage.wyglada_na_token(wartosc) is False


def test_a_proper_token_passes_the_shape_check():
    assert storage.wyglada_na_token("vercel_blob_rw_abc123_XYZ")
    assert not storage.wyglada_na_token("")
    assert not storage.wyglada_na_token("store_abc123")


# --- co przykleja się przy kopiowaniu tokenu -------------------------------------------------

TOKEN = "vercel_blob_rw_sklep123_LoSoWe=="


@pytest.mark.parametrize("wklejone", [
    TOKEN,
    f"  {TOKEN}  ",                                  # spacje z zaznaczenia
    f'"{TOKEN}"',                                    # cudzysłowy z pliku .env
    f"'{TOKEN}'",
    f'BLOB_READ_WRITE_TOKEN="{TOKEN}"',              # cała linia z zakładki .env.local
    f"BLOB_READ_WRITE_TOKEN={TOKEN}",
    f"MOJE_DANE_READ_WRITE_TOKEN={TOKEN}",
])
def test_the_token_survives_the_usual_copy_paste_mishaps(wklejone):
    """Token bierze się z zakładki `.env.local`, gdzie stoi jako NAZWA="wartość".

    Skopiowanie całej linii albo zostawienie cudzysłowów kończyło się nagłówkiem, którego
    magazyn nie rozpoznaje — i znowu „nie wykrywa magazynu", tym razem bez żadnej wskazówki.
    """
    assert storage.oczysc_token(wklejone) == TOKEN
    assert storage.wyglada_na_token(wklejone)


def test_the_trailing_equals_sign_is_part_of_the_token():
    """Token bywa zakończony `=` (dopełnienie base64) — nie wolno go uciąć."""
    assert storage.oczysc_token(TOKEN).endswith("==")


def test_a_pasted_line_is_cleaned_up_before_use(monkeypatch):
    czysto(monkeypatch)
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", f'BLOB_READ_WRITE_TOKEN="{TOKEN}"')
    assert storage.find_token() == ("BLOB_READ_WRITE_TOKEN", TOKEN)


def test_cleaning_does_not_turn_a_public_key_into_a_token():
    """Sprzątanie ma naprawiać literówki w kopiowaniu, a nie ukrywać pomyłkę co do wartości."""
    assert not storage.wyglada_na_token(KLUCZ_PUBLICZNY)


# --- token sklejony z sąsiednią wartością ----------------------------------------------------

KLUCZ_DER = "MCowBQYDK2VwAyEAnT+j6VX3AZzx8NCZresoiYD6wjUDDBezghEzhRajjoA="
SKLEJONE = f"{TOKEN}{KLUCZ_DER}"


def test_a_token_with_something_glued_after_it_is_recognised():
    """Token i klucz publiczny leżą w panelu obok siebie — myszą łatwo złapać oba naraz.

    Sklejona wartość zaczyna się poprawnie, więc sprawdzenie samego początku jej nie wyłapie.
    Rozstrzyga dopełnienie base64: `=` występuje wyłącznie na końcu ciągu, więc znak `=`
    w środku znaczy, że dalej idzie już druga wartość.
    """
    assert storage.wyglada_na_token(SKLEJONE)         # początek się zgadza…
    assert storage.sklejone_wartosci(SKLEJONE)        # …ale to nie jest jeden token


def test_a_clean_token_is_not_reported_as_glued():
    """Token kończący się dopełnieniem base64 jest poprawny i nie może wpaść w to sito."""
    assert not storage.sklejone_wartosci(TOKEN)
    assert not storage.sklejone_wartosci("vercel_blob_rw_sklep_bezDopelnienia")


def test_the_probe_names_the_glued_value(monkeypatch, tmp_path):
    czysto(monkeypatch)
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", SKLEJONE)
    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(tmp_path))
    storage.reset()
    try:
        wynik = storage.probe()
        assert wynik["token_glued"] is True
        assert "dwie wartości sklejone" in wynik["hint"]
        assert "BLOB_WEBHOOK_PUBLIC_KEY" in wynik["hint"]
    finally:
        storage.reset()


def test_the_glued_value_is_never_echoed_back(monkeypatch, tmp_path):
    """Komunikat trafia na ekran — nie może nieść ze sobą samego tokenu."""
    czysto(monkeypatch)
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", SKLEJONE)
    monkeypatch.setenv("BACKTESTER_STATE_DIR", str(tmp_path))
    storage.reset()
    try:
        assert TOKEN not in storage.probe()["hint"]
    finally:
        storage.reset()


# --- uwierzytelnianie bez tokenu statycznego (OIDC) ------------------------------------------


def test_oidc_works_without_any_static_token(monkeypatch):
    """Droga, która nie wymaga niczego wpisywanego ręcznie.

    `VERCEL_OIDC_TOKEN` wystawia Vercel przy każdym uruchomieniu funkcji, a `BLOB_STORE_ID`
    dokłada podpięcie magazynu. Nie ma tu czego pomylić ani skleić — w odróżnieniu od tokenu
    kopiowanego ręcznie z panelu.
    """
    czysto(monkeypatch)
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc_abc")
    monkeypatch.setenv("BLOB_STORE_ID", "store_sklep123")

    klucz, magazyn, skad = storage.blob_credentials()
    assert (klucz, magazyn, skad) == ("oidc_abc", "sklep123", "OIDC")


def test_the_store_id_prefix_is_stripped(monkeypatch):
    """W nagłówku identyfikator idzie bez przedrostka `store_`."""
    assert storage.normalizuj_id_magazynu("store_abc") == "abc"
    assert storage.normalizuj_id_magazynu("abc") == "abc"


def test_the_store_id_is_read_out_of_the_token():
    assert storage.id_magazynu_z_tokenu("vercel_blob_rw_sklep123_LoSoWe") == "sklep123"
    assert storage.id_magazynu_z_tokenu("cokolwiek") == ""


def test_a_connected_store_id_wins_over_the_one_inside_the_token(monkeypatch):
    """`BLOB_STORE_ID` pochodzi z aktualnego podpięcia, token bywa z magazynu, którego już nie ma.

    Właśnie to dawało odmowę „store_not_found": token wskazywał na nieistniejący magazyn.
    """
    czysto(monkeypatch)
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "vercel_blob_rw_stary_LoSoWe")
    monkeypatch.setenv("BLOB_STORE_ID", "store_biezacy")
    assert storage.blob_credentials()[1] == "biezacy"


def test_a_static_token_is_preferred_over_oidc(monkeypatch):
    """Token statyczny nie wygasa, więc gdy jest poprawny, zostajemy przy nim."""
    czysto(monkeypatch)
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "vercel_blob_rw_sklep_LoSoWe")
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc_abc")
    monkeypatch.setenv("BLOB_STORE_ID", "store_sklep")
    assert storage.blob_credentials()[2] == "token"


def test_a_glued_token_falls_back_to_oidc(monkeypatch):
    """Skoro token jest popsuty, a obok stoi droga bez tokenu — bierzemy ją."""
    czysto(monkeypatch)
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", SKLEJONE)
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc_abc")
    monkeypatch.setenv("BLOB_STORE_ID", "store_sklep")
    assert storage.blob_credentials()[2] == "OIDC"


def test_nothing_usable_means_no_credentials(monkeypatch):
    czysto(monkeypatch)
    monkeypatch.setenv("BLOB_STORE_ID", "store_sklep")      # sam identyfikator nie wystarczy
    assert storage.blob_credentials() == ("", "", "")


def test_the_store_id_travels_in_a_header(blob):
    """Token OIDC nie niesie identyfikatora magazynu — musi iść osobno."""
    magazyn = storage.BlobStorage("oidc_abc", "sklep123")
    magazyn.write("a.csv", "x")
    assert blob.naglowki_ostatniego.get("x-vercel-blob-store-id") == "sklep123"


def test_a_deleted_store_points_at_the_oidc_route():
    """Odmowa `store_not_found` znaczy, że token wskazuje na magazyn, którego już nie ma.

    Zamiast kazać szukać kolejnego tokenu, kierujemy na drogę bez tokenu — tam nie ma
    czego wpisywać, więc nie ma też czego pomylić.
    """
    odmowa = 'GET 404 Not Found — {"error":{"code":"store_not_found","message":"Store not found"}}'
    komunikat = storage._hint(ok=True, trwaly=False, token=True, kandydaci=[], blad=odmowa)

    assert "już nie istnieje" in komunikat
    assert "OIDC" in komunikat
    assert "BLOB_READ_WRITE_TOKEN" in komunikat


def test_another_refusal_is_not_mistaken_for_a_deleted_store():
    """Wskazówka o skasowanym magazynie ma pasować tylko do tej jednej odmowy."""
    komunikat = storage._hint(ok=True, trwaly=False, token=True, kandydaci=[],
                              blad="PUT 403 Forbidden — brak uprawnień")
    assert "już nie istnieje" not in komunikat
    assert "403" in komunikat
