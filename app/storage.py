"""Warstwa zapisu biblioteki — dysk albo magazyn obiektów.

Lokalnie pliki leżą po prostu na dysku i przeżywają restart. Przy wdrożeniu
bezserwerowym jedyne miejsce z prawem zapisu, `/tmp`, jest ulotne i lokalne dla
instancji — biblioteka wyparowywałaby przy każdym uśpieniu.

Rozwiązaniem jest magazyn obiektów. Gdy w środowisku jest token Vercel Blob, zapis
idzie tam i przeżywa zarówno uśpienie, jak i kolejne wdrożenia. Gdy tokenu nie ma
albo magazyn nie odpowiada, wracamy na dysk — aplikacja działa dalej, tyle że
z ulotną biblioteką, i mówi o tym wprost zamiast obiecywać trwałość.

Rozmowa z Vercel Blob idzie po jego API REST przez `urllib`, bez dokładania
zależności. Formatu tego API nie dało się sprawdzić na żywo w środowisku, w którym
projekt powstawał (polityka sieciowa blokuje ruch wychodzący), dlatego każda operacja
ma odwrót do dysku, a `/api/storage/probe` pozwala sprawdzić jednym kliknięciem,
czy magazyn faktycznie działa na Twoim wdrożeniu.
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
BLOB_PREFIX = "backtester/"
BLOB_API_VERSION = "7"
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
        self._root.mkdir(parents=True, exist_ok=True)

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

    def __init__(self, token: str) -> None:
        self._token = token
        self._urls: dict[str, str] = {}     # klucz -> publiczny adres obiektu
        self._listed_at = 0.0
        self._lock = threading.Lock()

    # --- niskopoziomowe wywołania ---------------------------------------------

    def _call(self, url: str, *, method: str = "GET", body: Optional[bytes] = None,
              headers: Optional[dict[str, str]] = None) -> Optional[bytes]:
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("x-api-version", BLOB_API_VERSION)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError):
            return None

    def _listing(self, force: bool = False) -> dict[str, str]:
        with self._lock:
            if not force and self._urls and time.monotonic() - self._listed_at < LISTING_TTL:
                return self._urls

        query = urllib.parse.urlencode({"prefix": BLOB_PREFIX, "limit": "1000"})
        raw = self._call(f"{BLOB_API}?{query}")
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
        raw = self._call(
            f"{BLOB_API}/{BLOB_PREFIX}{urllib.parse.quote(key)}",
            method="PUT",
            body=text.encode("utf-8"),
            headers={
                "x-content-type": "text/csv; charset=utf-8",
                # bez tego Vercel dokleja losowy przyrostek i klucz przestaje być przewidywalny
                "x-add-random-suffix": "0",
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
            f"{BLOB_API}/delete",
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
        return {"backend": self.name, "persistent": True, "location": BLOB_API + "/" + BLOB_PREFIX}


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
            "location": BLOB_API + "/" + BLOB_PREFIX,
            "degraded": self.degraded,
        }


_ACTIVE: Optional[object] = None
_SIGNATURE: Optional[tuple[str, str]] = None


def active():
    """Magazyn używany przez bibliotekę.

    Wybór jest zapamiętywany, ale związany z ustawieniami, z których wynika — zmiana
    tokenu albo katalogu unieważnia go sama. Bez tego zmiana środowiska (choćby
    w teście) zostawiałaby magazyn wskazujący na poprzednie miejsce.
    """
    global _ACTIVE, _SIGNATURE
    token = os.environ.get("BLOB_READ_WRITE_TOKEN", "").strip()
    signature = (token[:12], str(state_dir()))
    if _ACTIVE is None or _SIGNATURE != signature:
        local = LocalStorage()
        _ACTIVE = FallbackStorage(BlobStorage(token), local) if token else local
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
    opis = dict(magazyn.describe())
    opis["token_present"] = bool(os.environ.get("BLOB_READ_WRITE_TOKEN", "").strip())
    opis["steps"] = kroki
    opis["ok"] = all(k["ok"] for k in kroki)
    opis["hint"] = _hint(bool(opis["ok"]), bool(opis["persistent"]), bool(opis["token_present"]))
    return opis


def _hint(ok: bool, trwaly: bool, token: bool) -> str:
    """Jedno zdanie o tym, co wynik sondy właściwie znaczy.

    Sam „zapis się udał” niczego nie rozstrzyga: zapis do `/tmp` też się udaje, tyle że
    znika razem z instancją. Rozdzielamy więc „działa” od „przetrwa”.
    """
    if not ok and not token:
        return ("Zapis nie zadziałał w ogóle, a magazynu trwałego nie ma — sprawdź prawa do "
                "katalogu i podepnij magazyn Vercel Blob (instrukcja w DEPLOY.md).")
    if not ok:
        return ("Token jest, ale cykl zapis–odczyt się nie domknął. Sprawdź, czy magazyn Blob "
                "jest podpięty do tego projektu i czy token nadal jest ważny.")
    if not trwaly and token:
        return ("Zapis i odczyt działają, ale magazyn Blob nie przyjął danych — zostały tylko "
                "na dysku instancji, czyli znikną przy uśpieniu. Token jest ustawiony, więc "
                "problem leży po stronie magazynu: sprawdź, czy jest podpięty i czy token nie wygasł.")
    if not trwaly:
        return ("Zapis i odczyt działają, ale trafiają na dysk instancji — przy wdrożeniu "
                "bezserwerowym znikną przy uśpieniu. Żeby biblioteka przetrwała, podepnij "
                "magazyn Vercel Blob (instrukcja w DEPLOY.md).")
    return "Zapis jest trwały — biblioteka przeżyje uśpienie i kolejne wdrożenia."


def reset() -> None:
    """Zapomina wybór — przydatne po ręcznej zmianie zmiennych środowiskowych."""
    global _ACTIVE, _SIGNATURE
    _ACTIVE = _SIGNATURE = None
