"""Лес: высота крон по карте ETH Global Canopy Height 2020.

Часть пакета geodata (бывший obstacles.py); старое имя `import obstacles` продолжает работать."""
from __future__ import annotations
import math
import os
import threading
from collections import OrderedDict
from typing import (Callable, Dict, List, Optional, Sequence, Tuple)
from .common import (DEFAULT_MARGIN_KM, cell_bbox, route_cells)


# =====================================================================================================================
# ЛЕС: высота крон по карте ETH Global Canopy Height 2020 (Lang et al., ETH Zürich; CC BY 4.0)
#
# Зачем. SRTM меряет высоту земли, а не верхушек деревьев: борт в 8 м над рельефом посреди леса уже в кронах. Высота крон
# учитывается в проверке удара, в AGL на HUD (а значит, и в TER) и рисуется камерой на видеокарте.
#
# Данные. Готовая глобальная карта высоты крон с шагом ~10 м (1/12000 градуса): значение пикселя -- высота верхушек деревьев
# в метрах (0..254), 255 -- нет данных. Тайлы 3 x 3 градуса, Cloud Optimized GeoTIFF, внутри -- блоки 512 x 512: читаются ТОЛЬКО
# блоки с нужными точками (тайл целиком -- больше гигабайта). Привязка тайла -- из его GeoTIFF-тегов, не из имени.
# Тайлы лежат в папке canopy (как srtm); недостающие для маршрута качаются в фоне (CanopyDownloader), дальше -- офлайн.
#
# ДОПУЩЕНИЯ: состояние на 2020 год (вырубки позже не видны); низкие кроны карта слегка завышает -- ниже CANOPY_MIN_M считаем
# «не лес» (кусты, трава, шум модели).
# =====================================================================================================================
CANOPY_DIRNAME = "canopy"


CANOPY_MIN_M = 5.0              # ниже -- не лес


CANOPY_TILE_DEG = 3


CANOPY_URL = "https://libdrive.ethz.ch/index.php/s/cO8or7iOe5dT2Rt/download?path=%2F3deg_cogs&files={name}"


CANOPY_CACHE_MB = 128           # распакованные блоки в памяти на тайл (в тайлах ETH блок 1024 x 1024 = 1 МБ, чтение ~0.1 с)


def canopy_tile_name(lat: float, lon: float) -> str:
    """Имя тайла ETH, в котором лежит точка: по юго-западному углу квадрата 3 x 3 градуса."""
    la0 = int(math.floor(lat / CANOPY_TILE_DEG) * CANOPY_TILE_DEG)
    lo0 = int(math.floor(lon / CANOPY_TILE_DEG) * CANOPY_TILE_DEG)
    return (f"ETH_GlobalCanopyHeight_10m_2020_{'N' if la0 >= 0 else 'S'}{abs(la0):02d}"
            f"{'E' if lo0 >= 0 else 'W'}{abs(lo0):03d}_Map.tif")


def canopy_tiles_for_route(route_points: Sequence[Tuple[float, float]], margin_km: float = DEFAULT_MARGIN_KM) -> List[str]:
    """Имена тайлов ETH, покрывающих коридор маршрута (ближние к старту -- первыми)."""
    names: List[str] = []
    for i, j in route_cells(route_points, margin_km):
        s, w, n, e = cell_bbox(i, j)
        for la in (s, n - 1e-9):
            for lo in (w, e - 1e-9):
                nm = canopy_tile_name(la, lo)
                if nm not in names:
                    names.append(nm)
    pts = [(la, lo) for la, lo in route_points if la or lo]
    if pts:
        first = canopy_tile_name(*pts[0])
        names.sort(key=lambda nm: nm != first)
    return names


class _CanopyTile:
    """Один тайл ETH: чтение только нужных блоков, кэш распакованных блоков. Уровни: 0 -- полное разрешение (~10 м),
    1.. -- уменьшенные копии внутри COG (в 2, 4, 8... раз): дальним слоям земли на видеокарте полное разрешение не нужно."""

    def __init__(self, path: str):
        import tifffile
        self.path = path
        self.tf = tifffile.TiffFile(path)
        page = self.tf.pages[0]
        sx, sy = page.tags["ModelPixelScaleTag"].value[:2]
        tie = page.tags["ModelTiepointTag"].value
        self.sx, self.sy = float(sx), float(sy)
        self.lon0 = float(tie[3]) - float(tie[0]) * self.sx          # левый верхний угол растра
        self.lat0 = float(tie[4]) + float(tie[1]) * self.sy
        self.w0 = int(page.imagewidth)
        nod = page.tags.get("GDAL_NODATA")
        try:
            self.nodata = int(float(str(nod.value).strip("\x00 "))) if nod is not None else 255
        except ValueError:
            self.nodata = 255
        self.levels = []                                             # [(страница, w, h, bw, bh, nbx, шаг_град_x, шаг_град_y)]
        for pg in self.tf.pages:
            try:
                w, h = int(pg.imagewidth), int(pg.imagelength)
            except Exception:
                continue
            if self.levels and w >= self.levels[-1][1]:
                continue                                             # не уменьшенная копия (маска и т. п.)
            if pg.is_tiled:
                bw, bh = int(pg.tilewidth), int(pg.tilelength)
            else:
                bw, bh = w, int(pg.rowsperstrip or h)
            k = self.w0 / w
            self.levels.append((pg, w, h, bw, bh, -(-w // bw), self.sx * k, self.sy * k))
        self.lock = threading.Lock()
        self.blocks: "OrderedDict[Tuple[int, int], object]" = OrderedDict()
        self.cached_bytes = 0

    def level_for(self, res_m: Optional[float]) -> int:
        """Самая грубая копия, у которой пиксель не крупнее res_m метров (None -- полное разрешение)."""
        if not res_m:
            return 0
        best = 0
        for k, lv in enumerate(self.levels):
            if lv[7] * 111320.0 <= res_m:
                best = k
        return best

    def _block(self, level: int, idx: int):
        """Распакованный блок. Чтение и распаковка (~0.1 с) -- БЕЗ замка и своим дескриптором файла: такт симуляции не ждёт
        фоновое чтение соседнего блока; замок -- только на кэш."""
        import numpy as np
        with self.lock:
            b = self.blocks.get((level, idx))
            if b is not None:
                self.blocks.move_to_end((level, idx))
                return b
        pg, w, h, bw, bh = self.levels[level][:5]
        with open(self.path, "rb") as fh:
            fh.seek(int(pg.dataoffsets[idx]))
            data = fh.read(int(pg.databytecounts[idx]))
        b = np.asarray(pg.decode(data, idx)[0]).reshape(-1, bw)[:bh]
        with self.lock:
            if (level, idx) not in self.blocks:
                self.blocks[(level, idx)] = b
                self.cached_bytes += b.nbytes
                while self.cached_bytes > CANOPY_CACHE_MB * 1e6 and len(self.blocks) > 1:
                    _k, old = self.blocks.popitem(last=False)
                    self.cached_bytes -= old.nbytes
        return b

    def sample(self, lat, lon, level: int = 0):
        """Высоты крон (м, float) в точках этого тайла; вне тайла или «нет данных» -- 0."""
        import numpy as np
        _pg, w, h, bw, bh, nbx, sx, sy = self.levels[level]
        r = np.floor((self.lat0 - lat) / sy).astype(np.int64)
        c = np.floor((lon - self.lon0) / sx).astype(np.int64)
        out = np.zeros(r.shape)
        ok = (r >= 0) & (r < h) & (c >= 0) & (c < w)
        if not ok.any():
            return out
        rr, cc = r[ok], c[ok]
        key = (rr // bh) * nbx + (cc // bw)
        vals = np.zeros(rr.shape)
        for k in np.unique(key):
            sel = key == k
            blk = self._block(level, int(k))
            v = blk[rr[sel] % bh, cc[sel] % bw].astype(np.float64)
            v[v == self.nodata] = 0.0
            vals[sel] = v
        out[ok] = vals
        return out


class CanopyMap:
    """Высота крон в точках по тайлам ETH из папки. Тайл, которого нет, даёт 0 (и попадает в missing)."""

    def __init__(self, folder: str):
        self.dir = folder
        self._tiles: Dict[str, Optional[_CanopyTile]] = {}
        self.errors: Dict[str, str] = {}
        self._lock = threading.Lock()

    def refresh(self, name: Optional[str] = None) -> None:
        """Тайл докачан: открыть заново (None -- все отсутствовавшие)."""
        with self._lock:
            for nm in ([name] if name else [k for k, v in self._tiles.items() if v is None]):
                self._tiles.pop(nm, None)
                self.errors.pop(nm, None)

    def has(self, name: str) -> bool:
        return os.path.isfile(os.path.join(self.dir, name))

    def _tile(self, name: str) -> Optional[_CanopyTile]:
        with self._lock:
            if name in self._tiles:
                return self._tiles[name]
            path = os.path.join(self.dir, name)
            tile = None
            if os.path.isfile(path):
                try:
                    tile = _CanopyTile(path)
                except Exception as ex:                          # нет tifffile/imagecodecs, битый файл
                    self.errors[name] = f"{type(ex).__name__}: {ex}"
            self._tiles[name] = tile
            return tile

    def prefetch(self, lat: float, lon: float, radius_m: float = 2500.0) -> None:
        """Прочитать в фоне блоки вокруг точки (полное разрешение): к прилёту борта они уже в памяти. Одно чтение за раз."""
        if getattr(self, "_prefetching", False):
            return
        self._prefetching = True

        def work():
            try:
                import numpy as np
                dlat = radius_m / 110540.0
                dlon = radius_m / (111320.0 * max(0.2, math.cos(math.radians(lat))))
                g = np.linspace(-1.0, 1.0, 9)
                la, lo = np.meshgrid(lat + g * dlat, lon + g * dlon)
                self.canopy(la.ravel(), lo.ravel())
            except Exception:
                pass
            finally:
                self._prefetching = False
        threading.Thread(target=work, daemon=True).start()

    def canopy(self, lat, lon, res_m: Optional[float] = None):
        """Высота крон (м): CANOPY_MIN_M и выше -- лес, ниже -- 0. Числа или массивы numpy. res_m -- нужная детальность
        (шаг сетки): берётся уменьшенная копия тайла не грубее её; None -- полное разрешение (AGL, TER, удар)."""
        import numpy as np
        la, lo = np.asarray(lat, dtype=float), np.asarray(lon, dtype=float)
        shape = np.broadcast(la, lo).shape
        la, lo = np.broadcast_to(la, shape).ravel(), np.broadcast_to(lo, shape).ravel()
        out = np.zeros(la.shape)
        ti = np.floor(la / CANOPY_TILE_DEG).astype(np.int64) * 100000 + np.floor(lo / CANOPY_TILE_DEG).astype(np.int64)
        for k in np.unique(ti):
            sel = ti == k
            tile = self._tile(canopy_tile_name(float(la[sel][0]), float(lo[sel][0])))
            if tile is not None:
                out[sel] = tile.sample(la[sel], lo[sel], tile.level_for(res_m))
        out[out < CANOPY_MIN_M] = 0.0
        out = out.reshape(shape)
        return out if shape else float(out)


class CanopyDownloader:
    """Фоновая загрузка недостающих тайлов ETH для маршрута: в папку, через .part (оборванная загрузка не выдаётся за тайл).
    Один загрузчик на папку на всё время работы программы: повторный старт симуляции его не перезапускает."""
    _running: Dict[str, "CanopyDownloader"] = {}
    URL = CANOPY_URL

    @classmethod
    def ensure(cls, folder: str, names: Sequence[str], on_tile: Optional[Callable[[str], None]] = None) -> "CanopyDownloader":
        dl = cls._running.get(folder)
        if dl is None or dl.finished:
            dl = cls(folder, on_tile)
            cls._running[folder] = dl
        dl.add([n for n in names if not os.path.isfile(os.path.join(folder, n))])
        return dl

    def __init__(self, folder: str, on_tile: Optional[Callable[[str], None]] = None):
        self.dir, self.on_tile = folder, on_tile
        self.queue: List[str] = []
        self.current: Optional[Tuple[str, int, int]] = None     # (имя, скачано байт, всего байт или 0)
        self.done: List[str] = []
        self.failed: Dict[str, str] = {}
        self.finished = False
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def add(self, names: Sequence[str]) -> None:
        with self._lock:
            for n in names:
                if n not in self.queue and n not in self.done and n not in self.failed:
                    self.queue.append(n)
            if self.queue and (self._thread is None or not self._thread.is_alive()):
                self.finished = False
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def _run(self) -> None:
        import urllib.request
        try:
            while True:
                with self._lock:
                    if not self.queue:
                        return
                    name = self.queue.pop(0)
                path = os.path.join(self.dir, name)
                part = path + ".part"
                try:
                    os.makedirs(self.dir, exist_ok=True)
                    req = urllib.request.Request(self.URL.format(name=name), headers={"User-Agent": "MissionAnalyzer/1.0"})
                    with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as f:
                        total = int(resp.headers.get("Content-Length") or 0)
                        got = 0
                        self.current = (name, 0, total)
                        while True:
                            chunk = resp.read1(1 << 20)
                            if not chunk:
                                break
                            f.write(chunk)
                            got += len(chunk)
                            self.current = (name, got, total)
                    with open(part, "rb") as f:                   # это TIFF, а не страница ошибки
                        head = f.read(4)
                    if head not in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
                        raise ValueError("отримано не GeoTIFF (тайла немає в наборі або сервер повернув сторінку)")
                    os.replace(part, path)
                    self.done.append(name)
                    if self.on_tile is not None:
                        self.on_tile(name)
                except Exception as ex:
                    self.failed[name] = f"{type(ex).__name__}: {ex}"
                    try:
                        os.remove(part)
                    except OSError:
                        pass
                finally:
                    self.current = None
        finally:
            self.finished = True
