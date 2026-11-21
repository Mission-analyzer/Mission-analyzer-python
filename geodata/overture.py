"""Overture Maps: каталог STAC (рамки файлов), чтение GeoParquet по рамке, разбор геометрии WKB.

Часть пакета geodata (бывший obstacles.py); старое имя `import obstacles` продолжает работать."""
from __future__ import annotations
import json
import math
import os
import time
from typing import (List)


# ---------------------------------------------------------------------------------------------------------------------
# ЗДАНИЯ OVERTURE MAPS (сводный набор Overture: OpenStreetMap -> Esri -> Google -> Microsoft; ежемесячный выпуск). Главное
# для безопасности: у городских домов есть НАСТОЯЩАЯ высота или этажность (у Microsoft в Украине высот нет -- была условная
# высота по площади, не больше 12 м: 25-этажная башня считалась 12-метровым домом), и новые дома из OSM попадают быстрее.
# Чтение: каталог STAC выпуска даёт рамку каждого из ~500 файлов GeoParquet -> открываем только 1-3 нужных, pyarrow берёт
# из них лишь куски с нужным районом (частичными запросами по сети). Нет pyarrow или Overture недоступен -- Microsoft.
# ---------------------------------------------------------------------------------------------------------------------
OVERTURE_STAC = "https://stac.overturemaps.org"


OVERTURE_BUCKET = "overturemaps-us-west-2"


OVERTURE_REGION = "us-west-2"


OVERTURE_CACHE_VERSION = 1


OVERTURE_INDEX_TTL_DAYS = 30.0


OVERTURE_MAX_FAILS = 2           # сбоев подряд -- и до конца запуска здания берутся из Microsoft (не ждать на каждом квадрате)


FLOOR_HEIGHT_M = 3.0             # высота этажа, когда известна только этажность


ROOF_ALLOWANCE_M = 1.0           # к этажам -- кровля и парапет


def overture_available() -> bool:
    try:
        import pyarrow.dataset  # noqa: F401
        import pyarrow.fs  # noqa: F401
        return True
    except Exception:
        return False


# =====================================================================================================================
# ОРИЕНТИРЫ ИЗ OVERTURE (дороги, дорожные мосты, вода). Overpass для массовой загрузки не годится (общий сервер, пара
# запросов в минуту с адреса): клетка над Киевом грузилась десятки минут или не грузилась вовсе. Overture -- те же данные
# OSM, переведённые в свою схему, в облачных файлах: вся клетка 0.25 градуса читается за ~10 с (дороги 7 с, вода 5 с).
# Выход -- в виде элементов OSM (way + tags), чтобы дальше всё шло тем же путём: маски, мосты, удар о настил.
# Мосты -- из флага is_bridge дороги (доля длины between), слой -- level_rules, ширина -- width_rules; тоннели отбрасываются.
# =====================================================================================================================
OVT_MARKS_VERSION = 2            # 2: + третьестепенные дороги (tertiary) и их съезды -- старый кэш ориентиров перекачать


OVT_ROAD_CLASSES = ("motorway", "trunk", "primary", "secondary", "tertiary")


OVT_WATER_LINES = {"river": "river", "canal": "canal"}           # subtype -> waterway (канавы и ручьи -- не ориентиры)


OVT_WATER_POLY = ("water", "lake", "pond", "reservoir", "river", "canal")


def _wkb_polygons(b: bytes):
    """WKB Polygon/MultiPolygon -> [[кольцо (lon, lat)...] на полигон]; первое кольцо -- внешнее."""
    import struct
    out = []

    def rd_geom(off):
        bo = "<" if b[off] == 1 else ">"
        t = struct.unpack_from(bo + "I", b, off + 1)[0]
        dims = 2
        if t & 0x80000000 or t & 0x40000000:
            dims += (1 if t & 0x80000000 else 0) + (1 if t & 0x40000000 else 0)
            t &= 0xFFFF
        elif t >= 1000:
            dims += {1: 1, 2: 1, 3: 2}.get(t // 1000, 0)
        base = t % 1000
        off += 5
        if base == 6:
            n = struct.unpack_from(bo + "I", b, off)[0]
            off += 4
            for _ in range(n):
                off = rd_geom(off)
            return off
        if base != 3:
            raise ValueError("не полігон")
        nr = struct.unpack_from(bo + "I", b, off)[0]
        off += 4
        rings = []
        for _ in range(nr):
            npts = struct.unpack_from(bo + "I", b, off)[0]
            vals = struct.unpack_from(bo + "d" * (npts * dims), b, off + 4)
            rings.append([(vals[k * dims], vals[k * dims + 1]) for k in range(npts)])
            off += 4 + 8 * npts * dims
        out.append(rings)
        return off
    try:
        rd_geom(0)
    except Exception:
        pass
    return out


def _wkb_line(b: bytes):
    """WKB LineString (2D/Z) -> [(lon, lat)] или None."""
    import struct
    try:
        bo = "<" if b[0] == 1 else ">"
        t = struct.unpack_from(bo + "I", b, 1)[0]
        dims = 2
        if t & 0x80000000 or t & 0x40000000:
            dims += (1 if t & 0x80000000 else 0) + (1 if t & 0x40000000 else 0)
            t &= 0xFFFF
        elif t >= 1000:
            dims += {1: 1, 2: 1, 3: 2}.get(t // 1000, 0)
        if t % 1000 != 2:
            return None
        n = struct.unpack_from(bo + "I", b, 5)[0]
        vals = struct.unpack_from(bo + "d" * (n * dims), b, 9)
        return [(vals[k * dims], vals[k * dims + 1]) for k in range(n)]
    except Exception:
        return None


def _slice_line(pts, a: float, c: float):
    """Часть ломаной (lon, lat) между долями длины a..c."""
    if len(pts) < 2:
        return []
    kx = 111320.0 * math.cos(math.radians(pts[0][1]))
    seg = [math.hypot((q[0] - p[0]) * kx, (q[1] - p[1]) * 110540.0) for p, q in zip(pts, pts[1:])]
    L = sum(seg)
    if L <= 0:
        return []

    def at(d):
        acc = 0.0
        for (p, q), s_ in zip(zip(pts, pts[1:]), seg):
            if acc + s_ >= d:
                t = (d - acc) / s_ if s_ > 0 else 0.0
                return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)
            acc += s_
        return pts[-1]
    da, dc = max(0.0, a) * L, min(1.0, c) * L
    out, acc = [at(da)], 0.0
    for p, s_ in zip(pts[1:], seg):
        acc += s_
        if da < acc < dc:
            out.append(p)
    out.append(at(dc))
    return out if len(out) >= 2 else []


def _rule_ranges(rules, flag=None):
    """Правила Overture -> [(a, c, значение)]; flag -- отобрать только правила с этим флагом в values."""
    out = []
    for r in rules or []:
        btw = r.get("between") or [0.0, 1.0]
        if flag is None:
            out.append((float(btw[0]), float(btw[1]), r.get("value")))
        elif flag in (r.get("values") or []):
            out.append((float(btw[0]), float(btw[1]), True))
    return out


def _wkb_outer_ring(b: bytes):
    """Внешнее кольцо первого полигона (дом) -> [(lat, lon)] или None. Разбор -- общий (_wkb_polygons)."""
    polys = _wkb_polygons(b) if b else []
    if not polys or not polys[0] or len(polys[0][0]) < 4:
        return None
    return [(la, lo) for lo, la in polys[0][0]]


class OvertureReader:
    """Один тип выпуска Overture (тема/тип): каталог STAC с рамками файлов (кэш 30 дней) и чтение таблицы по рамке."""

    def __init__(self, folder: str, theme: str, typ: str):
        self.dir, self.theme, self.typ = folder, theme, typ
        self.files: List[str] = []
        self.boxes: List[List[float]] = []

    def _get_json(self, url: str):
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "MissionAnalyzer/1.0 (ArduPilot ground station tool)"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    def _fs(self):
        import pyarrow.fs as pafs
        return pafs.S3FileSystem(anonymous=True, region=OVERTURE_REGION)

    def load_index(self) -> None:
        path = os.path.join(self.dir, f"overture_index_{self.theme}_{self.typ}.json")
        try:
            if (time.time() - os.path.getmtime(path)) / 86400.0 <= OVERTURE_INDEX_TTL_DAYS:
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                self.files, self.boxes = d["files"], d["boxes"]
                return
        except (OSError, ValueError, KeyError):
            pass
        try:
            import pyarrow.fs as pafs
            rel = self._get_json(f"{OVERTURE_STAC}/catalog.json")["latest"]
            col = self._get_json(f"{OVERTURE_STAC}/{rel}/{self.theme}/{self.typ}/collection.json")
            boxes = col["extent"]["spatial"]["bbox"]
            root = f"{OVERTURE_BUCKET}/release/{rel}/theme={self.theme}/type={self.typ}"
            files = sorted(i.path for i in self._fs().get_file_info(pafs.FileSelector(root)) if i.path.endswith(".parquet"))
            if len(boxes) == len(files) + 1:                       # первая рамка -- общая, дальше -- по файлу
                boxes = boxes[1:]
            elif len(boxes) != len(files):                         # (у набора из одного файла рамка одна -- она и есть)
                raise RuntimeError(f"каталог Overture {self.typ}: рамок {len(boxes)}, файлів {len(files)}")
            self.files, self.boxes = files, boxes
            try:
                os.makedirs(self.dir, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"release": rel, "files": files, "boxes": self.boxes}, f)
            except OSError:
                pass
        except Exception:
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                self.files, self.boxes = d["files"], d["boxes"]
            else:
                raise

    def table(self, bbox, columns, extra=None):
        import pyarrow.dataset as ds
        if not self.files:
            self.load_index()
        S, W, N, E = bbox
        pick = [f for f, b in zip(self.files, self.boxes) if b[0] < E and b[2] > W and b[1] < N and b[3] > S]
        if not pick:
            return []
        f = ds.field
        flt = (f("bbox", "xmin") < E) & (f("bbox", "xmax") > W) & (f("bbox", "ymin") < N) & (f("bbox", "ymax") > S)
        if extra is not None:
            flt = flt & extra
        d = ds.dataset(pick, filesystem=self._fs(), format="parquet")
        cols = [c for c in columns if c in d.schema.names]
        return d.to_table(filter=flt, columns=cols).to_pylist()


def bridge_name_lines(rows, lang: str = "uk"):
    """Строки сегментов Overture (дороги и пути с names и флагами) -> [(имя, [(lat, lon)])] -- только мостовые части
    с именем. Имя части: правило names.rules, покрывающее её середину (у моста бывает своё имя на куске дороги), иначе
    names.common[lang], иначе names.primary."""
    out = []
    for r in rows:
        nm = r.get("names") or {}
        g = r.get("geometry")
        if not nm or not g:
            continue
        flags = (r.get("road_flags") or []) + (r.get("rail_flags") or [])
        brs = _rule_ranges(flags, "is_bridge")
        if not brs:
            continue
        pts = _wkb_line(g)
        if not pts or len(pts) < 2:
            continue
        common = nm.get("common") or {}
        if isinstance(common, list):                                 # pyarrow отдаёт map как список пар
            common = dict(common)
        base = common.get(lang) or nm.get("primary") or ""
        for a, c, _v in brs:
            mid = (a + c) / 2.0
            name = base
            for rule in nm.get("rules") or []:
                btw = rule.get("between")
                if rule.get("value") and btw and btw[0] <= mid <= btw[1] and rule.get("language") in (None, lang):
                    name = rule["value"]
                    break
            part = _slice_line(pts, a, c)
            if name and len(part) >= 2:
                out.append((str(name), [(la, lo) for lo, la in part]))
    return out


OVT_FOOT_CLASSES = ("footway", "pedestrian", "path", "cycleway", "steps", "bridleway")
FOOT_BRIDGE_W_M = 4.0            # ширина перехода по умолчанию (с перилами)
FOOT_BRIDGE_MIN_M = 15.0


def foot_bridge_ways(rows):
    """Строки сегментов Overture (пешеходные классы) -> элементы way мостовых частей: tags footbridge, bridge, layer, width.
    Ключ -- по геометрии (geo_ids), как у остальных данных."""
    out = []
    for r in rows:
        if r.get("class") not in OVT_FOOT_CLASSES or not r.get("geometry"):
            continue
        brs = _rule_ranges(r.get("road_flags") or [], "is_bridge")
        if not brs:
            continue
        pts = _wkb_line(r["geometry"])
        if not pts or len(pts) < 2:
            continue
        widths = [w for _a, _c, w in _rule_ranges(r.get("width_rules")) if w]
        for a, c, _v in brs:
            part = _slice_line(pts, a, c)
            if len(part) < 2:
                continue
            L = sum(math.hypot((q[0] - p_[0]) * 111320.0 * math.cos(math.radians(p_[1])), (q[1] - p_[1]) * 110540.0)
                    for p_, q in zip(part, part[1:]))
            if L < FOOT_BRIDGE_MIN_M:
                continue
            out.append({"type": "way", "id": f"ovt_f{len(out)}",
                        "tags": {"footbridge": "yes", "bridge": "yes", "width": str(widths[0] if widths else FOOT_BRIDGE_W_M)},
                        "geometry": [{"lat": la, "lon": lo} for lo, la in part]})
    return out


OVT_AERODROME_CLASSES = ("airport", "international_airport", "military_airport", "municipal_airport", "private_airport",
                         "regional_airport", "seaplane_airport", "airstrip", "heliport")


def infra_osm_like(rows):
    """Строки base/infrastructure (аэродромы и мосты) -> элементы «как у Overpass»: {"type", "tags", "center" или
    "geometry"}. Теги -- source_tags (это теги OSM без изменений: icao, iata, ele, bridge:structure...) плюс имя из names.
    Так паспорта аэродромов и типы мостов берутся из тех же облачных файлов Overture, без Overpass."""
    out = []
    for r in rows:
        cls, g = r.get("class"), r.get("geometry")
        if not g:
            continue
        st = r.get("source_tags") or {}
        tags = dict(st) if not isinstance(st, dict) else dict(st)
        nm = r.get("names") or {}
        if nm.get("primary") and not tags.get("name"):
            tags["name"] = nm["primary"]
        common = nm.get("common") or {}
        if isinstance(common, list):
            common = dict(common)
        if common.get("uk") and not tags.get("name:uk"):
            tags["name:uk"] = common["uk"]
        if cls in OVT_AERODROME_CLASSES:
            tags.setdefault("aeroway", "aerodrome")
            polys = _wkb_polygons(g)
            pts = polys[0][0] if polys and polys[0] else (_wkb_line(g) or [])
            if not pts:
                try:
                    import struct
                    bo = "<" if g[0] == 1 else ">"
                    pts = [struct.unpack_from(bo + "dd", g, 5)]
                except Exception:
                    continue
            out.append({"type": "node", "tags": tags, "center": {"lat": sum(p[1] for p in pts) / len(pts),
                                                                  "lon": sum(p[0] for p in pts) / len(pts)}})
        elif cls == "bridge":
            tags.setdefault("man_made", "bridge")
            polys = _wkb_polygons(g)
            pts = polys[0][0] if polys and polys[0] else (_wkb_line(g) or [])
            if len(pts) >= 2:
                out.append({"type": "way", "tags": tags, "geometry": [{"lat": la, "lon": lo} for lo, la in pts]})
    return out
