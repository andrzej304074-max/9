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

    # --- podpięcie pod urllib ---------------------------------------------------

    def urlopen(self, request, timeout=None):
        url, metoda = request.full_url, request.get_method()
        self.wywolania.append((metoda, url))
        if self.awaria:
            raise urllib.error.URLError("sieć niedostępna")

        naglowki = {k.lower(): v for k, v in request.headers.items()}
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
            pathname = urllib.parse.unquote(url.split(storage.BLOB_API + "/")[1])
            # Vercel domyślnie dokleja losowy przyrostek; klient ma to wyłączać.
            if naglowki.get("x-add-random-suffix") not in ("0", "false"):
                pathname += "-losowy123"
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
    assert "token nie wygasł" in wynik["hint"]


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
