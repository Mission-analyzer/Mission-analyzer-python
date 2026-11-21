"""Поправки рельефа: ровный продольный профиль путей, земля в застройке.

Часть пакета geodata (бывший obstacles.py); старое имя `import obstacles` продолжает работать."""
from __future__ import annotations
import math
from typing import (Dict, List, Tuple)


# ---------------------------------------------------------------------------------------------------------------------
# ПРОДОЛЬНЫЙ ПРОФИЛЬ ПУТИ: SRTM (поверхность с шагом 30 м: горбы от зданий, деревьев, насыпей) делал полотно «волнистым».
# Настоящая магистраль почти горизонтальна (уклон до 10-15 промилле), неровности земли снимают насыпи и выемки. Профиль:
# скользящее среднее рельефа на RAIL_SMOOTH_M, уклон не больше RAIL_MAX_GRADE, не дальше RAIL_CUTFILL_M от рельефа.
# Рельеф у пути, опоры и провода контактной сети берут высоту отсюда (RailGround) -- путь, опоры и провода на одной линии.
# ---------------------------------------------------------------------------------------------------------------------
RAIL_MAX_GRADE = 0.015


RAIL_SMOOTH_M = 400.0


RAIL_CUTFILL_M = 10.0


RAIL_PROFILE_STEP_M = 10.0


RAIL_CORE_M = 6.0                # до этого расстояния от оси -- ровно по профилю (полотно, опоры)


RAIL_CORRIDOR_M = 25.0           # дальше -- плавный переход к рельефу (откосы насыпи или выемки)


RAIL_BLEND_M = 25.0              # высоты соседних путей усредняются в этом радиусе (одно полотно -- одна высота)


def rail_profile(pts, ground_fn, step: float = RAIL_PROFILE_STEP_M, smooth: float = RAIL_SMOOTH_M,
                 grade: float = RAIL_MAX_GRADE, cutfill: float = RAIL_CUTFILL_M, median: float = 0.0):
    """(широты, долготы, высоты профиля) пути (или дороги -- со своими smooth/grade/cutfill) с шагом step.
    median > 0: сначала скользящая медиана на median м -- короткие горбы SRTM (лесополоса, дом у трассы) срезаются целиком,
    а не размазываются средним."""
    import numpy as np
    la = np.array([p[0] for p in pts])
    lo = np.array([p[1] for p in pts])
    kx = 111320.0 * math.cos(math.radians(float(la.mean())))
    cum = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(lo) * kx, np.diff(la) * 110540.0))])
    s_ = np.linspace(0.0, float(cum[-1]), max(2, int(math.ceil(cum[-1] / step)) + 1))
    lat, lon = np.interp(s_, cum, la), np.interp(s_, cum, lo)
    g = np.asarray(ground_fn(lat, lon), dtype=float)
    src = g
    km = int(median / step / 2) if median > 0 else 0
    if km >= 1 and len(g) > 2 * km:
        src = np.median(np.lib.stride_tricks.sliding_window_view(np.pad(g, km, mode="edge"), 2 * km + 1), axis=1)
    k = max(1, int(smooth / step / 2))
    p = np.convolve(np.pad(src, k, mode="edge"), np.ones(2 * k + 1) / (2 * k + 1), mode="valid")
    p = np.clip(p, g - cutfill, g + cutfill)                     # насыпь и выемка -- в разумных пределах...
    dmax = grade * (s_[1] - s_[0]) if len(s_) > 1 else 0.0
    for i in range(1, len(p)):                                   # ...но уклон важнее: не круче grade (вперёд и назад)
        p[i] = min(max(p[i], p[i - 1] - dmax), p[i - 1] + dmax)
    for i in range(len(p) - 2, -1, -1):
        p[i] = min(max(p[i], p[i + 1] - dmax), p[i + 1] + dmax)
    return lat, lon, p


class RailGround:
    """Высота «земли» у путей -- по их профилям: ближе RAIL_CORE_M к оси -- профиль, до RAIL_CORRIDOR_M -- плавно к рельефу.
    apply(lat, lon, g) возвращает поправленные высоты (массивы numpy). Поиск ближайшей точки профиля -- по хэш-сетке."""
    CELL = 0.0004                                                # ~30-45 м

    BLEND_M = RAIL_BLEND_M

    def __init__(self, rails_pts, ground_fn):
        self._build([(pts, rail_profile, RAIL_CORE_M, RAIL_CORRIDOR_M) for pts in rails_pts], ground_fn)

    def _build(self, lines, ground_fn):
        """lines: [(точки, функция профиля, «ровно» до м, переход до м)]."""
        import numpy as np
        la_, lo_, h_, c_, r_ = [], [], [], [], []
        for pts, prof, core, corr in lines:
            if len(pts) >= 2:
                la, lo, p = prof(pts, ground_fn)
                la_.append(la); lo_.append(lo); h_.append(p)
                c_.append(np.full(len(p), float(core))); r_.append(np.full(len(p), float(corr)))
        cat = lambda a: np.concatenate(a) if a else np.zeros(0)
        self.lat, self.lon, self.h, self.core, self.corr = cat(la_), cat(lo_), cat(h_), cat(c_), cat(r_)
        self.grid: Dict[Tuple[int, int], List[int]] = {}
        for i, (a, b) in enumerate(zip(self.lat.tolist(), self.lon.tolist())):
            self.grid.setdefault((int(a // self.CELL), int(b // self.CELL)), []).append(i)
        if len(self.h):                                          # одно полотно -- одна высота: усреднить соседей всех путей
            self.h = self._blend(self.BLEND_M)

    def _blend(self, radius_m: float):
        """Высоты профилей, усреднённые по всем точкам путей в радиусе radius_m: параллельные пути одного полотна и стыки
        кусков линии в OSM получают общую высоту (иначе провод «перескакивал» на профиль соседнего пути -- галочка)."""
        import numpy as np
        out = self.h.copy()
        kx = 111320.0 * math.cos(math.radians(float(np.mean(self.lat))))
        r2 = radius_m * radius_m
        for (ca, cb), idx in self.grid.items():
            cand = np.asarray([i for da in (-1, 0, 1) for db in (-1, 0, 1) for i in self.grid.get((ca + da, cb + db), ())])
            q = np.asarray(idx)
            d2 = ((self.lat[q, None] - self.lat[cand][None, :]) * 110540.0) ** 2 + ((self.lon[q, None] - self.lon[cand][None, :]) * kx) ** 2
            w = (d2 <= r2).astype(float)
            out[q] = (w * self.h[cand][None, :]).sum(1) / np.maximum(w.sum(1), 1.0)
        return out

    def __len__(self):
        return len(self.h)

    def apply(self, lat, lon, g):
        """Поправить высоты у путей. Поиск -- ГРУППАМИ: точки из одной клетки сетки ищут ближайшую точку профиля одним
        действием numpy (цикл по точкам на десятках тысяч проводов контактной сети отнимал процесс видеокарты у кадров)."""
        import numpy as np
        lat, lon, g = np.asarray(lat, dtype=float), np.asarray(lon, dtype=float), np.array(g, dtype=float)
        if not len(self.h):
            return g
        fl, fo, out = lat.ravel(), lon.ravel(), g.ravel()
        kx = 111320.0 * math.cos(math.radians(float(np.mean(fl))))
        ia = np.floor(fl / self.CELL).astype(np.int64)
        ib = np.floor(fo / self.CELL).astype(np.int64)
        keys = ia * 10_000_000 + ib
        order = np.argsort(keys, kind="stable")
        uk, start = np.unique(keys[order], return_index=True)
        ends = np.append(start[1:], len(order))
        for key, s0, s1 in zip(uk.tolist(), start.tolist(), ends.tolist()):
            ca, cb = key // 10_000_000, key % 10_000_000
            cand = [i for da in (-1, 0, 1) for db in (-1, 0, 1) for i in self.grid.get((ca + da, cb + db), ())]
            if not cand:
                continue
            q = order[s0:s1]
            c = np.asarray(cand)
            d2 = ((fl[q, None] - self.lat[c][None, :]) * 110540.0) ** 2 + ((fo[q, None] - self.lon[c][None, :]) * kx) ** 2
            j = np.argmin(d2, axis=1)
            d = np.sqrt(d2[np.arange(len(q)), j])
            k = c[j]
            w = np.clip(1.0 - (d - self.core[k]) / np.maximum(self.corr[k] - self.core[k], 1.0), 0.0, 1.0)
            out[q] = out[q] * (1.0 - w) + self.h[c[j]] * w
        return out.reshape(g.shape)


# ---------------------------------------------------------------------------------------------------------------------
# ПРОДОЛЬНЫЙ ПРОФИЛЬ ДОРОГ -- как у путей, но дорога круче и ближе к рельефу: SRTM (горбы от деревьев вдоль трассы, домов,
# лесополос) делал полотно волнистым. Профиль: среднее на ROAD_SMOOTH_M, уклон не круче ROAD_MAX_GRADE по классу, насыпь
# и выемка не больше ROAD_CUTFILL_M. Под полотном (полширины) -- ровно по профилю, дальше ROAD_SHOULDER_M -- плавно к рельефу.
# Главные и третьестепенные дороги со съездами (их и грузим: motorway..tertiary); мосты -- отдельно (настил), тоннели не грузятся.
# ---------------------------------------------------------------------------------------------------------------------
ROAD_SMOOTH_M = 200.0


ROAD_MEDIAN_M = 200.0


ROAD_MEDIAN_BY_CLASS = {"taxiway": 500.0}   # на лётном поле бугры шире (ангары, стоянки самолётов) -- окно больше            # горбы короче ~100 м (лесополоса, дома, мост через овраг в SRTM) срезаются медианой


ROAD_CUTFILL_M = 6.0


ROAD_PROFILE_STEP_M = 15.0


ROAD_SHOULDER_M = 20.0           # откос насыпи или выемки за краем полотна


ROAD_BLEND_M = 15.0              # стыки кусков и перекрёстки -- общая высота


ROAD_MAX_GRADE = {"motorway": 0.04, "trunk": 0.05, "primary": 0.06, "secondary": 0.07, "tertiary": 0.08,   # съезды (_link) -- 0.06
                  "taxiway": 0.015}                              # рулёжки: аэродром ровный, бугры SRTM (ангары, самолёты) -- срезать


class RoadGround(RailGround):
    """Высота земли у главных дорог по их профилям (см. RailGround). roads: [(класс, ширина м, точки)]."""
    BLEND_M = ROAD_BLEND_M
    CELL = 0.0006                                                # ~40-65 м: полоса автомагистрали с откосами целиком

    def __init__(self, roads, ground_fn):
        lines = []
        for cls, width, pts in roads:
            grade = ROAD_MAX_GRADE.get(cls, 0.06)
            md = ROAD_MEDIAN_BY_CLASS.get(cls, ROAD_MEDIAN_M)
            prof = (lambda p_, g_, gr=grade, md=md: rail_profile(p_, g_, ROAD_PROFILE_STEP_M, ROAD_SMOOTH_M, gr, ROAD_CUTFILL_M, md))
            half = max(float(width or 7.0) / 2.0, 3.0)
            lines.append((pts, prof, half, half + ROAD_SHOULDER_M))
        self._build(lines, ground_fn)


# =====================================================================================================================
# ЗЕМЛЯ В ЗАСТРОЙКЕ. SRTM -- радарная модель поверхности: в городе меряет смесь земли и крыш (ошибка глобальных моделей
# рельефа в городах -- 2-6 м, растёт с плотностью и высотой застройки). Дом ставился на SRTM и получал сверху ВСЮ свою
# высоту -- часть высоты считалась дважды (как было с лесом). Поправка: нижняя огибающая SRTM (минимум в окне ~270 м: на
# улицах, во дворах, в парках радар чаще видит землю), взвешенная плотностью застройки домов Microsoft (в редком частном
# секторе и в поле -- ноль: на склоне минимум окна ниже, хотя крыш нет) и не больше URBAN_MAX_CORR_M.
# Растры с шагом URBAN_STEP_M заранее, плитками URBAN_TILE_DEG, только там, где есть дома.
# =====================================================================================================================
URBAN_TILE_DEG = 0.05


URBAN_STEP_M = 30.0


URBAN_ENV_M = 135.0


URBAN_MAX_CORR_M = 6.0


URBAN_DENSITY_FULL = 0.25


URBAN_DENSITY_R_M = 150.0


URBAN_FILL = 0.75                # доля рамки дома, занятая самим домом (контуры не прямоугольники)


def _box_mean(np, a, r):
    """Среднее в квадратном окне (2r+1)^2 через суммы по префиксу (края -- по имеющимся клеткам)."""
    p = np.pad(a, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    H, W = a.shape
    i0 = np.clip(np.arange(H) - r, 0, H)
    i1 = np.clip(np.arange(H) + r + 1, 0, H)
    j0 = np.clip(np.arange(W) - r, 0, W)
    j1 = np.clip(np.arange(W) + r + 1, 0, W)
    s = p[i1][:, j1] - p[i0][:, j1] - p[i1][:, j0] + p[i0][:, j0]
    n = (i1 - i0)[:, None] * (j1 - j0)[None, :]
    return s / np.maximum(n, 1)


class UrbanGround:
    """apply(lat, lon, g) -> высоты земли с поправкой на застройку (массивы numpy). Пустой -- ничего не меняет."""

    def __init__(self, cells, ground_fn):
        import numpy as np
        la, lo, ar = [], [], []
        for cell in cells:
            if not len(cell):
                continue
            bb = np.asarray(cell.bbox, dtype=float)
            cla, clo = (bb[:, 0] + bb[:, 2]) / 2.0, (bb[:, 1] + bb[:, 3]) / 2.0
            kx = 111320.0 * np.cos(np.radians(cla))
            la.append(cla); lo.append(clo)
            ar.append((bb[:, 2] - bb[:, 0]) * 110540.0 * (bb[:, 3] - bb[:, 1]) * kx * URBAN_FILL)
        self.tiles: Dict[Tuple[int, int], tuple] = {}
        if not la:
            return
        la, lo, ar = np.concatenate(la), np.concatenate(lo), np.concatenate(ar)
        keys = set(zip(np.floor(la / URBAN_TILE_DEG).astype(int).tolist(), np.floor(lo / URBAN_TILE_DEG).astype(int).tolist()))
        for ti, tj in keys:
            self.tiles[(ti, tj)] = self._build(np, ti, tj, la, lo, ar, ground_fn)

    def __len__(self):
        return len(self.tiles)

    @staticmethod
    def _build(np, ti, tj, la, lo, ar, ground_fn):
        T = URBAN_TILE_DEG
        lat0, lon0 = ti * T, tj * T
        slat = URBAN_STEP_M / 110540.0
        slon = URBAN_STEP_M / (111320.0 * math.cos(math.radians(lat0 + T / 2)))
        m = int(math.ceil(max(URBAN_ENV_M, URBAN_DENSITY_R_M) / URBAN_STEP_M)) + 2
        nlat, nlon = int(math.ceil(T / slat)) + 2 * m + 1, int(math.ceil(T / slon)) + 2 * m + 1
        a0, b0 = lat0 - m * slat, lon0 - m * slon
        LA = a0 + np.arange(nlat)[:, None] * slat + np.zeros((1, nlon))
        LO = b0 + np.arange(nlon)[None, :] * slon + np.zeros((nlat, 1))
        G = np.asarray(ground_fn(LA.ravel(), LO.ravel()), dtype=float).reshape(nlat, nlon)
        k = int(round(URBAN_ENV_M / URBAN_STEP_M))
        P = np.pad(G, k, mode="edge")
        env = G.copy()
        for di in range(2 * k + 1):                              # минимум в окне: нижняя огибающая
            for dj in range(2 * k + 1):
                np.minimum(env, P[di:di + nlat, dj:dj + nlon], out=env)
        env = _box_mean(np, env, 1)
        dens = np.zeros((nlat, nlon))                            # площадь домов по клеткам -> доля застройки в радиусе
        ii = np.floor((la - a0) / slat).astype(int)
        jj = np.floor((lo - b0) / slon).astype(int)
        ok = (ii >= 0) & (ii < nlat) & (jj >= 0) & (jj < nlon)
        np.add.at(dens, (ii[ok], jj[ok]), ar[ok])
        dens = _box_mean(np, dens / (URBAN_STEP_M * URBAN_STEP_M), int(round(URBAN_DENSITY_R_M / URBAN_STEP_M)))
        w = np.clip(dens / URBAN_DENSITY_FULL, 0.0, 1.0)
        corr = (w * np.clip(G - env, 0.0, URBAN_MAX_CORR_M)).astype(np.float32)
        return a0, b0, slat, slon, corr

    def correction_at(self, lat: float, lon: float) -> float:
        """Одна точка -- без numpy (проверка удара зовёт её на каждом такте; массивы ради одной точки -- в разы дольше)."""
        t = self.tiles.get((math.floor(lat / URBAN_TILE_DEG), math.floor(lon / URBAN_TILE_DEG)))
        if t is None:
            return 0.0
        a0, b0, slat, slon, corr = t
        fi = min(max((lat - a0) / slat, 0.0), corr.shape[0] - 1.001)
        fj = min(max((lon - b0) / slon, 0.0), corr.shape[1] - 1.001)
        i0, j0 = int(fi), int(fj)
        di, dj = fi - i0, fj - j0
        c = corr
        return float(c[i0, j0] * (1 - di) * (1 - dj) + c[i0 + 1, j0] * di * (1 - dj)
                     + c[i0, j0 + 1] * (1 - di) * dj + c[i0 + 1, j0 + 1] * di * dj)

    def correction(self, lat, lon):
        import numpy as np
        lat, lon = np.asarray(lat, dtype=float), np.asarray(lon, dtype=float)
        out = np.zeros(lat.shape)
        if not self.tiles:
            return out
        fl, fo, fr = lat.ravel(), lon.ravel(), out.ravel()
        ti = np.floor(fl / URBAN_TILE_DEG).astype(int)
        tj = np.floor(fo / URBAN_TILE_DEG).astype(int)
        for key in set(zip(ti.tolist(), tj.tolist())):
            t = self.tiles.get(key)
            if t is None:
                continue
            a0, b0, slat, slon, corr = t
            q = np.nonzero((ti == key[0]) & (tj == key[1]))[0]
            fi = np.clip((fl[q] - a0) / slat, 0, corr.shape[0] - 1.001)
            fj = np.clip((fo[q] - b0) / slon, 0, corr.shape[1] - 1.001)
            i0, j0 = fi.astype(int), fj.astype(int)
            di, dj = fi - i0, fj - j0
            fr[q] = (corr[i0, j0] * (1 - di) * (1 - dj) + corr[i0 + 1, j0] * di * (1 - dj)
                     + corr[i0, j0 + 1] * (1 - di) * dj + corr[i0 + 1, j0 + 1] * di * dj)
        return out

    def apply(self, lat, lon, g):
        import numpy as np
        g = np.asarray(g, dtype=float)
        return g - self.correction(lat, lon) if self.tiles else g


# =====================================================================================================================
# УРОВЕНЬ ВОДЫ. Раньше уровень считался по каждому слою земли отдельно (процентиль SRTM под куском воды в слое), а река
# только «не выше уровня»: русло повторяло волны SRTM, озеро на стыке слоёв или кусков контура получало ступеньку.
# Теперь, как у путей: у реки -- продольный профиль (нижняя огибающая рельефа вдоль оси; вода течёт только вниз -- уровень
# от истока к устью не растёт, перепады растянуты не круче WATER_MAX_GRADE, не выше огибающей; соседние реки и куски одной
# реки -- общая высота), у водоёма -- ОДИН уровень на весь контур (не на кусок в слое).
# Водоём, через который течёт река: маленький -- уровень реки в нём, большой (русло Днепра контуром) -- профиль реки.
# =====================================================================================================================
WATER_PROFILE_STEP_M = 50.0


WATER_ENV_M = 1000.0             # окно нижней огибающей вдоль реки (SRTM над водой «рябой», берега и деревья -- выше)


WATER_ENV_PCT = 20.0


WATER_MAX_GRADE = 0.02           # перепад уровня растягивается не круче 20 м на км (без «ступенек» у плотин и порогов)


WATER_BLEND_M = 120.0            # слияния, протоки, куски одной реки -- общая высота


WATER_POLY_PCT = 15.0            # уровень водоёма -- по низким точкам рельефа под всем контуром


WATER_POLY_PROFILE_M = 2000.0    # водоём больше этого, через который течёт река, -- по профилю реки (у него есть уклон)


WATER_POLY_SAMPLES = 96          # сетка выборки рельефа под контуром (не больше N x N точек)


def water_profile(pts, ground_fn, step: float = WATER_PROFILE_STEP_M):
    """(широты, долготы, уровень) реки с шагом step: нижняя огибающая рельефа, только вниз по течению (направление --
    к более низкому концу: в данных оно не всегда по течению), перепады не круче WATER_MAX_GRADE, не выше огибающей."""
    import numpy as np
    la = np.array([p[0] for p in pts], dtype=float)
    lo = np.array([p[1] for p in pts], dtype=float)
    kx = 111320.0 * math.cos(math.radians(float(la.mean())))
    cum = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(lo) * kx, np.diff(la) * 110540.0))])
    s_ = np.linspace(0.0, float(cum[-1]), max(2, int(math.ceil(cum[-1] / step)) + 1))
    lat, lon = np.interp(s_, cum, la), np.interp(s_, cum, lo)
    g = np.asarray(ground_fn(lat, lon), dtype=float)
    k = max(1, int(WATER_ENV_M / step / 2))
    if len(g) > 2 * k:
        win = np.lib.stride_tricks.sliding_window_view(np.pad(g, k, mode="edge"), 2 * k + 1)
        p = np.percentile(win, WATER_ENV_PCT, axis=1)
    else:
        p = np.full(len(g), float(np.percentile(g, WATER_ENV_PCT)))
    rev = p[0] < p[-1]                                           # исток -- выше устья
    if rev:
        p = p[::-1]
    p = np.minimum.accumulate(p)                                 # вниз по течению уровень не растёт (без «волн» SRTM)
    dmax = WATER_MAX_GRADE * (s_[1] - s_[0]) if len(s_) > 1 else 0.0
    for i in range(len(p) - 2, -1, -1):                          # обрыв огибающей -- пологим спуском (выше по течению ниже)
        p[i] = min(p[i], p[i + 1] + dmax)
    if rev:
        p = p[::-1]
    return lat, lon, p


class WaterGround:
    """Уровни воды для слоёв земли. poly_level[i] -- уровень водоёма i (None: по профилю реки), level_at(lat, lon) --
    уровень ближайшей реки (NaN, если реки нет в пределах ~1 км). waters -- тот же список, по которому строилось
    (слой проверяет, что набор воды не сменился)."""
    CELL = 0.01                                                  # ~0.7-1.1 км: 3x3 клетки -- широкая река (Днепр) целиком

    def __init__(self, waters, rivers, ground_fn):
        import numpy as np
        self.waters = waters
        la_, lo_, h_ = [], [], []
        for _w, pts in rivers:
            if len(pts) >= 2:
                a, b, p = water_profile(pts, ground_fn)
                la_.append(a); lo_.append(b); h_.append(p)
        self.lat = np.concatenate(la_) if la_ else np.zeros(0)
        self.lon = np.concatenate(lo_) if lo_ else np.zeros(0)
        self.h = np.concatenate(h_) if h_ else np.zeros(0)
        self.grid: Dict[Tuple[int, int], List[int]] = {}
        for i, (a, b) in enumerate(zip(self.lat.tolist(), self.lon.tolist())):
            self.grid.setdefault((int(a // self.CELL), int(b // self.CELL)), []).append(i)
        if len(self.h):
            self.h = self._blend(WATER_BLEND_M)
        lv = [self._poly_level(np, outer, inners, ground_fn) for outer, inners in waters]
        self.poly_level = [a for a, _b in lv]                    # None -- по профилю реки
        self.poly_fallback = [b for _a, b in lv]                 # уровень всего контура: там, где река далеко

    def __len__(self):
        return len(self.h) + len(self.poly_level)

    def _blend(self, radius_m: float):
        import numpy as np
        out = self.h.copy()
        kx = 111320.0 * math.cos(math.radians(float(np.mean(self.lat))))
        r2 = radius_m * radius_m
        for (ca, cb), idx in self.grid.items():
            cand = np.asarray([i for da in (-1, 0, 1) for db in (-1, 0, 1) for i in self.grid.get((ca + da, cb + db), ())])
            q = np.asarray(idx)
            d2 = ((self.lat[q, None] - self.lat[cand][None, :]) * 110540.0) ** 2 + ((self.lon[q, None] - self.lon[cand][None, :]) * kx) ** 2
            w = (d2 <= r2).astype(float)
            out[q] = (w * self.h[cand][None, :]).sum(1) / np.maximum(w.sum(1), 1.0)
        return out

    def _poly_level(self, np, outer, inners, ground_fn):
        """Один уровень на весь контур -- или None (большой водоём, через который течёт река: по её профилю)."""
        if len(outer) < 3:
            return None, None
        from PIL import Image, ImageDraw
        a = np.asarray(outer, dtype=float)
        la0, la1, lo0, lo1 = a[:, 0].min(), a[:, 0].max(), a[:, 1].min(), a[:, 1].max()
        kx = 111320.0 * math.cos(math.radians(float((la0 + la1) / 2)))
        ext = max((la1 - la0) * 110540.0, (lo1 - lo0) * kx)
        N = int(min(WATER_POLY_SAMPLES, max(4, ext / 30.0)))
        dla, dlo = max(la1 - la0, 1e-9), max(lo1 - lo0, 1e-9)
        pxr = lambda r: [((p[1] - lo0) / dlo * (N - 1), (la1 - p[0]) / dla * (N - 1)) for p in r]
        img = Image.new("L", (N, N), 0)
        d = ImageDraw.Draw(img)
        d.polygon(pxr(outer), fill=1)
        for hole in inners:
            if len(hole) >= 3:
                d.polygon(pxr(hole), fill=0)
        m = np.asarray(img, dtype=bool)
        if len(self.h):                                          # реки внутри контура
            sel = (self.lat >= la0) & (self.lat <= la1) & (self.lon >= lo0) & (self.lon <= lo1)
            idx = np.nonzero(sel)[0]
            if len(idx):
                r = np.clip(np.round((la1 - self.lat[idx]) / dla * (N - 1)).astype(int), 0, N - 1)
                c = np.clip(np.round((self.lon[idx] - lo0) / dlo * (N - 1)).astype(int), 0, N - 1)
                inside = idx[m[r, c]]
                if len(inside):
                    lv = float(np.mean(self.h[inside]))
                    return (None, float(self.h[inside].min())) if ext > WATER_POLY_PROFILE_M else (lv, lv)
        if not m.any():
            m[N // 2, N // 2] = True
        rr, cc = np.nonzero(m)
        lat = la1 - rr / max(N - 1, 1) * dla
        lon = lo0 + cc / max(N - 1, 1) * dlo
        g = np.asarray(ground_fn(lat, lon), dtype=float)
        lv = float(np.percentile(g, WATER_POLY_PCT))
        return lv, lv

    def level_at(self, lat, lon):
        """Уровень ближайшей точки профилей рек (NaN, где рек поблизости нет). Поиск группами по клеткам сетки."""
        import numpy as np
        fl, fo = np.asarray(lat, dtype=float).ravel(), np.asarray(lon, dtype=float).ravel()
        out = np.full(fl.shape, np.nan)
        if not len(self.h) or not len(fl):
            return out.reshape(np.shape(lat))
        kx = 111320.0 * math.cos(math.radians(float(np.mean(fl))))
        keys = np.floor(fl / self.CELL).astype(np.int64) * 10_000_000 + np.floor(fo / self.CELL).astype(np.int64)
        order = np.argsort(keys, kind="stable")
        uk, start = np.unique(keys[order], return_index=True)
        ends = np.append(start[1:], len(order))
        for key, s0, s1 in zip(uk.tolist(), start.tolist(), ends.tolist()):
            ca, cb = key // 10_000_000, key % 10_000_000
            cand = [i for da in (-1, 0, 1) for db in (-1, 0, 1) for i in self.grid.get((ca + da, cb + db), ())]
            if not cand:
                continue
            q, c = order[s0:s1], np.asarray(cand)
            for q0 in range(0, len(q), 2048):                    # кусками: память матрицы расстояний ограничена
                qq = q[q0:q0 + 2048]
                d2 = ((fl[qq, None] - self.lat[c][None, :]) * 110540.0) ** 2 + ((fo[qq, None] - self.lon[c][None, :]) * kx) ** 2
                out[qq] = self.h[c[np.argmin(d2, axis=1)]]
        return out.reshape(np.shape(lat))


# =====================================================================================================================
# ЛЁТНОЕ ПОЛЕ ЦЕЛИКОМ (как Ortho4XP и osm2city): SRTM на аэродроме -- бугры от ангаров, терминалов, стоянок самолётов,
# лесополос. Полосы, рулёжки и перроны одного аэродрома -> выпуклый контур (с запасом AF_PAD_M), внутри -- одна плоскость
# по высотам вдоль полос (наименьшие квадраты, уклон не круче AF_MAX_GRADE), за краем -- переход к рельефу на AF_BLEND_M.
# Внутри контура -- стол: плоскость целиком, без исключений. Раньше отличие от плоскости больше AF_KEEP_M считалось
# «настоящим холмом» и оставалось -- в Борисполе так у полосы оставались гора (бугор SRTM от стоянок и ангаров) и яма
# (лишняя поправка застройки). Настоящий рельеф (AF_KEEP_M) бережётся только в полосе перехода за краем контура.
# Одна и та же поверхность у картинки и у проверки высоты над землёй (касание полосы -- там, где его видно).
# =====================================================================================================================
AF_PAD_M = 60.0


AF_BLEND_M = 200.0


AF_MAX_GRADE = 0.01


AF_KEEP_M = 15.0                 # за краем поля: отличие от плоскости больше -- настоящий рельеф, переход его не срезает


AF_KEEP_FADE_M = 10.0


AF_CLUSTER_M = 1500.0            # элементы ближе -- один аэродром


def _convex_hull(xy):
    """Выпуклая оболочка (монотонная цепь Эндрю): [(x, y)] против часовой."""
    pts = sorted(set(map(tuple, xy)))
    if len(pts) < 3:
        return pts
    cross = lambda o, a, b: (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


class AirfieldGround:
    """Ровное лётное поле. runways: [(вид, ширина, точки, ...)], aprons: [точки]; ground_fn -- земля (SRTM + застройка).
    apply(lat, lon, h) -> h с ровным полем. fields: [(контур x,y м, центр lat/lon, плоскость a,b,c, рамка)]."""

    def __init__(self, runways, aprons, ground_fn):
        import numpy as np
        items = [(k, list(p), float(w or 30.0)) for k, w, p, *_r in runways if len(p) >= 2] + \
                [("apron", list(a), 0.0) for a in aprons if len(a) >= 3]
        self.fields = []
        if not items:
            return
        lat0 = float(np.mean([q[0] for _k, p, _w in items for q in p]))
        self.kx = 111320.0 * math.cos(math.radians(lat0))
        # аэродромы: связные группы элементов (рамки ближе AF_CLUSTER_M)
        boxes = []
        for _k, p, _w in items:
            a = np.asarray(p, float)
            boxes.append((a[:, 0].min() * 110540.0, a[:, 1].min() * self.kx, a[:, 0].max() * 110540.0, a[:, 1].max() * self.kx))
        parent = list(range(len(items)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                bi, bj = boxes[i], boxes[j]
                if bi[0] - AF_CLUSTER_M <= bj[2] and bj[0] - AF_CLUSTER_M <= bi[2] and \
                        bi[1] - AF_CLUSTER_M <= bj[3] and bj[1] - AF_CLUSTER_M <= bi[3]:
                    parent[find(i)] = find(j)
        groups: Dict[int, list] = {}
        for i in range(len(items)):
            groups.setdefault(find(i), []).append(items[i])
        for grp in groups.values():
            if not any(it[0] == "runway" for it in grp):
                continue                                             # без полосы (отдельный перрон, вертолётка) -- не поле
            self._add_field(np, grp, ground_fn)

    def __len__(self):
        return len(self.fields)

    def _add_field(self, np, grp, ground_fn):
        xy = []
        for _k, p, w in grp:                                         # линии -- с шириной: вершины +-полширины (иначе одна
            a = np.asarray(p, float)                                 # прямая полоса -- «контур» без площади)
            q = np.stack([a[:, 1] * self.kx, a[:, 0] * 110540.0], -1)
            hw = w / 2.0
            for dx, dy in ((0, 0), (hw, hw), (hw, -hw), (-hw, hw), (-hw, -hw)) if hw > 0 else ((0, 0),):
                xy.append(q + np.array([dx, dy]))
        xy = np.concatenate(xy)
        hull = np.asarray(_convex_hull(xy), float)
        if len(hull) < 3:
            return
        c = hull.mean(0)                                             # запас: каждую вершину -- от центра наружу на AF_PAD_M
        d = hull - c
        hull = hull + d / np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1.0) * AF_PAD_M
        S = []                                                       # точки для плоскости: вдоль полос через ~50 м
        for k, p, _w in grp:
            if k != "runway":
                continue
            a = np.asarray(p, float)
            for (la0, lo0), (la1, lo1) in zip(a[:-1], a[1:]):
                n = max(2, int(math.hypot((la1 - la0) * 110540.0, (lo1 - lo0) * self.kx) / 50.0) + 1)
                t = np.linspace(0.0, 1.0, n)
                S.append(np.stack([la0 + (la1 - la0) * t, lo0 + (lo1 - lo0) * t], -1))
        S = np.concatenate(S)
        g = np.asarray(ground_fn(S[:, 0], S[:, 1]), float)
        X, Y = S[:, 1] * self.kx - c[0], S[:, 0] * 110540.0 - c[1]
        A = np.stack([np.ones_like(X), X, Y], -1)
        coef = None
        for _ in range(3):                                           # устойчиво: выбросы (бугры SRTM) -- прочь, ещё раз
            sel = np.ones(len(g), bool) if coef is None else np.abs(A @ coef - g) <= max(2.0, 2.5 * np.std(A @ coef - g))
            if sel.sum() < 3:
                break
            coef, *_ = np.linalg.lstsq(A[sel], g[sel], rcond=None)
        if coef is None:
            coef = np.array([float(np.median(g)), 0.0, 0.0])
        gr = math.hypot(coef[1], coef[2])
        if gr > AF_MAX_GRADE:                                        # уклон поля -- не круче AF_MAX_GRADE
            coef[1:] *= AF_MAX_GRADE / gr
            coef[0] = float(np.median(g - X * coef[1] - Y * coef[2]))
        pad = AF_BLEND_M
        box = (hull[:, 0].min() - pad, hull[:, 1].min() - pad, hull[:, 0].max() + pad, hull[:, 1].max() + pad)
        self.fields.append((hull, c, coef, box))

    def plane_at(self, i, lat, lon):
        import numpy as np
        hull, c, coef, _box = self.fields[i]
        return coef[0] + (np.asarray(lon, float) * self.kx - c[0]) * coef[1] + (np.asarray(lat, float) * 110540.0 - c[1]) * coef[2]

    def apply_at(self, lat: float, lon: float, h: float) -> float:
        """Одна точка -- без numpy (проверка удара и основания препятствий зовут её много раз на такте)."""
        x, y = lon * self.kx, lat * 110540.0
        for hull, c, coef, box in self.fields:
            if not (box[0] <= x <= box[2] and box[1] <= y <= box[3]):
                continue
            inside, dmin = False, float("inf")
            n = len(hull)
            for k in range(n):
                x0, y0 = float(hull[k][0]), float(hull[k][1])
                x1, y1 = float(hull[(k + 1) % n][0]), float(hull[(k + 1) % n][1])
                if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / ((y1 - y0) if y1 != y0 else 1e-9) + x0:
                    inside = not inside
                ex, ey = x1 - x0, y1 - y0
                L2 = ex * ex + ey * ey
                t = min(max(((x - x0) * ex + (y - y0) * ey) / (L2 if L2 > 0 else 1.0), 0.0), 1.0)
                dmin = min(dmin, math.hypot(x - (x0 + t * ex), y - (y0 + t * ey)))
            w = 1.0 if inside else min(max(1.0 - dmin / AF_BLEND_M, 0.0), 1.0)
            w = w * w * (3.0 - 2.0 * w)
            pl = float(coef[0] + (x - c[0]) * coef[1] + (y - c[1]) * coef[2])
            keep = 0.0 if inside else min(max((abs(h - pl) - AF_KEEP_M) / AF_KEEP_FADE_M, 0.0), 1.0)
            w *= 1.0 - keep
            h = h * (1.0 - w) + pl * w
        return h

    def apply(self, lat, lon, h):
        """Земля с ровным лётным полем: внутри контура -- плоскость, за краем -- переход на AF_BLEND_M."""
        import numpy as np
        lat, lon = np.asarray(lat, float), np.asarray(lon, float)
        h = np.array(h, dtype=float, copy=True)
        if not self.fields:
            return h
        x, y = lon * self.kx, lat * 110540.0
        for i, (hull, c, coef, box) in enumerate(self.fields):
            near = (x >= box[0]) & (x <= box[2]) & (y >= box[1]) & (y <= box[3])
            if not near.any():
                continue
            px, py = x[near], y[near]
            inside = np.zeros(px.shape, bool)
            dmin = np.full(px.shape, np.inf)
            for (x0, y0), (x1, y1) in zip(hull, np.roll(hull, -1, axis=0)):
                cr = ((y0 > py) != (y1 > py)) & (px < (x1 - x0) * (py - y0) / ((y1 - y0) if y1 != y0 else 1e-9) + x0)
                inside ^= cr
                ex, ey = x1 - x0, y1 - y0
                L2 = ex * ex + ey * ey
                t = np.clip(((px - x0) * ex + (py - y0) * ey) / (L2 if L2 > 0 else 1.0), 0.0, 1.0)
                dmin = np.minimum(dmin, np.hypot(px - (x0 + t * ex), py - (y0 + t * ey)))
            w = np.where(inside, 1.0, np.clip(1.0 - dmin / AF_BLEND_M, 0.0, 1.0))
            w = w * w * (3.0 - 2.0 * w)                              # плавно (без излома на краю перехода)
            pl = coef[0] + (px - c[0]) * coef[1] + (py - c[1]) * coef[2]
            hv = h[near]
            keep = np.where(inside, 0.0, np.clip((np.abs(hv - pl) - AF_KEEP_M) / AF_KEEP_FADE_M, 0.0, 1.0))   # внутри -- стол
            w = w * (1.0 - keep)
            h[near] = hv * (1.0 - w) + pl * w
        return h
