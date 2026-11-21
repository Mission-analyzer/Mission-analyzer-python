"""Аэродромы: полосы, рулёжки, перроны -- из Overture.

Часть пакета geodata (бывший obstacles.py); старое имя `import obstacles` продолжает работать."""
from __future__ import annotations
import math
from typing import (Iterable, List, Optional, Tuple)
from .common import (_width_tag, smooth_polyline)
from .overture import (OvertureReader, _wkb_line, _wkb_polygons)
from .osm_obstacles import (ObstacleLoader, geo_ids)


# =====================================================================================================================
# АЭРОДРОМЫ (OpenStreetMap: aeroway=runway / taxiway / apron) -- сильнейший ориентир и цель захода на посадку.
# Свой кэш (airfields_v*): запрос лёгкий, не тянет перекачку дорог и воды. Полоса -- линия с шириной, перрон -- контур.
# =====================================================================================================================
AIRFIELDS_VERSION = 2            # 2: + taxilane (рулёжки по перрону) и рулёжки-площади (раньше терялись)


AERO_WIDTH_M = {"runway": 45.0, "taxiway": 18.0, "taxilane": 12.0}


def runway_width_by_length(length_m: float) -> float:
    """Ширина полосы, когда в данных её нет (у Overture нет): магистральная -- 45 м, средняя -- 30 м, короткая -- 23 м."""
    return 45.0 if length_m >= 2400.0 else (30.0 if length_m >= 1200.0 else 23.0)



REF_MARGIN_M = 300.0        # район аэродрома справочника: рамка его полос и рулёжек плюс столько


def _catalog_width(pts) -> float:
    try:
        from geodata.airfield_objs import catalog_width
        return catalog_width(pts)
    except Exception:
        return 0.0


def _len_m(pts):
    return sum(math.hypot((b[1] - a[1]) * 111320.0 * math.cos(math.radians(a[0])), (b[0] - a[0]) * 110540.0)
               for a, b in zip(pts, pts[1:]))


def runway_from_outline(pts):
    """Контур полосы -> ((конец, конец), ширина м): главная ось контура, концы -- крайние проекции на неё, ширина --
    размах поперёк. Не похоже на полосу (короче 300 м или шире трети длины) -- None."""
    la0 = sum(p[0] for p in pts) / len(pts)
    lo0 = sum(p[1] for p in pts) / len(pts)
    kx = 111320.0 * math.cos(math.radians(la0))
    xs = [(p[1] - lo0) * kx for p in pts]
    ys = [(p[0] - la0) * 110540.0 for p in pts]
    best = None                                                      # прямоугольник наименьшей площади: ось -- вдоль
    for i in range(len(xs) - 1):                                     # одной из сторон контура
        dx, dy = xs[i + 1] - xs[i], ys[i + 1] - ys[i]
        n = math.hypot(dx, dy)
        if n < 1e-6:
            continue
        ux, uy = dx / n, dy / n
        along = [x * ux + y * uy for x, y in zip(xs, ys)]
        across = [-x * uy + y * ux for x, y in zip(xs, ys)]
        L_, W_ = max(along) - min(along), max(across) - min(across)
        if L_ < W_:
            continue
        if best is None or L_ * W_ < best[0]:
            best = (L_ * W_, ux, uy, along, across)
    if best is None:
        return None
    _a, ux, uy, along, across = best
    L, W = max(along) - min(along), max(across) - min(across)
    if L < 300.0 or W > L / 3.0 or W <= 0:
        return None
    c = (max(across) + min(across)) / 2.0
    ends = []
    for a in (min(along), max(along)):
        x, y = a * ux - c * uy, a * uy + c * ux
        ends.append((la0 + y / 110540.0, lo0 + x / kx))
    return (ends[0], ends[1]), round(W, 1)


class AirfieldSet:
    """Полосы и рулёжки [(вид, ширина, точки, обозначение)], перроны [контур] -- без повторов."""

    def __init__(self):
        self.runways: List[Tuple[str, float, tuple, str]] = []
        self.aprons: List[tuple] = []
        self._seen: set = set()
        self._ref: list = []                    # аэродромы справочника: [(юг, запад, север, восток, {виды})] -- главнее данных
        self.version = 0

    def add_reference(self, groups) -> int:
        """Полосы и рулёжки из справочника аэродромов (geodata.airfield_objs.reference_geometry): по аэродрому --
        [(вид, ширина, точки, обозначение)]. Рисуются всегда, даже если в скачанных данных их нет. Данные того же вида
        в районе этого аэродрома (рамка его геометрии + REF_MARGIN_M) потом пропускаются -- справочник главнее."""
        n = 0
        for items in groups:
            kinds, la_s, lo_s = set(), [], []
            for kind, width, pts, ref in items:
                pts = tuple((float(a), float(b)) for a, b in pts)
                if len(pts) < 2:
                    continue
                if kind == "pavement":                               # площадка бетона: рулёжки и перроны
                    self.aprons.append(pts)
                    kinds |= {"apron", "taxiway"}
                    la_s += [q[0] for q in pts]
                    lo_s += [q[1] for q in pts]
                    n += 1
                    continue
                w = float(width or 0) or (runway_width_by_length(_len_m(pts)) if kind == "runway" else AERO_WIDTH_M["taxiway"])
                if kind == "taxiway" and len(pts) > 2:
                    pts = smooth_polyline(pts)
                self.runways.append((kind, w, pts, str(ref or "")))
                kinds.add(kind)
                la_s += [q[0] for q in pts]
                lo_s += [q[1] for q in pts]
                n += 1
            if kinds:
                dla = REF_MARGIN_M / 110540.0
                dlo = REF_MARGIN_M / (111320.0 * math.cos(math.radians(la_s[0])))
                self._ref.append((min(la_s) - dla, min(lo_s) - dlo, max(la_s) + dla, max(lo_s) + dlo, kinds))
        if n:
            self.version += 1
        return n

    def _ref_covers(self, kind, pts) -> bool:
        """Элемент данных вида kind лежит на аэродроме, где этот вид задан справочником."""
        if not self._ref:
            return False
        kind = "taxiway" if kind == "taxilane" else kind
        la, lo = pts[len(pts) // 2]
        return any(s <= la <= n and w <= lo <= e and kind in kinds for s, w, n, e, kinds in self._ref)

    def add_ways(self, elements: Iterable[dict]) -> int:
        n = 0
        for el in elements:
            if el.get("type") != "way":
                continue
            tags = el.get("tags") or {}
            kind = tags.get("aeroway")
            pts = tuple((g["lat"], g["lon"]) for g in (el.get("geometry") or []) if "lat" in g and "lon" in g)
            if len(pts) < 2:
                continue
            # повтор -- по самой линии, не по id: у Overture id был номером строки в квадрате («ovt_a0», «ovt_a1»...),
            # и полоса второго квадрата с тем же номером считалась повтором и выбрасывалась (Гостомель без полосы)
            key = (kind, round(pts[0][0], 6), round(pts[0][1], 6), round(pts[-1][0], 6), round(pts[-1][1], 6), len(pts))
            if key in self._seen:
                continue
            self._seen.add(key)
            closed = len(pts) >= 4 and pts[0] == pts[-1]
            if kind in ("apron", "helipad") and closed:
                if self._ref_covers("apron", pts):
                    continue
                self.aprons.append(pts)
            elif kind in AERO_WIDTH_M and not closed:
                width = _width_tag(tags)
                if kind == "runway" and not width:                 # ширины нет (Overture) -- из справочника, иначе по длине
                    width = _catalog_width(pts) or runway_width_by_length(_len_m(pts))
                if kind in ("taxiway", "taxilane"):
                    pts = smooth_polyline(pts)                     # рулёжки плавные; полоса -- прямая, как есть
                elif kind == "runway":                             # поправка длины (airfields.json, «Довжина ЗПС» на «Карті»)
                    try:
                        from geodata.airfield_objs import fix_runway, runway_fixes
                        fixed = fix_runway(pts, runway_fixes())
                        if fixed is not pts:
                            pts = tuple(fixed)
                            L = sum(math.hypot((b[1] - a[1]) * 111320.0 * math.cos(math.radians(a[0])), (b[0] - a[0]) * 110540.0)
                                    for a, b in zip(pts, pts[1:]))
                            if not _width_tag(tags):
                                width = _catalog_width(pts) or runway_width_by_length(L)
                    except Exception:
                        pass
                if self._ref_covers(kind, pts):                    # аэродром есть в справочнике -- рисуется оттуда
                    continue
                self.runways.append(("taxiway" if kind == "taxilane" else kind, width or AERO_WIDTH_M[kind], pts,
                                     str(tags.get("ref") or "")))
            elif kind == "runway" and closed:                      # полоса контуром (Overture так отдаёт часть ВПП, напр.
                ln = runway_from_outline(pts)                      # Гостомель): -> осевая и ширина, с разметкой
                if ln is None:
                    if not self._ref_covers("runway", pts):
                        self.aprons.append(pts)
                else:
                    line, width = ln
                    if self._ref_covers("runway", line):
                        continue
                    self.runways.append(("runway", width, line, str(tags.get("ref") or "")))
            elif kind in AERO_WIDTH_M and closed:                  # рулёжка площадью -- ровный бетон, как перрон
                if self._ref_covers("taxiway", pts):
                    continue
                self.aprons.append(pts)
            else:
                continue
            n += 1
        if n:
            self.version += 1
        return n


class AirfieldLoader(ObstacleLoader):
    PREFIX = "airfields"
    VERSION = AIRFIELDS_VERSION
    COMPAT = ()

    def __init__(self, *a, fields: Optional["AirfieldSet"] = None, **kw):
        super().__init__(*a, **kw)
        if fields is None:
            fields = AirfieldSet()
            try:                                                     # полосы справочника -- сразу, данные -- поверх
                from geodata.airfield_objs import reference_geometry
                fields.add_reference(reference_geometry())
            except Exception:
                pass
        self.fields = fields


    def _absorb(self, i: int, j: int, elements: List[dict]) -> None:
        elements = geo_ids(elements)                                 # номера в квадратах повторяются -- ключ по геометрии
        if self.align is not None:                                   # совмещение со снимком (из карты)
            elements = self.align.apply_runways(elements)            # ВПП, подогнанные по снимку (ширина, концы, ось)
            elements = self.align.shift_elements(elements)
        self.fields.add_ways(elements)


class OvertureAirfieldLoader(AirfieldLoader):
    """Аэродромы из Overture (base/infrastructure, subtype=airport): полосы, рулёжки и taxilane линиями, перроны и
    вертолётные площадки полигонами. Кэш ovt_air_v*."""
    PREFIX = "ovt_air"
    VERSION = 1
    COMPAT = ()
    CLASSES = {"runway": "runway", "taxiway": "taxiway", "taxilane": "taxilane", "apron": "apron", "helipad": "helipad"}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._infra = OvertureReader(self.cache.dir, "base", "infrastructure")

    def _fetch_split(self, bbox, depth: int):
        try:
            import pyarrow.dataset as ds                             # без pyarrow -- ошибка квадрата, а не конец загрузки
            self.stage = "Overture: аеродроми"
            rows = self._infra.table(bbox, ["subtype", "class", "geometry"], ds.field("subtype") == "airport")
        except Exception as ex:
            return [], [f"Overture: {type(ex).__name__}: {ex}"]
        out = []
        for k, r in enumerate(rows):
            kind, g = self.CLASSES.get(r.get("class")), r.get("geometry")
            if not kind or not g:
                continue
            line = _wkb_line(g)
            if line is not None and kind in AERO_WIDTH_M and len(line) >= 2:
                pts = line
            elif line is None:
                polys = _wkb_polygons(g)
                if not polys or len(polys[0][0]) < 4:
                    continue
                pts = polys[0][0]
                if pts[0] != pts[-1]:
                    pts = pts + [pts[0]]
            else:
                continue
            out.append({"type": "way", "id": "ovt_a%.6f_%.6f_%d" % (pts[0][1], pts[0][0], len(pts)), "tags": {"aeroway": kind},
                        "geometry": [{"lat": la, "lon": lo} for lo, la in pts]})
        return out, []
