"""
obstacles.py -- слой ТВЁРДЫХ препятствий из OpenStreetMap: ветряки, мачты, башни, трубы, краны, опоры и провода ЛЭП.

Зачем. Пролёт сквозь мачту, ветряк или провод должен быть столкновением (в X-Plane синтетический слой «прозрачен» для пролёта).
Поэтому проверка идёт не по точке, а по ОТРЕЗКУ пути между двумя тактами: на 40 м/с борт проходит 4 м за такт, и проверка в
одной точке пропустила бы мачту радиусом 2 м.

Данные. Район делится на ячейки CELL_DEG (0.25 градуса); для маршрута берутся ячейки вдоль него с запасом margin_km. Каждая
ячейка запрашивается из Overpass один раз и кэшируется на диск (повторные миссии и работа без сети). Клиент Overpass берётся из
populated_areas (три сервера, запасное переключение, обязательный User-Agent).

ДОПУЩЕНИЯ (все помечены; в сообщении о столкновении сказано, если высота условная):
  * высота объекта без тега height берётся по умолчанию по классу (DEFAULT_HEIGHT_M, TOWER_TYPE_HEIGHT, высота опоры по
    напряжению); height_known=False;
  * у ветряка башня -- цилиндр радиусом TURBINE_TOWER_R_M до центра ротора, ротор -- КРУГ радиусом R вокруг башни (направление
    ветра неизвестно, берём с запасом);
  * провод ЛЭП занимает по высоте полосу WIRE_BAND (доли высоты опоры над землёй): высот проводов в OSM нет;
  * столбы power=pole не загружаются (их тысячи; провод и так учтён);
  * не учитываются растяжки мачт и размах крыла сверх AIRCRAFT_HALF_SPAN_M.

Модуль не зависит от Tk. Сеть подаётся извне (query_fn), поэтому всё проверяется без сети.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
from collections import OrderedDict
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

CELL_DEG = 0.25                 # размер ячейки запроса и кэша
CACHE_VERSION = 1
CACHE_TTL_DAYS = 30.0
DEFAULT_MARGIN_KM = 8.0         # ширина коридора по обе стороны маршрута
POLITE_DELAY_S = 0.4            # пауза между сетевыми запросами к Overpass
RETRY_DELAY_S = 3.0

AIRCRAFT_HALF_SPAN_M = 1.5      # запас по горизонтали на размах крыла
AIRCRAFT_HALF_HEIGHT_M = 0.5    # запас по вертикали
TURBINE_TOWER_R_M = 3.0
WIRE_BAND = (0.75, 1.0)         # провод ЛЭП: доля высоты опоры над землёй (учёт провиса)
WIRE_BAND_MINOR = (0.60, 1.0)   # столбы малых линий: провод ниже верха столба

KINDS = ("turbine", "mast", "tower", "chimney", "crane", "pylon", "power_line")

DEFAULT_HEIGHT_M = {"turbine": 120.0, "mast": 60.0, "tower": 40.0, "chimney": 60.0, "crane": 40.0, "pylon": 25.0}
DEFAULT_RADIUS_M = {"turbine": TURBINE_TOWER_R_M, "mast": 1.5, "tower": 4.0, "chimney": 5.0, "crane": 3.0, "pylon": 3.0}
TOWER_TYPE_HEIGHT = {"communication": 60.0, "lighting": 25.0, "observation": 30.0, "bell_tower": 30.0, "radar": 40.0,
                     "watchtower": 15.0, "cooling": 60.0, "monitoring": 40.0, "siren": 15.0}
MINOR_LINE_SUPPORT_M = 10.0     # высота столба малой линии, если неизвестна


# ------------------------------------------------------------------------------------------- модель и разбор тегов
@dataclass(frozen=True)
class PointObstacle:
    """Вертикальное сооружение. height_m -- верх над землёй; для ветряка hub_m и rotor_r_m описывают ротор."""
    kind: str
    lat: float
    lon: float
    height_m: float
    radius_m: float
    height_known: bool
    name: str = ""
    hub_m: float = 0.0
    rotor_r_m: float = 0.0
    osm_id: str = ""


@dataclass(frozen=True)
class LineObstacle:
    """Провода между опорами: ломаная и полоса высоты проводов (доли высоты опоры над землёй)."""
    points: Tuple[Tuple[float, float], ...]
    support_h_m: float
    band: Tuple[float, float]
    voltage_v: Optional[int]
    height_known: bool
    name: str = ""
    osm_id: str = ""
    kind: str = "power_line"


_LEN = re.compile(r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*(m|meter|meters|metre|metres|ft|feet|'|\u2032)?\s*$", re.I)


def parse_length_m(value) -> Optional[float]:
    """Длина из тега OSM в метрах: '25', '25 m', '25,5', '82 ft', "82'". Мусор и нереальные значения дают None."""
    if value is None:
        return None
    m = _LEN.match(str(value))
    if not m:
        return None
    num = float(m.group(1).replace(",", "."))
    if (m.group(2) or "").lower() in ("ft", "feet", "'", "\u2032"):
        num *= 0.3048
    return num if 0.0 < num <= 700.0 else None


def parse_voltage_v(value) -> Optional[int]:
    """Напряжение из тега voltage ('110000', '110000;35000' -> наибольшее)."""
    if value is None:
        return None
    nums = [int(x) for x in re.findall(r"\d+", str(value))]
    return max(nums) if nums else None


def pylon_height_by_voltage(voltage_v: Optional[int]) -> float:
    """Типовая высота опоры по напряжению (ПРИБЛИЗИТЕЛЬНО: в OSM высоты опор почти не указывают)."""
    if voltage_v is None:
        return DEFAULT_HEIGHT_M["pylon"]
    if voltage_v >= 330000:
        return 45.0
    if voltage_v >= 110000:
        return 30.0
    if voltage_v >= 35000:
        return 22.0
    return 15.0


def classify_point(tags: Dict[str, str], element_type: str) -> Optional[str]:
    """Класс вертикального сооружения по тегам или None."""
    if not tags:
        return None
    if tags.get("power") == "generator" and tags.get("generator:source") == "wind":
        return "turbine"
    man_made = tags.get("man_made")
    if man_made in ("mast", "communications_tower"):
        return "mast"
    if man_made == "tower":
        return "tower"
    if man_made == "chimney":
        return "chimney"
    if man_made == "crane":
        return "crane"
    if tags.get("power") == "tower" and element_type == "node":
        return "pylon"
    return None


def _centroid(geometry: Sequence[dict]) -> Optional[Tuple[float, float]]:
    pts = [(g["lat"], g["lon"]) for g in geometry if "lat" in g and "lon" in g]
    if not pts:
        return None
    return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)


def _point_from_element(el: dict, kind: str) -> Optional[PointObstacle]:
    tags = el.get("tags") or {}
    if el.get("type") == "node":
        if "lat" not in el or "lon" not in el:
            return None
        lat, lon = el["lat"], el["lon"]
    else:
        c = _centroid(el.get("geometry") or [])
        if c is None:
            return None
        lat, lon = c
    name = tags.get("name") or tags.get("name:uk") or tags.get("name:en") or ""
    osm_id = f"{el.get('type', '?')[0]}{el.get('id', '')}"
    height = parse_length_m(tags.get("height"))
    if kind == "turbine":
        hub_tag = parse_length_m(tags.get("height:hub"))
        rotor = parse_length_m(tags.get("rotor:diameter"))
        hub = hub_tag
        if height is not None:                                          # полная высота известна
            total = height
            if hub is None:
                hub = total * (1.0 - (rotor / 2.0 / total if rotor else 0.38))
            r = max(total - hub, 1.0)
        elif hub is not None:                                           # известна высота центра ротора
            r = rotor / 2.0 if rotor else 0.5 * hub
            total = hub + r
        else:                                                           # ничего не известно: типовой современный ветряк
            total = DEFAULT_HEIGHT_M["turbine"]
            hub, r = 0.62 * total, 0.38 * total
        known = height is not None or hub_tag is not None
        return PointObstacle("turbine", lat, lon, total, TURBINE_TOWER_R_M, known, name, hub_m=hub, rotor_r_m=r, osm_id=osm_id)
    if height is not None:
        return PointObstacle(kind, lat, lon, height, DEFAULT_RADIUS_M[kind], True, name, osm_id=osm_id)
    if kind == "pylon":
        h = pylon_height_by_voltage(parse_voltage_v(tags.get("voltage")))
    else:
        h = TOWER_TYPE_HEIGHT.get(tags.get("tower:type", ""), DEFAULT_HEIGHT_M[kind]) if kind in ("mast", "tower") else DEFAULT_HEIGHT_M[kind]
    return PointObstacle(kind, lat, lon, h, DEFAULT_RADIUS_M[kind], False, name, osm_id=osm_id)


def _line_from_element(el: dict) -> Optional[LineObstacle]:
    tags = el.get("tags") or {}
    kind = tags.get("power")
    if kind not in ("line", "minor_line") or el.get("type") != "way":
        return None
    pts = tuple((g["lat"], g["lon"]) for g in (el.get("geometry") or []) if "lat" in g and "lon" in g)
    if len(pts) < 2:
        return None
    voltage = parse_voltage_v(tags.get("voltage"))
    if kind == "line":
        support, band = pylon_height_by_voltage(voltage), WIRE_BAND
    else:
        support, band = MINOR_LINE_SUPPORT_M, WIRE_BAND_MINOR
    name = tags.get("name") or tags.get("ref") or ""
    return LineObstacle(pts, support, band, voltage, voltage is not None, name, f"w{el.get('id', '')}")


def parse_elements(elements: Iterable[dict]) -> Tuple[List[PointObstacle], List[LineObstacle]]:
    """Ответ Overpass (out geom) -> вертикальные сооружения и линии. Неподходящие и неполные элементы пропускаются."""
    points: List[PointObstacle] = []
    lines: List[LineObstacle] = []
    for el in elements:
        if not isinstance(el, dict) or el.get("type") not in ("node", "way"):
            continue
        tags = el.get("tags") or {}
        if tags.get("power") in ("line", "minor_line"):
            ln = _line_from_element(el)
            if ln is not None:
                lines.append(ln)
            continue
        kind = classify_point(tags, el["type"])
        if kind:
            p = _point_from_element(el, kind)
            if p is not None:
                points.append(p)
    return points, lines


# ------------------------------------------------------------------------------------------- ячейки, запрос, кэш
def route_cells(points: Sequence[Tuple[float, float]], margin_km: float = DEFAULT_MARGIN_KM,
                cell_deg: float = CELL_DEG) -> List[Tuple[int, int]]:
    """Ячейки (i, j) = (floor(lat / cell), floor(lon / cell)), которых касается коридор шириной margin_km по обе стороны."""
    pts = [(la, lo) for la, lo in points if la or lo]
    if not pts:
        return []
    cells = set()
    step_km = cell_deg * 111.0 / 4.0
    samples: List[Tuple[float, float]] = []
    for a, b in zip(pts, pts[1:] or [pts[0]]):
        n = max(1, int(math.hypot((b[0] - a[0]) * 111.0, (b[1] - a[1]) * 111.0 * math.cos(math.radians(a[0]))) / step_km) + 1)
        samples += [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n + 1)]
    samples.append(pts[-1])
    for la, lo in samples:
        dlat = margin_km / 111.0
        dlon = margin_km / (111.0 * max(math.cos(math.radians(la)), 0.05))
        for i in range(math.floor((la - dlat) / cell_deg), math.floor((la + dlat) / cell_deg) + 1):
            for j in range(math.floor((lo - dlon) / cell_deg), math.floor((lo + dlon) / cell_deg) + 1):
                cells.add((i, j))
    return sorted(cells)


def cell_bbox(i: int, j: int, cell_deg: float = CELL_DEG) -> Tuple[float, float, float, float]:
    """(юг, запад, север, восток)."""
    return i * cell_deg, j * cell_deg, (i + 1) * cell_deg, (j + 1) * cell_deg


def build_query(bbox: Tuple[float, float, float, float], timeout_s: int = 60) -> str:
    s, w, n, e = bbox
    b = f"({s:.6f},{w:.6f},{n:.6f},{e:.6f})"
    return (f"[out:json][timeout:{timeout_s}];\n(\n"
            f'  node["power"="generator"]["generator:source"="wind"]{b};\n'
            f'  way["power"="generator"]["generator:source"="wind"]{b};\n'
            f'  node["man_made"~"^(mast|tower|communications_tower|chimney|crane)$"]{b};\n'
            f'  way["man_made"~"^(tower|communications_tower|chimney|crane)$"]{b};\n'
            f'  node["power"="tower"]{b};\n'
            f'  way["power"~"^(line|minor_line)$"]{b};\n'
            f");\nout geom;")


def default_query_fn(query: str) -> dict:
    """Запрос к Overpass через клиент проекта: серверы по очереди, на каждом две попытки."""
    import populated_areas as pa
    errors: List[str] = []
    for url in pa.OVERPASS_SERVERS:
        for attempt in (1, 2):
            try:
                return pa._query_overpass(url, query)
            except pa.OverpassError as e:
                errors.append(str(e))
                if attempt == 1:
                    time.sleep(RETRY_DELAY_S)
    raise pa.OverpassError("; ".join(errors))


class ObstacleCache:
    """Дисковый кэш ответов Overpass по ячейкам (JSON, запись атомарная)."""

    def __init__(self, directory: str, ttl_days: float = CACHE_TTL_DAYS):
        self.dir, self.ttl_days = directory, ttl_days

    def _path(self, i: int, j: int) -> str:
        return os.path.join(self.dir, f"obstacles_v{CACHE_VERSION}_{i}_{j}.json")

    def load(self, i: int, j: int, allow_stale: bool = False) -> Optional[List[dict]]:
        try:
            with open(self._path(i, j), "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("v") != CACHE_VERSION or not isinstance(data.get("elements"), list):
                return None
            age_days = (time.time() - float(data.get("fetched", 0.0))) / 86400.0
            if age_days > self.ttl_days and not allow_stale:
                return None
            return data["elements"]
        except (OSError, ValueError, TypeError):
            return None

    def save(self, i: int, j: int, elements: List[dict]) -> bool:
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = self._path(i, j) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"v": CACHE_VERSION, "fetched": time.time(), "cell": [i, j], "elements": elements}, f)
            os.replace(tmp, self._path(i, j))
            return True
        except OSError:
            return False


# ------------------------------------------------------------------------------------------- набор и индекс
def _m_per_deg(lat: float) -> Tuple[float, float]:
    phi = math.radians(lat)
    return (111412.84 * math.cos(phi) - 93.5 * math.cos(3 * phi),
            111132.92 - 559.82 * math.cos(2 * phi) + 1.175 * math.cos(4 * phi))


class ObstacleSet:
    """Препятствия с пространственным индексом (клетка 0.02 градуса). Добавление и запросы потокобезопасны."""

    GRID_DEG = 0.02

    def __init__(self):
        self._lock = threading.RLock()
        self._points: List[PointObstacle] = []
        self._lines: List[LineObstacle] = []
        self._pgrid: Dict[Tuple[int, int], List[int]] = {}
        self._sgrid: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
        self._seen: set = set()

    def _cell(self, lat: float, lon: float) -> Tuple[int, int]:
        return math.floor(lat / self.GRID_DEG), math.floor(lon / self.GRID_DEG)

    def add(self, points: Iterable[PointObstacle], lines: Iterable[LineObstacle]) -> int:
        """Добавляет объекты; уже известные по osm_id пропускает (объект на границе ячеек приходит дважды). Возвращает число новых."""
        added = 0
        with self._lock:
            for p in points:
                if p.osm_id and p.osm_id in self._seen:
                    continue
                self._seen.add(p.osm_id)
                self._points.append(p)
                self._pgrid.setdefault(self._cell(p.lat, p.lon), []).append(len(self._points) - 1)
                added += 1
            for ln in lines:
                if ln.osm_id and ln.osm_id in self._seen:
                    continue
                self._seen.add(ln.osm_id)
                self._lines.append(ln)
                li = len(self._lines) - 1
                for si, (a, b) in enumerate(zip(ln.points, ln.points[1:])):
                    c0, c1 = self._cell(min(a[0], b[0]), min(a[1], b[1])), self._cell(max(a[0], b[0]), max(a[1], b[1]))
                    for ci in range(c0[0], c1[0] + 1):
                        for cj in range(c0[1], c1[1] + 1):
                            self._sgrid.setdefault((ci, cj), []).append((li, si))
                added += 1
        return added

    def __len__(self) -> int:
        with self._lock:
            return len(self._points) + len(self._lines)

    @property
    def points(self) -> List[PointObstacle]:
        with self._lock:
            return list(self._points)

    @property
    def lines(self) -> List[LineObstacle]:
        with self._lock:
            return list(self._lines)

    def near(self, lat: float, lon: float, radius_m: float):
        """(вертикальные сооружения, отрезки проводов (линия, индекс отрезка)) в радиусе radius_m от точки."""
        per_lon, per_lat = _m_per_deg(lat)
        di, dj = math.ceil(radius_m / per_lat / self.GRID_DEG), math.ceil(radius_m / per_lon / self.GRID_DEG)
        ci, cj = self._cell(lat, lon)
        pts, segs, seen_seg = [], [], set()
        with self._lock:
            for i in range(ci - di, ci + di + 1):
                for j in range(cj - dj, cj + dj + 1):
                    for pi in self._pgrid.get((i, j), ()):
                        pts.append(self._points[pi])
                    for key in self._sgrid.get((i, j), ()):
                        if key not in seen_seg:
                            seen_seg.add(key)
                            segs.append((self._lines[key[0]], key[1]))
        return pts, segs

    def summary(self) -> Dict[str, int]:
        with self._lock:
            out: Dict[str, int] = {k: 0 for k in KINDS}
            for p in self._points:
                out[p.kind] += 1
            out["power_line"] = len(self._lines)
            out["assumed_height"] = sum(1 for p in self._points if not p.height_known) + sum(1 for ln in self._lines if not ln.height_known)
            return out


# ------------------------------------------------------------------------------------------- твёрдые столкновения
@dataclass(frozen=True)
class AirPos:
    lat: float
    lon: float
    alt_amsl: float


def _segment_cylinder(p0, p1, c, rho, zlo, zhi) -> Optional[Tuple[float, float]]:
    """Пересечение отрезка p0->p1 (x, y, z) с вертикальным цилиндром (центр c, радиус rho, высоты zlo..zhi): (t, z) входа или None."""
    dx, dy, dz = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
    ox, oy = p0[0] - c[0], p0[1] - c[1]
    a = dx * dx + dy * dy
    cc = ox * ox + oy * oy - rho * rho
    if a < 1e-12:                                                       # стоим на месте
        return (0.0, p0[2]) if cc <= 0.0 and zlo <= p0[2] <= zhi else None
    b = 2.0 * (dx * ox + dy * oy)
    disc = b * b - 4.0 * a * cc
    if disc < 0.0:
        return None
    root = math.sqrt(disc)
    t_in, t_out = max(0.0, (-b - root) / (2.0 * a)), min(1.0, (-b + root) / (2.0 * a))
    if t_in > t_out:
        return None
    z_in, z_out = p0[2] + dz * t_in, p0[2] + dz * t_out                  # вертикаль линейна по t: достаточно концов интервала
    if max(z_in, z_out) < zlo or min(z_in, z_out) > zhi:
        return None
    return t_in, z_in


def check_path(obs: ObstacleSet, p0: AirPos, p1: AirPos, ground_amsl: Callable[[float, float], float]) -> Optional[dict]:
    """Пересекает ли путь p0 -> p1 хоть одно препятствие. ground_amsl(lat, lon) -- высота земли AMSL в точке (всегда число).
    Возвращает описание первого найденного столкновения или None."""
    per_lon, per_lat = _m_per_deg(p1.lat)
    def xyz(p: AirPos):
        return (p.lon - p1.lon) * per_lon, (p.lat - p1.lat) * per_lat, p.alt_amsl
    a, b = xyz(p0), xyz(p1)
    path_len = math.hypot(b[0] - a[0], b[1] - a[1])
    points, segs = obs.near(p1.lat, p1.lon, path_len + 160.0)
    half = AIRCRAFT_HALF_SPAN_M
    for p in points:
        c = ((p.lon - p1.lon) * per_lon, (p.lat - p1.lat) * per_lat)
        base = ground_amsl(p.lat, p.lon)
        top = base + (p.hub_m if p.kind == "turbine" else p.height_m)
        hit = _segment_cylinder(a, b, c, p.radius_m + half, base - AIRCRAFT_HALF_HEIGHT_M, top + AIRCRAFT_HALF_HEIGHT_M)
        part = "tower"
        if hit is None and p.kind == "turbine":
            hub_z = base + p.hub_m
            hit = _segment_cylinder(a, b, c, p.rotor_r_m + half, hub_z - p.rotor_r_m - AIRCRAFT_HALF_HEIGHT_M, hub_z + p.rotor_r_m + AIRCRAFT_HALF_HEIGHT_M)
            part = "rotor"
        if hit is not None:
            return {"kind": p.kind, "part": part, "name": p.name, "lat": p.lat, "lon": p.lon, "alt_amsl": hit[1],
                    "top_amsl": base + p.height_m, "height_m": p.height_m, "height_known": p.height_known, "osm_id": p.osm_id}
    dxp, dyp = b[0] - a[0], b[1] - a[1]
    for ln, si in segs:
        (la, lo), (lb, lob) = ln.points[si], ln.points[si + 1]
        w0 = ((lo - p1.lon) * per_lon, (la - p1.lat) * per_lat)
        w1 = ((lob - p1.lon) * per_lon, (lb - p1.lat) * per_lat)
        ex, ey = w1[0] - w0[0], w1[1] - w0[1]
        den = dxp * ey - dyp * ex
        if abs(den) < 1e-9:
            continue                                                      # путь параллелен проводу: пересечения нет
        qx, qy = w0[0] - a[0], w0[1] - a[1]
        t = (qx * ey - qy * ex) / den
        u = (qx * dyp - qy * dxp) / den
        if not (0.0 <= t <= 1.0 and 0.0 <= u <= 1.0):
            continue
        z = a[2] + (b[2] - a[2]) * t
        glat = p1.lat + (a[1] + dyp * t) / per_lat
        glon = p1.lon + (a[0] + dxp * t) / per_lon
        ground = ground_amsl(glat, glon)
        lo_z = ground + ln.band[0] * ln.support_h_m - AIRCRAFT_HALF_HEIGHT_M
        hi_z = ground + ln.band[1] * ln.support_h_m + AIRCRAFT_HALF_HEIGHT_M
        if lo_z <= z <= hi_z:
            return {"kind": "power_line", "part": "wire", "name": ln.name, "lat": glat, "lon": glon, "alt_amsl": z,
                    "top_amsl": ground + ln.support_h_m, "height_m": ln.support_h_m, "height_known": ln.height_known,
                    "osm_id": ln.osm_id}
    return None


# ------------------------------------------------------------------------------------------- фоновая загрузка
class ObstacleLoader:
    """Собирает препятствия вдоль маршрута в фоне: по ячейкам, сначала кэш, затем сеть. current_set() можно звать в любой момент:
    отдаёт то, что уже загружено. Ошибка одной ячейки не останавливает остальные."""

    def __init__(self, route_points: Sequence[Tuple[float, float]], cache_dir: str,
                 query_fn: Optional[Callable[[str], dict]] = None, margin_km: float = DEFAULT_MARGIN_KM,
                 ttl_days: float = CACHE_TTL_DAYS, on_progress: Optional[Callable[["ObstacleLoader"], None]] = None,
                 polite_delay_s: float = POLITE_DELAY_S):
        self.cells = route_cells(route_points, margin_km)
        self.cache = ObstacleCache(cache_dir, ttl_days)
        self.query_fn = query_fn or default_query_fn
        self.on_progress = on_progress
        self.polite_delay_s = polite_delay_s
        self.set = ObstacleSet()
        self.cells_done = 0
        self.from_cache = 0
        self.from_net = 0
        self.stale_used = 0
        self.failed: List[Tuple[int, int, str]] = []
        self.finished = False
        self._closed = False
        self._thread: Optional[threading.Thread] = None

    @property
    def cells_total(self) -> int:
        return len(self.cells)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._closed = True

    def current_set(self) -> ObstacleSet:
        return self.set

    def join(self, timeout: float = 10.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        try:
            for n, (i, j) in enumerate(self.cells):
                if self._closed:
                    return
                elements = self.cache.load(i, j)
                if elements is not None:
                    self.from_cache += 1
                else:
                    if self.from_net:
                        time.sleep(self.polite_delay_s)
                    try:
                        payload = self.query_fn(build_query(cell_bbox(i, j)))
                        elements = payload.get("elements", []) if isinstance(payload, dict) else []
                        self.cache.save(i, j, elements)
                        self.from_net += 1
                    except Exception as ex:                          # сеть, сервер, формат: берём устаревший кэш, если он есть
                        elements = self.cache.load(i, j, allow_stale=True)
                        if elements is None:
                            self.failed.append((i, j, f"{type(ex).__name__}: {ex}"))
                        else:
                            self.stale_used += 1
                if elements is not None:
                    pts, lns = parse_elements(elements)
                    self.set.add(pts, lns)
                self.cells_done = n + 1
                self._notify()
        finally:
            self.finished = True
            self._notify()

    def _notify(self) -> None:
        if self.on_progress is not None:
            try:
                self.on_progress(self)
            except Exception:
                pass


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
                    req = urllib.request.Request(CANOPY_URL.format(name=name), headers={"User-Agent": "MissionAnalyzer/1.0"})
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
BUILDINGS_INDEX_URLS = ("https://bfppub.blob.core.windows.net/%24web/2026-07-24/dataset-links.csv",
                        "https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv")
BUILDINGS_INDEX_TTL_DAYS = 30.0
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


def _ring_area_m2(ring: Sequence[Tuple[float, float]]) -> float:
    lat0 = ring[0][0]
    kx, ky = 111320.0 * math.cos(math.radians(lat0)), 110540.0
    a = 0.0
    for (la1, lo1), (la2, lo2) in zip(ring, ring[1:]):
        a += (lo1 * kx) * (la2 * ky) - (lo2 * kx) * (la1 * ky)
    return abs(a) / 2.0


def building_height_by_area(area_m2: float) -> float:
    for limit, h in BUILDING_HEIGHT_BY_AREA:
        if area_m2 < limit:
            return h
    return BUILDING_HEIGHT_BY_AREA[-1][1]


def parse_ms_buildings(text: str) -> List[Tuple[List[Tuple[float, float]], float, bool]]:
    """Строки GeoJSON Microsoft -> [(кольцо (lat, lon) с повтором первой точки, высота, условная ли)]."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            f = json.loads(line)
            geom = f.get("geometry") or {}
            coords = geom.get("coordinates") or []
            if geom.get("type") == "MultiPolygon":
                coords = coords[0] if coords else []
            ring = [(float(p[1]), float(p[0])) for p in (coords[0] if coords else [])]
        except (ValueError, TypeError, IndexError, KeyError):
            continue
        if len(ring) < 4:
            continue
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        h = (f.get("properties") or {}).get("height")
        try:
            h = float(h)
        except (TypeError, ValueError):
            h = -1.0
        if 1.0 < h < 400.0:
            out.append((ring, h, False))
        else:
            out.append((ring, building_height_by_area(_ring_area_m2(ring)), True))
    return out


class BuildingCell:
    """Здания одной ячейки в массивах: вершины подряд (lat, lon), границы колец, высоты, рамки контуров."""

    def __init__(self, lat, lon, offsets, height, assumed):
        import numpy as np
        self.lat, self.lon = np.asarray(lat, dtype=np.float64), np.asarray(lon, dtype=np.float64)
        self.offsets = np.asarray(offsets, dtype=np.int64)
        self.height, self.assumed = np.asarray(height, dtype=np.float32), np.asarray(assumed, dtype=bool)
        n = len(self.height)
        if n:
            idx = np.repeat(np.arange(n), np.diff(self.offsets))
            self.bbox = np.zeros((n, 4))
            self.bbox[:, 0] = np.minimum.reduceat(self.lat, self.offsets[:-1])
            self.bbox[:, 1] = np.minimum.reduceat(self.lon, self.offsets[:-1])
            self.bbox[:, 2] = np.maximum.reduceat(self.lat, self.offsets[:-1])
            self.bbox[:, 3] = np.maximum.reduceat(self.lon, self.offsets[:-1])
            del idx
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

    def top_at(self, lat: float, lon: float) -> Tuple[float, bool]:
        """Высота здания (м) под точкой: 0 -- не над зданием. Рамки -- векторно, затем точка-в-многоугольнике для кандидатов."""
        import numpy as np
        if not len(self):
            return 0.0, False
        b = self.bbox
        cand = np.nonzero((b[:, 0] <= lat) & (lat <= b[:, 2]) & (b[:, 1] <= lon) & (lon <= b[:, 3]))[0]
        best, best_assumed = 0.0, False
        for k in cand:
            la, lo = self.ring(int(k))
            j = np.roll(np.arange(len(la)), 1)
            crosses = ((la > lat) != (la[j] > lat)) & (lon < (lo[j] - lo) * (lat - la) / np.where(la[j] - la == 0, 1e-12, la[j] - la) + lo)
            if np.count_nonzero(crosses) % 2 == 1 and self.height[k] > best:
                best, best_assumed = float(self.height[k]), bool(self.assumed[k])
        return best, best_assumed


class BuildingLoader:
    """Фоновая загрузка зданий Microsoft по коридору маршрута: npz в папке buildings, иначе скачивание квадрата (.csv.gz)."""

    def __init__(self, route_points: Sequence[Tuple[float, float]], folder: str, margin_km: float = DEFAULT_MARGIN_KM,
                 fetch_fn: Optional[Callable[[str], bytes]] = None, ttl_days: float = CACHE_TTL_DAYS):
        self.cells = buildings_quadkeys_for_route(route_points, margin_km)   # квадраты quadkey (имя «cells» -- для строки состояния)
        self.dir = folder
        self.fetch_fn = fetch_fn or self._fetch
        self.ttl_days = ttl_days
        self.data: Dict[str, BuildingCell] = {}
        self.bbox: Dict[str, Tuple[float, float, float, float]] = {k: quadkey_bbox(k) for k in self.cells}
        self.cells_done = self.from_cache = self.from_net = 0
        self.part = None
        self.last_error = None
        self.parts_ok = self.parts_failed = 0
        self.failed: List[Tuple[str, str]] = []
        self.finished, self._closed, self._thread = False, False, None
        self._index: Optional[Dict[str, List[str]]] = None

    @staticmethod
    def _fetch(url: str) -> bytes:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "MissionAnalyzer/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()

    def path(self, qk: str) -> str:
        return os.path.join(self.dir, f"ms_v{BUILDINGS_CACHE_VERSION}_{qk}.npz")

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

    def _save(self, qk: str, cell: BuildingCell) -> None:
        import numpy as np
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = self.path(qk) + ".tmp.npz"
            np.savez_compressed(tmp, lat=cell.lat, lon=cell.lon, offsets=cell.offsets, height=cell.height,
                                assumed=cell.assumed, fetched=np.array(time.time()))
            os.replace(tmp, self.path(qk))
        except OSError:
            pass

    def _load_index(self) -> Dict[str, List[str]]:
        """Список файлов набора: quadkey -> адреса (на границах стран квадрат бывает в нескольких файлах). Кэш -- 30 дней."""
        import csv
        import io
        cache = os.path.join(self.dir, "dataset-links.csv")
        text = None
        try:
            if (time.time() - os.path.getmtime(cache)) / 86400.0 <= BUILDINGS_INDEX_TTL_DAYS:
                with open(cache, "r", encoding="utf-8") as f:
                    text = f.read()
        except OSError:
            pass
        if text is None:
            errors = []
            for url in BUILDINGS_INDEX_URLS:
                try:
                    text = self.fetch_fn(url).decode("utf-8")
                    break
                except Exception as ex:
                    errors.append(f"{url.split('/')[2]}: {type(ex).__name__}: {ex}")
            if text is None:
                if os.path.isfile(cache):                    # сети нет -- старый список лучше никакого
                    with open(cache, "r", encoding="utf-8") as f:
                        text = f.read()
                else:
                    raise RuntimeError("список файлів недоступний: " + "; ".join(errors))
            else:
                try:
                    os.makedirs(self.dir, exist_ok=True)
                    with open(cache, "w", encoding="utf-8") as f:
                        f.write(text)
                except OSError:
                    pass
        index: Dict[str, List[str]] = {}
        for r in csv.DictReader(io.StringIO(text)):
            qk = (r.get("QuadKey") or "").strip()
            if qk and r.get("Url"):
                index.setdefault(qk.zfill(BUILDINGS_QK_ZOOM), []).append(r["Url"])
        return index

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
        return [(self.path(k), self.bbox[k]) for k in list(self.data) if os.path.isfile(self.path(k))]

    def top_at(self, lat: float, lon: float) -> Tuple[float, bool]:
        cell = self.data.get(quadkey(lat, lon))
        return cell.top_at(lat, lon) if cell is not None else (0.0, False)

    def _run(self) -> None:
        import gzip
        try:
            for n, qk in enumerate(self.cells):
                if self._closed:
                    return
                cell = self.load_file(self.path(qk), self.ttl_days)
                if cell is not None:
                    self.data[qk] = cell
                    self.from_cache += 1
                else:
                    try:
                        if self._index is None:
                            self.part = (n + 1, "список файлів")
                            self._index = self._load_index()
                        urls = self._index.get(qk, [])
                        items = []
                        for k, url in enumerate(urls):
                            self.part = (n + 1, f"файл {k + 1}/{len(urls)}")
                            items += parse_ms_buildings(gzip.decompress(self.fetch_fn(url)).decode("utf-8"))
                        cell = BuildingCell.from_parsed(items)           # квадрата нет в наборе -- пустой (тоже кэшируется)
                        self._save(qk, cell)
                        self.data[qk] = cell
                        self.from_net += 1
                        self.parts_ok += 1
                    except Exception as ex:
                        self.last_error = f"{type(ex).__name__}: {ex}"
                        self.parts_failed += 1
                        old = self.load_file(self.path(qk))              # устаревший кэш лучше, чем ничего
                        if old is not None:
                            self.data[qk] = old
                        self.failed.append((qk, self.last_error))
                self.cells_done = n + 1
                self.part = None
        finally:
            self.finished = True
            self.part = None
