"""Модель земли -- часть КАРТЫ: считается один раз на вкладке «Карта» и лежит на диске; камера и проверка удара только читают.

Что в ней (всё, что раньше камера и программа пересчитывали в каждом полёте, а над Киевом -- каждые 15-30 с):
  * поправка застройки (UrbanGround): крыши в SRTM не считать землёй -- сетки поправки по плиткам 0.05 градуса;
  * ровное лётное поле (AirfieldGround): контур и плоскость каждого аэродрома;
  * продольные профили дорог (с рулёжками) и путей (RoadGround, RailGround): точки профиля, полуширина, откос;
  * уровень воды (WaterGround): профили рек и уровень каждого водоёма (по ключу контура).
Порядок тот же, что в полёте: SRTM -> застройка -> лётное поле -> профили дорог, путей, воды. Геометрия -- уже совмещённая
со снимком (geodata.align), поэтому модель считается ПОСЛЕ совмещения и устаревает, если совмещение пересчитали.

Файлы -- по клеткам 0.25 градуса, как кэш ЛЭП и ориентиров: ground_v1_<i>_<j>.npz. В клетку кладутся точки профилей,
лежащие в ней с запасом (профиль дороги считается по всей линии, поэтому у соседних клеток на стыке одни и те же
значения). Нет файла клетки -- камера и программа считают как раньше (в полёте)."""
from __future__ import annotations

import math
import os
import time
from typing import Dict, List, Optional, Sequence, Tuple

from .common import CELL_DEG, cell_bbox
from .ground_fixes import (AirfieldGround, RailGround, RoadGround, UrbanGround, WaterGround, URBAN_TILE_DEG,
                           RAIL_CORE_M, RAIL_CORRIDOR_M)

GROUND_VERSION = 2                # 2: + исправленный рельеф сеткой (DEM): SRTM -> застройка -> лётное поле
GROUND_VERSION = 3                # 3: лётное поле -- стол целиком (без «настоящих холмов» внутри контура)
GROUND_VERSION = 4                # 4: + мосты (geodata.bridges): настил, опоры, пилоны, ванты, арки
DEM_STEP_DEG = 1.0 / 3600.0       # шаг сетки -- 1 угловая секунда, как у самого SRTM (~30 м)
MARGIN_PROFILE_M = 400.0         # точки профилей дорог и путей -- в клетку с таким запасом
MARGIN_RIVER_M = 2500.0          # профили рек -- с большим (уровень ищется у ближайшей точки реки до ~2 км)


def poly_key(outer) -> str:
    """Ключ водоёма по геометрии (как geo_ids): первая, средняя, последняя точка и число точек."""
    a, m, b = outer[0], outer[len(outer) // 2], outer[-1]
    return f"{a[0]:.6f},{a[1]:.6f}:{m[0]:.6f},{m[1]:.6f}:{b[0]:.6f},{b[1]:.6f}:{len(outer)}"


def ground_path(folder: str, i: int, j: int) -> str:
    return os.path.join(folder, f"ground_v{GROUND_VERSION}_{i}_{j}.npz")


def _in_box(np, lat, lon, box, margin_m):
    s, w, n_, e = box
    dla = margin_m / 110540.0
    dlo = margin_m / (111320.0 * math.cos(math.radians((s + n_) / 2)))
    return (lat >= s - dla) & (lat <= n_ + dla) & (lon >= w - dlo) & (lon <= e + dlo)


# ------------------------------------------------------------------------------------------------- расчёт (вкладка «Карта»)
def build_ground(np, cells, raw_fn, bld_cells, roads, rails, waters, rivers, runways, aprons, folder, align_mtime=None,
                 progress=None, cancel=None, bridges=(), bridge_names=(), bridge_hints=(), airfield_info=(), osm_failed=(), align=None):
    """Модель земли района и запись по клеткам. raw_fn(lat, lon) -> SRTM (массивы). Возвращает число записанных клеток.
    Шаги: застройка -> лётное поле -> профили дорог (с рулёжками), путей, воды; затем раскладка по клеткам."""
    say = progress or (lambda *_a: None)
    stop = cancel or (lambda: False)
    say("застройка")
    urban = UrbanGround(bld_cells, raw_fn)
    g_urb = lambda la, lo: urban.apply(la, lo, raw_fn(la, lo))
    if stop():
        return 0
    say("льотне поле")
    afg = AirfieldGround(runways, aprons, g_urb)
    g_af = (lambda la, lo: afg.apply(la, lo, g_urb(la, lo))) if len(afg) else g_urb
    from geodata.airfield_objs import build_objects, load_overrides, save_cells as save_airfields
    try:                                                             # аэродромы -- сущностями (полосы, рулёжки, перроны, паспорт)
        af_objs = build_objects(np, runways, aprons, afg, list(airfield_info), load_overrides())
    except Exception:
        af_objs = []
    if stop():
        return 0
    say("профіль доріг")
    taxi = [("taxiway", float(w), p) for k, w, p, *_r in runways if k == "taxiway" and len(p) >= 2]
    road = RoadGround(list(roads) + taxi, g_af)
    if stop():
        return 0
    say("профіль колій")
    rail = RailGround([list(zip(la.tolist(), lo.tolist())) for la, lo in rails], g_af)
    if stop():
        return 0
    say("рівень води")
    water = WaterGround(list(waters), list(rivers), g_af)
    keys = [poly_key(o) for o, _h in water.waters]
    pieces, structures = [], []
    if bridges:                                                      # мосты -- по готовой земле с профилями дорог и путей
        say("мости")
        from geodata.bridges import build_models
        g_line = lambda la, lo: rail.apply(la, lo, road.apply(la, lo, g_af(la, lo)))
        pieces, structures = build_models(np, list(bridges), g_line, named=list(bridge_names), hints=list(bridge_hints))
    os.makedirs(folder, exist_ok=True)
    written = 0
    for i, j in cells:
        if stop():
            break
        box = cell_bbox(i, j)
        out = {"version": np.array(GROUND_VERSION), "align_mtime": np.array(float(align_mtime or 0.0)),
               "align_sig": np.array(align.cell_sig(*box) if align is not None else ""),   # совмещение ЭТОЙ клетки
               "built": np.array(time.time())}
        for name, pg, margin in (("road", road, MARGIN_PROFILE_M), ("rail", rail, MARGIN_PROFILE_M)):
            m = _in_box(np, pg.lat, pg.lon, box, margin) if len(pg.h) else np.zeros(0, bool)
            for f in ("lat", "lon", "h", "core", "corr"):
                out[f"{name}_{f}"] = getattr(pg, f)[m] if len(pg.h) else np.zeros(0)
        m = _in_box(np, water.lat, water.lon, box, MARGIN_RIVER_M) if len(water.h) else np.zeros(0, bool)
        for f in ("lat", "lon", "h"):
            out[f"river_{f}"] = getattr(water, f)[m] if len(water.h) else np.zeros(0)
        wk, wl, wf = [], [], []
        for k, (o, _h), lv, fb in zip(keys, water.waters, water.poly_level, water.poly_fallback):
            a = np.asarray(o, float)
            if not _in_box(np, a[:, 0], a[:, 1], box, 0.0).any():
                continue
            wk.append(k)
            wl.append(np.nan if lv is None else lv)
            wf.append(np.nan if fb is None else fb)
        out["water_key"] = np.array(wk, dtype=object)
        out["water_level"] = np.array(wl, float)
        out["water_fallback"] = np.array(wf, float)
        uk = []
        for (ti, tj), (a0, b0, slat, slon, corr) in urban.tiles.items():
            if box[0] <= (ti + 0.5) * URBAN_TILE_DEG < box[2] and box[1] <= (tj + 0.5) * URBAN_TILE_DEG < box[3]:
                uk.append((ti, tj))
                out[f"urban_{ti}_{tj}"] = corr
                out[f"urbanmeta_{ti}_{tj}"] = np.array([a0, b0, slat, slon])
        out["urban_keys"] = np.array(uk, dtype=np.int64).reshape(-1, 2)
        fields = []
        for hull, c, coef, fbox in getattr(afg, "fields", []):
            hl = np.stack([hull[:, 1] / 110540.0, hull[:, 0] / afg.kx], -1)          # контур -> (lat, lon)
            if not _in_box(np, hl[:, 0], hl[:, 1], box, 300.0).any():
                continue
            fields.append((hl, (c[1] / 110540.0, c[0] / afg.kx), coef))
        out["af_n"] = np.array(len(fields))
        for n, (hl, cll, coef) in enumerate(fields):
            out[f"af_hull_{n}"] = hl
            out[f"af_c_{n}"] = np.array(cll)
            out[f"af_coef_{n}"] = np.asarray(coef, float)
        # исправленный рельеф клетки: одна сетка для всех точек (над полем, в городе, в поле -- одинаково)
        n_ = int(round(CELL_DEG / DEM_STEP_DEG)) + 1
        la_ = box[0] + np.arange(n_) * DEM_STEP_DEG
        lo_ = box[1] + np.arange(n_) * DEM_STEP_DEG
        LA, LO = np.meshgrid(la_, lo_, indexing="ij")
        dem = np.empty((n_, n_), np.float32)
        for r0 in range(0, n_, 64):                                  # кусками: память и проверка отмены
            dem[r0:r0 + 64] = np.asarray(g_af(LA[r0:r0 + 64].ravel(), LO[r0:r0 + 64].ravel()), float).reshape(-1, n_)
        out["dem"] = dem
        out["dem_meta"] = np.array([box[0], box[1], DEM_STEP_DEG])
        from geodata.bridges import save_cells                       # мосты -- своими файлами (geodata.bridges), мост целиком;
        save_cells(np, folder, [(i, j)], pieces, structures, CELL_DEG)
        save_airfields(folder, [(i, j)], af_objs, CELL_DEG, osm_ok=(i, j) not in set(osm_failed))   # пустой файл -- «мостов нет», а не «не считали»
        tmp = ground_path(folder, i, j) + ".part.npz"
        np.savez_compressed(tmp, **out)
        os.replace(tmp, ground_path(folder, i, j))
        written += 1
        say(f"клітинка {written} з {len(cells)}")
    return written


# ------------------------------------------------------------------------------------------------- чтение (камера, программа)
class GroundModel:
    """Модель земли из файлов карты. load(cells) -> готовые поправки (те же классы, что считались в полёте):
    urban, airfield, road, rail -- и water_for(waters) -> WaterGround для текущего списка водоёмов."""

    def __init__(self, folder: Optional[str], align_mtime: Optional[float] = None, align=None):
        self.folder = folder
        self.align_mtime = align_mtime
        self.align = align                                           # AlignMap: свежесть клетки -- по подписи её совмещения
        self.cells: set = set()
        self.urban = self.airfield = self.road = self.rail = None
        self.dems: Dict[Tuple[int, int], tuple] = {}                 # клетка -> ((lat0, lon0, шаг), сетка высот)
        self._river = None
        self._wlevels: Dict[str, Tuple[float, float]] = {}
        self.bridges = ([], [])                                      # (куски, конструкции) -- geodata.bridges
        self.bridge_objs: list = []
        self.stadiums: list = []

    def has(self, i: int, j: int) -> bool:
        """Есть ли свежий файл клетки (не старше совмещения со снимком)."""
        if not self.folder:
            return False
        p = ground_path(self.folder, i, j)
        if not os.path.isfile(p):
            return False
        if self.align is not None:                                   # подпись совмещения клетки: устарела, только если
            try:                                                     # совмещение изменилось в самой клетке (и рядом)
                import numpy as _np
                with _np.load(p, allow_pickle=False) as z:
                    sig = str(z["align_sig"]) if "align_sig" in z.files else None
            except Exception:
                sig = None
            if sig:
                return sig == self.align.cell_sig(*cell_bbox(i, j))
        if self.align_mtime:                                         # файл старого образца -- по дате совмещения
            try:
                return os.path.getmtime(p) >= float(self.align_mtime) - 1.0
            except OSError:
                return False
        return True

    def load(self, cells: Sequence[Tuple[int, int]]) -> int:
        """Прочитать клетки (только те, что есть). Возвращает, сколько клеток в модели."""
        import numpy as np
        prof = {"road": [], "rail": []}
        river = []
        afields = []
        utiles = {}
        wl = {}
        dems = {}
        got = set()
        bpieces, bstruct, bobjs = [], [], []
        from geodata.bridges import from_json, load_cell
        for i, j in cells:
            p = ground_path(self.folder, i, j) if self.folder else None
            if not p or not os.path.isfile(p):
                continue
            try:
                with np.load(p, allow_pickle=True) as z:
                    if int(z["version"]) != GROUND_VERSION:
                        continue
                    for name in prof:
                        prof[name].append(tuple(np.asarray(z[f"{name}_{f}"], float) for f in ("lat", "lon", "h", "core", "corr")))
                    river.append(tuple(np.asarray(z[f"river_{f}"], float) for f in ("lat", "lon", "h")))
                    for k, lv, fb in zip(z["water_key"].tolist(), z["water_level"].tolist(), z["water_fallback"].tolist()):
                        wl[str(k)] = (None if lv != lv else float(lv), None if fb != fb else float(fb))
                    for ti, tj in np.asarray(z["urban_keys"]).reshape(-1, 2).tolist():
                        a0, b0, slat, slon = np.asarray(z[f"urbanmeta_{ti}_{tj}"], float).tolist()
                        utiles[(ti, tj)] = (a0, b0, slat, slon, np.asarray(z[f"urban_{ti}_{tj}"], np.float32))
                    st_ = load_cell(np, self.folder, i, j)
                    if st_ is not None:                              # мосты -- из своих файлов
                        bpieces += st_[0]
                        bstruct += st_[1]
                        bobjs += st_[2]
                    elif "bridges_json" in z.files:                   # модель старого образца: мосты внутри неё
                        bp, bs = from_json(np, str(z["bridges_json"]))
                        bpieces += bp
                        bstruct += bs
                    if "dem" in z.files:
                        dems[(i, j)] = (tuple(np.asarray(z["dem_meta"], float).tolist()), np.asarray(z["dem"], np.float32))
                    for n in range(int(z["af_n"])):
                        afields.append((np.asarray(z[f"af_hull_{n}"], float), tuple(np.asarray(z[f"af_c_{n}"], float).tolist()),
                                        np.asarray(z[f"af_coef_{n}"], float)))
                got.add((i, j))
            except Exception:
                continue
        self.cells = got
        self.dems = dems
        try:                                                         # настил ровной линией -- и у посчитанных раньше мостов
            from geodata.bridges import smooth_deck                  # (без пересчёта модели); мосты из справочника --
            withst = {st.get("bridge") for st in bstruct}            # как посчитаны (у них свой пролёт и просвет)
            grp = {}
            for p in bpieces:
                if p.get("bridge") not in withst and int(p.get("layer", 1) or 1) <= 1:
                    grp.setdefault(p.get("bridge") or p["id"], []).append(p)
            for ps in grp.values():
                smooth_deck(np, ps)
        except Exception:
            pass
        self.bridges = (bpieces, bstruct)
        self.bridge_objs = bobjs                                     # сущности «мост» (geodata.bridges.objects)
        try:                                                         # стадионы (свои файлы, слой «Стадіони» на «Карте»)
            from geodata.stadiums import load_cell as _st_load
            self.stadiums = [o for i, j in cells for o in (_st_load(self.folder, i, j) or [])]
        except Exception:
            self.stadiums = []
        u = UrbanGround([], None)
        u.tiles = utiles
        self.urban = u
        self.road = _profile_from_arrays(np, RoadGround, prof["road"])
        self.rail = _profile_from_arrays(np, RailGround, prof["rail"])
        self.airfield = _airfield_from_fields(np, afields)
        cat = lambda k: np.concatenate([r[k] for r in river]) if river else np.zeros(0)
        self._river = (cat(0), cat(1), cat(2))
        self._wlevels = wl
        return len(got)

    def dem_at(self, lat: float, lon: float) -> Optional[float]:
        """Исправленная высота земли в точке (SRTM -> застройка -> лётное поле) из сетки карты; нет клетки -- None.
        Одна и та же простая операция для любой точки: четыре соседних узла и билинейная интерполяция, без numpy-массивов."""
        d = self.dems.get((int(lat // CELL_DEG), int(lon // CELL_DEG)))
        if d is None:
            return None
        (a0, b0, st), g = d
        fi, fj = (lat - a0) / st, (lon - b0) / st
        i0, j0 = int(fi), int(fj)
        if i0 < 0 or j0 < 0 or i0 >= g.shape[0] - 1 or j0 >= g.shape[1] - 1:
            return None
        di, dj = fi - i0, fj - j0
        return float(g[i0, j0] * (1 - di) * (1 - dj) + g[i0 + 1, j0] * di * (1 - dj)
                     + g[i0, j0 + 1] * (1 - di) * dj + g[i0 + 1, j0 + 1] * di * dj)

    def dem(self, lat, lon):
        """То же для массивов; точки вне клеток модели -- NaN."""
        import numpy as np
        la, lo = np.atleast_1d(np.asarray(lat, float)), np.atleast_1d(np.asarray(lon, float))
        out = np.full(la.shape, np.nan)
        ci, cj = np.floor(la / CELL_DEG).astype(int), np.floor(lo / CELL_DEG).astype(int)
        for key in set(zip(ci.tolist(), cj.tolist())):
            d = self.dems.get(key)
            if d is None:
                continue
            (a0, b0, st), g = d
            q = np.nonzero((ci == key[0]) & (cj == key[1]))[0]
            fi = np.clip((la[q] - a0) / st, 0, g.shape[0] - 1.001)
            fj = np.clip((lo[q] - b0) / st, 0, g.shape[1] - 1.001)
            i0, j0 = fi.astype(int), fj.astype(int)
            di, dj = fi - i0, fj - j0
            out[q] = (g[i0, j0] * (1 - di) * (1 - dj) + g[i0 + 1, j0] * di * (1 - dj)
                      + g[i0, j0 + 1] * (1 - di) * dj + g[i0 + 1, j0 + 1] * di * dj)
        return out

    def water_for(self, waters):
        """WaterGround для текущего списка водоёмов: уровни -- по ключам контуров из карты; неизвестный водоём -- None
        (камера возьмёт уровень ближайшей реки или куска, как без карты)."""
        import numpy as np
        wg = WaterGround.__new__(WaterGround)
        wg.waters = waters
        la, lo, h = self._river if self._river else (np.zeros(0), np.zeros(0), np.zeros(0))
        wg.lat, wg.lon, wg.h = la, lo, h
        wg.grid = {}
        for i, (a, b) in enumerate(zip(la.tolist(), lo.tolist())):
            wg.grid.setdefault((int(a // wg.CELL), int(b // wg.CELL)), []).append(i)
        lv = [self._wlevels.get(poly_key(o), (None, None)) for o, _h in waters]
        wg.poly_level = [a for a, _b in lv]
        wg.poly_fallback = [b for _a, b in lv]
        return wg


def _profile_from_arrays(np, cls, parts):
    """RoadGround/RailGround из готовых точек профиля (без пересчёта и без повторного усреднения)."""
    obj = cls.__new__(cls)
    if parts:
        obj.lat, obj.lon, obj.h, obj.core, obj.corr = (np.concatenate([p[k] for p in parts]) for k in range(5))
        key = np.round(obj.lat * 1e6).astype(np.int64) * 1_000_000_000 + np.round(obj.lon * 1e6).astype(np.int64)
        _u, first = np.unique(key, return_index=True)              # точки на стыке клеток пришли дважды
        first.sort()
        obj.lat, obj.lon, obj.h, obj.core, obj.corr = (a[first] for a in (obj.lat, obj.lon, obj.h, obj.core, obj.corr))
    else:
        obj.lat = obj.lon = obj.h = obj.core = obj.corr = np.zeros(0)
    obj.grid = {}
    for i, (a, b) in enumerate(zip(obj.lat.tolist(), obj.lon.tolist())):
        obj.grid.setdefault((int(a // obj.CELL), int(b // obj.CELL)), []).append(i)
    return obj


def _airfield_from_fields(np, fields):
    """AirfieldGround из сохранённых полей: контур (lat, lon), центр (lat, lon), плоскость (м)."""
    af = AirfieldGround.__new__(AirfieldGround)
    af.fields = []
    if not fields:
        af.kx = 111320.0
        return af
    lat0 = float(np.mean([c[0] for _h, c, _k in fields]))
    af.kx = 111320.0 * math.cos(math.radians(lat0))
    from .ground_fixes import AF_BLEND_M
    seen = set()
    for hl, (cla, clo), coef in fields:
        key = (round(cla, 6), round(clo, 6))
        if key in seen:                                              # поле на стыке клеток -- одно
            continue
        seen.add(key)
        hull = np.stack([hl[:, 1] * af.kx, hl[:, 0] * 110540.0], -1)
        c = np.array([clo * af.kx, cla * 110540.0])
        box = (hull[:, 0].min() - AF_BLEND_M, hull[:, 1].min() - AF_BLEND_M, hull[:, 0].max() + AF_BLEND_M, hull[:, 1].max() + AF_BLEND_M)
        af.fields.append((hull, c, np.asarray(coef, float), box))
    return af
