"""Здания: Overture Maps (высота и этажность), кэш квадратов, резерв из прежних файлов.

Часть пакета geodata (бывший obstacles.py); старое имя `import obstacles` продолжает работать."""
from __future__ import annotations
import math
import os
import threading
import time
from typing import (Dict, List, Optional, Sequence, Tuple)
from .common import (CACHE_TTL_DAYS, DEFAULT_MARGIN_KM, _ring_area_m2, cell_bbox, route_cells)
from .overture import (
    FLOOR_HEIGHT_M,
    OVERTURE_CACHE_VERSION,
    OVERTURE_MAX_FAILS,
    OvertureReader,
    ROOF_ALLOWANCE_M,
    _wkb_outer_ring,
    overture_available,
)


# =====================================================================================================================
# ЗДАНИЯ: Microsoft Global ML Building Footprints (контуры, найденные нейросетью на снимках; лицензия ODbL, как у OSM)
#
# Зачем. Ориентиры на малой высоте, ночной тепловизор (дома -- тёплые), удар о здания при низких проходах.
# Данные. Готовые файлы по квадратам quadkey уровня 9 (~45 x 45 км): сжатый GeoJSON по строке на здание. Сервер ничего не
# считает -- просто отдаёт файл (Overpass с объёмными запросами у нас не справлялся). Квадрат скачивается один раз в папку
# buildings рядом с кэшем, переводится в компактные массивы (npz); дальше -- офлайн.
# ДОПУЩЕНИЯ: высоты в наборе есть не везде (в нашем районе -- ни одной): нет высоты -- условная по площади контура
# (BUILDING_HEIGHT_BY_AREA), такая помечается «умовна»; контур -- внешнее кольцо (дворы-колодцы не вырезаются).
# =====================================================================================================================
BUILDINGS_DIRNAME = "buildings"


BUILDINGS_QK_ZOOM = 9


# Отдельный загрузчик Microsoft убран: его дома входят в Overture (заполняют пустоты после OSM). Старые файлы ms_v1_*
# остаются резервом, если Overture для квадрата не ответил.
BUILDINGS_CACHE_VERSION = 1


BUILDING_HEIGHT_BY_AREA = ((40.0, 3.0), (250.0, 6.0), (1500.0, 9.0), (float("inf"), 12.0))   # (площадь до, м2) -> высота, м


def quadkey(lat: float, lon: float, z: int = BUILDINGS_QK_ZOOM) -> str:
    n = 2 ** z
    x = min(n - 1, max(0, int((lon + 180.0) / 360.0 * n)))
    s_ = math.sin(math.radians(max(-85.05, min(85.05, lat))))
    y = min(n - 1, max(0, int((0.5 - math.log((1 + s_) / (1 - s_)) / (4 * math.pi)) * n)))
    return "".join(str(((x >> i) & 1) + 2 * ((y >> i) & 1)) for i in range(z - 1, -1, -1))


def quadkey_bbox(qk: str) -> Tuple[float, float, float, float]:
    """(юг, запад, север, восток) квадрата quadkey."""
    x = y = 0
    for ch in qk:
        d = int(ch)
        x, y = x * 2 + (d & 1), y * 2 + (d >> 1)
    n = 2 ** len(qk)

    def lat(yy):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))
    return lat(y + 1), x / n * 360.0 - 180.0, lat(y), (x + 1) / n * 360.0 - 180.0


def buildings_quadkeys_for_route(route_points: Sequence[Tuple[float, float]], margin_km: float = DEFAULT_MARGIN_KM) -> List[str]:
    """Квадраты, покрывающие коридор маршрута (ближние к старту -- первыми)."""
    keys: List[str] = []
    for i, j in route_cells(route_points, margin_km):
        s_, w, n_, e = cell_bbox(i, j)
        for a in range(5):
            for b in range(5):
                k = quadkey(s_ + (n_ - s_) * a / 4.0 - (1e-9 if a == 4 else 0), w + (e - w) * b / 4.0 - (1e-9 if b == 4 else 0))
                if k not in keys:
                    keys.append(k)
    pts = [(la, lo) for la, lo in route_points if la or lo]
    if pts:
        la0, lo0 = pts[0]

        def dist(k):
            s_, w, n_, e = quadkey_bbox(k)
            return ((s_ + n_) / 2 - la0) ** 2 + ((w + e) / 2 - lo0) ** 2
        keys.sort(key=dist)
    return keys


def building_height_by_area(area_m2: float) -> float:
    for limit, h in BUILDING_HEIGHT_BY_AREA:
        if area_m2 < limit:
            return h
    return BUILDING_HEIGHT_BY_AREA[-1][1]


class BuildingCell:
    """Здания одной ячейки в массивах: вершины подряд (lat, lon), границы колец, высоты, рамки контуров."""

    def __init__(self, lat, lon, offsets, height, assumed):
        import numpy as np
        self.lat, self.lon = np.asarray(lat, dtype=np.float64), np.asarray(lon, dtype=np.float64)
        self.offsets = np.asarray(offsets, dtype=np.int64)
        self.height, self.assumed = np.asarray(height, dtype=np.float32), np.asarray(assumed, dtype=bool)
        n = len(self.height)
        if n:
            self.bbox = np.zeros((n, 4))
            self.bbox[:, 0] = np.minimum.reduceat(self.lat, self.offsets[:-1])
            self.bbox[:, 1] = np.minimum.reduceat(self.lon, self.offsets[:-1])
            self.bbox[:, 2] = np.maximum.reduceat(self.lat, self.offsets[:-1])
            self.bbox[:, 3] = np.maximum.reduceat(self.lon, self.offsets[:-1])
        else:
            self.bbox = np.zeros((0, 4))

    @classmethod
    def from_parsed(cls, items):
        import numpy as np
        lat, lon, off, hh, aa = [], [], [0], [], []
        for ring, h, assumed in items:
            ring = ring[:-1]                               # без повтора первой точки
            lat += [p[0] for p in ring]
            lon += [p[1] for p in ring]
            off.append(off[-1] + len(ring))
            hh.append(h)
            aa.append(assumed)
        return cls(np.array(lat), np.array(lon), np.array(off), np.array(hh), np.array(aa, dtype=bool))

    def __len__(self):
        return len(self.height)

    def ring(self, k: int):
        a, b = self.offsets[k], self.offsets[k + 1]
        return self.lat[a:b], self.lon[a:b]

    def indices_at(self, lat: float, lon: float):
        """Номера зданий, в контуре которых лежит точка. Рамки -- векторно, затем точка-в-многоугольнике для кандидатов."""
        import numpy as np
        if not len(self):
            return []
        b = self.bbox
        cand = np.nonzero((b[:, 0] <= lat) & (lat <= b[:, 2]) & (b[:, 1] <= lon) & (lon <= b[:, 3]))[0]
        out = []
        for k in cand:
            la, lo = self.ring(int(k))
            j = np.roll(np.arange(len(la)), 1)
            crosses = ((la > lat) != (la[j] > lat)) & (lon < (lo[j] - lo) * (lat - la) / np.where(la[j] - la == 0, 1e-12, la[j] - la) + lo)
            if np.count_nonzero(crosses) % 2 == 1:
                out.append(int(k))
        return out

    def top_at(self, lat: float, lon: float) -> Tuple[float, bool]:
        """Высота здания (м) под точкой: 0 -- не над зданием."""
        best, best_assumed = 0.0, False
        for k in self.indices_at(lat, lon):
            if self.height[k] > best:
                best, best_assumed = float(self.height[k]), bool(self.assumed[k])
        return best, best_assumed


class BuildingLoader:
    """Фоновая загрузка зданий Overture по коридору маршрута: npz в папке buildings, иначе чтение квадрата из Overture."""

    def __init__(self, route_points: Sequence[Tuple[float, float]], folder: str, margin_km: float = DEFAULT_MARGIN_KM,
                 ttl_days: float = CACHE_TTL_DAYS, use_overture: Optional[bool] = None):
        self.cells = buildings_quadkeys_for_route(route_points, margin_km)   # квадраты quadkey (имя «cells» -- для строки состояния)
        self._route0 = tuple(route_points[0]) if len(route_points) else None
        self.dir = folder
        self.ttl_days = ttl_days
        self.data: Dict[str, BuildingCell] = {}
        self.bbox: Dict[str, Tuple[float, float, float, float]] = {k: quadkey_bbox(k) for k in self.cells}
        self.cells_done = self.from_cache = self.from_net = 0
        self.part = None
        self.last_error = None
        self.parts_ok = self.parts_failed = 0
        self.failed: List[Tuple[str, str]] = []
        self.finished, self._closed, self._thread = False, False, None
        self.paths: Dict[str, str] = {}                                  # квадрат -> файл кэша (Overture или Microsoft)
        self.use_overture = overture_available() if use_overture is None else use_overture
        self._ovt = OvertureBuildings(folder) if self.use_overture else None
        self.source_counts = {"overture": 0, "microsoft": 0}
        self.overture_error: Optional[str] = None
        self.overture_fails = 0                       # подряд; после OVERTURE_MAX_FAILS за этот запуск -- только Microsoft

    def ovt_path(self, qk: str) -> str:
        return os.path.join(self.dir, f"ovt_v{OVERTURE_CACHE_VERSION}_{qk}.npz")

    def _fetch(url: str) -> bytes:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "MissionAnalyzer/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()

    def path(self, qk: str) -> str:
        return os.path.join(self.dir, f"ms_v{BUILDINGS_CACHE_VERSION}_{qk}.npz")

    def path_for(self, qk: str) -> str:
        return self.paths.get(qk) or self.path(qk)

    @staticmethod
    def load_file(path: str, ttl_days: Optional[float] = None) -> Optional[BuildingCell]:
        import numpy as np
        try:
            with np.load(path) as z:
                if ttl_days is not None and (time.time() - float(z["fetched"])) / 86400.0 > ttl_days:
                    return None
                return BuildingCell(z["lat"], z["lon"], z["offsets"], z["height"], z["assumed"])
        except (OSError, KeyError, ValueError):
            return None

    def _save(self, qk: str, cell: BuildingCell, path: Optional[str] = None) -> None:
        import numpy as np
        path = path or self.path(qk)
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = path + ".tmp.npz"
            np.savez_compressed(tmp, lat=cell.lat, lon=cell.lon, offsets=cell.offsets, height=cell.height,
                                assumed=cell.assumed, fetched=np.array(time.time()))
            os.replace(tmp, path)
        except OSError:
            pass

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._closed = True

    def total(self) -> int:
        return sum(len(c) for c in list(self.data.values()))

    def loaded(self) -> List[Tuple[str, Tuple[float, float, float, float]]]:
        """Загруженные квадраты: [(файл npz, рамка)] -- видеокарта читает их сама."""
        return [(self.path_for(k), self.bbox[k]) for k in list(self.data) if os.path.isfile(self.path_for(k))]

    def top_at(self, lat: float, lon: float) -> Tuple[float, bool]:
        cell = self.data.get(quadkey(lat, lon))
        return cell.top_at(lat, lon) if cell is not None else (0.0, False)

    def cell_at(self, lat: float, lon: float):
        """Ячейка зданий (BuildingCell), в которой лежит точка, или None (ещё не загружена)."""
        return self.data.get(quadkey(lat, lon))

    follow = False                # в полёте: квадраты вокруг борта догружаются и вне маршрута
    align = None                  # geodata.align.AlignMap: дома сдвигаются под снимок при загрузке (файл на диске -- как скачан)
    focus = None                  # (широта, долгота) борта -- ставит симуляция
    FOLLOW_RADIUS_KM = 6.0
    FOLLOW_RETRY_S = 60.0
    window_km = None              # в полёте: квадраты маршрута ближе window_km к борту (None -- все сразу, как раньше)
    UNLOAD_KM = 40.0              # ... а дальше этого -- выгружать из памяти (вернётся борт -- загрузятся снова)
    idle = False
    version = 0                   # меняется, когда меняется набор загруженных квадратов

    def _load_qk(self, n: int, qk: str) -> None:
        """Один квадрат: Overture (кэш, сеть), иначе прежние файлы на диске."""
        cell = None
        if self.use_overture:                                    # Overture: свой кэш, затем сеть
            cell = self.load_file(self.ovt_path(qk), self.ttl_days)
            if cell is not None:
                self.paths[qk] = self.ovt_path(qk)
                self.source_counts["overture"] += 1
            elif self.overture_fails < OVERTURE_MAX_FAILS:
                try:
                    self.part = (n + 1, "Overture")
                    items = self._ovt.read(self.bbox[qk])
                    cell = BuildingCell.from_parsed(items)
                    self._save(qk, cell, self.ovt_path(qk))
                    self.paths[qk] = self.ovt_path(qk)
                    self.from_net += 1
                    self.parts_ok += 1
                    self.source_counts["overture"] += 1
                    self.overture_fails = 0
                except Exception as ex:                          # Overture не вышел -- этот квадрат из резервного кэша
                    self.overture_error = f"{type(ex).__name__}: {ex}"
                    self.overture_fails += 1
                    cell = None
            if cell is not None:
                self.data[qk] = self.align.shift_cell(cell) if self.align is not None else cell
                self.cells_done = n + 1
                self.part = None
                return
        # Overture не дал квадрат (нет pyarrow, нет сети) -- резерв: прежние файлы Overture или Microsoft на диске
        for path in (self.ovt_path(qk), self.path(qk)):
            old = self.load_file(path)
            if old is not None:
                self.data[qk] = self.align.shift_cell(old) if self.align is not None else old
                self.paths[qk] = path
                self.source_counts["overture" if path == self.ovt_path(qk) else "microsoft"] += 1
                self.from_cache += 1
                break
        else:
            self.last_error = self.overture_error or ("pyarrow не встановлено" if not self.use_overture else "немає даних")
            self.parts_failed += 1
            self.failed.append((qk, self.last_error))
        self.cells_done = n + 1
        self.part = None

    def _qk_km(self, qk, f) -> float:
        s_, w_, n_, e_ = self.bbox.get(qk) or quadkey_bbox(qk)
        dla = max(s_ - f[0], 0.0, f[0] - n_)
        dlo = max(w_ - f[1], 0.0, f[1] - e_)
        return math.hypot(dla * 110.54, dlo * 111.32 * max(math.cos(math.radians(f[0])), 0.05))

    def _run_window(self) -> None:
        """В полёте: держать загруженными квадраты маршрута ближе window_km к борту (и вокруг борта FOLLOW_RADIUS_KM вне
        маршрута), дальние UNLOAD_KM -- выгружать. Раньше грузился весь маршрут сразу: на длинном -- миллионы домов, гигабайты
        памяти и минуты процессора, а борт мог и не долететь."""
        route = list(self.cells)
        tried: Dict[str, float] = {}
        n = 0
        while not self._closed:
            f = self.focus or getattr(self, "_route0", None)
            if f is None:
                time.sleep(0.5)
                continue
            want = [q for q in route if self._qk_km(q, f) <= self.window_km]
            if self.follow:
                want += [q for q in buildings_quadkeys_for_route([f], self.FOLLOW_RADIUS_KM) if q not in want]
            for q in [q for q in list(self.data) if self._qk_km(q, f) > self.UNLOAD_KM]:
                self.data.pop(q, None)                                   # далеко позади (или впереди после перемотки)
                self.version += 1
            todo = [q for q in want if q not in self.data and time.time() - tried.get(q, -1e9) > self.FOLLOW_RETRY_S]
            if not todo:
                self.idle = True
                if not self.finished and all(q in self.data or q in tried for q in route):
                    self.finished = True
                time.sleep(1.0)
                continue
            self.idle = False
            todo.sort(key=lambda q: self._qk_km(q, f))
            qk = todo[0]
            tried[qk] = time.time()
            if qk not in self.bbox:
                self.bbox[qk] = quadkey_bbox(qk)
            if qk not in self.cells:
                self.cells.append(qk)
            try:
                self._load_qk(n, qk)
            except Exception as exc:
                self.failed.append((qk, f"{type(exc).__name__}: {exc}"))
            n += 1
            self.version += 1
        self.part = None

    def _run(self) -> None:
        if self.window_km:
            return self._run_window()
        try:
            for n, qk in enumerate(list(self.cells)):
                if self._closed:
                    return
                self._load_qk(n, qk)
        finally:
            self.finished = True
            self.part = None
        tried: Dict[str, float] = {}
        while self.follow and not self._closed:                      # полёт вне коридора: квадраты вокруг борта
            f = self.focus
            todo = [q for q in (buildings_quadkeys_for_route([f], self.FOLLOW_RADIUS_KM) if f else [])
                    if q not in self.data and time.time() - tried.get(q, -1e9) > self.FOLLOW_RETRY_S]
            if not todo:
                time.sleep(1.0)
                continue
            fi, fo = f
            todo.sort(key=lambda q: (sum(quadkey_bbox(q)[0::2]) / 2 - fi) ** 2 + (sum(quadkey_bbox(q)[1::2]) / 2 - fo) ** 2)
            qk = todo[0]
            tried[qk] = time.time()
            if qk not in self.bbox:
                self.bbox[qk] = quadkey_bbox(qk)
                self.cells.append(qk)
            try:
                self._load_qk(len(self.cells) - 1, qk)
            except Exception as exc:
                self.failed.append((qk, f"{type(exc).__name__}: {exc}"))


class OvertureBuildings(OvertureReader):
    """Здания Overture: общий читатель (каталог STAC, только нужные файлы) + высота из height или этажности."""

    def __init__(self, folder: str):
        super().__init__(folder, "buildings", "building")

    def read(self, bbox) -> List[Tuple[List[Tuple[float, float]], float, bool]]:
        """Здания рамки (юг, запад, север, восток): [(кольцо (lat, lon), высота, условная ли)]."""
        out = []
        for r in self.table(bbox, ["height", "num_floors", "geometry"]):
            ring = _wkb_outer_ring(r.get("geometry"))
            if ring is None:
                continue
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            h, fl = r.get("height"), r.get("num_floors")
            if h is not None and 1.5 <= float(h) <= 900.0:
                out.append((ring, float(h), False))
            elif fl is not None and 0 < int(fl) < 250:
                out.append((ring, int(fl) * FLOOR_HEIGHT_M + ROOF_ALLOWANCE_M, False))
            else:
                out.append((ring, building_height_by_area(_ring_area_m2(ring)), True))
        return out
