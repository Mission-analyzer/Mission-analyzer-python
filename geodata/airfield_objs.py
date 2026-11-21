"""Аэродром -- одна сущность («всё про аэродром -- в аэродроме», как мосты в geodata.bridges).

Из чего собирается. Полосы, рулёжки и перроны из данных (Overture / OSM, geodata.airfields) и «стол» лётного поля
(geodata.ground_fixes.AirfieldGround: контур поля и его плоскость). Паспорт -- название, ИКАО, ИАТА, высота -- из OSM
(Overture base/infrastructure: имя и source_tags -- теги OSM icao, iata, ele; при расчёте модели земли) и из необязательного airfields.json рядом с программой.

Запись аэродрома: id, name, icao, iata, elev_m (высота стола в центре, м AMSL), elev_osm (из OSM, если есть), field
(контур стола [(lat, lon)]), runways [{ref, ends, width, length, heading, pts}], taxiways [{width, pts}], aprons [контур],
lat/lon центра, bbox. Хранение: файлы airfields_v1_<клетка>.json рядом с моделью земли; аэродром -- целиком в клетке
своего центра. Дальше сюда же -- аэровокзал и прочие постройки."""
from __future__ import annotations

import json
import math
import os
import sys
import time
from typing import List, Optional


STORE_VERSION = 1
INFO_NEAR_M = 3000.0              # паспорт OSM (центр аэродрома) -- к полю, если центр в контуре или не дальше этого


def store_path(folder: str, i: int, j: int) -> str:
    return os.path.join(folder, f"airfields_v{STORE_VERSION}_{i}_{j}.json")


# ------------------------------------------------------------------------------------------------- паспорт из OSM
def osm_query(s, w, n, e) -> str:
    return f'[out:json][timeout:60];nwr["aeroway"="aerodrome"]({s:.5f},{w:.5f},{n:.5f},{e:.5f});out tags center;'


def osm_info(elements) -> List[dict]:
    """Элементы Overpass -> [{name, icao, iata, elev_osm, lat, lon}]."""
    out = []
    for el in elements:
        tags = el.get("tags") or {}
        c = el.get("center") or ({"lat": el["lat"], "lon": el["lon"]} if "lat" in el else None)
        if not c:
            continue
        try:
            ele = float(str(tags.get("ele", "")).replace(",", ".").split()[0]) if tags.get("ele") else None
        except (ValueError, IndexError):
            ele = None
        out.append({"name": tags.get("name:uk") or tags.get("name") or tags.get("name:en") or "",
                    "icao": tags.get("icao", ""), "iata": tags.get("iata", ""), "elev_osm": ele,
                    "lat": float(c["lat"]), "lon": float(c["lon"])})
    return out


def _override_dirs():
    return [os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else None,
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.getcwd()]


def overrides_path() -> str:
    """Где лежит (или будет лежать) airfields.json: первый существующий, иначе -- рядом с программой."""
    dirs = [d for d in _override_dirs() if d]
    for d in dirs:
        if os.path.isfile(os.path.join(d, "airfields.json")):
            return os.path.join(d, "airfields.json")
    return os.path.join(dirs[0], "airfields.json")


def save_override(entry: dict) -> str:
    """Записать поправку аэродрома (по id) в airfields.json: название, ИКАО, ИАТА, поправки полос. Прежние поля записи
    сохраняются (переименование не стирает поправку длины полосы и наоборот)."""
    path = overrides_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = [e for e in json.load(fh) if isinstance(e, dict)]
    except (OSError, ValueError):
        data = []
    prev = next((e for e in data if e.get("id") == entry.get("id")), None) or match_override(entry, data) or {}
    data = [e for e in data if e is not prev and e.get("id") != entry.get("id")] + [dict(prev, **entry)]   # дополнить: имя не стирает поправку полосы
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


# Справочник: аэродромы, у которых в открытых данных нет названия (или оно неверное). Подставляется, только если
# своей правки (airfields.json) нет; своя -- главнее. Поиск по месту: центр поля ближе CATALOG_NEAR_M.
CATALOG_FILE = "airfields.dat"   # справочник: формат X-Plane apt.dat 1200 рядом с этим модулем (правится в WED)


def _catalog_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CATALOG_FILE)


def load_catalog(path: Optional[str] = None):
    """geodata/airfields.dat -> (записи справочника, {ключ: строки геометрии}). Запись: lat, lon (1302 datum_*), name
    (заголовок), icao, iata, rwy / length / width / elev_m (1302 ma_*, иначе по главной ВПП и заголовку), geo (код из
    заголовка, если ICAO нет), source (1302 ma_source). Геометрия -- строки 100/110../1300.. как есть."""
    from geodata.xplane_apt import KEEP
    out, raw = [], {}
    try:
        fh = open(path or _catalog_path(), encoding="utf-8", errors="replace")
    except OSError:
        return out, raw
    cur, lines = None, None

    def close():
        if cur is None:
            return
        m = cur["meta"]
        rw = [ln.split() for ln in lines if ln.startswith("100 ")]
        main = None
        for t in rw:
            try:
                L = _length([(float(t[9]), float(t[10])), (float(t[18]), float(t[19]))])
            except (IndexError, ValueError):
                continue
            if main is None or L > main[0]:
                main = (L, t)
        num = lambda k, d=0.0: float(m[k]) if k in m and m[k].replace(".", "", 1).replace("-", "", 1).isdigit() else d
        try:
            lat, lon = float(m["datum_lat"]), float(m["datum_lon"])
        except (KeyError, ValueError):
            if main is None:
                return
            t = main[1]
            lat, lon = (float(t[9]) + float(t[18])) / 2.0, (float(t[10]) + float(t[19])) / 2.0
        icao = m.get("icao_code", "")
        e = {"lat": lat, "lon": lon, "name": cur["name"], "icao": icao, "iata": m.get("iata_code", ""),
             "rwy": m.get("ma_rwy", f"{main[1][8]}/{main[1][17]}" if main else ""),
             "length": num("ma_length", round(main[0]) if main else 0.0),
             "width": num("ma_width", float(main[1][1]) if main else 0.0),
             "elev_m": num("ma_elev_m", round(cur["elev_ft"] * 0.3048)), "source": m.get("ma_source", "")}
        if not icao:
            e["geo"] = cur["code"]
        out.append(e)
        if rw:
            raw[(icao or cur["code"]).upper()] = (cur, lines)

    with fh:
        for ln in fh:
            c = ln.split(None, 1)
            if not c:
                continue
            if c[0] in ("1", "16", "17"):
                close()
                t = ln.split(None, 5)
                cur = {"code": t[4] if len(t) > 4 else "", "name": t[5].strip() if len(t) > 5 else "",
                       "elev_ft": float(t[1]) if len(t) > 1 else 0.0, "meta": {}}
                lines = []
            elif c[0] == "99":
                break
            elif cur is not None and c[0] == "1302":
                t = ln.rstrip("\n").split(None, 2)
                if len(t) > 2:
                    cur["meta"][t[1]] = t[2].strip()
            elif cur is not None and c[0] in KEEP:
                lines.append(ln)
    close()
    return out, raw


CATALOG: List[dict] = []          # заполняется в конце модуля (load_catalog нужны функции ниже)
_CATALOG_RAW: dict = {}
CATALOG_NEAR_M = 3000.0
OVR_NEAR_M = 1500.0     # правка аэродрома держится за место: id сменился (докачка, совмещение сдвинули центр) -- ищем рядом


def _id_pos(aid):
    """id аэродрома -- его округлённый центр («a50.4800_30.3800») -> (широта, долгота); старые правки без координат."""
    try:
        a, b = str(aid)[1:].split("_")
        return float(a), float(b)
    except (ValueError, TypeError, AttributeError):
        return None


def match_override(o, ovs):
    """Правка airfields.json к аэродрому o: по id; не нашлась -- ближайшая с координатами ближе OVR_NEAR_M (id -- это
    округлённый центр поля: после докачки он может сдвинуться, и имя «отрывалось»); или по ICAO (запись без id)."""
    for ov in ovs:
        if ov.get("id") and ov.get("id") == o.get("id"):
            return ov
    best = None
    try:
        la, lo = float(o["lat"]), float(o["lon"])
        kx = 111320.0 * math.cos(math.radians(la))
        for ov in ovs:
            pos = (ov["lat"], ov["lon"]) if "lat" in ov and "lon" in ov else _id_pos(ov.get("id"))
            if pos:
                d = math.hypot((float(pos[0]) - la) * 110540.0, (float(pos[1]) - lo) * kx)
                if d <= OVR_NEAR_M and (best is None or d < best[0]):
                    best = (d, ov)
    except (KeyError, TypeError, ValueError):
        pass
    if best:
        return best[1]
    for ov in ovs:
        if ov.get("icao") and ov.get("icao") == o.get("icao") and not ov.get("id"):
            return ov
    return None


def catalog_width(pts) -> float:
    """Ширина полосы из справочника: запись CATALOG, чей центр ближе CATALOG_NEAR_M к середине полосы и чья главная ВПП
    (обозначение "rwy") идёт тем же курсом; 0 -- не найдено. Справочник главнее ширины из данных (контур Overture,
    подгонка по снимку) -- те часто уже настоящей."""
    try:
        la, lo = (float(pts[0][0]) + float(pts[-1][0])) / 2.0, (float(pts[0][1]) + float(pts[-1][1])) / 2.0
    except (TypeError, ValueError, IndexError):
        return 0.0
    kx = 111320.0 * math.cos(math.radians(la))
    hd = _heading([(float(q[0]), float(q[1])) for q in (pts[0], pts[-1])])
    best = None
    for c in CATALOG:
        d = math.hypot((c["lat"] - la) * 110540.0, (c["lon"] - lo) * kx)
        if not c.get("width") or d > CATALOG_NEAR_M:
            continue
        num = str(c.get("rwy", "")).split("/")[0].rstrip("LRC")
        if num.isdigit():                                            # ширина -- главной ВПП: курс должен совпасть с обозначением
            dh = abs(((hd - int(num) * 10.0) + 180.0) % 360.0 - 180.0)
            if 20.0 < dh < 160.0:
                continue
        if best is None or d < best[0]:
            best = (d, float(c["width"]))
    return best[1] if best else 0.0


def apply_catalog(o):
    """Название и коды из CATALOG -- аэродрому без своей правки, если он рядом с записью справочника. Пустые поля
    заполняются; непустое имя из данных заменяется, только если ICAO совпал (данные назвали его иначе)."""
    try:
        la, lo = float(o["lat"]), float(o["lon"])
    except (KeyError, TypeError, ValueError):
        return o
    kx = 111320.0 * math.cos(math.radians(la))
    for c in CATALOG:
        if math.hypot((c["lat"] - la) * 110540.0, (c["lon"] - lo) * kx) <= CATALOG_NEAR_M:
            same = not o.get("icao") or o.get("icao") == c["icao"]
            for key in ("name", "icao", "iata"):
                if c.get(key) and (not o.get(key) or same):
                    o[key] = c[key]
            break
    return o


def apply_overrides(objs, overrides=None):
    """Поправки airfields.json -- поверх записей (при чтении: переименование видно сразу, без пересчёта модели).
    Поправки длины полос (runway_fixes) -- тоже: полоса в списке и на схеме -- уже исправленная."""
    ovs = load_overrides() if overrides is None else overrides
    fixes = [f for ov in ovs for f in (ov.get("runway_fixes") or []) if isinstance(f, dict)]
    for o in objs:
        ov = match_override(o, ovs)
        if ov:
            for key in ("name", "icao", "iata", "elev_m"):
                if key in ov:
                    o[key] = ov[key]
        else:
            apply_catalog(o)
        src = _geometry_source(o, ov)
        if src:                                                      # справочник задаёт полосы -- поправки длины не нужны
            apply_geometry(o, src)
            continue
        for r in o.get("runways", []):                               # ширина из справочника (в данных её нет)
            w = catalog_width(r.get("pts") or [])
            if w:
                r["width"] = w
        if fixes:
            for r in o.get("runways", []):
                pts = [tuple(q) for q in r.get("pts", [])]
                new = fix_runway(pts, fixes)
                if new is not pts:
                    r.setdefault("orig_pts", r.get("pts"))
                    r["pts"] = [[round(a, 7), round(b, 7)] for a, b in new]
                    r["length"] = round(_length(new), 0)
                    r["fixed"] = True
    return objs


# --------------------------------------------------------------------------------- поправка длины полосы
RWY_FIX_NEAR_M = 400.0      # полоса из данных -- та, что исправлена: середина ближе этого
RWY_FIX_HDG_DEG = 12.0      # ... и курс отличается меньше этого (в любую сторону)


def runway_fix_entry(pts, length_m: float, keep: str) -> dict:
    """Поправка полосы (данные рисуют её длиннее/короче настоящей): новая длина length_m, неподвижный конец keep --
    "first" / "last" (конец у первой / последней точки линии из данных) или "center" (поровну с обоих концов).
    -> запись для airfields.json: исходные середина и курс (по ним узнаётся полоса) и новые концы."""
    p0, p1 = tuple(pts[0]), tuple(pts[-1])
    L0 = _length([p0, p1]) or 1.0
    t = float(length_m) / L0
    if keep == "first":
        a, b = 0.0, t
    elif keep == "last":
        a, b = 1.0 - t, 1.0
    else:
        a, b = 0.5 - t / 2.0, 0.5 + t / 2.0
    lerp = lambda u: (p0[0] + (p1[0] - p0[0]) * u, p0[1] + (p1[1] - p0[1]) * u)
    mid = lerp(0.5)
    return {"mid": [round(mid[0], 7), round(mid[1], 7)], "heading": round(_heading([p0, p1]), 1),
            "length_m": round(float(length_m), 0), "keep": keep,
            "ends": [[round(x, 7) for x in lerp(a)], [round(x, 7) for x in lerp(b)]]}


def fix_runway(pts, fixes):
    """Линия полосы из данных -> исправленная по поправке (если есть), иначе -- та же самая (тот же объект)."""
    if len(pts) < 2 or not fixes:
        return pts
    mid = ((pts[0][0] + pts[-1][0]) / 2.0, (pts[0][1] + pts[-1][1]) / 2.0)
    hd = _heading([pts[0], pts[-1]])
    kx = 111320.0 * math.cos(math.radians(mid[0]))
    for f in fixes:
        try:
            fm, fh, ends = f["mid"], float(f["heading"]), f["ends"]
        except (KeyError, TypeError, ValueError):
            continue
        d = math.hypot((fm[0] - mid[0]) * 110540.0, (fm[1] - mid[1]) * kx)
        dh = abs(((hd - fh) + 180.0) % 360.0 - 180.0)
        if d <= RWY_FIX_NEAR_M and (dh <= RWY_FIX_HDG_DEG or dh >= 180.0 - RWY_FIX_HDG_DEG):
            e0, e1 = tuple(ends[0]), tuple(ends[1])
            return (e0, e1) if dh <= 90.0 else (e1, e0)               # порядок концов -- как у линии из данных
    return pts


_FIX_CACHE = {"key": None, "fixes": []}


def runway_fixes() -> list:
    """Все поправки полос из airfields.json (перечитываются, когда файл изменился)."""
    path = overrides_path()
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        key = (path, None)
    if _FIX_CACHE["key"] != key:
        _FIX_CACHE["key"] = key
        _FIX_CACHE["fixes"] = [f for ov in load_overrides() for f in (ov.get("runway_fixes") or []) if isinstance(f, dict)]
    return _FIX_CACHE["fixes"]


def load_overrides(folder: Optional[str] = None) -> List[dict]:
    """airfields.json рядом с программой: [{"id" или "icao", "name", "icao", "iata", "elev_m"}]."""
    dirs = [folder] if folder else _override_dirs()
    for here in dict.fromkeys(d for d in dirs if d):
        try:
            with open(os.path.join(here, "airfields.json"), encoding="utf-8") as fh:
                return [e for e in json.load(fh) if isinstance(e, dict)]
        except (OSError, ValueError):
            continue
    return []


# ------------------------------------------------------------------------------------------------- сборка
def _heading(pts) -> float:
    (la0, lo0), (la1, lo1) = pts[0], pts[-1]
    kx = math.cos(math.radians(la0))
    return math.degrees(math.atan2((lo1 - lo0) * kx, la1 - la0)) % 360.0


def _length(pts) -> float:
    return sum(math.hypot((b[0] - a[0]) * 110540.0, (b[1] - a[1]) * 111320.0 * math.cos(math.radians(a[0])))
               for a, b in zip(pts, pts[1:]))


def runway_ends(ref: str, heading: float):
    """Обозначения концов: (у первой точки, у последней). У торца написан курс посадки ЧЕРЕЗ него: у первой точки --
    номер, близкий к курсу «от первой точки к последней». Из ref («18/36», «08L/26R»), какой ближе к курсу; без ref --
    по курсу (истинному, магнитный отличается на склонение -- номер может разойтись на единицу)."""
    parts = [p.strip() for p in str(ref or "").replace("-", "/").split("/") if p.strip()]

    def num(p):
        d = "".join(ch for ch in p if ch.isdigit())
        return int(d) if d else None
    if len(parts) == 2 and num(parts[0]) is not None and num(parts[1]) is not None:
        a, b = parts
        da = abs(((num(a) * 10 - heading) + 180) % 360 - 180)
        db = abs(((num(b) * 10 - heading) + 180) % 360 - 180)
        return (a, b) if da <= db else (b, a)
    n0 = int(round(heading / 10.0)) % 36 or 36
    n1 = (n0 + 18 - 1) % 36 + 1
    return f"{n0:02d}", f"{n1:02d}"


def _in_poly(x, y, px, py) -> bool:
    inside = False
    for i in range(len(px)):
        x0, y0, x1, y1 = px[i - 1], py[i - 1], px[i], py[i]
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / ((y1 - y0) or 1e-12):
            inside = not inside
    return inside


def build_objects(np, runways, aprons, afg, info=(), overrides=()) -> List[dict]:
    """Аэродромы из полей стола afg (AirfieldGround): что попало в контур поля -- его полосы, рулёжки, перроны."""
    out = []
    if afg is None or not len(afg):
        return out
    kx = afg.kx
    for k, (hull, c, coef, _box) in enumerate(afg.fields):
        hx, hy = hull[:, 0].tolist(), hull[:, 1].tolist()
        inside = lambda la, lo: _in_poly(lo * kx, la * 110540.0, hx, hy)
        rws, txs, aps = [], [], []
        for kind, w, p, *r in runways:
            p = [tuple(q) for q in p]
            if len(p) < 2:
                continue
            mid = p[len(p) // 2]
            if not inside(*mid):
                continue
            if kind == "runway":
                hd = _heading(p)
                rws.append({"ref": str(r[0] if r else ""), "ends": list(runway_ends(r[0] if r else "", hd)),
                            "width": round(float(w), 1), "length": round(_length(p), 0), "heading": round(hd, 1),
                            "pts": [[round(a, 7), round(b, 7)] for a, b in p]})
            else:
                txs.append({"width": round(float(w), 1), "pts": [[round(a, 7), round(b, 7)] for a, b in p]})
        for a in aprons:
            a = [tuple(q) for q in a]
            if len(a) >= 3 and inside(sum(q[0] for q in a) / len(a), sum(q[1] for q in a) / len(a)):
                aps.append([[round(x, 7), round(y, 7)] for x, y in a])
        if not rws:
            continue
        clat, clon = float(c[1]) / 110540.0, float(c[0]) / kx
        field = [[round(float(y) / 110540.0, 7), round(float(x) / kx, 7)] for x, y in hull]
        main = max(rws, key=lambda r_: r_["length"])
        aid = "a%.4f_%.4f" % (round(clat, 4), round(clon, 4))
        o = {"id": aid, "name": "", "icao": "", "iata": "", "elev_m": round(float(coef[0]), 1), "elev_osm": None,
             "lat": round(clat, 7), "lon": round(clon, 7), "field": field, "runways": rws, "taxiways": txs, "aprons": aps,
             "bbox": [min(q[0] for q in field), min(q[1] for q in field), max(q[0] for q in field), max(q[1] for q in field)],
             "main_runway": main["ref"] or "/".join(main["ends"])}
        best = None                                                  # паспорт OSM: центр в контуре или ближайший в 3 км
        for inf in info:
            d = math.hypot((inf["lat"] - clat) * 110540.0, (inf["lon"] - clon) * kx)
            if inside(inf["lat"], inf["lon"]) or d <= INFO_NEAR_M:
                score = 0.0 if inside(inf["lat"], inf["lon"]) else d
                if best is None or score < best[0]:
                    best = (score, inf)
        if best:
            for key in ("name", "icao", "iata", "elev_osm"):
                o[key] = best[1].get(key) or o[key]
        ov = match_override(o, overrides)                            # airfields.json -- главнее
        if ov:
            for key in ("name", "icao", "iata", "elev_m"):
                if key in ov:
                    o[key] = ov[key]
        out.append(o)
    return out


# ------------------------------------------------------------------------------------------------- хранение
def save_cells(folder, cells, objs, cell_deg, osm_ok=True) -> int:
    """osm_ok=False -- OSM не ответил (паспорта, типы мостов): докачка посчитает клетку ещё раз."""
    os.makedirs(folder, exist_ok=True)
    n = 0
    for i, j in cells:
        mine = [o for o in objs if (int(o["lat"] // cell_deg), int(o["lon"] // cell_deg)) == (i, j)]
        tmp = store_path(folder, i, j) + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": STORE_VERSION, "built": time.time(), "osm_ok": bool(osm_ok), "airfields": mine}, fh,
                      ensure_ascii=False)
        os.replace(tmp, store_path(folder, i, j))
        n += len(mine)
    return n


def load_cell(folder, i, j) -> Optional[List[dict]]:
    try:
        with open(store_path(folder, i, j), encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    return apply_overrides(list(d.get("airfields", []))) if int(d.get("version", 0)) == STORE_VERSION else None


def list_all(folder, with_catalog: bool = False) -> List[dict]:
    """Все аэродромы скачанных карт (с правками); with_catalog -- плюс аэродромы справочника, которых в картах нет."""
    out = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for name in names:
        if name.startswith(f"airfields_v{STORE_VERSION}_") and name.endswith(".json"):
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as fh:
                    out += list(json.load(fh).get("airfields", []))
            except (OSError, ValueError):
                continue
    extra = apply_overrides(cache_only(folder, out))
    out = [o for o in apply_overrides(out) + extra if str(o.get("name") or "").strip()]   # безымянные -- не показываем
    return out + catalog_only(out) if with_catalog else out


CLUSTER_M = 3000.0      # полосы из кэша карт ближе этого друг к другу -- один аэродром


def cache_only(folder, have) -> List[dict]:
    """Аэродромы из кэша карт (ovt_air_v*: то, что рисует камера), для которых модель земли ещё не считалась: строка
    таблицы с полосами, рулёжками и перронами. Иначе облётанная полоса есть в камере, но нет в таблице. В таблицу
    (list_all) попадают только получившие название (справочник, airfields.json): безымянных полос в кэше -- сотни."""
    from geodata.airfields import AirfieldSet, OvertureAirfieldLoader
    pre = f"{OvertureAirfieldLoader.PREFIX}_v{OvertureAirfieldLoader.VERSION}_"
    s = AirfieldSet()                                                # без справочника: только данные карт
    try:
        names = sorted(n for n in os.listdir(folder) if n.startswith(pre) and n.endswith(".json"))
    except OSError:
        return []
    for name in names:
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                els = json.load(fh).get("elements")
            if isinstance(els, list):
                s.add_ways(els)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    groups = []                                                      # [[полосы]] -- по близости середин
    for rw in s.runways:
        if rw[0] != "runway":
            continue
        p = rw[2]
        mid = ((p[0][0] + p[-1][0]) / 2.0, (p[0][1] + p[-1][1]) / 2.0)
        for g in groups:
            if any(_near({"lat": mid[0], "lon": mid[1]}, {"lat": m[0], "lon": m[1]}, CLUSTER_M) for m, _r in g):
                g.append((mid, rw))
                break
        else:
            groups.append([(mid, rw)])
    out = []
    for g in groups:
        clat = sum(m[0] for m, _r in g) / len(g)
        clon = sum(m[1] for m, _r in g) / len(g)
        c = {"lat": clat, "lon": clon}
        if any(_near(c, o, CATALOG_NEAR_M) for o in have):
            continue
        la = [q[0] for _m, r in g for q in r[2]]
        lo = [q[1] for _m, r in g for q in r[2]]
        d = 0.006
        box = [min(la) - d, min(lo) - d * 1.6, max(la) + d, max(lo) + d * 1.6]
        inb = lambda pts: box[0] <= pts[len(pts) // 2][0] <= box[2] and box[1] <= pts[len(pts) // 2][1] <= box[3]
        rws = []
        for _m, (kind, w, p, ref) in g:
            hd = _heading(p)
            rws.append({"ref": ref, "ends": list(runway_ends(ref, hd)), "width": round(float(w), 1),
                        "length": round(_length(p), 0), "heading": round(hd, 1), "pts": [[round(a, 7), round(b, 7)] for a, b in p]})
        txs = [{"width": round(float(w), 1), "pts": [[round(a, 7), round(b, 7)] for a, b in p]}
               for kind, w, p, _r in s.runways if kind != "runway" and inb(p)]
        aps = [[[round(a, 7), round(b, 7)] for a, b in a_] for a_ in s.aprons if inb(a_)]
        out.append({"id": "a%.4f_%.4f" % (round(clat, 4), round(clon, 4)), "name": "", "icao": "", "iata": "",
                    "elev_m": None, "elev_osm": None, "lat": round(clat, 7), "lon": round(clon, 7), "field": [],
                    "runways": rws, "taxiways": txs, "aprons": aps, "bbox": box,
                    "main_runway": max(rws, key=lambda r: r["length"])["ref"]})
    return out


# ------------------------------------------------------------------------------------------------- геометрия справочника
# Запись аэродрома в справочнике (CATALOG или своя -- airfields.json) может хранить, чем его рисовать:
#   "runways":  [{"ref": "15/33", "width": 56, "ends": [[шир, долг], [шир, долг]]}]      -- торцы полосы
#   "taxiways": [{"ref": "A", "width": 18, "pts": [[шир, долг], [шир, долг], ...]}]     -- осевые рулёжек
# Тогда полосы и рулёжки берутся отсюда -- в камере, рельефе (стол поля), таблице и схеме, -- даже если в скачанных
# данных их нет; данные того же вида на этом аэродроме не рисуются. Своя запись главнее записи CATALOG.
#   "pavements": [[[шир, долг], ...], ...]                                             -- площадки бетона (контуры)
# Геометрия встроенного справочника -- в нём самом: geodata/airfields.dat (формат X-Plane apt.dat). Аэродром,
# у которого в airfields.json есть своя поправка полосы (облётан), геометрию справочника не получает -- остаётся как был.
_GEO = {}


def catalog_geometry() -> dict:
    """Геометрия справочника (airfields.dat): {ключ: {"runways", "pavements"}} -- разбирается при первом обращении."""
    if "v" not in _GEO:
        from geodata.xplane_apt import _parse
        g = {}
        for key, (head, lines) in _CATALOG_RAW.items():
            try:
                ap = _parse({"icao": key, "name": head["name"], "elev_ft": head["elev_ft"]}, lines)
            except Exception:
                continue
            g[key] = {"runways": ap["runways"], "pavements": [p["ring"] for p in ap["pavements"]]}
        _GEO["v"] = g
    return _GEO["v"]


def _with_geo(c):
    """Запись справочника + её геометрия."""
    g = catalog_geometry().get(str(c.get("icao") or c.get("geo") or "").upper())
    return dict(g, **c) if g else c


def entry_geometry(e) -> list:
    """Запись справочника -> [(вид, ширина, точки, обозначение)]; вид "pavement" -- площадка (контур); кривые
    записи пропускаются."""
    out = []
    for ring in e.get("pavements") or []:
        try:
            pts = tuple((float(q[0]), float(q[1])) for q in ring)
        except (TypeError, ValueError, IndexError):
            continue
        if len(pts) >= 4:
            out.append(("pavement", 0.0, pts, ""))
    for kind, key, pk in (("runway", "runways", "ends"), ("taxiway", "taxiways", "pts")):
        for r in e.get(key) or []:
            try:
                pts = tuple((float(q[0]), float(q[1])) for q in r[pk])
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            if len(pts) >= 2:
                out.append((kind, float(r.get("width") or 0), pts, str(r.get("ref", ""))))
    return out


def _near(a, b, dist_m) -> bool:
    try:
        la, lo = float(a["lat"]), float(a["lon"])
        return math.hypot((float(b["lat"]) - la) * 110540.0,
                          (float(b["lon"]) - lo) * 111320.0 * math.cos(math.radians(la))) <= dist_m
    except (KeyError, TypeError, ValueError):
        return False


def reference_geometry() -> list:
    """Все аэродромы справочника с геометрией: [[(вид, ширина, точки, обозначение)], ...] -- по аэродрому."""
    out, own = [], []
    for ov in load_overrides():
        g = entry_geometry(ov)
        if g:
            out.append(g)
            own.append(ov)
    flown = [ov for ov in load_overrides() if ov.get("runway_fixes")]
    for c in CATALOG:
        g = entry_geometry(_with_geo(c))
        if g and not any(_near(c, ov, CATALOG_NEAR_M) for ov in own + flown if "lat" in ov):
            out.append(g)
    return out


def _geometry_source(o, ov):
    """Откуда геометрия строки таблицы: своя запись, иначе запись CATALOG рядом; None -- из данных."""
    if ov and entry_geometry(ov):
        return ov
    if ov and ov.get("runway_fixes"):                               # своя поправка полосы (облётан) -- данные карты, не справочник
        return None
    for c in CATALOG:
        if _near(o, c, CATALOG_NEAR_M):
            c = _with_geo(c)
            if entry_geometry(c):
                return c
    return None


def apply_geometry(o, src) -> dict:
    """Полосы и рулёжки строки -- из справочника (src): как в build_objects, с пометкой ref_geom."""
    rws, txs, aps = [], [], []
    for kind, w, p, ref in entry_geometry(src):
        if kind == "pavement":
            aps.append([list(q) for q in p])
        elif kind == "runway":
            hd = _heading(p)
            rws.append({"ref": ref, "ends": list(runway_ends(ref, hd)), "width": round(w or float(src.get("width") or 0), 1),
                        "length": round(_length(p), 0), "heading": round(hd, 1), "pts": [list(q) for q in p]})
        else:
            txs.append({"ref": ref, "width": round(w, 1), "pts": [list(q) for q in p]})
    if rws:
        o["runways"] = rws
    if txs or aps:
        o["taxiways"] = txs
    if aps:
        o["aprons"] = aps
    o["ref_geom"] = True
    if o.get("catalog"):                                             # строка только из справочника: рамка по геометрии
        la = [q[0] for r in rws + txs for q in r["pts"]] + [q[0] for a in aps for q in a]
        lo = [q[1] for r in rws + txs for q in r["pts"]] + [q[1] for a in aps for q in a]
        if la:
            o["bbox"] = [min(la), min(lo), max(la), max(lo)]
    return o


def geometry_entry(o) -> dict:
    """Строка таблицы -> поля записи справочника: полосы (торцы) и рулёжки -- такие, какие сейчас нарисованы."""
    rws = [{"ref": r.get("ref") or "/".join(r.get("ends", [])), "width": r.get("width", 0),
            "ends": [list(r["pts"][0]), list(r["pts"][-1])]} for r in o.get("runways", []) if len(r.get("pts", [])) >= 2]
    txs = [{"ref": t.get("ref", ""), "width": t.get("width", 0), "pts": [list(q) for q in t["pts"]]}
           for t in o.get("taxiways", []) if len(t.get("pts", [])) >= 2]
    aps = [[list(q) for q in a] for a in o.get("aprons", []) if len(a) >= 4]
    return {"runways": rws, "taxiways": txs, "pavements": aps}


def catalog_only(have) -> List[dict]:
    """Аэродромы справочника, которых нет в скачанных картах: строка в таблице (название, коды, справочная полоса,
    место). Схемы и полос на местности у них нет -- пока район не скачан. Пометка catalog=True."""
    out = []
    for c in CATALOG:
        kx = 111320.0 * math.cos(math.radians(c["lat"]))
        if any(math.hypot((float(o["lat"]) - c["lat"]) * 110540.0, (float(o["lon"]) - c["lon"]) * kx) <= CATALOG_NEAR_M
               for o in have):
            continue
        ref = c.get("rwy", "")
        ends = [x for x in ref.split("/")] if "/" in ref else ["", ""]
        d = 0.015
        o = {"id": "c" + (c["icao"] or c.get("geo", "")), "name": c["name"], "icao": c["icao"], "iata": c.get("iata", ""),
             "elev_m": float(c.get("elev_m", 0)), "elev_osm": None, "lat": c["lat"], "lon": c["lon"], "catalog": True,
             "runways": [{"ref": ref, "ends": ends, "length": float(c.get("length", 0)), "width": float(c.get("width", 0)),
                          "heading": (int(ends[0]) * 10.0 if ends[0].isdigit() else 0.0), "pts": []}],
             "taxiways": [], "aprons": [], "field": [], "bbox": [c["lat"] - d, c["lon"] - d * 1.6, c["lat"] + d, c["lon"] + d * 1.6]}
        ov = match_override(o, load_overrides())
        if ov:
            for key in ("name", "icao", "iata", "elev_m"):
                if key in ov:
                    o[key] = ov[key]
        src = _geometry_source(o, ov)
        if src:
            apply_geometry(o, src)
        out.append(o)
    return out


CATALOG[:], _CATALOG_RAW = load_catalog()
