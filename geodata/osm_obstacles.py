"""Слой ТВЁРДЫХ препятствий из OpenStreetMap: ветряки, мачты, башни, трубы, краны, опоры и провода ЛЭП.

Зачем. Пролёт сквозь мачту, ветряк или провод должен быть столкновением (в X-Plane синтетический слой «прозрачен» для пролёта).
Поэтому проверка идёт не по точке, а по ОТРЕЗКУ пути между двумя тактами: на 40 м/с борт проходит 4 м за такт, и проверка в
одной точке пропустила бы мачту радиусом 2 м.

Данные. Район делится на ячейки CELL_DEG (0.25 градуса); для маршрута берутся ячейки вдоль него с запасом margin_km. Каждая
ячейка читается из Overture Maps (base/infrastructure с исходными тегами OSM, transportation/segment для путей) один раз и
кэшируется на диск (повторные миссии и работа без сети). Overpass не используется: для массовой загрузки он слишком медленный.
Чего в Overture нет: электрификации путей (контактная сеть -- над всеми действующими магистральными, трамвайными и
легкорельсовыми путями, с запасом), труб, кранов и мачт без тега вышки связи.

ДОПУЩЕНИЯ (все помечены; в сообщении о столкновении сказано, если высота условная):
  * высота объекта без тега height берётся по умолчанию по классу (DEFAULT_HEIGHT_M, TOWER_TYPE_HEIGHT, высота опоры по
    напряжению); height_known=False;
  * у ветряка башня -- цилиндр радиусом TURBINE_TOWER_R_M до центра ротора, ротор -- КРУГ радиусом R вокруг башни (направление
    ветра неизвестно, берём с запасом);
  * провод ЛЭП занимает по высоте полосу WIRE_BAND (доли высоты опоры над землёй): высот проводов в OSM нет;
  * опоры ставятся в вершинах линий (в OSM вершина ЛЭП -- опора), у малых линий -- столбы;
  * не учитываются растяжки мачт и размах крыла сверх AIRCRAFT_HALF_SPAN_M.

Модуль не зависит от Tk; чтение клетки -- в _fetch_split загрузчика, поэтому всё проверяется без сети.

Часть пакета geodata (бывший obstacles.py); старое имя `import obstacles` продолжает работать."""
from __future__ import annotations
import json
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from .overture import OvertureReader, _rule_ranges, _slice_line, _wkb_line, _wkb_polygons
from .common import (
    AIRCRAFT_HALF_HEIGHT_M,
    AIRCRAFT_HALF_SPAN_M,
    CACHE_TTL_DAYS,
    CACHE_VERSION,
    CATENARY_BAND,
    CATENARY_MAST_OFFSET_M,
    CATENARY_POLE_R_M,
    CATENARY_SPACING_M,
    CATENARY_SUPPORT_M,
    DEFAULT_HEIGHT_M,
    DEFAULT_MARGIN_KM,
    DEFAULT_RADIUS_M,
    KINDS,
    MINOR_LINE_SUPPORT_M,
    OBSTACLES_COMPAT,
    POLITE_DELAY_S,
    TOWER_TYPE_HEIGHT,
    TURBINE_TOWER_R_M,
    WIRE_BAND,
    WIRE_BAND_MINOR,
    _m_per_deg,
    _width_tag,
    cell_bbox,
    parse_length_m,
    parse_voltage_v,
    route_cells,
    route_order,
    CELL_DEG,
    smooth_polyline,
)
from .cache import ObstacleCache


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


# ---------------------------------------------------------------------------------------------------------------------
# МОСТЫ: только где OSM прямо говорит bridge=yes/viaduct (своей логики «где нужен мост» нет -- на ней ошибается X-Plane:
# «дорога в небо», «арка в 300 м»). Концы -- на берегах (рельеф), настил -- прямая между ними, но не ниже «рельеф + просвет»
# (просвет растёт с длиной), сглажен, и не выше BRIDGE_MAX_ABOVE_M над прямой. Один расчёт -- и для картинки, и для удара.
# ---------------------------------------------------------------------------------------------------------------------
BRIDGE_TAGS = ("yes", "viaduct", "cantilever", "trestle", "movable", "suspension")


BRIDGE_MIN_LEN_M = 20.0          # короче -- трубы и мостики через канавы


BRIDGE_MAX_ABOVE_M = 60.0        # настил не выше этого над прямой между берегами


BRIDGE_STEP_M = 10.0


BRIDGE_VALLEY_M = 3.0            # земля под мостом ниже концов меньше чем на столько -- ровное место (не река, не овраг)


BRIDGE_OVERPASS_CLEAR_M = 6.0    # просвет путепровода над ровным местом


BRIDGE_DECK_M = 1.8              # толщина настила с балкой


RAIL_BRIDGE_W_M = 6.0


RAIL_KINDS = ("rail", "light_rail", "narrow_gauge", "tram", "subway")


def parse_bridges(elements: Iterable[dict]) -> List[Tuple[str, float, tuple, int, str]]:
    """Мосты дорог (крупных) и железных дорог: [(«road»|«rail», ширина, точки, слой, osm_id)]."""
    out = []
    for el in elements:
        if el.get("type") != "way":
            continue
        tags = el.get("tags") or {}
        if tags.get("bridge") not in BRIDGE_TAGS:
            continue
        hw, rw = tags.get("highway"), tags.get("railway")
        if hw in ROAD_WIDTH_M:
            kind, width = "road", _width_tag(tags) or ROAD_WIDTH_M[hw]
        elif rw in RAIL_KINDS:
            kind, width = "rail", _width_tag(tags) or RAIL_BRIDGE_W_M
        else:
            continue
        pts = tuple((g["lat"], g["lon"]) for g in (el.get("geometry") or []) if "lat" in g and "lon" in g)
        if len(pts) < 2:
            continue
        L = sum(math.hypot((b[1] - a[1]) * 111320.0 * math.cos(math.radians(a[0])), (b[0] - a[0]) * 110540.0)
                for a, b in zip(pts, pts[1:]))
        if L < BRIDGE_MIN_LEN_M:
            continue
        try:
            layer = int(str(tags.get("layer", "1")).split(";")[0])
        except ValueError:
            layer = 1
        out.append((kind, float(width), pts, layer, f"w{el.get('id', '')}"))
    return out


def bridge_deck(pts, ground_fn, step: float = BRIDGE_STEP_M):
    """Профиль настила: (широты, долготы, высота настила AMSL, рельеф под ним) с шагом step вдоль моста."""
    import numpy as np
    la = np.array([p[0] for p in pts])
    lo = np.array([p[1] for p in pts])
    kx = 111320.0 * math.cos(math.radians(float(la.mean())))
    seg = np.hypot(np.diff(lo) * kx, np.diff(la) * 110540.0)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    L = float(cum[-1])
    s_ = np.linspace(0.0, L, max(2, int(math.ceil(L / step)) + 1))
    lat, lon = np.interp(s_, cum, la), np.interp(s_, cum, lo)
    g = np.asarray(ground_fn(lat, lon), dtype=float)
    t = s_ / L if L > 0 else np.zeros_like(s_)
    lin = g[0] + (g[-1] - g[0]) * t                                  # прямая между берегами
    dep = float(np.max(lin - g)) if len(g) > 2 else 0.0              # насколько земля под мостом ниже прямой между концами
    if dep < BRIDGE_VALLEY_M:                                        # над ровным (поле аэропорта, дорога, пути) -- путепровод:
        clear = BRIDGE_OVERPASS_CLEAR_M                              # раньше длинная эстакада получала горб до 40 м и в него били
    else:                                                            # река, овраг: просвет растёт с длиной, но в меру понижения
        clear = min(40.0, 5.0 + 0.02 * L, dep + 15.0)
    need = g + clear * np.sin(np.pi * t)                             # к берегам просвет сходит на нет
    deck = np.maximum(lin, need)
    if len(deck) >= 5:                                               # сгладить (без изломов), концы -- на берегах
        k = np.ones(5) / 5.0
        sm = np.convolve(np.pad(deck, 2, mode="edge"), k, mode="valid")
        deck = np.maximum(sm, lin)
    deck = np.minimum(deck, lin + BRIDGE_MAX_ABOVE_M)                # урок X-Plane: никаких «мостов в небо»
    deck[0], deck[-1] = lin[0], lin[-1]                              # концы -- ровно на берегах (сглаживание их приподнимало)
    return lat, lon, deck, g


def bridge_hit(profiles, lat: float, lon: float, alt: float):
    """Борт в настиле моста? profiles -- [(мост, широты, долготы, настил)]. Возвращает (мост, высота настила) или None.
    Под мостом пролететь можно -- удар только в толщине настила."""
    import numpy as np
    kx = 111320.0 * math.cos(math.radians(lat))
    for br, la, lo, deck in profiles:
        if lat < la.min() - 0.001 or lat > la.max() + 0.001 or lon < lo.min() - 0.0015 or lon > lo.max() + 0.0015:
            continue
        d = np.hypot((lo - lon) * kx, (la - lat) * 110540.0)
        k = int(np.argmin(d))
        th = 1.0 if br[0] == "foot" else BRIDGE_DECK_M                 # переход -- тонкий настил (как нарисован)
        if d[k] <= br[1] / 2.0 + BRIDGE_STEP_M / 2.0 and deck[k] - th <= alt <= deck[k] + 0.3:
            return br, float(deck[k])
    return None


def _railway_from_element(el: dict) -> Optional[LineObstacle]:
    """Путь железной дороги -> линия: электрифицированный -- «catenary» (контактная сеть над путём, препятствие),
    остальные -- «railway» (только полоса пути на земле, препятствием не считается). Тоннели -- нет."""
    tags = el.get("tags") or {}
    if el.get("type") != "way" or tags.get("tunnel") in ("yes", "building_passage"):
        return None
    if tags.get("bridge") in BRIDGE_TAGS:
        return None                                              # путь по мосту -- на настиле (parse_bridges), не «по воде»
    pts = tuple((g["lat"], g["lon"]) for g in (el.get("geometry") or []) if "lat" in g and "lon" in g)
    if len(pts) < 2:
        return None
    pts = smooth_polyline(pts)                                   # плавная колея: опоры и провода -- по ней же
    name = tags.get("name") or tags.get("ref") or ""
    if tags.get("electrified") == "contact_line":
        return LineObstacle(pts, CATENARY_SUPPORT_M, CATENARY_BAND, parse_voltage_v(tags.get("voltage")), False, name,
                            f"w{el.get('id', '')}", kind="catenary")
    return LineObstacle(pts, 0.0, (0.0, 0.0), None, True, name, f"w{el.get('id', '')}", kind="railway")


def catenary_masts(line: LineObstacle, spacing_m: float = CATENARY_SPACING_M) -> List[PointObstacle]:
    """Опоры контактной сети вдоль пути через spacing_m (первая -- в начале пути). Шаг сквозной по всей ломаной."""
    out: List[PointObstacle] = []
    carry = 0.0                                                     # сколько осталось пройти до следующей опоры
    # у каждой опоры СВОЙ номер: ObstacleSet отбрасывает повторы по osm_id, с номером пути сам путь считался бы повтором
    for (la1, lo1), (la2, lo2) in zip(line.points, line.points[1:]):
        per_lon, per_lat = _m_per_deg((la1 + la2) / 2.0)
        seg = math.hypot((lo2 - lo1) * per_lon, (la2 - la1) * per_lat)
        d = carry
        # сдвиг на обочину: влево по ходу пути, на CATENARY_MAST_OFFSET_M (в градусах)
        nx, ny = (-(la2 - la1) * per_lat, (lo2 - lo1) * per_lon) if seg > 0 else (0.0, 0.0)
        off_lon = nx / seg * CATENARY_MAST_OFFSET_M / per_lon if seg > 0 else 0.0
        off_lat = ny / seg * CATENARY_MAST_OFFSET_M / per_lat if seg > 0 else 0.0
        while d <= seg:
            t = d / seg if seg > 0 else 0.0
            out.append(PointObstacle("catenary_mast", la1 + (la2 - la1) * t + off_lat, lo1 + (lo2 - lo1) * t + off_lon, CATENARY_SUPPORT_M,
                                     CATENARY_POLE_R_M, False, line.name, osm_id=f"{line.osm_id}#m{len(out)}"))
            d += spacing_m
        carry = d - seg
    return out


def line_supports(line: LineObstacle, skip: set = frozenset()) -> List[PointObstacle]:
    """Опоры ЛЭП в вершинах линии: в OSM каждая вершина линии электропередачи -- опора. Раньше опоры брались только из
    отдельных точек power=tower: у узла без тега, у столбов малых линий (power=pole -- не запрашивались) и за краем
    загруженных ячеек провода висели без опор. skip -- координаты, где опора уже есть (точка power=tower)."""
    minor = tuple(line.band) == tuple(WIRE_BAND_MINOR)
    kind = "pole" if minor else "pylon"
    out = []
    for k, (la, lo) in enumerate(line.points):
        if (round(la, 7), round(lo, 7)) in skip:
            continue
        out.append(PointObstacle(kind, la, lo, line.support_h_m, DEFAULT_RADIUS_M[kind], line.height_known, line.name,
                                 osm_id=f"{line.osm_id}#s{k}"))
    return out


def parse_elements(elements: Iterable[dict]) -> Tuple[List[PointObstacle], List[LineObstacle]]:
    """Элементы вида OSM (из Overture) -> вертикальные сооружения и линии. Неподходящие и неполные элементы пропускаются."""
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
        if tags.get("railway"):
            ln = _railway_from_element(el)
            if ln is not None:
                lines.append(ln)
                if ln.kind == "catenary":
                    points += catenary_masts(ln)
            continue
        kind = classify_point(tags, el["type"])
        if kind:
            p = _point_from_element(el, kind)
            if p is not None:
                points.append(p)
    # мачта из точки power=tower стоит на линии -- высота по линии (по напряжению), а не стандартные 25 м: иначе провода
    # (их высота -- от линии) проходили выше верхушек мачт и «висели в воздухе»
    line_h: Dict[Tuple[float, float], float] = {}
    for ln in lines:
        if ln.kind == "power_line":
            for la, lo in ln.points:
                key = (round(la, 7), round(lo, 7))
                line_h[key] = max(line_h.get(key, 0.0), ln.support_h_m)
    from dataclasses import replace as _dc_replace
    for k, p in enumerate(points):
        if p.kind == "pylon" and not p.height_known:
            h = line_h.get((round(p.lat, 7), round(p.lon, 7)))
            if h:
                points[k] = _dc_replace(p, height_m=h)
    towers = {(round(p.lat, 7), round(p.lon, 7)) for p in points if p.kind == "pylon"}
    for ln in list(lines):
        if ln.kind == "power_line":
            points += line_supports(ln, towers)
    return points, lines



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
        self.bridges: List[Tuple[str, float, tuple, int, str]] = []    # мосты путей (дорожные -- в ориентирах)

    def add_bridges(self, bridges) -> int:
        n = 0
        with self._lock:
            for br in bridges:
                if ("bridge", br[4]) in self._seen:
                    continue
                self._seen.add(("bridge", br[4]))
                self.bridges.append(br)
                n += 1
        return n

    def _cell(self, lat: float, lon: float) -> Tuple[int, int]:
        return math.floor(lat / self.GRID_DEG), math.floor(lon / self.GRID_DEG)

    def add(self, points: Iterable[PointObstacle], lines: Iterable[LineObstacle]) -> int:
        """Добавляет объекты; уже известные по osm_id пропускает (объект на границе ячеек приходит дважды). Возвращает число новых."""
        added = 0
        with self._lock:
            for p in points:
                if p.osm_id and p.osm_id in self._seen:
                    continue
                if p.kind in ("pylon", "pole"):                          # одна опора: точка power=tower и вершина линии
                    pos = ("support", round(p.lat, 7), round(p.lon, 7))
                    if pos in self._seen:
                        continue
                    self._seen.add(pos)
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
            for ln in self._lines:
                out[ln.kind] = out.get(ln.kind, 0) + 1
            out["assumed_height"] = sum(1 for p in self._points if not p.height_known) + sum(1 for ln in self._lines if not ln.height_known)
            return out


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
        if ln.kind == "railway":
            continue                                                      # путь без контактной сети -- на земле, не препятствие
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
            return {"kind": ln.kind, "part": "wire", "name": ln.name, "lat": glat, "lon": glon, "alt_amsl": z,
                    "top_amsl": ground + ln.support_h_m, "height_m": ln.support_h_m, "height_known": ln.height_known,
                    "osm_id": ln.osm_id}
    return None


# ------------------------------------------------------------------------------------------- фоновая загрузка
class ObstacleLoader:
    """Загрузка слоя вдоль маршрута в фоне по клеткам 0.25 градуса: сначала кэш, затем сеть. current_set() можно звать в любой
    момент -- отдаёт то, что уже загружено. Ошибка одной клетки не останавливает остальные.
    Наследники задают PREFIX/VERSION файлов кэша, _fetch_split (чтение клетки -- из Overture) и _absorb (разбор клетки)."""
    PREFIX = "obstacles"
    VERSION = CACHE_VERSION
    COMPAT = OBSTACLES_COMPAT


    def _fetch_split(self, bbox, depth: int):
        """Элементы клетки: (элементы вида OSM, ошибки). Задают наследники."""
        raise NotImplementedError

    def _absorb(self, i: int, j: int, elements: List[dict]) -> None:
        elements = geo_ids(elements)                                 # номера в квадратах повторяются -- ключ по геометрии
        if self.align is not None:                                   # совмещение со снимком (из карты) -- сразу в данных
            elements = self.align.shift_elements(elements)
        pts, lns = parse_elements(elements)
        self.set.add(pts, lns)
        self.set.add_bridges([b for b in parse_bridges(elements) if b[0] == "rail"])

    def __init__(self, route_points: Sequence[Tuple[float, float]], cache_dir: str,
                 margin_km: float = DEFAULT_MARGIN_KM,
                 ttl_days: float = CACHE_TTL_DAYS, on_progress: Optional[Callable[["ObstacleLoader"], None]] = None,
                 polite_delay_s: float = POLITE_DELAY_S, oset: Optional["ObstacleSet"] = None):
        self.cells = route_order(route_cells(route_points, margin_km), route_points)   # по ходу маршрута, не по координатам
        self._route0 = tuple(route_points[0]) if len(route_points) else None
        self.idle = False                             # окно (window_km) вокруг борта загружено, ждём, пока борт сдвинется
        self.cache = ObstacleCache(cache_dir, ttl_days, self.PREFIX, self.VERSION, self.COMPAT)
        self._shared_set = oset                         # детали -- в набор главного загрузчика
        self.stage = ""                               # что читается сейчас (для строки хода)
        self.on_progress = on_progress
        self.polite_delay_s = polite_delay_s
        self.set = self._shared_set if self._shared_set is not None else ObstacleSet()
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


    focus: Optional[Tuple[float, float]] = None     # где борт (и чуть впереди): эту ячейку и ближайшие -- первыми

    def _next_cell(self, left):
        """Следующая ячейка: ближайшая к борту (focus), пока он не задан -- по порядку маршрута."""
        f = self.focus
        if f is None:
            return left[0]
        fi, fj = f[0] / CELL_DEG - 0.5, f[1] / CELL_DEG - 0.5
        kx = max(math.cos(math.radians(f[0])), 0.05)
        return min(left, key=lambda c: (c[0] - fi) ** 2 + ((c[1] - fj) * kx) ** 2)

    follow = False                # в полёте: ячейки вокруг борта догружаются и вне маршрута (полёт руками, уход с маршрута)
    window_km = None              # в полёте: грузить ячейки маршрута не все сразу, а ближе window_km к борту (None -- все)
    align = None                  # geodata.align.AlignMap: совмещение со снимком -- геометрия сдвигается при загрузке
    FOLLOW_RADIUS_KM = 12.0       # сколько вокруг борта держать загруженным
    FOLLOW_RETRY_S = 60.0         # не загрузилась -- повторить не раньше

    def _load_cell(self, i: int, j: int) -> None:
        """Одна ячейка: кэш, иначе сеть (не всё пришло -- устаревший кэш, хоть часть), затем -- в набор."""
        elements = self.cache.load(i, j)
        if elements is not None:
            self.from_cache += 1
        else:
            if self.from_net:
                time.sleep(self.polite_delay_s)
            got, errors = self._fetch_split(cell_bbox(i, j), 0)
            if not errors:
                elements = got
                self.cache.save(i, j, elements)
                self.from_net += 1
            else:                                            # не всё пришло: устаревший кэш, кэш прежней версии, хоть часть
                old = self.cache.load(i, j, allow_stale=True)
                if old is None:
                    old = self.cache.load_older(i, j)
                if old is not None:
                    elements = old + (got or [])             # прежние данные плюс то, что всё же пришло сейчас
                    self.stale_used += 1
                elif got:
                    elements = got                           # частично -- в кэш не пишем: в следующий раз догрузится
                self.failed.append((i, j, f"{len(errors)} частин не завантажено: {errors[0]}"))
        if elements is not None:
            self._absorb(i, j, elements)

    def _cells_near(self, lat: float, lon: float, radius_km: float):
        """Ячейки, задевающие круг radius_km вокруг точки, -- ближние первыми."""
        dlat = radius_km / 111.0
        dlon = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.05))
        out = [(i, j) for i in range(math.floor((lat - dlat) / CELL_DEG), math.floor((lat + dlat) / CELL_DEG) + 1)
               for j in range(math.floor((lon - dlon) / CELL_DEG), math.floor((lon + dlon) / CELL_DEG) + 1)]
        fi, fj, kx = lat / CELL_DEG - 0.5, lon / CELL_DEG - 0.5, max(math.cos(math.radians(lat)), 0.05)
        return sorted(out, key=lambda c: (c[0] - fi) ** 2 + ((c[1] - fj) * kx) ** 2)

    def _cell_km(self, c, f) -> float:
        """Расстояние (км) от точки f до клетки c (0 -- точка внутри)."""
        s_, w_, n_, e_ = cell_bbox(*c)
        dla = max(s_ - f[0], 0.0, f[0] - n_)
        dlo = max(w_ - f[1], 0.0, f[1] - e_)
        return math.hypot(dla * 110.54, dlo * 111.32 * max(math.cos(math.radians(f[0])), 0.05))

    def _run(self) -> None:
        done: set = set()
        try:
            left = list(self.cells)
            n = -1
            while left:
                if self._closed:
                    return
                if self.window_km:                               # только то, что рядом с бортом; дальнее -- когда долетит
                    f = self.focus or self._route0
                    near = [c for c in left if f is None or self._cell_km(c, f) <= self.window_km]
                    if not near:
                        if not self.idle:
                            self.idle = True
                            self._notify()
                        time.sleep(1.0)
                        continue
                    self.idle = False
                    i, j = self._next_cell(near)
                else:
                    i, j = self._next_cell(left)
                n += 1
                left.remove((i, j))
                if self._closed:
                    return
                self._load_cell(i, j)
                done.add((i, j))
                self.cells_done = n + 1
                self._notify()
        finally:
            self.finished = True
            self._notify()
        tried: Dict[Tuple[int, int], float] = {}
        while self.follow and not self._closed:                  # полёт вне коридора маршрута: догружать вокруг борта
            f = self.focus
            todo = [c for c in (self._cells_near(f[0], f[1], self.FOLLOW_RADIUS_KM) if f else [])
                    if c not in done and time.time() - tried.get(c, -1e9) > self.FOLLOW_RETRY_S]
            if not todo:
                time.sleep(1.0)
                continue
            c = todo[0]
            tried[c] = time.time()
            nfail = len(self.failed)
            try:
                self._load_cell(*c)
            except Exception as exc:
                self.failed.append((c[0], c[1], f"{type(exc).__name__}: {exc}"))
            if len(self.failed) == nfail:
                done.add(c)
            self.cells.append(c) if c not in self.cells else None
            self.cells_done += 1
            self._notify()

    def _notify(self) -> None:
        if self.on_progress is not None:
            try:
                self.on_progress(self)
            except Exception:
                pass


def geo_ids(elements: List[dict]) -> List[dict]:
    """Ключ объекта -- по геометрии, а не по номеру из файла. Номера Overture-элементов (ovt_r1, oi5, ...) считаются
    заново в КАЖДОМ квадрате: при сборке нескольких квадратов «повторы» по номеру отбрасывались, и дороги, вода, пути,
    полосы соседних квадратов пропадали (кусок Днепра за мостом). Один и тот же объект, попавший в два квадрата,
    по-прежнему берётся один раз: у него та же геометрия. Работает и на уже скачанном кэше (номера в нём не важны)."""
    def pt(p):
        return (p["lat"], p["lon"]) if isinstance(p, dict) else (p[0], p[1])

    def key(tag, pts):
        pts = [pt(p) for p in pts]
        a, b, m = pts[0], pts[-1], pts[len(pts) // 2]
        return f"{tag}:{a[0]:.6f},{a[1]:.6f}:{m[0]:.6f},{m[1]:.6f}:{b[0]:.6f},{b[1]:.6f}:{len(pts)}"
    out = []
    for el in elements:
        t = el.get("type")
        try:
            if t == "way" and el.get("geometry"):
                k = key("w", el["geometry"])
            elif t == "ovt_water" and el.get("outer"):
                k = key("p", el["outer"])
            elif t == "node" and "lat" in el:
                k = f"n:{el['lat']:.6f},{el['lon']:.6f}"
            else:
                out.append(el)
                continue
        except (KeyError, TypeError, IndexError, ValueError):
            out.append(el)
            continue
        out.append(dict(el, id=k))
    return out


ROAD_WIDTH_M = {"motorway": 24.0, "trunk": 18.0, "primary": 12.0, "secondary": 9.0, "tertiary": 7.0,
                "motorway_link": 7.0, "trunk_link": 7.0, "primary_link": 7.0, "secondary_link": 6.0, "tertiary_link": 6.0}



# ---------------------------------------------------------------------------------------------------------------------
# ПРЕПЯТСТВИЯ ИЗ OVERTURE (Overpass не используется). base/infrastructure хранит исходные теги OSM (source_tags), поэтому
# ЛЭП, опоры, ветряки, вышки связи и башни подаются в разбор как элементы OSM -- высоты по напряжению и типу башни те же.
# Пути -- transportation/segment (subtype=rail): мосты и тоннели по флагам, заброшенные и строящиеся участки отбрасываются.
# ЧЕГО В OVERTURE НЕТ: электрификации путей -- контактная сеть ставится над всеми действующими магистральными, трамвайными,
# легкорельсовыми путями и наземным метро (с запасом, в безопасную сторону); труб, кранов, мачт без тега вышки связи.
# ---------------------------------------------------------------------------------------------------------------------
OVT_OBST_VERSION = 2                 # 2: метро отдельно (railway=subway, без контактной сети); v1 -- читается, пока не скачано
OVT_INFRA_SUBTYPES = ("power", "communication", "tower", "utility")
OVT_INFRA_CLASSES = {"power_line": {"power": "line"}, "minor_line": {"power": "minor_line"},
                     "power_tower": {"power": "tower"}, "portal": {"power": "tower"},
                     "generator": {"power": "generator"},
                     "communication_tower": {"man_made": "communications_tower"},
                     "mobile_phone_tower": {"man_made": "mast"},
                     "water_tower": {"man_made": "tower", "tower:type": "water"},
                     "observation": {"man_made": "tower", "tower:type": "observation"},
                     "lighting": {"man_made": "tower", "tower:type": "lighting"},
                     "bell_tower": {"man_made": "tower", "tower:type": "bell_tower"},
                     "cooling": {"man_made": "tower", "tower:type": "cooling"},
                     "minaret": {"man_made": "tower", "tower:type": "minaret"},
                     "defensive": {"man_made": "tower", "tower:type": "defensive"}}
OVT_RAIL_CLASSES = {"standard_gauge": "rail", "light_rail": "light_rail", "tram": "tram", "subway": "subway",
                    "narrow_gauge": "narrow_gauge", "unknown": "rail"}
OVT_RAIL_ELECTRIFIED = ("rail", "light_rail", "tram")     # метро -- контактный рельс, проводов над ним нет
OVT_RAIL_SKIP_FLAGS = ("is_tunnel", "is_abandoned", "is_disused", "is_under_construction")


def _ovt_point(g: bytes):
    """WKB Point -> (lon, lat) или None."""
    import struct
    try:
        bo = "<" if g[0] == 1 else ">"
        t = struct.unpack_from(bo + "I", g, 1)[0]
        if (t & 0xFFFF) % 1000 != 1:
            return None
        return struct.unpack_from(bo + "dd", g, 5)
    except Exception:
        return None


def overture_obstacle_elements(infra_rows, rail_rows):
    """Строки Overture -> элементы вида OSM для parse_elements / parse_bridges."""
    out = []
    for k, r in enumerate(infra_rows):
        g, cls = r.get("geometry"), r.get("class")
        if not g or cls not in OVT_INFRA_CLASSES:
            continue
        tags = dict(r.get("source_tags") or [])
        for key, val in OVT_INFRA_CLASSES[cls].items():
            tags.setdefault(key, val)
        if r.get("height") is not None and "height" not in tags:
            tags["height"] = str(r["height"])
        line = _wkb_line(g)
        if line is not None:
            if tags.get("power") in ("line", "minor_line") and len(line) >= 2:
                out.append({"type": "way", "id": f"oi{k}", "tags": tags, "geometry": [{"lat": la, "lon": lo} for lo, la in line]})
            continue
        pt = _ovt_point(g)
        if pt is None:
            polys = _wkb_polygons(g)
            if not polys or not polys[0]:
                continue
            ring = polys[0][0]
            pt = (sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring))
        out.append({"type": "node", "id": f"oi{k}", "lat": pt[1], "lon": pt[0], "tags": tags})
    for k, r in enumerate(rail_rows):
        rw = OVT_RAIL_CLASSES.get(r.get("class"))
        pts = _wkb_line(r.get("geometry")) if r.get("geometry") else None
        if not rw or not pts or len(pts) < 2:
            continue
        flags = r.get("rail_flags") or []
        if any(set(x.get("values") or []) & {"is_abandoned", "is_disused", "is_under_construction"} and not x.get("between")
               for x in flags):
            continue
        skip = [rng for f_ in OVT_RAIL_SKIP_FLAGS for rng in _rule_ranges(flags, f_)]
        if r.get("class") == "subway" and not skip:
            skip = [(0.0, 1.0, True)]                                    # метро без флагов -- под землёй
        bridges = _rule_ranges(flags, "is_bridge")
        levels = _rule_ranges(r.get("level_rules"))
        cuts = sorted({0.0, 1.0} | {x for a, c, _ in skip + bridges for x in (a, c)})
        for a, c in zip(cuts, cuts[1:]):
            mid = (a + c) / 2.0
            if c - a < 1e-6 or any(sa <= mid <= sc for sa, sc, _ in skip):
                continue
            part = _slice_line(pts, a, c)
            if len(part) < 2:
                continue
            tags = {"railway": rw}
            if rw in OVT_RAIL_ELECTRIFIED:
                tags["electrified"] = "contact_line"
            if any(ba <= mid <= bc for ba, bc, _ in bridges):
                tags["bridge"] = "yes"
                lv = [v for la_, lc_, v in levels if v is not None and la_ <= mid <= lc_]
                tags["layer"] = str(lv[0] if lv else 1)
            out.append({"type": "way", "id": f"or{k}_{len(out)}", "tags": tags, "geometry": [{"lat": la, "lon": lo} for lo, la in part]})
    return out


class OvertureObstacleLoader(ObstacleLoader):
    """Препятствия (ЛЭП, опоры, вышки, башни, ветряки, пути и ж/д мосты) из Overture по клеткам 0.25 градуса. Кэш ovt_obst_v*."""
    PREFIX = "ovt_obst"
    VERSION = OVT_OBST_VERSION
    COMPAT = ()                  # прежняя версия -- только если скачать не удалось (load_older), иначе докачка её не сменит

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._infra = OvertureReader(self.cache.dir, "base", "infrastructure")
        self._rail = OvertureReader(self.cache.dir, "transportation", "segment")

    def _fetch_split(self, bbox, depth: int):
        try:
            import pyarrow.dataset as ds                             # без pyarrow -- ошибка квадрата, а не конец загрузки
            self.stage = "Overture: ЛЕП і споруди"
            infra = self._infra.table(bbox, ["subtype", "class", "height", "source_tags", "geometry"],
                                      ds.field("subtype").isin(list(OVT_INFRA_SUBTYPES)))
            self.stage = "Overture: колії"
            rail = self._rail.table(bbox, ["class", "rail_flags", "level_rules", "geometry"], ds.field("subtype") == "rail")
            return overture_obstacle_elements(infra, rail), []
        except Exception as ex:
            return [], [f"Overture: {type(ex).__name__}: {ex}"]
