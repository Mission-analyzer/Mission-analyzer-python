"""
country_borders.py -- державний кордон (не лінія фронту) як альтернативне
джерело "чиясь територія" для оптимізації висоти. НА ВІДМІНУ від
occupied_layer.py (deepstatemap.live, оновлюється щодня, показує
ФАКТИЧНИЙ контроль) -- тут СТАТИЧНІ дані (Natural Earth, публічний
домен), кордон не змінюється, кешується один раз і назавжди (доки
користувач сам не видалить файл).

Джерело: https://github.com/datasets/geo-countries -- Natural Earth,
конвертовано ogr2ogr, публічний домен (Open Data Commons PDDL).
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError

COUNTRIES_URL = "https://raw.githubusercontent.com/datasets/geo-countries/main/data/countries.geojson"


def fetch_country_polygon(
    cache_dir: str, iso_a3: str, timeout: float = 15.0,
) -> list | None:
    """Повертає полігон країни (список [[(lon,lat), ...], ...] -- той
    самий формат, що occupied_layer.extract_polygons) за ISO3166-1
    Alpha-3 кодом (напр. "RUS", "UKR"). Кешується ОДИН РАЗ у
    countries.geojson поруч з рештою кешу (не щодня, як лінія фронту --
    державний кордон не змінюється). Повертає None, якщо немає ні
    мережі, ні кешу, ні такої країни у файлі."""
    cache_path = Path(cache_dir) / "countries.geojson"
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    data = None
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None  # пошкоджений кеш -- спробуємо завантажити заново

    if data is None:
        req = urllib.request.Request(
            COUNTRIES_URL, headers={"User-Agent": "MissionAnalyzer/1.0 (personal UAV mission-planning tool)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            data = json.loads(raw)
            try:
                cache_path.write_bytes(raw)
            except OSError:
                pass
        except (URLError, HTTPError, TimeoutError, OSError, json.JSONDecodeError):
            return None

    for feat in data.get("features", []):
        props = feat.get("properties", {})
        if props.get("ISO3166-1-Alpha-3") == iso_a3:
            geom = feat.get("geometry") or {}
            gtype = geom.get("type")
            coords = geom.get("coordinates", [])
            if gtype == "MultiPolygon":
                return list(coords)
            if gtype == "Polygon":
                return [coords]
    return None
