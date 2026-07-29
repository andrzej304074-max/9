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
    return plan if isinstance(plan, dict) and plan.get("instruments") else None


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
    if poprzedni and poprzedni.get("state") == "running":
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
                "dataset_id": None,
                "note": "",
            }
            for kod in kody
        ],
    }
    _zapisz(plan)
    return plan


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
        )
    except DataError as exc:
        # Archiwum nie odpowiada — nie ma sensu mielić dalej tego instrumentu.
        pozycja["state"] = "error"
        pozycja["note"] = str(exc)
        return

    if wynik.bars:
        pozycja["parts"] += 1
        storage.active().write(_klucz_kawalka(plan["id"], pozycja["code"], pozycja["parts"]),
                               _bez_naglowka(wynik.bars))
        pozycja["bars"] += len(wynik.bars)
    pozycja["failed_hours"] += wynik.failed_hours

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
    magazyn = storage.active()
    czesci = [magazyn.read(_klucz_kawalka(plan["id"], pozycja["code"], n))
              for n in range(1, pozycja["parts"] + 1)]
    wiersze = "".join(c for c in czesci if c)

    if not wiersze.strip():
        pozycja["state"] = "empty"
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
    if pozycja["failed_hours"]:
        pozycja["note"] = f"{pozycja['failed_hours']} godzin nie udało się pobrać — reszta jest kompletna."
    _sprzataj_pozycje(plan, pozycja)


# --- kawałki -----------------------------------------------------------------------


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
