"""Покрытие земли (материал поверхности): ESA WorldCover 2021 v200, 10 м.

Зачем. Снимок показывает, как место ВЫГЛЯДИТ, но не говорит, ЧТО это: пашня, луг, застройка, голый грунт. В тренажёрах для
этого есть отдельный слой -- материал поверхности (в OGC CDB -- Raster Material). Он нужен тепловизору (поле, луг, город и
грунт днём и ночью греются по-разному, а по цвету снимка этого не понять), подписи удара («поле», «вода», «застройка») и
дальше -- касанию земли.

Данные. ESA WorldCover 10 m 2021 v200 (CC BY 4.0: © ESA WorldCover project / Contains modified Copernicus Sentinel data
(2021) processed by ESA WorldCover consortium). Тайлы 3 x 3 градуса, Cloud Optimized GeoTIFF, значение пикселя -- класс:
10 деревья, 20 кустарник, 30 луг, 40 пашня, 50 застройка, 60 голый грунт / редкая растительность, 70 снег и лёд,
80 вода, 90 болото, 95 мангры, 100 мох и лишайник; 0 -- нет данных. Тайлы лежат в папке landcover рядом с canopy,
недостающие для маршрута качаются в фоне, дальше -- офлайн. Читаются только нужные блоки (как лес ETH).

ДОПУЩЕНИЯ: состояние на 2021 год; лес и вода у нас и так свои (высота крон ETH, вода OSM) -- здесь они для подписи удара
и для тепловизора там, где своих данных нет."""
from __future__ import annotations

import math
import os
import threading
from typing import Dict, List, Optional, Sequence, Tuple

from .canopy import CanopyDownloader, _CanopyTile
from .common import DEFAULT_MARGIN_KM, cell_bbox, route_cells

LANDCOVER_DIRNAME = "landcover"
LC_TILE_DEG = 3
LC_URL = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/{name}"

# класс WorldCover -> подпись удара (ASCII, HUD)
LC_NAMES = {10: "TREES", 20: "SHRUBS", 30: "GRASS", 40: "FIELD", 50: "BUILT-UP", 60: "BARE GROUND", 70: "SNOW",
            80: "WATER", 90: "WETLAND", 95: "MANGROVES", 100: "MOSS"}
# каналы маски материала для видеокарты: R -- пашня, G -- луг/кустарник/болото, B -- застройка, A -- голый грунт/снег
# (лес и вода -- своими данными, сюда не входят)
LC_CHANNELS = ((40,), (20, 30, 90, 95, 100), (50,), (60, 70))
LC_MASK_PX = 512                  # маска материала слоя земли (на ближнем слое ~6 м пиксель -- как данные)


def landcover_tile_name(lat: float, lon: float) -> str:
    """Имя тайла WorldCover по юго-западному углу квадрата 3 x 3 градуса."""
    la0 = int(math.floor(lat / LC_TILE_DEG) * LC_TILE_DEG)
    lo0 = int(math.floor(lon / LC_TILE_DEG) * LC_TILE_DEG)
    return (f"ESA_WorldCover_10m_2021_v200_{'N' if la0 >= 0 else 'S'}{abs(la0):02d}"
            f"{'E' if lo0 >= 0 else 'W'}{abs(lo0):03d}_Map.tif")


def landcover_tiles_for_route(route_points: Sequence[Tuple[float, float]], margin_km: float = DEFAULT_MARGIN_KM) -> List[str]:
    names: List[str] = []
    for i, j in route_cells(route_points, margin_km):
        s, w, n, e = cell_bbox(i, j)
        for la in (s, n - 1e-9):
            for lo in (w, e - 1e-9):
                nm = landcover_tile_name(la, lo)
                if nm not in names:
                    names.append(nm)
    pts = [(la, lo) for la, lo in route_points if la or lo]
    if pts:
        first = landcover_tile_name(*pts[0])
        names.sort(key=lambda nm: nm != first)
    return names


class LandcoverMap:
    """Класс покрытия в точках по тайлам из папки; тайла нет -- 0 («нет данных»)."""

    def __init__(self, folder: str):
        self.dir = folder
        self._tiles: Dict[str, Optional[_CanopyTile]] = {}
        self.errors: Dict[str, str] = {}
        self._lock = threading.Lock()
        self.needed: List[str] = []

    def refresh(self, name: Optional[str] = None) -> None:
        with self._lock:
            for nm in ([name] if name else [k for k, v in self._tiles.items() if v is None]):
                self._tiles.pop(nm, None)
                self.errors.pop(nm, None)

    def has(self, name: str) -> bool:
        return os.path.isfile(os.path.join(self.dir, name))

    def count(self) -> int:
        try:
            return sum(1 for n in os.listdir(self.dir) if n.startswith("ESA_WorldCover") and n.endswith("_Map.tif"))
        except OSError:
            return 0

    def _tile(self, name: str) -> Optional[_CanopyTile]:
        with self._lock:
            if name in self._tiles:
                return self._tiles[name]
            path = os.path.join(self.dir, name)
            tile = None
            if os.path.isfile(path):
                try:
                    tile = _CanopyTile(path)
                    tile.nodata = 0
                except Exception as ex:
                    self.errors[name] = f"{type(ex).__name__}: {ex}"
            self._tiles[name] = tile
            return tile

    def classes(self, lat, lon, res_m: Optional[float] = None):
        """Классы WorldCover (uint8; 0 -- нет данных). res_m -- нужная детальность (уменьшенная копия тайла не грубее)."""
        import numpy as np
        la, lo = np.asarray(lat, dtype=float), np.asarray(lon, dtype=float)
        shape = np.broadcast(la, lo).shape
        la, lo = np.broadcast_to(la, shape).ravel(), np.broadcast_to(lo, shape).ravel()
        out = np.zeros(la.shape, np.uint8)
        ti = np.floor(la / LC_TILE_DEG).astype(np.int64) * 100000 + np.floor(lo / LC_TILE_DEG).astype(np.int64)
        for k in np.unique(ti):
            sel = ti == k
            tile = self._tile(landcover_tile_name(float(la[sel][0]), float(lo[sel][0])))
            if tile is not None:
                out[sel] = np.clip(tile.sample(la[sel], lo[sel], tile.level_for(res_m)), 0, 255).astype(np.uint8)
        out = out.reshape(shape)
        return out if shape else int(out)

    def name_at(self, lat: float, lon: float) -> str:
        """Подпись поверхности в точке («FIELD», «WATER»...) или "" -- данных нет."""
        try:
            return LC_NAMES.get(int(self.classes(lat, lon)), "")
        except Exception:
            return ""


def material_mask(np, classes):
    """Классы (N x N) -> маска материала RGBA uint8 по LC_CHANNELS (доля 0..255 в канале)."""
    out = np.zeros(classes.shape + (4,), np.uint8)
    for c, codes in enumerate(LC_CHANNELS):
        out[..., c] = np.isin(classes, codes).astype(np.uint8) * 255
    return out


class LandcoverDownloader(CanopyDownloader):
    """Фоновая загрузка тайлов WorldCover (тот же механизм, что у леса: .part, проверка, что пришёл GeoTIFF)."""
    _running: Dict[str, "CanopyDownloader"] = {}
    URL = LC_URL
