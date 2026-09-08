"""
restricted_zones.py -- шар заборонних/обльотних зон поверх карти.

Крок 1 (цей файл зараз) -- ЛИШЕ населені пункти (Overpass API, через
вже наявний populated_areas.fetch_settlements), з ПРОСТОРОВИМ файловим
кешем на диску. Крок 2 (пізніше) -- додасть ручні зони (координата +
радіус, напр. АЕС чи закритий об'єкт), об'єднані з цими в один шар для
"Обліт заборонних зон"/"Оптимізація".

Просторовий кеш -- ключова відмінність від попереднього in-memory
кешу в populated_areas.py (кешував ЦІЛИЙ bbox запиту одним ключем --
будь-який ІНШИЙ, навіть частково перетинний маршрут, повністю
промахувався повз кеш). Тут простір розбитий на клітинки XYZ-сітки
(ТОЙ САМИЙ tile-scheme, що вже використовується для тайлів карти --
geo.lonlat_to_tile_xy, не окрема система координат) -- кеш росте
клітинка за клітинкою, і повторний чи частково новий маршрут довантажує
з мережі ЛИШЕ дійсно нові клітинки, а не все заново.
"""

from __future__ import annotations

import json
import os
import sys

from geo import lonlat_to_tile_xy, pixel_to_lonlat, TILE_SIZE
import populated_areas

# ~150км на клітинку на середніх широтах (Україна/Росія) -- розумний
# розмір одного запиту Overpass, не задрібно (забагато запитів на
# великий маршрут) і не завелико (один промах -- дорогий повторний
# запит по всій великій площі).
CACHE_ZOOM = 6


def _cell_bounds(tx: int, ty: int, zoom: int = CACHE_ZOOM) -> tuple[float, float, float, float]:
    """Межі клітинки (lat_min, lat_max, lon_min, lon_max) за її XYZ-
    індексом -- через pixel_to_lonlat (уже перевірена, точна обернена
    до lonlat_to_pixel), а не власна тригонометрія."""
    lat_max, lon_min = pixel_to_lonlat(tx * TILE_SIZE, ty * TILE_SIZE, zoom)
    lat_min, lon_max = pixel_to_lonlat((tx + 1) * TILE_SIZE, (ty + 1) * TILE_SIZE, zoom)
    return lat_min, lat_max, lon_min, lon_max


def _cells_covering(lat_min: float, lat_max: float, lon_min: float, lon_max: float,
                     zoom: int = CACHE_ZOOM) -> set[tuple[int, int]]:
    """Усі клітинки XYZ-сітки, що перетинають bbox. min/max явно (не
    покладаємось на порядок north-west/south-east) -- у XYZ-схемі Y
    росте на ПІВДЕНЬ, легко переплутати напрямок і отримати порожній
    чи неправильний діапазон."""
    x0, y0 = lonlat_to_tile_xy(lat_max, lon_min, zoom)
    x1, y1 = lonlat_to_tile_xy(lat_min, lon_max, zoom)
    x_min, x_max = min(x0, x1), max(x0, x1)
    y_min, y_max = min(y0, y1), max(y0, y1)
    return {(x, y) for x in range(x_min, x_max + 1) for y in range(y_min, y_max + 1)}


def default_cache_path() -> str:
    """Файл поруч з .exe/app.py -- та сама логіка, що aircraft_profiles.
    default_profiles_path(), незалежно продубльована (не імпортуємо з
    app.py) щоб уникнути циклічного імпорту."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "restricted_zones_cache.json")


def _load_cache(path: str) -> dict:
    """Пошкоджений/відсутній файл -- тихо порожній кеш (не падає, не
    ламає перший запуск чи випадкове затирання файлу)."""
    if not os.path.exists(path):
        return {"covered_cells": [], "settlements": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "covered_cells": data.get("covered_cells", []),
            "settlements": data.get("settlements", []),
        }
    except (json.JSONDecodeError, OSError, TypeError):
        return {"covered_cells": [], "settlements": []}


def _save_cache(path: str, covered_cells: set, settlements: list) -> None:
    data = {
        "covered_cells": [list(c) for c in covered_cells],
        "settlements": settlements,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _dedupe_key(s: dict) -> tuple:
    """Назва + округлені координати -- сусідні клітинки можуть повернути
    той самий НП біля спільної межі (Overpass повертає точку, якщо вона
    хоч трохи заходить у межі запиту)."""
    return (s["name"], round(s["lat"], 4), round(s["lon"], 4))


def get_settlements_for_bbox(
    lat_min: float, lat_max: float, lon_min: float, lon_max: float,
    cache_path: str | None = None,
    progress_callback=None,
) -> tuple[list[dict], bool]:
    """Населені пункти в межах bbox з просторового файлового кешу.
    Мережа торкається ЛИШЕ клітинок, які ще жодного разу не
    запитувались -- той самий маршрут (чи маршрут, що перетинається з
    уже проаналізованим) при повторному відкритті не йде в мережу
    повторно за вже відомі ділянки.

    Повертає (settlements, all_covered) -- all_covered=False означає,
    що ОДНА чи більше клітинок не довантажились (тимчасова мережева
    проблема) і НЕ позначені покритими -- спробуються знову наступного
    разу, а не залишаться прогалиною в даних назавжди. Той успіх, що
    ВЖЕ стався (інші клітинки), однаково зберігається на диск -- не
    втрачається через одну невдалу клітинку.

    progress_callback(done, total) -- прогрес довантаження відсутніх
    клітинок (можуть бути кілька на великий/новий маршрут)."""
    cache_path = cache_path or default_cache_path()
    raw = _load_cache(cache_path)
    covered_cells = {tuple(c) for c in raw["covered_cells"]}
    settlements = raw["settlements"]

    needed = _cells_covering(lat_min, lat_max, lon_min, lon_max)
    missing = sorted(needed - covered_cells)
    all_covered = True

    if missing:
        existing_keys = {_dedupe_key(s) for s in settlements}
        for i, cell in enumerate(missing):
            cell_lat_min, cell_lat_max, cell_lon_min, cell_lon_max = _cell_bounds(*cell)
            try:
                new_settlements = populated_areas.fetch_settlements(
                    cell_lat_min, cell_lat_max, cell_lon_min, cell_lon_max,
                )
            except populated_areas.OverpassError:
                all_covered = False
                if progress_callback:
                    progress_callback(i + 1, len(missing))
                continue  # НЕ позначаємо покритою -- спробуємо знову наступного разу

            for s in new_settlements:
                key = _dedupe_key(s)
                if key not in existing_keys:
                    settlements.append(s)
                    existing_keys.add(key)
            covered_cells.add(cell)
            if progress_callback:
                progress_callback(i + 1, len(missing))

        _save_cache(cache_path, covered_cells, settlements)

    result = [
        s for s in settlements
        if lat_min <= s["lat"] <= lat_max and lon_min <= s["lon"] <= lon_max
    ]
    return result, all_covered


def clear_cache(cache_path: str | None = None) -> None:
    """Повне скидання кешу -- якщо потрібні гарантовано свіжі дані
    (напр. дані OSM для регіону оновились)."""
    cache_path = cache_path or default_cache_path()
    if os.path.exists(cache_path):
        os.remove(cache_path)
