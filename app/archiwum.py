"""Masowe pobranie archiwum Dukascopy prosto do biblioteki — z poziomu aplikacji.

Pobranie dziesięciu lat dla kompletu instrumentów to pół miliona plików godzinowych
i grubo ponad godzina pracy. Żadne pojedyncze żądanie tego nie obejmie, a przy wdrożeniu
bezserwerowym limit wynosi kilkadziesiąt sekund — więc zamiast jednego długiego pobierania
robimy **plan i kroki**.

Plan mówi, co jest do zrobienia i dokąd doszliśmy: dla każdego instrumentu trzyma kursor
z pierwszym niepobranym dniem. Każdy krok pobiera tyle, ile mieści się w budżecie czasu,
dopisuje wynik jako kolejny kawałek i przesuwa kursor. Gdy instrument dobiegnie końca,
kawałki są sklejane w jeden zbiór i trafiają do biblioteki.

Sedno jest w tym, gdzie leży plan: w tym samym magazynie co biblioteka. Dzięki temu kolejny
krok może trafić na zupełnie inną instancję i podjąć pracę dokładnie tam, gdzie poprzednia
skończyła — a zamknięcie przeglądarki w trakcie niczego nie kasuje, tylko wstrzymuje.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from . import library, storage
from .csv_loader import Bar, DataError, bars_to_csv
from .dukascopy import INSTRUMENTS, download_window, hours_in_range

PLAN_KEY = "archiwum/plan.json"
PIERWSZY_ROK = 2003          # tak głęboko sięga archiwum Dukascopy
MAX_LAT = 25
INTERWALY = (1, 5, 15, 30, 60)

# Numer wersji zasad pobierania. Plan leży w magazynie i przeżywa wdrożenia, więc potrafi
# pochodzić z kodu, który uznawał za awarię coś, co dziś jest zwykłą dziurą — a jego notatka
# o błędzie wygląda na ekranie identycznie jak świeża awaria. Po podbiciu numeru nieudane
# instrumenty ze starego planu dostają drugą szansę wedle nowych zasad. Podbij go zawsze,
# gdy zmieniasz to, co kończy instrument błędem.
WERSJA_PLANU = 2

# Plan „w trakcie", którego nikt nie ruszył od tylu sekund, jest porzucony: przeglądarka
# zamknięta w połowie nie może blokować kolejnego pobierania na zawsze.
PORZUCONY_PO = 900.0

# Po tylu odcinkach z rzędu bez ani jednej świecy uznajemy, że archiwum nas blokuje,
# i kończymy instrument zamiast mielić przez lata na pusto.
MAX_BEZOWOCNYCH = 3

# Ile odmów archiwum pod rząd wolno ominąć, zanim uznamy instrument za nieudany. Pojedyncza
# zła doba nie może przekreślać dziesięciu lat historii — dopiero seria znaczy realną awarię.
MAX_ODMOW = 8


# --- zakres i oszacowanie ------------------------------------------------------------


def zakres_lat(lata: int) -> tuple[date, date]:
    """Okres kończy się wczoraj — dzisiejsze godziny bywają w archiwum jeszcze niegotowe."""
    koniec = date.today() - timedelta(days=1)
    poczatek = max(date(PIERWSZY_ROK, 1, 1), koniec - timedelta(days=round(365.25 * lata)))
    return poczatek, koniec


def szacunek(instrumenty: list[str], lata: int, interwal: int) -> dict[str, Any]:
    """Ile to będzie plików, świec, megabajtów i czasu — zanim cokolwiek ruszy.

    Prędkości łącza nie da się zgadnąć, więc czas podajemy widełkami: od szybkiego
    archiwum do wolnego. To ma ustawić oczekiwania, a nie udawać precyzję.
    """
    poczatek, koniec = zakres_lat(lata)
    plikow = len(hours_in_range(poczatek, koniec))
    swiec = plikow * (60 // interwal if interwal <= 60 else 1)
    razem = plikow * max(1, len(instrumenty))
    return {
        "date_from": poczatek.isoformat(),
        "date_to": koniec.isoformat(),
        "instruments": len(instrumenty),
        "files_per_instrument": plikow,
        "files_total": razem,
        "bars_per_instrument": swiec,
        "bytes_total": swiec * 66 * max(1, len(instrumenty)),
        # 24 pliki naraz; 50 ms na plik to łącze szybkie, 150 ms — przeciętne
        "seconds_fast": razem * 0.050 / 24,
        "seconds_slow": razem * 0.150 / 24,
    }


# --- plan --------------------------------------------------------------------------


def _zapisz(plan: dict[str, Any]) -> None:
    plan["updated_at"] = time.time()
    storage.active().write(PLAN_KEY, json.dumps(plan, ensure_ascii=False, indent=1))


def stan() -> Optional[dict[str, Any]]:
    """Bieżący plan albo `None`, gdy żadnego nie ma."""
    raw = storage.active().read(PLAN_KEY)
    if raw is None:
        return None
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not (isinstance(plan, dict) and plan.get("instruments")):
        return None
    return _po_wdrozeniu(plan)


def _po_wdrozeniu(plan: dict[str, Any]) -> dict[str, Any]:
    """Daje drugą szansę instrumentom, które poległy na zasadach starszych niż obecny kod.

    Plan przeżywa wdrożenie, a jego notatka o błędzie — nie. Zapisane „nie udało się pobrać
    ani jednego pliku" pochodzi z kodu, który przerywał po trzech nieudanych plikach, więc
    ginął na niedzieli: ma w archiwum tylko trzy godziny. Nowy kod tak nie robi, ale sam
    z siebie nie tknąłby zapisanego wyniku — użytkownik do końca świata widziałby awarię,
    której już nie ma. Wznawiamy więc takie instrumenty od ich kursora i pobieranie samo
    rusza dalej po odświeżeniu strony.
    """
    if plan.get("wersja") == WERSJA_PLANU:
        return plan

    nieudane = [p for p in plan["instruments"] if p.get("state") == "error"]
    for pozycja in nieudane:
        _wznow_pozycje(plan, pozycja)
    plan["wersja"] = WERSJA_PLANU
    if nieudane and plan.get("state") == "done":
        plan["state"] = "running"       # przerwane plany zostawiamy przerwanymi
    _zapisz(plan)
    return plan


def zacznij(instrumenty: list[str], lata: int, interwal: int, cena: str) -> dict[str, Any]:
    """Zakłada nowy plan. Trwający plan trzeba najpierw przerwać — inaczej dwa pobierania
    deptałyby sobie po kawałkach tego samego instrumentu."""
    kody = [k.upper() for k in instrumenty]
    nieznane = [k for k in kody if k not in INSTRUMENTS]
    if nieznane:
        raise DataError(f"Nieznane instrumenty: {', '.join(nieznane)}.")
    if not kody:
        raise DataError("Wybierz przynajmniej jeden instrument.")
    if interwal not in INTERWALY:
        raise DataError(f"Długość świecy musi być jedną z: {', '.join(map(str, INTERWALY))} minut.")
    if not 1 <= lata <= MAX_LAT:
        raise DataError(f"Liczba lat musi mieścić się w zakresie 1–{MAX_LAT}.")
    if cena not in ("bid", "ask", "mid"):
        raise DataError("Cena musi być jedną z: bid, ask, mid.")

    poprzedni = stan()
    if poprzedni and poprzedni.get("state") == "running" and not _porzucony(poprzedni):
        raise DataError("Pobieranie archiwum już trwa. Przerwij je albo poczekaj na koniec.")
    if poprzedni:
        _sprzataj(poprzedni)

    poczatek, koniec = zakres_lat(lata)
    dni = (koniec - poczatek).days + 1
    plan = {
        "id": uuid.uuid4().hex[:12],
        "state": "running",
        "interval_minutes": interwal,
        "price": cena,
        "years": lata,
        "wersja": WERSJA_PLANU,
        "date_from": poczatek.isoformat(),
        "date_to": koniec.isoformat(),
        "started_at": time.time(),
        "seconds_spent": 0.0,
        "days_total": dni * len(kody),
        "instruments": [
            {
                "code": kod,
                "label": str(INSTRUMENTS[kod]["label"]),
                "state": "pending",
                "cursor": poczatek.isoformat(),
                "days_total": dni,
                "days_done": 0,
                "bars": 0,
                "parts": 0,
                "failed_hours": 0,
                "skipped_days": 0,      # doby, z których nie przyszło nic — dziury w danych
                "bezowocne": 0,         # odcinki z rzędu bez ani jednej świecy
                "odmowy": 0,            # odmowy archiwum z rzędu, każda pomija jedną dobę
                "powody": {},           # dlaczego pliki nie przyszły — powód → ile razy
                "dataset_id": None,
                "note": "",
            }
            for kod in kody
        ],
    }
    _zapisz(plan)
    return plan


def _porzucony(plan: dict[str, Any]) -> bool:
    """Czy plan „w trakcie" ma jeszcze kogoś, kto go posuwa.

    Kroki idą z przeglądarki, jeden po drugim. Karta zamknięta w połowie zostawia plan
    w stanie „running" na zawsze — a wtedy przycisk startu odmawiałby bez końca. Milczenie
    dłuższe niż kilkanaście minut znaczy, że nikt tego planu już nie prowadzi.
    """
    return time.time() - float(plan.get("updated_at") or 0) > PORZUCONY_PO


def przerwij() -> Optional[dict[str, Any]]:
    """Zatrzymuje plan i sprząta niedokończone kawałki. Instrumenty domknięte wcześniej
    zostają w bibliotece — przerwanie nie odbiera tego, co już się udało."""
    plan = stan()
    if plan is None:
        return None
    if plan.get("state") == "running":
        plan["state"] = "cancelled"
    _sprzataj(plan)
    _zapisz(plan)
    return plan


def ponow() -> dict[str, Any]:
    """Wznawia instrumenty, które skończyły się błędem, nie ruszając tych udanych.

    Powtórka nie zaczyna od zera: kursor pamięta pierwszy niepobrany dzień, a ściągnięte
    godziny siedzą w pamięci podręcznej, więc dochodzi do miejsca awarii szybko. Od nowa
    rusza tylko instrument, który przeszedł cały zakres bez ani jednej świecy — tam nie ma
    czego wznawiać.
    """
    plan = stan()
    if plan is None:
        raise DataError("Nie ma planu do ponowienia.")
    if plan.get("state") == "running":
        raise DataError("Pobieranie archiwum już trwa.")

    nieudane = [p for p in plan["instruments"] if p.get("state") == "error"]
    if not nieudane:
        raise DataError("W planie nie ma nieudanych instrumentów.")

    for pozycja in nieudane:
        _wznow_pozycje(plan, pozycja)
    plan["state"] = "running"
    _zapisz(plan)
    return plan


def _wznow_pozycje(plan: dict[str, Any], pozycja: dict[str, Any]) -> None:
    """Kasuje ślad po nieudanej próbie i ustawia instrument z powrotem do kolejki."""
    pozycja["state"] = "pending"
    pozycja["note"] = ""
    pozycja["odmowy"] = 0
    pozycja["bezowocne"] = 0

    if date.fromisoformat(pozycja["cursor"]) > date.fromisoformat(plan["date_to"]):
        # Instrument przeszedł cały zakres i nic z niego nie wyszło — wznawianie od kursora
        # nie miałoby czego pobrać, więc zaczynamy od początku.
        _sprzataj_pozycje(plan, pozycja)
        pozycja.update(cursor=plan["date_from"], days_done=0, bars=0,
                       failed_hours=0, skipped_days=0)


def zapomnij() -> None:
    """Usuwa plan razem z kawałkami — po to, żeby dało się zacząć od czysta."""
    plan = stan()
    if plan:
        _sprzataj(plan)
    storage.active().delete(PLAN_KEY)


# --- wykonanie ---------------------------------------------------------------------


def krok(budzet_sekund: float, cache_dir: Optional[Path] = None) -> dict[str, Any]:
    """Wykonuje tyle pracy, ile zmieści się w budżecie, i zwraca stan planu.

    Wywołujący (przeglądarka) powtarza to wywołanie, aż plan przestanie być `running`.
    Każdy krok zapisuje postęp w magazynie, więc przerwa w dowolnym momencie kosztuje
    najwyżej ostatni, niedokończony kawałek.
    """
    plan = stan()
    if plan is None:
        raise DataError("Nie ma rozpoczętego pobierania archiwum.")
    if plan.get("state") != "running":
        return plan

    zaczeto = time.monotonic()
    koniec_budzetu = zaczeto + max(0.0, budzet_sekund)

    # Budżet sprawdzamy po każdym odcinku, nigdy przed pierwszym: krok, który nie posunął
    # niczego do przodu, zapętliłby całe pobieranie na wywołaniach bez efektu.
    while True:
        pozycja = next((p for p in plan["instruments"] if p["state"] in ("pending", "running")), None)
        if pozycja is None:
            plan["state"] = "done"
            break
        _kawalek(plan, pozycja, koniec_budzetu, cache_dir)
        if time.monotonic() >= koniec_budzetu:
            break

    # Przerwanie mogło przyjść w trakcie kroku — wtedy w magazynie leży już plan oznaczony
    # jako przerwany, a my trzymamy w ręku jego wersję sprzed pobierania. Decyzja użytkownika
    # jest ważniejsza niż wynik kroku, więc jej nie nadpisujemy; kawałki dociągnięte po
    # przerwaniu trzeba przy okazji posprzątać, bo `przerwij` jeszcze ich nie widział.
    zapisany = stan()
    if zapisany and zapisany.get("state") == "cancelled":
        plan["state"] = "cancelled"
        _sprzataj(plan)
    # Krok kończy się z wyczerpanym budżetem także wtedy, gdy pracy już nie ma. Bez tego plan
    # zostawałby „w trakcie" do następnego wywołania, a interfejs pytałby o krok bez powodu.
    elif not any(p["state"] in ("pending", "running") for p in plan["instruments"]):
        plan["state"] = "done"

    plan["seconds_spent"] = round(plan.get("seconds_spent", 0.0) + time.monotonic() - zaczeto, 1)
    _zapisz(plan)
    return plan


def _kawalek(plan: dict[str, Any], pozycja: dict[str, Any], koniec_budzetu: float,
             cache_dir: Optional[Path]) -> None:
    """Pobiera jeden odcinek jednego instrumentu i przesuwa jego kursor."""
    kursor = date.fromisoformat(pozycja["cursor"])
    koniec = date.fromisoformat(plan["date_to"])
    if kursor > koniec:
        _domknij(plan, pozycja)
        return

    if not hours_in_range(kursor, koniec):
        # Ogon zakresu bez ani jednej godziny handlu — najczęściej sama sobota na końcu.
        # Archiwum odmówiłoby takiego okna, a to nie jest awaria, tylko koniec roboty.
        pozycja["cursor"] = (koniec + timedelta(days=1)).isoformat()
        _domknij(plan, pozycja)
        return

    pozycja["state"] = "running"
    try:
        wynik = download_window(
            instrument=pozycja["code"],
            start=kursor,
            end=koniec,
            interval_minutes=plan["interval_minutes"],
            price=plan["price"],
            cache_dir=cache_dir,
            deadline=koniec_budzetu,
            # Odcinek zaczyna się od dowolnego dnia, więc pojedyncza martwa doba na jego
            # początku nie może przekreślić lat ściągniętej już historii.
            tolerate_gaps=pozycja["bars"] > 0,
        )
    except DataError as exc:
        # Odmowa archiwum nie może przekreślać instrumentu przy pierwszym potknięciu. Dopiero
        # seria takich odmów pod rząd znaczy, że dalsze próby to mielenie na pusto; pojedyncza
        # to zła doba, którą trzeba ominąć i iść dalej. Zdarza się to naprawdę — bywają dni,
        # z których archiwum nie oddaje nic, a reszta zakresu jest w porządku.
        pozycja["odmowy"] = pozycja.get("odmowy", 0) + 1
        pozycja["note"] = str(exc)
        if pozycja["odmowy"] >= MAX_ODMOW:
            pozycja["state"] = "error"
            return

        pozycja["skipped_days"] = pozycja.get("skipped_days", 0) + 1
        pozycja["days_done"] += 1
        pozycja["cursor"] = (kursor + timedelta(days=1)).isoformat()
        if date.fromisoformat(pozycja["cursor"]) > koniec:
            _domknij(plan, pozycja)
        return

    if wynik.bars:
        pozycja["parts"] += 1
        storage.active().write(_klucz_kawalka(plan["id"], pozycja["code"], pozycja["parts"]),
                               _bez_naglowka(wynik.bars))
        pozycja["bars"] += len(wynik.bars)
    pozycja["failed_hours"] += wynik.failed_hours
    # Powody z całego pobrania, nie tylko z ostatniego odcinka — dziura zgłoszona na końcu
    # ma powiedzieć, co ją spowodowało, a to widać dopiero po zsumowaniu odcinków.
    powody = pozycja.setdefault("powody", {})
    for powod, ile in wynik.reasons.items():
        powody[powod] = powody.get(powod, 0) + ile
    pozycja["skipped_days"] = pozycja.get("skipped_days", 0) + len(wynik.skipped_days)

    # Odcinek, który nie przyniósł ani jednej świecy, a same puste doby, to sygnał blokady.
    # Kilka takich z rzędu znaczy, że dalsze próby to mielenie na pusto — lepiej powiedzieć
    # to wprost i zostawić w bibliotece to, co się udało, niż ciągnąć przez lata bez danych.
    if wynik.skipped_days and not wynik.bars:
        pozycja["bezowocne"] = pozycja.get("bezowocne", 0) + 1
        if pozycja["bezowocne"] >= MAX_BEZOWOCNYCH:
            pozycja["state"] = "error"
            pozycja["note"] = (
                f"Archiwum przestało oddawać dane po {pozycja['days_done']} dniach "
                f"(pominięto {pozycja['skipped_days']} dób). Najczęściej to chwilowy limit "
                "żądań — spróbuj ponownie za jakiś czas, pobrane godziny są w pamięci podręcznej."
            )
            return
    elif wynik.bars:
        pozycja["bezowocne"] = 0
        pozycja["odmowy"] = 0
        # Notatka opisywała ominiętą dobę. Skoro dane znów płyną, przestała być prawdziwa —
        # inaczej instrument skończyłby jako „gotowe” z komunikatem o błędzie obok.
        pozycja["note"] = ""

    if wynik.covered_to is None:
        # Budżet skończył się, zanim domknął się choćby jeden dzień. Nic nie tracimy —
        # kolejny krok ruszy od tego samego dnia, a pobrane godziny są w pamięci podręcznej.
        if not wynik.stopped_early:
            pozycja["state"] = "error"
            pozycja["note"] = "Archiwum nie zwróciło żadnego domkniętego dnia."
        return

    pozycja["days_done"] += (wynik.covered_to - kursor).days + 1
    pozycja["cursor"] = (wynik.covered_to + timedelta(days=1)).isoformat()
    if date.fromisoformat(pozycja["cursor"]) > koniec:
        _domknij(plan, pozycja)


def _domknij(plan: dict[str, Any], pozycja: dict[str, Any]) -> None:
    """Skleja kawałki w jeden zbiór, zapisuje go w bibliotece i sprząta po sobie."""
    # Instrument doszedł do końca swojego zakresu, więc licznik dni ma pokazywać komplet —
    # dni bez notowań na końcu okresu nie mogą zostawiać paska postępu na 97%.
    pozycja["days_done"] = pozycja["days_total"]

    magazyn = storage.active()
    czesci = [magazyn.read(_klucz_kawalka(plan["id"], pozycja["code"], n))
              for n in range(1, pozycja["parts"] + 1)]
    wiersze = "".join(c for c in czesci if c)

    if not wiersze.strip():
        # Pusty okres i okres, z którego archiwum nic nie oddało, wyglądają tak samo w danych,
        # a znaczą co innego. Odmowy odnotowane po drodze rozstrzygają, o który przypadek chodzi.
        pozycja["state"] = "error" if pozycja.get("odmowy") else "empty"
        pozycja["note"] = pozycja["note"] or "Brak danych w tym okresie."
        _sprzataj_pozycje(plan, pozycja)
        return

    tekst = _naglowek() + wiersze
    pierwsza, ostatnia = wiersze.split("\n", 1)[0], wiersze.rstrip("\n").rsplit("\n", 1)[-1]
    od, do = pierwsza[:10], ostatnia[:10]
    nazwa = f"{pozycja['label']} · {plan['interval_minutes']} min · {od} → {do}"
    zrodlo = f"Dukascopy ({pozycja['code']})"

    dataset_id = library.dataset_id_for(tekst)
    library.save(dataset_id, tekst, nazwa, zrodlo)
    library.describe(dataset_id, nazwa, zrodlo, {
        "bars": pozycja["bars"],
        "interval_minutes": plan["interval_minutes"],
        "first_date": od,
        "last_date": do,
    })

    pozycja["state"] = "done"
    pozycja["dataset_id"] = dataset_id
    pozycja["note"] = _uwaga_o_dziurach(pozycja)
    _sprzataj_pozycje(plan, pozycja)


# --- kawałki -----------------------------------------------------------------------


def _uwaga_o_dziurach(pozycja: dict[str, Any]) -> str:
    """Co powiedzieć o kompletności gotowego zbioru.

    Pominięte doby są ważniejsze od pojedynczych godzin: godzina to drobna luka, cała doba
    to brakujący dzień handlowy, który w backteście po prostu nie istnieje. Powtórzenie
    pobrania uzupełnia takie dziury, bo udane godziny siedzą w pamięci podręcznej.
    """
    pominiete, godziny = pozycja.get("skipped_days", 0), pozycja.get("failed_hours", 0)
    if pominiete:
        jedna = pominiete == 1
        return (f"Pominięto {pominiete} {_odmiana(pominiete, 'dobę', 'doby', 'dób')} — archiwum "
                f"nie oddało z {'niej' if jedna else 'nich'} nic i "
                f"{'ten dzień nie wejdzie' if jedna else 'te dni nie wejdą'} do backtestu. "
                f"Powtórz pobranie, żeby uzupełnić braki.{_powod(pozycja)}")
    if godziny:
        return (f"Nie udało się pobrać {godziny} "
                f"{'godziny' if godziny == 1 else 'godzin'} — reszta jest "
                f"kompletna.{_powod(pozycja)}")
    return ""


def _powod(pozycja: dict[str, Any]) -> str:
    """Najczęstszy powód, dla którego pliki nie przyszły — dopisek do notatki o dziurach."""
    powody = pozycja.get("powody") or {}
    if not powody:
        return ""
    glowny = max(powody.items(), key=lambda p: p[1])
    return f" Najczęstszy powód: {glowny[0]} ({glowny[1]}×)."


def _odmiana(ile: int, jeden: str, kilka: str, wiele: str) -> str:
    """Polska odmiana rzeczownika po liczbie: 1 dobę, 2 doby, 5 dób, 12 dób, 22 doby."""
    if ile == 1:
        return jeden
    if 2 <= ile % 10 <= 4 and not 12 <= ile % 100 <= 14:
        return kilka
    return wiele


def _klucz_kawalka(plan_id: str, kod: str, numer: int) -> str:
    return f"archiwum/{plan_id}/{kod}/{numer:04d}.csv"


def _naglowek() -> str:
    return bars_to_csv([])


def _bez_naglowka(bars: list[Bar]) -> str:
    tekst = bars_to_csv(bars)
    return tekst.split("\n", 1)[1] if "\n" in tekst else ""


def _sprzataj_pozycje(plan: dict[str, Any], pozycja: dict[str, Any]) -> None:
    """Kawałki są tylko rusztowaniem — po sklejeniu nie mają po co zajmować miejsca."""
    magazyn = storage.active()
    for n in range(1, pozycja["parts"] + 1):
        magazyn.delete(_klucz_kawalka(plan["id"], pozycja["code"], n))
    pozycja["parts"] = 0


def _sprzataj(plan: dict[str, Any]) -> None:
    for pozycja in plan.get("instruments", []):
        _sprzataj_pozycje(plan, pozycja)


# --- podsumowanie dla interfejsu ------------------------------------------------------


def postep(plan: dict[str, Any]) -> dict[str, Any]:
    """Plan przeliczony na to, co pokazuje pasek postępu."""
    zrobione = sum(p["days_done"] for p in plan["instruments"])
    razem = max(1, plan.get("days_total") or 1)
    gotowe = [p for p in plan["instruments"] if p["state"] in ("done", "empty", "error")]
    udzial = min(1.0, zrobione / razem)
    zuzyte = plan.get("seconds_spent", 0.0)
    return {
        "days_done": zrobione,
        "days_total": razem,
        "fraction": udzial,
        "instruments_done": len(gotowe),
        "instruments_total": len(plan["instruments"]),
        "bars": sum(p["bars"] for p in plan["instruments"]),
        # Prognoza z tempa osiągniętego do tej pory — po kilku krokach jest już sensowna.
        "seconds_left": round(zuzyte / udzial - zuzyte) if udzial > 0.01 else None,
    }
