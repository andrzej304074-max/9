"""Biblioteka wczytanych zbiorów danych.

Każdy plik, który raz trafił do aplikacji — wgrany ręcznie, pobrany z Dukascopy czy
z Yahoo — zostaje zapisany razem z opisem: skąd pochodzi, ile ma świec, jaki obejmuje
okres i ile waży. Dzięki temu da się do niego wrócić bez ponownego pobierania.

Trwałość zależy od środowiska. Lokalnie pliki leżą w katalogu `data/` i przetrwają
restart. Przy wdrożeniu bezserwerowym jedynym zapisywalnym miejscem jest `/tmp`, które
jest ulotne i lokalne dla instancji — biblioteka działa tam jako wygoda w obrębie sesji,
a nie jako trwałe archiwum. Nazywamy to wprost zamiast obiecywać coś, czego platforma
nie jest w stanie dotrzymać.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional

from .runtime import state_dir

INDEX_NAME = "index.json"


def dataset_id_for(text: str) -> str:
    """Identyfikator wynika z treści, więc te same dane zawsze dają tę samą pozycję."""
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def _root() -> Path:
    root = state_dir() / "datasets"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _index_path() -> Path:
    return _root() / INDEX_NAME


def _read_index() -> dict[str, dict[str, Any]]:
    path = _index_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}   # uszkodzony indeks nie może wywrócić aplikacji


def _write_index(index: dict[str, dict[str, Any]]) -> None:
    try:
        _index_path().write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass        # brak miejsca albo katalog tylko do odczytu — praca trwa dalej


def _csv_path(dataset_id: str) -> Path:
    return _root() / f"{dataset_id}.csv"


def save(dataset_id: str, text: str, name: str, source: str, meta: Optional[dict[str, Any]] = None) -> None:
    """Zapisuje zbiór i jego opis. Powtórny zapis tego samego identyfikatora tylko
    odświeża opis — treść jest przecież identyczna, bo identyfikator z niej wynika."""
    path = _csv_path(dataset_id)
    try:
        if not path.exists():
            path.write_text(text, encoding="utf-8")
    except OSError:
        return      # nie udało się zapisać — zbiór zostaje tylko w pamięci procesu

    index = _read_index()
    entry = index.get(dataset_id, {})
    entry.update(
        {
            "id": dataset_id,
            # Nazwy nie wymyślamy: w chwili zapisu znamy tylko surową treść. Uzupełni ją
            # `describe`, gdy zbiór zostanie sparsowany i będzie wiadomo, skąd pochodzi.
            "name": entry.get("name") or name,
            "source": source,
            "bytes": len(text.encode("utf-8")),
            "saved_at": entry.get("saved_at") or time.time(),
            "used_at": time.time(),
        }
    )
    if meta:
        entry.update({k: v for k, v in meta.items() if v is not None})
    index[dataset_id] = entry
    _write_index(index)


def describe(dataset_id: str, name: str, source: str, meta: dict[str, Any]) -> None:
    """Uzupełnia opis już zapisanego zbioru. Metadane znamy dopiero po sparsowaniu,
    czyli później niż w chwili zapisu treści."""
    index = _read_index()
    entry = index.get(dataset_id)
    if entry is None:
        return
    if not entry.get("name"):        # pusta nazwa to brak nazwy, a nie nazwa
        entry["name"] = name
    entry["source"] = source or entry.get("source", "")
    entry.update({k: v for k, v in meta.items() if v is not None})
    entry["used_at"] = time.time()
    index[dataset_id] = entry
    _write_index(index)


def entries() -> list[dict[str, Any]]:
    """Zapisane zbiory, od ostatnio używanych. Pozycje bez pliku na dysku odpadają."""
    index = _read_index()
    out = [e for i, e in index.items() if _csv_path(i).exists()]
    out.sort(key=lambda e: e.get("used_at", 0), reverse=True)
    return out


def get_text(dataset_id: str) -> Optional[str]:
    path = _csv_path(dataset_id)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    touch(dataset_id)
    return text


def touch(dataset_id: str) -> None:
    """Odnotowuje użycie, żeby lista układała się od ostatnio otwieranych."""
    index = _read_index()
    if dataset_id in index:
        index[dataset_id]["used_at"] = time.time()
        _write_index(index)


def rename(dataset_id: str, name: str) -> bool:
    name = name.strip()
    if not name:
        return False
    index = _read_index()
    if dataset_id not in index:
        return False
    index[dataset_id]["name"] = name[:120]
    _write_index(index)
    return True


def remove(dataset_id: str) -> bool:
    index = _read_index()
    existed = dataset_id in index or _csv_path(dataset_id).exists()
    index.pop(dataset_id, None)
    _write_index(index)
    try:
        _csv_path(dataset_id).unlink(missing_ok=True)
    except OSError:
        pass
    return existed


def usage() -> dict[str, Any]:
    """Ile miejsca zajmuje biblioteka — przydaje się przy ulotnym `/tmp`."""
    items = entries()
    return {
        "count": len(items),
        "bytes": sum(int(e.get("bytes") or 0) for e in items),
    }
