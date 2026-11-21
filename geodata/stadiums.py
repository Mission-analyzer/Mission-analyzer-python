"""Стадионы -- одна сущность (как мосты и аэродромы): хороший ориентир в городе, и без описания камера рисовала их
неправильно -- контур стадиона из данных о зданиях становился «домом» на всё поле (Олимпийский).

Данные (Overture, по клеткам 0.25 градуса): base/land_use -- stadium (территория), pitch (поле, вид спорта в source_tags),
track (беговая дорожка); buildings -- stadium (чаша), grandstand (трибуна), с высотой; building_part чаши -- части с
min_height (навес/крыша над трибунами).

Типовые отрисовки (kind):
  * bowl   -- чаша: трибуны кольцом от бровки поля (дорожки) до внешней стены, наружная стена, навес над трибунами,
              если он есть в данных (части с min_height) или чаша высокая (Олимпийский, Арена Львов);
  * stands -- трибуны вдоль сторон (здания grandstand -- рисуются как здания), поле открыто;
  * field  -- просто поле (и дорожка) без трибун.
Закрытые арены (sports_hall, крыша над полем) -- обычные здания, сюда не входят.

Форма чаши хранится «в полярных координатах» от центра поля: на каждый из POLAR_N лучей -- радиус бровки (r_in) и
радиус внешней стены (r_out). По ним одинаково строится картинка (gl_meshes) и считается удар (height_at) -- удар только
о нарисованное. Дом с центром на поле -- ложный (это и есть контур чаши), его камера и удар отсеивают."""
from __future__ import annotations

import json
import math
import os
import time
from typing import List, Optional

STORE_VERSION = 1
POLAR_N = 72
LAND_CLASSES = ("stadium", "pitch", "track")
BLD_CLASSES = ("stadium", "grandstand")
EDGE_LOW_M = 2.5                  # бровка: высота нижнего ряда трибун над полем
BOWL_H_MIN, BOWL_H_MAX = 10.0, 60.0
ROOF_AUTO_H = 28.0                # чаша выше этого -- с навесом, даже если частей крыши в данных нет
ROOF_DEPTH = 0.65                 # навес -- на такую долю глубины трибун от внешней стены
ROOF_TH_M = 2.0


def store_path(folder: str, i: int, j: int) -> str:
    return os.path.join(folder, f"stadiums_v{STORE_VERSION}_{i}_{j}.json")


# ------------------------------------------------------------------------------------------------- геометрия
def _ring_xy(ring, lat0, lon0):
    kx = 111320.0 * math.cos(math.radians(lat0))
    return [((lo - lon0) * kx, (la - lat0) * 110540.0) for la, lo in ring]


def _area(xy) -> float:
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(xy, xy[1:] + xy[:1]))) / 2.0


def _centroid(ring):
    return sum(q[0] for q in ring) / len(ring), sum(q[1] for q in ring) / len(ring)


def _inside(la, lo, ring) -> bool:
    ins = False
    for (a0, b0), (a1, b1) in zip(ring, ring[1:] + ring[:1]):
        if (a0 > la) != (a1 > la) and lo < b0 + (la - a0) * (b1 - b0) / ((a1 - a0) or 1e-12):
            ins = not ins
    return ins


def _ray_r(xy, ang) -> Optional[float]:
    """Наибольшее пересечение луча из (0, 0) под углом ang с контуром xy (м); None -- не пересекает."""
    dx, dy = math.cos(ang), math.sin(ang)
    best = None
    for (x0, y0), (x1, y1) in zip(xy, xy[1:] + xy[:1]):
        ex, ey = x1 - x0, y1 - y0
        den = dx * ey - dy * ex
        if abs(den) < 1e-12:
            continue
        t = (x0 * ey - y0 * ex) / den
        u = (x0 * dy - y0 * dx) / den
        if t > 0 and 0.0 <= u <= 1.0:
            best = t if best is None else max(best, t)
    return best


def polar(outer, inner, lat0, lon0, pad_in=0.0):
    """Радиусы бровки и стены по POLAR_N лучам из центра (lat0, lon0). pad_in -- запас наружу от контура поля, м."""
    xo, xi = _ring_xy(outer, lat0, lon0), _ring_xy(inner, lat0, lon0)
    r_in, r_out = [], []
    for k in range(POLAR_N):
        a = 2.0 * math.pi * k / POLAR_N
        ro, ri = _ray_r(xo, a), _ray_r(xi, a)
        if ro is None or ri is None:
            return None
        ri = min(ri + pad_in, 0.85 * ro)
        r_in.append(round(ri, 1))
        r_out.append(round(ro, 1))
    return r_in, r_out


def height_at(o, lat, lon) -> float:
    """Высота над землёй того, что нарисовано у стадиона в точке: трибуны (наклон от бровки к стене), навес. 0 -- поле,
    снаружи или стадион без чаши."""
    if o.get("kind") != "bowl" or not o.get("polar"):
        return 0.0
    kx = 111320.0 * math.cos(math.radians(o["clat"]))
    x, y = (lon - o["clon"]) * kx, (lat - o["clat"]) * 110540.0
    r = math.hypot(x, y)
    a = math.atan2(y, x) % (2.0 * math.pi)
    f = a / (2.0 * math.pi) * POLAR_N
    k0 = int(f) % POLAR_N
    k1 = (k0 + 1) % POLAR_N
    t = f - int(f)
    r_in = o["polar"][0][k0] * (1 - t) + o["polar"][0][k1] * t
    r_out = o["polar"][1][k0] * (1 - t) + o["polar"][1][k1] * t
    if r < r_in or r > r_out:
        return 0.0
    H = float(o["height"])
    h = EDGE_LOW_M + (H - EDGE_LOW_M) * (r - r_in) / max(r_out - r_in, 1.0)
    if o.get("roof") and r >= r_out - ROOF_DEPTH * (r_out - r_in):
        h = max(h, H + ROOF_TH_M)
    return h


# ------------------------------------------------------------------------------------------------- сборка
def _rows_polys(rows):
    """Строки Overture -> [(класс, контур [(lat, lon)], строка)] (берётся внешний контур первого многоугольника)."""
    from .overture import _wkb_polygons
    out = []
    for r in rows:
        g = r.get("geometry")
        if not g:
            continue
        polys = _wkb_polygons(g)
        if not polys or not polys[0] or len(polys[0][0]) < 4:
            continue
        ring = [(la, lo) for lo, la in polys[0][0]]
        if ring[0] == ring[-1]:
            ring = ring[:-1]
        out.append((r.get("class"), ring, r))
    return out


def _name(r) -> str:
    nm = r.get("names") or {}
    common = nm.get("common") or {}
    if isinstance(common, list):
        common = dict(common)
    return common.get("uk") or nm.get("primary") or ""


def _tags(r) -> dict:
    st = r.get("source_tags") or {}
    return dict(st) if not isinstance(st, dict) else st


def build(land_rows, bld_rows, part_rows=()) -> List[dict]:
    """Стадионы из строк land_use (stadium/pitch/track), buildings (stadium/grandstand) и building_part."""
    land = _rows_polys(land_rows)
    blds = _rows_polys(bld_rows)
    parts = _rows_polys(part_rows)
    areas = [(ring, r, "land") for c, ring, r in land if c == "stadium"] + \
        [(ring, r, "bld") for c, ring, r in blds if c == "stadium"]
    pitches = [(ring, r) for c, ring, r in land if c == "pitch"]
    tracks = [(ring, r) for c, ring, r in land if c == "track"]
    stands = [(ring, r) for c, ring, r in blds if c == "grandstand"]
    used = [False] * len(areas)
    out = []
    # территории, накрывающие друг друга (land_use stadium и здание stadium) -- один стадион
    for i, (ring, r, _src) in enumerate(areas):
        if used[i]:
            continue
        group = [i]
        used[i] = True
        c = _centroid(ring)
        for j in range(i + 1, len(areas)):
            if not used[j] and (_inside(*_centroid(areas[j][0]), ring) or _inside(*c, areas[j][0])):
                group.append(j)
                used[j] = True
        outline = max((areas[k][0] for k in group), key=lambda rg: _area(_ring_xy(rg, *c)))
        mine = lambda rg: _inside(*_centroid(rg), outline)
        pt = [(rg, rr) for rg, rr in pitches if mine(rg)]
        tr = [(rg, rr) for rg, rr in tracks if mine(rg)]
        sd = [(rg, rr) for rg, rr in stands if mine(rg) or any(_inside(*q, outline) for q in rg)]
        if not pt and not tr:
            continue                                                 # без поля -- не стадион для нас (арена, комплекс)
        main = max(pt or tr, key=lambda x: _area(_ring_xy(x[0], *c)))
        clat, clon = _centroid(main[0])
        bowl_bld = [areas[k][:2] for k in group if areas[k][2] == "bld" and _inside(clat, clon, areas[k][0])]
        name = next((_name(areas[k][1]) for k in group if _name(areas[k][1])), "") or _name(main[1])
        o = {"id": "s%.4f_%.4f" % (round(clat, 4), round(clon, 4)), "name": name, "clat": round(clat, 7),
             "clon": round(clon, 7), "outline": [[round(a, 7), round(b, 7)] for a, b in outline],
             "pitches": [{"ring": [[round(a, 7), round(b, 7)] for a, b in rg], "sport": _tags(rr).get("sport", "")}
                         for rg, rr in pt],
             "tracks": [[[round(a, 7), round(b, 7)] for a, b in rg] for rg, _rr in tr],
             "stands": [{"ring": [[round(a, 7), round(b, 7)] for a, b in rg], "height": rr.get("height")} for rg, rr in sd],
             "kind": "field", "height": 0.0, "roof": False, "polar": None}
        if bowl_bld:                                                 # чаша: здание stadium вокруг поля
            ring_b, rb = max(bowl_bld, key=lambda x: _area(_ring_xy(x[0], clat, clon)))
            inner = max(tr, key=lambda x: _area(_ring_xy(x[0], clat, clon)))[0] if tr else main[0]
            pol = polar(ring_b, inner, clat, clon, pad_in=2.0 if tr else 6.0)
            if pol is not None:
                area = _area(_ring_xy(ring_b, clat, clon))
                H = rb.get("height") or (float(rb["num_floors"]) * 3.5 if rb.get("num_floors") else None)
                H = float(H) if H else min(BOWL_H_MAX, max(BOWL_H_MIN, 0.09 * math.sqrt(area)))
                H = min(max(H, BOWL_H_MIN), BOWL_H_MAX)
                bid = rb.get("id")
                roof = any(p_[2].get("building_id") == bid and float(p_[2].get("min_height") or 0) > 3.0 for p_ in parts) \
                    or H >= ROOF_AUTO_H
                o.update(kind="bowl", height=round(H, 1), roof=bool(roof), polar=list(pol),
                         bowl=[[round(a, 7), round(b, 7)] for a, b in ring_b])
        elif sd:
            o["kind"] = "stands"
        lats = [q[0] for q in outline]
        lons = [q[1] for q in outline]
        o["bbox"] = [min(lats), min(lons), max(lats), max(lons)]
        out.append(o)
    return out


def pitch_polys(objs):
    """Контуры полей -- для отсева ложных «домов» (центр дома на поле стадиона -- это контур чаши, а не дом)."""
    out = []
    for o in objs:
        for p in o.get("pitches", []):
            out.append((tuple(tuple(q) for q in p["ring"]), ()))
        for t in o.get("tracks", []):
            out.append((tuple(tuple(q) for q in t), ()))
    return out


# ------------------------------------------------------------------------------------------------- хранение
def save_cells(folder, cells, objs, cell_deg) -> int:
    os.makedirs(folder, exist_ok=True)
    n = 0
    for i, j in cells:
        mine = [o for o in objs if (int(o["clat"] // cell_deg), int(o["clon"] // cell_deg)) == (i, j)]
        tmp = store_path(folder, i, j) + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": STORE_VERSION, "built": time.time(), "stadiums": mine}, fh, ensure_ascii=False)
        os.replace(tmp, store_path(folder, i, j))
        n += len(mine)
    return n


def load_cell(folder, i, j) -> Optional[List[dict]]:
    try:
        with open(store_path(folder, i, j), encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    return list(d.get("stadiums", [])) if int(d.get("version", 0)) == STORE_VERSION else None


def load_near(folder, lat, lon, km=30.0, cell_deg=0.25) -> List[dict]:
    dla, dlo = km / 111.0, km / (111.0 * max(math.cos(math.radians(lat)), 0.05))
    out = []
    for i in range(int((lat - dla) // cell_deg), int((lat + dla) // cell_deg) + 1):
        for j in range(int((lon - dlo) // cell_deg), int((lon + dlo) // cell_deg) + 1):
            out += load_cell(folder, i, j) or []
    return out


def fetch_cell(folder, i, j, cell_deg=0.25) -> List[dict]:
    """Скачать (Overture) и собрать стадионы клетки. Без сети -- исключение (клетка останется недокачанной)."""
    import pyarrow.dataset as pds
    from .common import cell_bbox
    from .overture import OvertureReader
    box = cell_bbox(i, j)
    land = OvertureReader(folder, "base", "land_use").table(box, ["class", "names", "source_tags", "geometry"],
                                                            pds.field("class").isin(list(LAND_CLASSES)))
    blds = OvertureReader(folder, "buildings", "building").table(
        box, ["id", "class", "names", "height", "num_floors", "has_parts", "geometry"], pds.field("class").isin(list(BLD_CLASSES)))
    ids = [r["id"] for r in blds if r.get("class") == "stadium" and r.get("has_parts")]
    parts = OvertureReader(folder, "buildings", "building_part").table(
        box, ["building_id", "height", "min_height", "geometry"], pds.field("building_id").isin(ids)) if ids else []
    return build(land, blds, parts)


def list_all(folder) -> List[dict]:
    out = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for name in names:
        if name.startswith(f"stadiums_v{STORE_VERSION}_") and name.endswith(".json"):
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as fh:
                    out += list(json.load(fh).get("stadiums", []))
            except (OSError, ValueError):
                continue
    return out
