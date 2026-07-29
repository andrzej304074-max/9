#!/usr/bin/env python3
"""Masowe pobranie archiwum Dukascopy do biblioteki aplikacji — z wiersza poleceń.

Pobiera zadany okres dla wybranych instrumentów i zapisuje każdy z nich jako gotowy
zbiór w bibliotece, tej samej, którą widać w panelu „Zapisane dane". Po zakończeniu
wystarczy uruchomić aplikację i wczytać dane jednym kliknięciem, bez czekania na sieć.

To samo da się dziś zrobić z poziomu aplikacji: panel „Zapisane dane" → „Pobierz całe
archiwum Dukascopy". Tamta droga dzieli pracę na kroki mieszczące się w limicie czasu
serwera, więc działa także na wdrożeniu bezserwerowym. Ten skrypt zostaje dla pracy
lokalnej: nie wymaga otwartej przeglądarki i pobiera jednym ciągiem, bez dzielenia.

Przykłady:

    python3 tools/pobierz_archiwum.py --sprawdz          # tylko oszacowanie, bez pobierania
    python3 tools/pobierz_archiwum.py                    # 10 lat, wszystkie instrumenty
    python3 tools/pobierz_archiwum.py --lata 3 --instrumenty GBPUSD EURUSD
    python3 tools/pobierz_archiwum.py --interwal 5 --cena mid

Pobieranie da się przerwać Ctrl+C — pobrane godziny zostają w pamięci podręcznej,
więc ponowne uruchomienie ruszy znacznie szybciej i dokończy resztę.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import library
from app.archiwum import zakres_lat
from app.csv_loader import Bar, DataError, bars_to_csv
from app.dukascopy import INSTRUMENTS, download_window, hours_in_range
from app.runtime import state_dir

CACHE = state_dir() / "dukascopy_cache"


def ludzko(sekundy: float) -> str:
    if sekundy < 90:
        return f"{sekundy:.0f} s"
    if sekundy < 5400:
        return f"{sekundy / 60:.0f} min"
    return f"{sekundy / 3600:.1f} h"


def rozmiar(bajty: float) -> str:
    if bajty >= 1024 ** 3:
        return f"{bajty / 1024 ** 3:.1f} GB"
    if bajty >= 1024 ** 2:
        return f"{bajty / 1024 ** 2:.0f} MB"
    return f"{bajty / 1024:.0f} KB"


def lata_wstecz(lata: int) -> tuple[date, date]:
    """Ten sam zakres, co liczy aplikacja — reguła musi być jedna, inaczej skrypt i panel
    pobierałyby różne okresy pod tą samą nazwą."""
    return zakres_lat(lata)


def oszacuj(instrumenty: list[str], poczatek: date, koniec: date, interwal: int) -> None:
    plikow = len(hours_in_range(poczatek, koniec))
    swiec = plikow * (60 // interwal if interwal <= 60 else 1)
    print(f"  okres:            {poczatek} → {koniec}")
    print(f"  instrumentów:     {len(instrumenty)} ({', '.join(instrumenty)})")
    print(f"  plików na jeden:  {plikow:,}".replace(",", " "))
    print(f"  plików razem:     {plikow * len(instrumenty):,}".replace(",", " "))
    print(f"  świec na jeden:   ~{swiec:,}".replace(",", " "))
    print(f"  rozmiar razem:    ~{rozmiar(swiec * 66 * len(instrumenty))}")
    for ms in (50, 150):
        print(f"  czas przy {ms:3} ms/plik: ~{ludzko(plikow * len(instrumenty) * ms / 1000 / 24)}")


def pobierz_instrument(kod: str, poczatek: date, koniec: date, interwal: int, cena: str) -> bool:
    """Pobiera jeden instrument rok po roku i zapisuje go w bibliotece."""
    bars: list[Bar] = []
    rok_od = poczatek
    zaczeto = time.monotonic()

    while rok_od <= koniec:
        rok_do = min(koniec, date(rok_od.year, 12, 31))
        print(f"    {rok_od.year}: ", end="", flush=True)
        try:
            wynik = download_window(
                instrument=kod, start=rok_od, end=rok_do,
                interval_minutes=interwal, price=cena, cache_dir=CACHE,
            )
        except DataError as exc:
            print(f"nie udało się — {exc}")
            return False

        bars.extend(wynik.bars)
        braki = f", {wynik.failed_hours} godzin nieudanych" if wynik.failed_hours else ""
        print(f"{len(wynik.bars):>7,} świec{braki}".replace(",", " "))
        rok_od = date(rok_od.year + 1, 1, 1)

    if not bars:
        print("    brak danych w tym okresie — pomijam")
        return False

    bars.sort(key=lambda b: b.ts)
    tekst = bars_to_csv(bars)
    dataset_id = library.dataset_id_for(tekst)
    nazwa = f"{INSTRUMENTS[kod]['label']} · {interwal} min · {bars[0].ts:%Y-%m-%d} → {bars[-1].ts:%Y-%m-%d}"

    library.save(dataset_id, tekst, nazwa, f"Dukascopy ({kod})")
    library.describe(dataset_id, nazwa, f"Dukascopy ({kod})", {
        "bars": len(bars),
        "interval_minutes": interwal,
        "first_date": bars[0].ts.date().isoformat(),
        "last_date": bars[-1].ts.date().isoformat(),
    })
    print(f"    zapisano: {len(bars):,} świec, {rozmiar(len(tekst.encode()))}, "
          f"{ludzko(time.monotonic() - zaczeto)}".replace(",", " "))
    return True


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--lata", type=int, default=10, help="ile lat wstecz (domyślnie 10)")
    p.add_argument("--instrumenty", nargs="+", default=sorted(INSTRUMENTS),
                   help="kody instrumentów; domyślnie wszystkie")
    p.add_argument("--interwal", type=int, default=15, help="długość świecy w minutach (domyślnie 15)")
    p.add_argument("--cena", choices=("bid", "ask", "mid"), default="bid")
    p.add_argument("--sprawdz", action="store_true", help="tylko oszacuj, nic nie pobieraj")
    p.add_argument("--tak", action="store_true", help="nie pytaj o potwierdzenie")
    args = p.parse_args()

    nieznane = [k for k in args.instrumenty if k.upper() not in INSTRUMENTS]
    if nieznane:
        print(f"Nieznane instrumenty: {', '.join(nieznane)}")
        print(f"Dostępne: {', '.join(sorted(INSTRUMENTS))}")
        return 2

    instrumenty = [k.upper() for k in args.instrumenty]
    poczatek, koniec = lata_wstecz(args.lata)

    print("Pobieranie archiwum Dukascopy do biblioteki aplikacji\n")
    oszacuj(instrumenty, poczatek, koniec, args.interwal)
    print(f"\n  biblioteka:       {state_dir() / 'datasets'}")
    print(f"  pamięć podręczna: {CACHE}")

    if args.sprawdz:
        print("\n(tryb sprawdzenia — nic nie pobrano)")
        return 0

    if args.interwal == 1 and args.lata > 2:
        print("\n  UWAGA: świece 1-minutowe przy wieloletnim zakresie dają pliki rzędu setek MB.")

    if not args.tak:
        try:
            if input("\nZaczynamy? [t/N] ").strip().lower() not in ("t", "tak", "y"):
                return 0
        except (EOFError, KeyboardInterrupt):
            return 0

    print()
    udane = 0
    zaczeto = time.monotonic()
    try:
        for i, kod in enumerate(instrumenty, 1):
            print(f"[{i}/{len(instrumenty)}] {INSTRUMENTS[kod]['label']} ({kod})")
            if pobierz_instrument(kod, poczatek, koniec, args.interwal, args.cena):
                udane += 1
    except KeyboardInterrupt:
        print("\n\nPrzerwano. Pobrane godziny zostały w pamięci podręcznej — "
              "kolejne uruchomienie ruszy znacznie szybciej.")
        return 130

    uzycie = library.usage()
    print(f"\nGotowe: {udane} z {len(instrumenty)} instrumentów w {ludzko(time.monotonic() - zaczeto)}.")
    print(f"Biblioteka zawiera teraz {uzycie['count']} zbiorów, razem {rozmiar(uzycie['bytes'])}.")
    print("Uruchom aplikację (./run.sh) — dane czekają w panelu „Zapisane dane”.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
