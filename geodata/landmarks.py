"""Ориентиры: дороги, дорожные мосты, вода -- из Overture.

Часть пакета geodata (бывший obstacles.py); старое имя `import obstacles` продолжает работать."""
from __future__ import annotations
import json
import math
import os
from typing import (Dict, Iterable, List, Optional, Tuple)
from .common import (_join_rings, _width_tag, smooth_polyline)
from .overture import (
    OVT_MARKS_VERSION,
    OVT_ROAD_CLASSES,
    OVT_WATER_LINES,
    OVT_WATER_POLY,
    OvertureReader,
    _rule_ranges,
    _slice_line,
    _wkb_line,
    _wkb_polygons,
)
from .osm_obstacles import (BRIDGE_TAGS, ObstacleLoader, ROAD_WIDTH_M, geo_ids, parse_bridges)


# =====================================================================================================================
# ОРИЕНТИРЫ (OpenStreetMap): крупные дороги, реки и каналы, водоёмы -- то, по чему пилот узнаёт местность без приборов.
#
# Свои файлы кэша (landmarks_v*), тот же механизм, что у ЛЭП (кусками, со сроком, с кэшем кусков). Большие водоёмы (Днепр,
# водохранилища) -- огромные мультиполигоны: в запросе куска только их НОМЕРА, геометрия каждого -- отдельным запросом
# ОДИН раз за всё время (кэш по номеру): на таких мультиполигонах Overpass ломался, когда так грузили лес.
# =====================================================================================================================
LANDMARKS_VERSION = 2            # 2: главное отдельно от деталей (запрос легче); файлы версии 1 содержат всё и засчитываются


LANDMARKS_COMPAT = (("landmarks", 1),)


WATER_REL_VERSION = 1            # файлы больших водоёмов (по номеру) не зависят от запроса дорог: смена версии их не трогает


WATER_BIG_PERIMETER_M = 2000     # озеро одним контуром длиннее этого -- в главной очереди (сервер отсекает по длине сам)


RIVER_WIDTH_M = {"river": 25.0, "canal": 12.0}


WATER_REL_DEADLINE_S = 150.0



class LandmarkSet:
    """Ориентиры без повторов: дороги [(класс, ширина, точки)], реки [(ширина, точки)], водоёмы [(внешнее кольцо, дыры)]."""

    def __init__(self):
        self.roads: List[Tuple[str, float, tuple]] = []
        self.rivers: List[Tuple[float, tuple]] = []
        self.waters: List[Tuple[tuple, tuple]] = []
        self.bridges: List[Tuple[str, float, tuple, int, str]] = []    # мосты крупных дорог
        self._seen: set = set()
        self.version = 0                                       # растёт с каждым добавлением -- видеокарте пора перестроить

    def add_ways(self, elements: Iterable[dict]) -> int:
        n = 0
        for el in elements:
            if el.get("type") != "way":
                continue
            key = ("w", el.get("id"))
            if key in self._seen:
                continue
            tags = el.get("tags") or {}
            pts = tuple((g["lat"], g["lon"]) for g in (el.get("geometry") or []) if "lat" in g and "lon" in g)
            if len(pts) < 2:
                continue
            self._seen.add(key)
            hw, ww = tags.get("highway"), tags.get("waterway")
            if hw in ROAD_WIDTH_M and tags.get("bridge") in BRIDGE_TAGS:   # дорога по мосту -- настилом, не асфальтом на воде
                self.bridges += parse_bridges([el])
                n += 1
                continue
            if hw in ROAD_WIDTH_M or ww in RIVER_WIDTH_M:
                pts = smooth_polyline(pts)                         # дороги и реки -- плавные, без изломов в вершинах
            if hw in ROAD_WIDTH_M and tags.get("tunnel") not in ("yes",):
                self.roads.append((hw, _width_tag(tags) or ROAD_WIDTH_M[hw], pts))
            elif ww in RIVER_WIDTH_M and tags.get("tunnel") not in ("yes", "culvert"):
                self.rivers.append((_width_tag(tags) or RIVER_WIDTH_M[ww], pts))
            elif len(pts) >= 4 and pts[0] == pts[-1]:            # natural=water / водохранилище -- замкнутый контур
                self.waters.append((pts, ()))
            else:
                continue
            n += 1
        if n:
            self.version += 1
        return n

    def add_relation(self, rid: int, outers, inners) -> None:
        key = ("r", rid)
        if key in self._seen:
            return
        self._seen.add(key)
        for ring in outers:                                  # дыры (острова) -- общим списком к каждому внешнему кольцу
            self.waters.append((tuple(ring), tuple(tuple(r) for r in inners)))
        self.version += 1

    def summary(self) -> Dict[str, float]:
        def km(pts):
            return sum(math.hypot((b[1] - a[1]) * 111320.0 * math.cos(math.radians(a[0])), (b[0] - a[0]) * 110540.0)
                       for a, b in zip(pts, pts[1:])) / 1000.0
        return {"roads_km": round(sum(km(p) for _c, _w, p in self.roads), 1),
                "rivers_km": round(sum(km(p) for _w, p in self.rivers), 1), "waters": len(self.waters)}


class LandmarkLoader(ObstacleLoader):
    """Ориентиры по коридору маршрута: тот же механизм, что у ЛЭП (кусками, со сроком, с кэшем), свои файлы landmarks_v*."""
    PREFIX = "landmarks"
    VERSION = LANDMARKS_VERSION
    COMPAT = LANDMARKS_COMPAT

    def __init__(self, *a, marks: Optional["LandmarkSet"] = None, **kw):
        super().__init__(*a, **kw)
        self.marks = marks if marks is not None else LandmarkSet()     # детали -- в набор главного загрузчика


    def current_marks(self) -> LandmarkSet:
        return self.marks


def overture_marks_elements(roads_rows, water_rows):
    """Строки Overture -> элементы вида OSM: way (дороги, мосты, реки, озёра) и ovt_water (полигоны с островами)."""
    ll = lambda pts: [{"lat": la, "lon": lo} for lo, la in pts]
    out, nid = [], 0
    for r in roads_rows:
        if r.get("subtype") != "road" or r.get("class") not in OVT_ROAD_CLASSES or not r.get("geometry"):
            continue
        pts = _wkb_line(r["geometry"])
        if not pts or len(pts) < 2:
            continue
        flags = r.get("road_flags") or []
        link = r.get("subclass") == "link" or any("is_link" in (x.get("values") or []) and (x.get("between") is None) for x in flags)
        hw = r["class"] + ("_link" if link else "")                  # съезды развязок -- свой класс (уже, свой уклон)
        if hw not in ROAD_WIDTH_M:
            hw = r["class"]
        widths = [w for _a, _c, w in _rule_ranges(r.get("width_rules")) if w]
        bridges = _rule_ranges(flags, "is_bridge")
        tunnels = _rule_ranges(flags, "is_tunnel")
        cuts = sorted({0.0, 1.0} | {x for a, c, _ in bridges + tunnels for x in (a, c)})
        for a, c in zip(cuts, cuts[1:]):
            mid = (a + c) / 2.0
            if c - a < 1e-6 or any(ta <= mid <= tc for ta, tc, _ in tunnels):
                continue
            part = _slice_line(pts, a, c)
            if len(part) < 2:
                continue
            tags = {"highway": hw}
            if widths:
                tags["width"] = str(widths[0])
            if any(ba <= mid <= bc for ba, bc, _ in bridges):
                tags["bridge"] = "yes"
                lv = [v for la_, lc_, v in _rule_ranges(r.get("level_rules")) if v is not None and la_ <= mid <= lc_]
                tags["layer"] = str(lv[0] if lv else 1)
            nid += 1
            out.append({"type": "way", "id": f"ovt_r{nid}", "tags": tags, "geometry": ll(part)})
    for r in water_rows:
        g, st = r.get("geometry"), r.get("subtype")
        if not g or r.get("class") in ("swimming_pool", "drain", "ditch") or r.get("is_intermittent"):
            continue
        line = _wkb_line(g)
        if line is not None:
            if st in OVT_WATER_LINES and len(line) >= 2:
                nid += 1
                out.append({"type": "way", "id": f"ovt_w{nid}", "tags": {"waterway": OVT_WATER_LINES[st]}, "geometry": ll(line)})
            continue
        if st not in OVT_WATER_POLY:
            continue
        for rings in _wkb_polygons(g):
            if not rings or len(rings[0]) < 4:
                continue
            nid += 1
            out.append({"type": "ovt_water", "id": f"ovt_p{nid}",
                        "outer": [[la, lo] for lo, la in rings[0]],
                        "inner": [[[la, lo] for lo, la in ring] for ring in rings[1:] if len(ring) >= 4]})
    return out


class OvertureMarksLoader(LandmarkLoader):
    """Ориентиры (дороги, дорожные мосты, вода) из Overture -- по клеткам 0.25 градуса, кэш ovt_marks_v*."""
    PREFIX = "ovt_marks"
    VERSION = OVT_MARKS_VERSION
    COMPAT = ()

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._roads = OvertureReader(self.cache.dir, "transportation", "segment")
        self._water = OvertureReader(self.cache.dir, "base", "water")

    def _fetch_split(self, bbox, depth: int):
        try:
            import pyarrow.dataset as ds                             # без pyarrow -- ошибка квадрата, а не конец загрузки
            self.stage = "Overture: дороги"
            road_rows = self._roads.table(bbox, ["subtype", "class", "subclass", "road_flags", "level_rules", "width_rules", "geometry"],
                                          ds.field("subtype") == "road")
            self.stage = "Overture: вода"
            water_rows = self._water.table(bbox, ["subtype", "class", "is_intermittent", "geometry"])
            return overture_marks_elements(road_rows, water_rows), []
        except Exception as ex:
            return [], [f"Overture: {type(ex).__name__}: {ex}"]

    def _absorb(self, i: int, j: int, elements: List[dict]) -> None:
        elements = geo_ids(elements)                                 # номера в квадратах повторяются -- ключ по геометрии
        if self.align is not None:                                   # совмещение со снимком (из карты)
            elements = self.align.shift_elements(elements)
        self.marks.add_ways([el for el in elements if el.get("type") == "way"])
        for el in elements:
            if el.get("type") == "ovt_water":
                self.marks.add_relation(el["id"], [[tuple(p) for p in el["outer"]]],
                                        [[tuple(p) for p in r] for r in el.get("inner", [])])
