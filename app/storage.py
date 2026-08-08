"""Warstwa zapisu biblioteki — dysk albo magazyn obiektów.

Lokalnie pliki leżą po prostu na dysku i przeżywają restart. Przy wdrożeniu
bezserwerowym jedyne miejsce z prawem zapisu, `/tmp`, jest ulotne i lokalne dla
instancji — biblioteka wyparowywałaby przy każdym uśpieniu.

Rozwiązaniem jest magazyn obiektów. Gdy w środowisku jest token Vercel Blob, zapis
idzie tam i przeżywa zarówno uśpienie, jak i kolejne wdrożenia. Gdy tokenu nie ma
albo magazyn nie odpowiada, wracamy na dysk — aplikacja działa dalej, tyle że
z ulotną biblioteką, i mówi o tym wprost zamiast obiecywać trwałość.

Rozmowa z Vercel Blob idzie po jego API REST przez `urllib`, bez dokładania zależności.
Kontrakt jest odtworzony z oficjalnego klienta (`vercel_blob`): ścieżka idzie w parametrze
`pathname`, a nie jako segment adresu, zapis wymaga nagłówka `access` i zgody na nadpisanie,
i obowiązuje konkretna wersja API. Ruchu wychodzącego nie da się sprawdzić na żywo
w środowisku, w którym projekt powstawał, dlatego każda operacja ma odwrót do dysku,
zapamiętuje odpowiedź magazynu przy odmowie, a `/api/storage/probe` pozwala sprawdzić
jednym kliknięciem, czy magazyn faktycznie działa na Twoim wdrożeniu.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from .runtime import state_dir

BLOB_API = "https://blob.vercel-storage.com"
# Nowszy klient używa innego adresu. Oba są żywe, ale nie wiadomo z góry, który obsłuży
# dane wdrożenie — więc przy odmowie próbujemy drugiego i zapamiętujemy ten, który zadziałał.
BLOB_API_ALT = "https://vercel.com/api/blob"
BLOB_PREFIX = "backtester/"
BLOB_API_VERSION = "12"
TIMEOUT = 10
LISTING_TTL = 30.0      # przez tyle sekund ufamy zapamiętanej liście obiektów


def przetrwa_restart(root: Path) -> bool:
    """Czy pliki w tym katalogu przeżyją zniknięcie instancji.

    Katalog tymczasowy systemu (`/tmp` — jedyne miejsce z prawem zapisu na Vercelu) znika
    razem z instancją; każde inne miejsce na dysku przeżywa restart procesu.
    """
    tmp = tempfile.gettempdir().rstrip("/")
    sciezka = str(Path(root).resolve())
    return not (sciezka == tmp or sciezka.startswith(tmp + "/"))


class LocalStorage:
    """Zwykłe pliki na dysku."""

    name = "dysk"

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = root or (state_dir() / "datasets")
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Biblioteka to wygoda, nie warunek działania — brak katalogu ma odbierać zapis,
            # a nie przewracać aplikację przy pierwszym żądaniu.
            pass

    @property
    def persistent(self) -> bool:
        return przetrwa_restart(self._root)

    def _path(self, key: str) -> Path:
        return self._root / key

    def read(self, key: str) -> Optional[str]:
        try:
            return self._path(key).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def write(self, key: str, text: str) -> bool:
        try:
            sciezka = self._path(key)
            # Klucze bywają wielopoziomowe (`archiwum/GBPUSD/0001.csv`) — w magazynie obiektów
            # to zwykły tekst, na dysku muszą powstać katalogi.
            sciezka.parent.mkdir(parents=True, exist_ok=True)
            sciezka.write_text(text, encoding="utf-8")
            return True
        except OSError:
            return False

    def delete(self, key: str) -> bool:
        try:
            self._path(key).unlink(missing_ok=True)
            return True
        except OSError:
            return False

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def describe(self) -> dict[str, object]:
        return {"backend": self.name, "persistent": self.persistent, "location": str(self._root)}


class BlobStorage:
    """Magazyn obiektów Vercel Blob, obsługiwany po jego API REST."""

    name = "Vercel Blob"
    persistent = True

    def __init__(self, token: str, store_id: str = "") -> None:
        self._token = token
        self._store_id = store_id
        self._urls: dict[str, str] = {}     # klucz -> publiczny adres obiektu
        self._listed_at = 0.0
        self._lock = threading.Lock()
        self.last_error = ""                # ostatnia odmowa magazynu, dla diagnostyki
        self.api = BLOB_API                 # adres, który ostatnio zadziałał

    # --- niskopoziomowe wywołania ---------------------------------------------

    def _call(self, url: str, *, method: str = "GET", body: Optional[bytes] = None,
              headers: Optional[dict[str, str]] = None) -> Optional[bytes]:
        odpowiedz = self._raw_call(url, method=method, body=body, headers=headers)
        if odpowiedz is not None or not url.startswith(self.api):
            return odpowiedz

        # Odmowa pod jednym adresem API nie musi znaczyć, że magazyn nie działa — Vercel
        # utrzymuje dwa i nie wiadomo z góry, który obsłuży to wdrożenie. Próbujemy drugiego
        # i zapamiętujemy ten, który odpowiedział, żeby nie płacić za to przy każdym żądaniu.
        drugi = BLOB_API_ALT if self.api == BLOB_API else BLOB_API
        odpowiedz = self._raw_call(drugi + url[len(self.api):], method=method, body=body,
                                   headers=headers)
        if odpowiedz is not None:
            self.api = drugi
            self.last_error = ""
        return odpowiedz

    def _raw_call(self, url: str, *, method: str = "GET", body: Optional[bytes] = None,
                  headers: Optional[dict[str, str]] = None) -> Optional[bytes]:
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("x-api-version", BLOB_API_VERSION)
        if self._store_id:
            # Token OIDC nie niesie identyfikatora magazynu, więc idzie on osobno. Przy tokenie
            # statycznym nagłówek też nie szkodzi, a ratuje przypadek, w którym identyfikatora
            # nie da się z tokenu odczytać.
            request.add_header("x-vercel-blob-store-id", self._store_id)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            # Sama informacja „nie udało się" kazała zgadywać, co magazyn odrzucił.
            # Kod i początek odpowiedzi mówią to wprost — i nie zawierają tokenu.
            tresc = ""
            try:
                tresc = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            self.last_error = f"{method} {exc.code} {exc.reason}" + (f" — {tresc}" if tresc else "")
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.last_error = f"{method} — brak połączenia: {exc}"
            return None
        except Exception as exc:
            # Np. wartość tokenu z nowymi liniami — nagłówek nie da się złożyć. To wciąż
            # odmowa zapisu, a nie powód, żeby wywrócić aplikację przy zapisie biblioteki.
            self.last_error = f"{method} — nie udało się wysłać żądania: {exc}"
            return None

    def _listing(self, force: bool = False) -> dict[str, str]:
        with self._lock:
            if not force and self._urls and time.monotonic() - self._listed_at < LISTING_TTL:
                return self._urls

        query = urllib.parse.urlencode({"prefix": BLOB_PREFIX, "limit": "1000"})
        raw = self._call(f"{self.api}?{query}")
        if raw is None:
            return self._urls           # zostajemy przy tym, co wiemy

        try:
            blobs = json.loads(raw.decode("utf-8")).get("blobs") or []
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._urls

        found = {}
        for blob in blobs:
            pathname, url = blob.get("pathname"), blob.get("url")
            if pathname and url and pathname.startswith(BLOB_PREFIX):
                found[pathname[len(BLOB_PREFIX):]] = url
        with self._lock:
            self._urls = found
            self._listed_at = time.monotonic()
        return found

    # --- interfejs magazynu ----------------------------------------------------

    def read(self, key: str) -> Optional[str]:
        url = self._listing().get(key)
        if url is None:
            url = self._listing(force=True).get(key)     # może właśnie doszedł
        if url is None:
            return None
        raw = self._call(url)
        if raw is None:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def write(self, key: str, text: str) -> bool:
        # Ścieżka idzie w parametrze `pathname`, a nie jako segment adresu — to nie jest
        # kosmetyka, bo pod segmentem magazyn nie rozpoznaje żądania w ogóle.
        raw = self._call(
            f"{self.api}/?pathname={urllib.parse.quote(BLOB_PREFIX + key)}",
            method="PUT",
            body=text.encode("utf-8"),
            headers={
                "access": "public",
                "x-content-type": "text/csv; charset=utf-8",
                # Ten sam klucz zapisujemy wielokrotnie — indeks biblioteki zmienia się przy
                # każdym dodaniu zbioru. Bez zgody na nadpisanie magazyn odrzuca drugi zapis.
                "x-allow-overwrite": "1",
                # Bez tego CDN trzymałby indeks rok, a my potrzebujemy świeżego przy każdym
                # odczycie — inaczej nowy zbiór byłby niewidoczny dla kolejnej instancji.
                "x-cache-control-max-age": "0",
            },
        )
        if raw is None:
            return False
        try:
            url = json.loads(raw.decode("utf-8")).get("url")
        except (json.JSONDecodeError, UnicodeDecodeError):
            url = None
        if url:
            with self._lock:
                self._urls[key] = url
        return True

    def delete(self, key: str) -> bool:
        url = self._listing().get(key)
        if url is None:
            return True                  # nie ma czego kasować
        raw = self._call(
            f"{self.api}/delete",
            method="POST",
            body=json.dumps({"urls": [url]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with self._lock:
            self._urls.pop(key, None)
        return raw is not None

    def exists(self, key: str) -> bool:
        return key in self._listing()

    def describe(self) -> dict[str, object]:
        return {"backend": self.name, "persistent": True,
                "location": self.api + "/" + BLOB_PREFIX,
                "store_id": self._store_id, "last_error": self.last_error}


class FallbackStorage:
    """Magazyn trwały z dyskiem jako siatką bezpieczeństwa.

    Odczyt sprawdza najpierw magazyn, potem dysk — dzięki temu dane zapisane, zanim
    magazyn został podpięty, nadal są widoczne. Zapis idzie do obu miejsc: magazyn daje
    trwałość, a kopia na dysku sprawia, że w obrębie jednej instancji nic nie zależy
    od dostępności sieci.
    """

    def __init__(self, primary: BlobStorage, backup: LocalStorage) -> None:
        self.primary, self.backup = primary, backup
        self.degraded = False        # magazyn odmówił współpracy przy ostatnim zapisie

    @property
    def name(self) -> str:
        return f"{self.primary.name} (kopia na dysku)"

    @property
    def persistent(self) -> bool:
        return not self.degraded

    def read(self, key: str) -> Optional[str]:
        return self.primary.read(key) or self.backup.read(key)

    def write(self, key: str, text: str) -> bool:
        ok = self.primary.write(key, text)
        self.degraded = not ok
        self.backup.write(key, text)
        return ok or self.backup.exists(key)

    def delete(self, key: str) -> bool:
        primary = self.primary.delete(key)
        backup = self.backup.delete(key)
        return primary or backup

    def exists(self, key: str) -> bool:
        return self.primary.exists(key) or self.backup.exists(key)

    def describe(self) -> dict[str, object]:
        return {
            "backend": self.name,
            "persistent": self.persistent,
            "location": self.primary.api + "/" + BLOB_PREFIX,
            "degraded": self.degraded,
            "last_error": self.primary.last_error,
        }


TOKEN_PREFIX = "vercel_blob_rw_"        # tak zaczyna się każdy token Vercel Blob
TOKEN_SUFFIX = "_READ_WRITE_TOKEN"


def oczysc_token(wartosc: str) -> str:
    """Zdejmuje z wartości to, co przykleja się przy kopiowaniu.

    Token bierze się z zakładki `.env.local`, gdzie stoi w postaci `NAZWA="wartość"`. Bardzo
    łatwo skopiować całą linię albo zostawić cudzysłowy — a wtedy w nagłówku ląduje coś,
    czego magazyn nie rozpozna, i znowu wychodzi „nie wykrywa magazynu". Sam token może
    kończyć się znakiem `=` (dopełnienie base64), więc rozcinamy tylko wtedy, gdy przed
    pierwszym `=` stoi nazwa zmiennej, a nie początek tokenu.
    """
    wartosc = wartosc.strip()
    if not wartosc.startswith(TOKEN_PREFIX) and "=" in wartosc:
        nazwa, _, reszta = wartosc.partition("=")
        if nazwa.strip() and nazwa.strip().replace("_", "").isalnum():
            wartosc = reszta.strip()
    return wartosc.strip().strip('"').strip("'").strip()


def normalizuj_id_magazynu(wartosc: str) -> str:
    """Identyfikator bywa podawany z przedrostkiem `store_`, a w nagłówku ma go nie być."""
    wartosc = wartosc.strip()
    return wartosc[len("store_"):] if wartosc.startswith("store_") else wartosc


def id_magazynu_z_tokenu(token: str) -> str:
    """Token ma postać `vercel_blob_rw_<id magazynu>_<losowe>` — identyfikator to czwarty człon.

    Bierzemy go bez wymagania dalszych członów, tak samo jak oficjalny klient: token skrócony
    albo o nietypowej budowie ma dać to, co się da odczytać, a nie nic.
    """
    czlony = oczysc_token(token).split("_")
    return czlony[3] if len(czlony) >= 4 else ""


def sklejone_wartosci(wartosc: str) -> bool:
    """Czy w jednej wartości siedzą dwie sklejone.

    Dopełnienie base64 (`=`) występuje wyłącznie na końcu ciągu. Jeśli po nim idzie coś
    jeszcze, to znaczy, że za tokenem doklejono kolejną wartość — w praktyce klucz publiczny
    webhooków, który w panelu leży tuż obok i przy zaznaczaniu myszą łatwo złapać oba naraz.
    Magazyn odrzuca wtedy taki token, nie tłumacząc, że problem jest w jego końcówce.
    """
    return "=" in oczysc_token(wartosc).rstrip("=")


def wyglada_na_token(wartosc: str) -> bool:
    """Czy to w ogóle jest token magazynu.

    Identyfikator magazynu jest zaszyty w samym tokenie, dlatego musi mieć konkretny
    kształt. Wklejenie w to miejsce klucza publicznego webhooków — leży obok w panelu
    i też wygląda na „coś do uwierzytelniania" — kończy się odmową „Cannot get store id
    from token or header", z której nie wynika, że pomyliły się wartości.
    """
    return oczysc_token(wartosc).startswith(TOKEN_PREFIX)


def find_token() -> tuple[str, str]:
    """Szuka tokenu magazynu w środowisku. Zwraca (nazwa zmiennej, wartość).

    Vercel nazywa zmienną `BLOB_READ_WRITE_TOKEN` tylko wtedy, gdy zostawi się domyślny
    przedrostek. Przy nazwanym magazynie albo drugim magazynie w projekcie przedrostek jest
    inny — dostajemy `MOJE_DANE_READ_WRITE_TOKEN` i tak dalej. Trzymanie się jednej nazwy
    kończyło się tym, że podpięty magazyn był niewidoczny, a aplikacja twierdziła, że go nie ma.

    Pierwszeństwo ma wartość, która **wygląda** na token — dopiero potem sama nazwa zmiennej.
    Dzięki temu wartość wklejona pod właściwą nazwą, ale nie ta co trzeba, nie przesłania
    prawdziwego tokenu leżącego gdzie indziej.
    """
    domyslny = os.environ.get("BLOB_READ_WRITE_TOKEN", "")
    if wyglada_na_token(domyslny):
        return "BLOB_READ_WRITE_TOKEN", oczysc_token(domyslny)

    for nazwa, wartosc in sorted(os.environ.items()):
        if nazwa.endswith(TOKEN_SUFFIX) and wyglada_na_token(wartosc):
            return nazwa, oczysc_token(wartosc)

    for nazwa, wartosc in sorted(os.environ.items()):
        if wyglada_na_token(wartosc):
            return nazwa, oczysc_token(wartosc)

    # Nic nie ma właściwego kształtu. Oddajemy to, co stoi pod domyślną nazwą — sonda powie
    # wprost, że wartość nie wygląda na token, zamiast udawać, że zmiennej nie ma wcale.
    if domyslny.strip():
        return "BLOB_READ_WRITE_TOKEN", domyslny.strip()
    for nazwa, wartosc in sorted(os.environ.items()):
        if nazwa.endswith(TOKEN_SUFFIX) and wartosc.strip():
            return nazwa, wartosc.strip()
    return "", ""


def token_candidates() -> list[str]:
    """Nazwy zmiennych, które wyglądają na związane z magazynem — bez wartości.

    Sama nazwa nie jest tajemnicą, a wartość owszem, więc pokazujemy wyłącznie nazwy.
    Bez tego „nie wykrywa magazynu" jest nie do zdiagnozowania zdalnie: nie wiadomo,
    czy zmiennej nie ma, czy nazywa się inaczej, niż aplikacja szuka.
    """
    return sorted(
        nazwa for nazwa, wartosc in os.environ.items()
        if "BLOB" in nazwa.upper() or nazwa.endswith(TOKEN_SUFFIX)
        or wartosc.strip().startswith(TOKEN_PREFIX)
    )


def blob_credentials() -> tuple[str, str, str]:
    """Czym się uwierzytelnić i do którego magazynu. Zwraca (klucz, identyfikator, skąd).

    Vercel daje dwie drogi i obie są poprawne:

    * **token statyczny** — powstaje przy tworzeniu magazynu, niesie identyfikator w sobie,
    * **OIDC** — `VERCEL_OIDC_TOKEN` wystawiany automatycznie przy każdym uruchomieniu funkcji,
      plus `BLOB_STORE_ID` z podpięcia magazynu. Identyfikator nie siedzi w tokenie, więc idzie
      osobnym nagłówkiem.

    Druga droga nie wymaga niczego wpisywanego ręcznie, więc nie da się w niej pomylić wartości
    ani skleić dwóch w jedną. Bierzemy ją, gdy tokenu statycznego brak albo gdy nie da się z niego
    odczytać identyfikatora — wtedy `BLOB_STORE_ID` z podpięcia jest wiarygodniejszy.
    """
    _, token = find_token()
    z_tokenu = id_magazynu_z_tokenu(token) if wyglada_na_token(token) else ""
    ze_zmiennej = normalizuj_id_magazynu(os.environ.get("BLOB_STORE_ID", ""))
    oidc = os.environ.get("VERCEL_OIDC_TOKEN", "").strip()

    if wyglada_na_token(token) and not sklejone_wartosci(token) and (z_tokenu or ze_zmiennej):
        return oczysc_token(token), ze_zmiennej or z_tokenu, "token"
    if oidc and ze_zmiennej:
        return oidc, ze_zmiennej, "OIDC"
    if wyglada_na_token(token) and z_tokenu:
        return oczysc_token(token), z_tokenu, "token"
    return "", "", ""


_ACTIVE: Optional[object] = None
_SIGNATURE: Optional[tuple[str, str]] = None


def active():
    """Magazyn używany przez bibliotekę.

    Wybór jest zapamiętywany, ale związany z ustawieniami, z których wynika — zmiana
    tokenu albo katalogu unieważnia go sama. Bez tego zmiana środowiska (choćby
    w teście) zostawiałaby magazyn wskazujący na poprzednie miejsce.
    """
    global _ACTIVE, _SIGNATURE
    klucz, magazyn_id, _ = blob_credentials()
    signature = (klucz[:12] + magazyn_id, str(state_dir()))
    if _ACTIVE is None or _SIGNATURE != signature:
        local = LocalStorage()
        _ACTIVE = FallbackStorage(BlobStorage(klucz, magazyn_id), local) if klucz else local
        _SIGNATURE = signature
    return _ACTIVE


def probe() -> dict[str, object]:
    """Sprawdza magazyn pełnym cyklem: zapis, odczyt, potwierdzenie treści, usunięcie.

    Formatu API Vercel Blob nie dało się sprawdzić na żywo w środowisku, w którym projekt
    powstawał, więc to jedyny sposób, żeby rozstrzygnąć bez zgadywania, czy trwały zapis
    faktycznie działa na konkretnym wdrożeniu. Nigdy nie rzuca wyjątkiem.
    """
    magazyn = active()
    klucz = "_proba_zapisu.txt"
    tresc = f"proba {time.time():.0f}"
    kroki: list[dict[str, object]] = []

    def krok(nazwa: str, ok: bool, szczegol: str = "") -> bool:
        kroki.append({"krok": nazwa, "ok": bool(ok), "szczegol": szczegol})
        return ok

    try:
        started = time.monotonic()
        zapis = magazyn.write(klucz, tresc)
        krok("zapis", zapis, f"{round((time.monotonic() - started) * 1000)} ms")

        odczyt = magazyn.read(klucz)
        krok("odczyt", odczyt is not None)
        krok("zgodność treści", odczyt == tresc,
             "" if odczyt == tresc else "odczytano co innego, niż zapisano")

        krok("usunięcie", magazyn.delete(klucz))
        krok("po usunięciu pusto", not magazyn.exists(klucz))
    except Exception as exc:            # diagnostyka nie może sama się wywrócić
        krok("wyjątek", False, str(exc))

    # Opis czytamy dopiero po cyklu: dla magazynu z odwrotem to właśnie zapis rozstrzyga,
    # czy magazyn trwały odpowiada, czy zostaliśmy na kopii dyskowej.
    nazwa_zmiennej, token = find_token()
    opis = dict(magazyn.describe())
    opis["token_present"] = bool(token)
    opis["token_env"] = nazwa_zmiennej            # z której zmiennej wzięliśmy token
    opis["token_shape_ok"] = wyglada_na_token(token)
    opis["token_glued"] = bool(token) and sklejone_wartosci(token)
    opis["token_candidates"] = token_candidates()  # same nazwy, nigdy wartości
    opis["steps"] = kroki
    opis["ok"] = all(k["ok"] for k in kroki)
    opis["hint"] = _hint(bool(opis["ok"]), bool(opis["persistent"]), bool(opis["token_present"]),
                         list(opis["token_candidates"]), str(opis.get("last_error") or ""),
                         bool(opis["token_shape_ok"]), nazwa_zmiennej,
                         bool(opis["token_glued"]))
    return opis


def _hint(ok: bool, trwaly: bool, token: bool, kandydaci: Optional[list[str]] = None,
          blad: str = "", ksztalt_ok: bool = True, zmienna: str = "",
          sklejony: bool = False) -> str:
    """Jedno zdanie o tym, co wynik sondy właściwie znaczy.

    Sam „zapis się udał” niczego nie rozstrzyga: zapis do `/tmp` też się udaje, tyle że
    znika razem z instancją. Rozdzielamy więc „działa” od „przetrwa”.
    """
    if token and sklejony:
        # Token i klucz publiczny leżą w panelu obok siebie; przy zaznaczaniu myszą łatwo
        # złapać oba naraz. Magazyn odrzuca wtedy token, nie mówiąc, że rzecz jest w końcówce.
        return (f"Wartość w zmiennej {zmienna or 'z tokenem'} wygląda na dwie wartości sklejone "
                "w jedną: token, a zaraz za nim coś jeszcze. Token kończy się tam, gdzie kończy "
                "się jego własny ciąg — nic nie może po nim następować. Skopiuj samą wartość "
                "BLOB_READ_WRITE_TOKEN, bez sąsiedniego BLOB_WEBHOOK_PUBLIC_KEY.")
    if token and not ksztalt_ok:
        # Najczęstsza pomyłka przy ręcznym dodawaniu zmiennej: obok tokenu leży w panelu
        # klucz publiczny webhooków i łatwo skopiować nie tę wartość.
        return (f"Wartość w zmiennej {zmienna or 'z tokenem'} nie wygląda na token magazynu — "
                f"token zaczyna się od „{TOKEN_PREFIX}”. Jeśli wkleiłeś klucz publiczny "
                "(„-----BEGIN PUBLIC KEY-----”), to jest BLOB_WEBHOOK_PUBLIC_KEY, czyli inna "
                "wartość. Właściwą znajdziesz w Storage → magazyn → zakładka „.env.local”.")
    if not ok and not token:
        return ("Zapis nie zadziałał w ogóle, a magazynu trwałego nie ma — sprawdź prawa do "
                "katalogu i podepnij magazyn Vercel Blob (instrukcja w DEPLOY.md).")
    if not ok:
        return ("Token jest, ale cykl zapis–odczyt się nie domknął. Sprawdź, czy magazyn Blob "
                "jest podpięty do tego projektu i czy token nadal jest ważny.")
    if not trwaly and token and "store_not_found" in blad:
        # Identyfikator magazynu z tokenu nie odpowiada żadnemu istniejącemu magazynowi —
        # najczęściej token pochodzi z magazynu, który został skasowany albo odtworzony.
        return ("Magazyn o identyfikatorze z tego tokenu już nie istnieje (odpowiedź: "
                "store_not_found). Token pochodzi zapewne ze skasowanego magazynu. Najprościej "
                "usuń zmienną BLOB_READ_WRITE_TOKEN i zostaw samo podpięcie magazynu — "
                "aplikacja uwierzytelni się wtedy przez OIDC, korzystając z BLOB_STORE_ID, "
                "i nie ma tam czego wpisywać ręcznie. Po zmianie zrób Redeploy.")
    if not trwaly and token:
        return ("Zapis i odczyt działają, ale magazyn Blob nie przyjął danych — zostały tylko "
                "na dysku instancji, czyli znikną przy uśpieniu. Token jest ustawiony, więc "
                "problem leży po stronie magazynu."
                + (f" Magazyn odpowiedział: {blad}" if blad else
                   " Sprawdź, czy jest podpięty i czy token nie wygasł."))
    if not trwaly:
        podstawa = ("Zapis i odczyt działają, ale trafiają na dysk instancji — przy wdrożeniu "
                    "bezserwerowym znikną przy uśpieniu.")
        if "BLOB_STORE_ID" in (kandydaci or []):
            # Magazyn jest podpięty — widać jego identyfikator — ale brakuje statycznego tokenu.
            # Podpięcie istniejącego magazynu uwierzytelnia przez OIDC i dokłada tylko
            # `BLOB_STORE_ID` oraz klucz webhooków; token do zapisu powstaje przy *tworzeniu*
            # magazynu. Aplikacja rozmawia z API po tokenie, więc trzeba go dodać ręcznie.
            return (f"{podstawa} Magazyn jest podpięty (widzę BLOB_STORE_ID), ale brakuje tokenu "
                    "do zapisu. Wejdź w Storage → swój magazyn → zakładka „.env.local”, skopiuj "
                    "wartość BLOB_READ_WRITE_TOKEN, dodaj ją w Settings → Environment Variables "
                    "pod tą samą nazwą i zrób Redeploy.")
        if kandydaci:
            # Zmienne są, tylko żadna nie wygląda na token — najczęściej wdrożenie jest jeszcze
            # sprzed ich dodania, bo zmienne wchodzą w życie dopiero przy nowym wdrożeniu.
            return (f"{podstawa} Widzę zmienne: {', '.join(kandydaci)} — ale żadna nie zawiera "
                    "tokenu. Najczęściej znaczy to, że wdrożenie jest starsze niż podpięcie "
                    "magazynu: zrób ponowne wdrożenie (Deployments → Redeploy).")
        return (f"{podstawa} Nie widzę żadnej zmiennej z tokenem magazynu. Jeżeli podpięcie już "
                "zrobiłeś, zrób ponowne wdrożenie — zmienne wchodzą w życie dopiero przy nowym "
                "wdrożeniu (Deployments → Redeploy). Instrukcja jest w DEPLOY.md.")
    return "Zapis jest trwały — biblioteka przeżyje uśpienie i kolejne wdrożenia."


def reset() -> None:
    """Zapomina wybór — przydatne po ręcznej zmianie zmiennych środowiskowych."""
    global _ACTIVE, _SIGNATURE
    _ACTIVE = _SIGNATURE = None
