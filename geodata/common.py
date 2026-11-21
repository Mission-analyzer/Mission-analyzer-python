"""Общие константы и геометрия: разбор длины и напряжения, клетки маршрута, сглаживание линий.

Часть пакета geodata (бывший obstacles.py); старое имя `import obstacles` продолжает работать."""
from __future__ import annotations
import math
import re
from typing import (List, Optional, Sequence, Tuple)


CELL_DEG = 0.25                 # размер ячейки запроса и кэша


CACHE_VERSION = 4               # 2: + контактная сеть; 3: + все пути; 4: ГЛАВНОЕ отдельно от деталей (запрос в разы легче)


# Загрузка в две очереди: сначала главное (ЛЭП, магистральные пути, мачты, трубы, краны, ветряки) -- лёгкий запрос, с ним
# уже можно лететь; потом детали (линии 0.4-10 кВ, служебные пути, трамвай) -- obstacles_detail_*. Отдельные точки опор
# (power=tower) не запрашиваются вовсе: опоры ставятся в вершинах линий. Ячейки версии 3 содержат всё сразу и засчитываются
# для обеих очередей (перекачивать их не нужно).
OBSTACLES_COMPAT = (("obstacles", 3),)


CACHE_TTL_DAYS = 30.0


DEFAULT_MARGIN_KM = 8.0         # ширина коридора по обе стороны маршрута


POLITE_DELAY_S = 0.4            # пауза между сетевыми чтениями клеток


RETRY_DELAY_S = 3.0


AIRCRAFT_HALF_SPAN_M = 1.5      # запас по горизонтали на размах крыла


AIRCRAFT_HALF_HEIGHT_M = 0.5    # запас по вертикали


TURBINE_TOWER_R_M = 3.0


WIRE_BAND = (0.75, 1.0)         # провод ЛЭП: доля высоты опоры над землёй (учёт провиса)


WIRE_BAND_MINOR = (0.60, 1.0)   # столбы малых линий: провод ниже верха столба


KINDS = ("turbine", "mast", "tower", "chimney", "crane", "pylon", "pole", "power_line", "catenary", "catenary_mast", "railway")


# Контактная сеть электрифицированной железной дороги (railway + electrified=contact_line). Опор в OSM почти нет --
# расставляются вдоль пути с шагом CATENARY_SPACING_M. Провода: контактный (~5.5 м) и несущий трос (~7 м) над осью пути.
CATENARY_SUPPORT_M = 7.5        # высота опоры над землёй


CATENARY_BAND = (5.3 / 7.5, 7.1 / 7.5)   # полоса проводов: от контактного до несущего троса (доли высоты опоры)


CATENARY_SPACING_M = 60.0


CATENARY_POLE_R_M = 0.3


CATENARY_MAST_OFFSET_M = 3.1    # опора -- на обочине, сбоку от оси пути (провод к пути подаёт консоль)


DEFAULT_HEIGHT_M = {"turbine": 120.0, "mast": 60.0, "tower": 40.0, "chimney": 60.0, "crane": 40.0, "pylon": 25.0,
                    "catenary_mast": CATENARY_SUPPORT_M, "pole": 10.0}


DEFAULT_RADIUS_M = {"turbine": TURBINE_TOWER_R_M, "mast": 1.5, "tower": 4.0, "chimney": 5.0, "crane": 3.0, "pylon": 3.0,
                    "catenary_mast": CATENARY_POLE_R_M, "pole": 0.3}


TOWER_TYPE_HEIGHT = {"communication": 60.0, "lighting": 25.0, "observation": 30.0, "bell_tower": 30.0, "radar": 40.0,
                     "watchtower": 15.0, "cooling": 60.0, "monitoring": 40.0, "siren": 15.0}


MINOR_LINE_SUPPORT_M = 10.0     # высота столба малой линии, если неизвестна


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


SMOOTH_ITERS = 2                 # сглаживание ломаных OSM: столько проходов скругления углов


SMOOTH_MAX_CUT_M = 12.0          # угол срезается не дальше этого (второй проход -- вдвое меньше): линия не уезжает со снимка


SMOOTH_MIN_TURN_DEG = 3.0        # почти прямые вершины не трогаем


def smooth_polyline(pts, iters: int = SMOOTH_ITERS, max_cut_m: float = SMOOTH_MAX_CUT_M):
    """Ломаная OSM -> плавная: в каждой вершине с поворотом угол заменяется двумя точками на соседних отрезках (срезание
    углов, как у Чайкина), но не дальше max_cut_m от вершины -- у длинных отрезков иначе срезались бы сотни метров и линия
    уезжала бы от своего места на снимке. Концы на месте, прямые участки не трогаются, замкнутые контуры -- тоже."""
    pts = [tuple(p) for p in pts]
    if len(pts) < 3 or pts[0] == pts[-1]:
        return tuple(pts)
    kx = 111320.0 * math.cos(math.radians(pts[0][0]))
    ky = 110540.0

    def towards(p, q, dist):                                   # точка на отрезке p->q в dist метрах от p
        L = math.hypot((q[0] - p[0]) * ky, (q[1] - p[1]) * kx)
        t = dist / L if L > 0 else 0.0
        return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)
    for it in range(iters):
        cut = max_cut_m / (2 ** it)
        out = [pts[0]]
        for k in range(1, len(pts) - 1):
            a, c, b = pts[k - 1], pts[k], pts[k + 1]
            h1 = math.atan2((c[0] - a[0]) * ky, (c[1] - a[1]) * kx)
            h2 = math.atan2((b[0] - c[0]) * ky, (b[1] - c[1]) * kx)
            if abs((math.degrees(h2 - h1) + 180.0) % 360.0 - 180.0) < SMOOTH_MIN_TURN_DEG:
                out.append(c)
                continue
            d1 = math.hypot((c[0] - a[0]) * ky, (c[1] - a[1]) * kx)
            d2 = math.hypot((b[0] - c[0]) * ky, (b[1] - c[1]) * kx)
            out.append(towards(c, a, min(0.25 * d1, cut)))
            out.append(towards(c, b, min(0.25 * d2, cut)))
        out.append(pts[-1])
        pts = out
    return tuple(pts)


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


def route_order(cells: Sequence[Tuple[int, int]], points: Sequence[Tuple[float, float]],
                cell_deg: float = CELL_DEG) -> List[Tuple[int, int]]:
    """Ячейки по ходу маршрута: сначала у старта, дальше -- как их проходит борт (раньше -- по координатам, с юго-запада:
    при полёте на восток нужные впереди ячейки грузились последними)."""
    pts = [(la, lo) for la, lo in points if la or lo]
    if not pts or not cells:
        return list(cells)
    samples, along = [], []
    dist = 0.0
    for a, b in zip(pts, pts[1:] or [pts[0]]):
        seg = math.hypot((b[0] - a[0]) * 111.0, (b[1] - a[1]) * 111.0 * math.cos(math.radians(a[0])))
        n = max(1, int(seg / 2.0) + 1)
        for k in range(n):
            samples.append((a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n))
            along.append(dist + seg * k / n)
        dist += seg
    samples.append(pts[-1]); along.append(dist)

    def key(c):
        ci, cj = (c[0] + 0.5) * cell_deg, (c[1] + 0.5) * cell_deg
        kx = math.cos(math.radians(ci))
        k = min(range(len(samples)), key=lambda q: (samples[q][0] - ci) ** 2 + ((samples[q][1] - cj) * kx) ** 2)
        return (along[k], c)
    return sorted(cells, key=key)


def cell_bbox(i: int, j: int, cell_deg: float = CELL_DEG) -> Tuple[float, float, float, float]:
    """(юг, запад, север, восток)."""
    return i * cell_deg, j * cell_deg, (i + 1) * cell_deg, (j + 1) * cell_deg


# ------------------------------------------------------------------------------------------- набор и индекс
def _m_per_deg(lat: float) -> Tuple[float, float]:
    phi = math.radians(lat)
    return (111412.84 * math.cos(phi) - 93.5 * math.cos(3 * phi),
            111132.92 - 559.82 * math.cos(2 * phi) + 1.175 * math.cos(4 * phi))


def _ring_area_m2(ring: Sequence[Tuple[float, float]]) -> float:
    lat0 = ring[0][0]
    kx, ky = 111320.0 * math.cos(math.radians(lat0)), 110540.0
    a = 0.0
    for (la1, lo1), (la2, lo2) in zip(ring, ring[1:]):
        a += (lo1 * kx) * (la2 * ky) - (lo2 * kx) * (la1 * ky)
    return abs(a) / 2.0


def _width_tag(tags: dict) -> Optional[float]:
    v = parse_length_m(tags.get("width"))
    if v and 1.0 < v < 2000.0:
        return v
    try:
        lanes = float(str(tags.get("lanes", "")).split(";")[0])
        if 0 < lanes < 20:
            return lanes * 3.5 + 1.0
    except ValueError:
        pass
    return None


def _join_rings(ways: List[List[Tuple[float, float]]]) -> List[List[Tuple[float, float]]]:
    """Куски границы мультиполигона -> замкнутые кольца (склейка по общим концам)."""
    rings, open_ = [], [list(w) for w in ways if len(w) >= 2]
    while open_:
        cur = open_.pop()
        changed = True
        while cur[0] != cur[-1] and changed:
            changed = False
            for k, w in enumerate(open_):
                if w[0] == cur[-1]:
                    cur += w[1:]
                elif w[-1] == cur[-1]:
                    cur += w[-2::-1]
                elif w[-1] == cur[0]:
                    cur = w[:-1] + cur
                elif w[0] == cur[0]:
                    cur = w[:0:-1] + cur
                else:
                    continue
                open_.pop(k)
                changed = True
                break
        if len(cur) >= 4 and cur[0] == cur[-1]:
            rings.append(cur)
    return rings
