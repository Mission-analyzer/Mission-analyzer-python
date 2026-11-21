"""Мосты: подробное описание для камеры, проверки удара и модели земли (как «стол» у аэродромов).

Мост в данных (Overture / OSM) -- это кусок линии дороги или пути с шириной и уровнем. Из него строится описание:
  * НАСТИЛ -- профиль высоты по готовой земле (obstacles.bridge_deck: берега на профиле дороги, просвет над руслом);
  * ОПОРЫ -- по месту: для известных мостов -- по настоящим пролётам из справочника, для остальных -- с шагом по длине
    моста (эстакада ~33 м, длинный мост ~55 м, очень длинный ~63 м);
  * КОНСТРУКЦИЯ -- для мостов из справочника: вантовый (пилон и ванты «арфой» или «веером»), арочный (арка над главным
    пролётом с подвесками), ферма. Без справочника -- балочный мост на опорах.
Справочник: CATALOG ниже (открытые данные, у каждой записи источник) + необязательный bridges.json рядом с программой
(записи с тем же "name" заменяют встроенные; так поправляются место пилона, пролёты, высоты -- без кода).

Описание считается на вкладке «Карта» вместе с моделью земли (ground_model) и читается камерой и проверкой удара; без
модели -- тем же кодом на лету. Конструкции отдаются одинаково для обоих: список «балок» (отрезок, радиус, часть) --
камера строит из них призмы, проверка удара меряет расстояние до них. Удар -- только о нарисованное."""
from __future__ import annotations

import json
import math
import os
import time
from typing import List, Optional, Sequence

# встроенный справочник: известные мосты. spans -- пролёты по порядку от конца spans_from (м); pylon_at -- номер опоры
# (0 -- первый берег, 1 -- между 1-м и 2-м пролётом, ...); height_m -- высота пилона над землёй; clearance_m -- просвет
# над водой в главном пролёте; rise_m -- подъём арки над настилом. Все куски моста в radius_m от точки -- одна конструкция.
CATALOG: List[dict] = [
    {"name": "Південний міст (Київ)", "lat": 50.39481, "lon": 30.58814, "radius_m": 700, "min_piece_m": 300,
     "type": "cable_stayed", "spans_from": "west",
     "spans": [80.5, 90.0, 271.0, 63.0, 60.0, 58.7, 79.0, 79.0, 79.0, 79.0, 79.0, 79.0, 79.0, 50.5],
     "pylon_at": 2, "height_m": 135.0, "pylon_form": "double", "cables": "harp", "cable_n": 11, "width_m": 41.5,
     "source": "structurae.net (Dnepr River South Bridge): пролёты, пилон 135 м, ширина 41.5 м; сторона пилона -- оценка"},
    {"name": "Кримський міст (арка)", "lat": 45.30861, "lon": 36.50611, "radius_m": 600, "min_piece_m": 150,
     "type": "arch", "main_span_m": 227.0, "clearance_m": 35.0, "rise_m": 45.0,
     "source": "Wikipedia (Crimean Bridge): наибольший пролёт 227 м, просвет 35 м, координаты; подъём арки -- оценка"},
]


PIER_MIN_UNDER_M = 4.0             # под настилом меньше -- опор не рисуем (путепровод над дорогой -- на устоях)
DECK_LAYER_M = 5.0                 # мост над мостом (layer) -- выше на столько за уровень
CABLE_R_M = 0.25                   # радиусы «балок» конструкции (для призм камеры и для удара)
HANGER_R_M = 0.15
ARCH_R_M = 1.4
PYLON_R_M = 3.0
PIER_R_M = 1.6
FOOT_PIER_R_M = 0.7               # пешеходный переход: тонкие стойки и тонкий настил
FOOT_DECK_TH_M = 1.0


def load_catalog(folder: Optional[str] = None) -> List[dict]:
    """Встроенный справочник + bridges.json (из folder или рядом с программой); записи с тем же name -- заменяются."""
    import sys
    cat = {e["name"]: dict(e) for e in CATALOG}
    dirs = [folder] if folder else [os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else None,
                                     os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.getcwd()]
    for here in dict.fromkeys(d for d in dirs if d):                 # рядом с .exe / с программой / в рабочей папке
        try:
            with open(os.path.join(here, "bridges.json"), encoding="utf-8") as fh:
                for e in json.load(fh):
                    if isinstance(e, dict) and e.get("name"):
                        cat[e["name"]] = {**cat.get(e["name"], {}), **e}
            break
        except (OSError, ValueError):
            continue
    return [e for e in cat.values() if "lat" in e and "lon" in e]


def _pier_step(L: float) -> float:
    if L <= 40.0:
        return 0.0
    if L <= 300.0:
        return 33.0
    if L <= 1500.0:
        return 55.0
    return 63.0


def _cum(np, lat, lon):
    kx = 111320.0 * math.cos(math.radians(float(np.mean(lat))))
    return np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(lon) * kx, np.diff(lat) * 110540.0))]), kx


def _project(np, lat, lon, alat, alon, as_):
    """s по оси (alat, alon, as_) для точек (lat, lon): ближайшая точка оси (ось сэмплирована часто -- 10 м)."""
    kx = 111320.0 * math.cos(math.radians(float(np.mean(alat))))
    d = np.hypot((lat[:, None] - alat[None, :]) * 110540.0, (lon[:, None] - alon[None, :]) * kx)
    k = np.argmin(d, axis=1)
    if len(as_) < 2:
        return as_[k], d.min(axis=1)
    ax_, ay_ = alon * kx, alat * 110540.0                            # уточнение: проекция на соседние отрезки оси
    px_, py_ = lon * kx, lat * 110540.0
    best_s, best_d = as_[k].astype(float), d[np.arange(len(k)), k]
    for k0 in (np.clip(k - 1, 0, len(as_) - 2), np.clip(k, 0, len(as_) - 2)):
        x0, y0, x1, y1 = ax_[k0], ay_[k0], ax_[k0 + 1], ay_[k0 + 1]
        dx, dy = x1 - x0, y1 - y0
        L2 = np.maximum(dx * dx + dy * dy, 1e-9)
        t = np.clip(((px_ - x0) * dx + (py_ - y0) * dy) / L2, 0.0, 1.0)
        dd = np.hypot(px_ - (x0 + t * dx), py_ - (y0 + t * dy))
        sv = as_[k0] + t * (as_[k0 + 1] - as_[k0])
        better = dd < best_d
        best_s, best_d = np.where(better, sv, best_s), np.where(better, dd, best_d)
    return best_s, best_d


def build_models(np, bridges: Sequence[tuple], ground_fn, catalog: Optional[List[dict]] = None, named=(), hints=()):
    """Мосты [(вид, ширина, точки, уровень, id)] -> (куски, конструкции). Кусок: dict(id, kind, width, lat, lon, s, deck, g,
    piers [s]); конструкция: dict(name, type, lat, lon, s, deck, g, width, supports [s], main (s0, s1), параметры)."""
    from obstacles import bridge_deck
    catalog = load_catalog() if catalog is None else catalog
    pieces = []
    for kind, width, pts, layer, bid in bridges:
        if len(pts) < 2:
            continue
        lat, lon, deck, g = bridge_deck(pts, ground_fn)
        if kind == "foot":                                           # переход над трассой: настил ровный, над дорогой с просветом
            deck = np.full_like(np.asarray(g, float), float(np.median(g)) + FOOT_DECK_M)
        s, _kx = _cum(np, lat, lon)
        pieces.append({"id": str(bid), "kind": str(kind), "width": float(width), "lat": lat, "lon": lon, "s": s,
                       "deck": np.asarray(deck, float), "g": np.asarray(g, float), "piers": None, "layer": int(layer)})
    groups = group_pieces(np, pieces)                                # куски одного моста -> одна сущность «мост»
    for grp in groups:                                               # настил -- ровной линией по всему мосту, не по рельефу
        smooth_deck(np, [pieces[k] for k in grp])
    for p in pieces:
        if p["layer"] > 1:
            p["deck"] = p["deck"] + DECK_LAYER_M * (p["layer"] - 1)
    for grp in groups:
        bid = "b" + min((pieces[k]["id"] for k in grp), key=lambda x: (len(x), x))
        for k in grp:
            pieces[k]["bridge"] = bid
            pieces[k]["bname"] = ""
    if named:                                                        # имена из данных (мостовые части дорог и путей)
        assign_names(np, pieces, groups, named)
    if hints:                                                        # тип конструкции (и имя) из OSM
        assign_types(np, pieces, groups, hints)
    structures = []
    for e in catalog:
        if e.get("bridge_id"):                                       # запись к мосту по его id (из списка мостов на «Карте»)
            mine = [p for p in pieces if p.get("bridge") == str(e["bridge_id"])]
        else:
            clat, clon = float(e["lat"]), float(e["lon"])
            kx = 111320.0 * math.cos(math.radians(clat))
            R = float(e.get("radius_m", 600.0))
            mine = [p for p in pieces if p["s"][-1] >= float(e.get("min_piece_m", 100.0))
                    and float(np.min(np.hypot((p["lat"] - clat) * 110540.0, (p["lon"] - clon) * kx))) <= R]
        if not mine:
            continue
        for p in mine:
            for q in pieces:                                         # имя -- всему мосту
                if q.get("bridge") == p.get("bridge"):
                    q["bname"] = str(e.get("name", ""))
        if not e.get("type"):                                        # запись без конструкции -- только имя
            continue
        grps = [[p] for p in mine] if e.get("type") in ("arch", "truss") else [mine]   # арка/ферма -- над каждым мостом своя
        for grp in grps:
            st = _structure(np, e, grp, ground_fn)
            ax = max(grp, key=lambda p: p["s"][-1])
            st["bridge"], st["pieces"] = ax.get("bridge", ""), [p["id"] for p in grp]
            structures.append(st)
    done = {st.get("bridge") for st in structures}
    for grp in groups:                                               # тип из OSM, справочника нет -- конструкция по геометрии
        ps = [pieces[k] for k in grp]
        t = ps[0].get("btype", "beam")
        if ps[0].get("bridge") in done or t == "beam":
            continue
        if t == "floating":                                          # наплавной: настил на воде, без опор
            for p in ps:
                p["deck"] = p["g"] + 1.0
                p["piers"] = []
            continue
        ax = max(ps, key=lambda p: p["s"][-1])
        if ax["s"][-1] < AUTO_MIN_M:
            continue
        e = auto_entry(np, ax, t, ps[0].get("bname", ""))
        mine = [p for p in ps if p["s"][-1] >= 0.3 * ax["s"][-1]]
        grps = [[p] for p in mine] if t in ("arch", "truss") else [mine]
        for g_ in grps:
            st = _structure(np, e, g_, ground_fn)
            st["bridge"], st["pieces"], st["auto"] = ax.get("bridge", ""), [p["id"] for p in g_], True
            structures.append(st)
    for grp in groups:                                               # остальные -- опоры с шагом, у параллельных -- общими рядами
        _shared_piers(np, [pieces[k] for k in grp])
    return pieces, structures


# ------------------------------------------------------------------------------------------------- тип моста из OSM
AUTO_MIN_M = 60.0                 # короче -- конструкцию не рисуем (балка): ферма/арка на 20 м -- шум данных
HINT_NEAR_M = 25.0                # линия OSM -- к куску, если четверти куска не дальше (OSM не сдвинут под снимок)
OSM_STRUCTURE = {"beam": "beam", "girder": "beam", "humpback": "beam", "box_girder": "beam",
                 "truss": "truss", "cantilever": "truss", "arch": "arch", "tied_arch": "arch",
                 "cable-stayed": "cable_stayed", "cable_stayed": "cable_stayed",
                 "suspension": "suspension", "simple-suspension": "suspension", "floating": "floating"}


def osm_structure_query(s, w, n, e) -> str:
    """Запрос Overpass: пути с типом моста и контуры мостов (man_made=bridge -- у них часто имя и тип)."""
    bb = f"({s:.5f},{w:.5f},{n:.5f},{e:.5f})"
    return (f'[out:json][timeout:60];(way["bridge:structure"]{bb};way["man_made"="bridge"]{bb};'
            f'way["bridge"="floating"]{bb};);out tags geom;')


def structure_hints(elements) -> List[dict]:
    """Элементы Overpass -> подсказки: {"type", "name", "lat", "lon", "area"}. Без типа и без имени -- пропуск."""
    out = []
    for el in elements:
        if el.get("type") != "way":
            continue
        tags = el.get("tags") or {}
        geo = [(g["lat"], g["lon"]) for g in el.get("geometry") or [] if "lat" in g]
        if len(geo) < 2:
            continue
        t = OSM_STRUCTURE.get(str(tags.get("bridge:structure", "")).lower())
        if tags.get("bridge") == "floating":
            t = "floating"
        name = tags.get("bridge:name") or (tags.get("name") if tags.get("man_made") == "bridge" else "") or ""
        if not t and not name:
            continue
        out.append({"type": t or "", "name": name, "lat": [q[0] for q in geo], "lon": [q[1] for q in geo],
                    "area": tags.get("man_made") == "bridge" and geo[0] == geo[-1]})
    return out


def _in_poly(x, y, poly_x, poly_y) -> bool:
    inside = False
    n = len(poly_x)
    for i in range(n):
        x0, y0, x1, y1 = poly_x[i - 1], poly_y[i - 1], poly_x[i], poly_y[i]
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / ((y1 - y0) or 1e-12):
            inside = not inside
    return inside


def assign_types(np, pieces, groups, hints):
    """Тип (и имя, если его ещё нет) мосту из подсказок OSM: линия рядом с куском или контур, в котором середина куска.
    Тип моста -- самый частый по длине среди его кусков; «балочный» -- если подсказок нет."""
    hs = []
    for h in hints:
        la, lo = np.asarray(h["lat"], float), np.asarray(h["lon"], float)
        s_, _kx = _cum(np, la, lo)
        hs.append((h, la, lo, s_, (la.min() - 0.0004, lo.min() - 0.0006, la.max() + 0.0004, lo.max() + 0.0006)))
    for grp in groups:
        tscore, nscore = {}, {}
        for k in grp:
            p = pieces[k]
            n_ = len(p["lat"])
            idx = sorted({n_ // 4, n_ // 2, (3 * n_) // 4})
            qla, qlo = p["lat"][idx], p["lon"][idx]
            for h, la, lo, s_, bb in hs:
                if qla.max() < bb[0] or qla.min() > bb[2] or qlo.max() < bb[1] or qlo.min() > bb[3]:
                    continue
                if h["area"]:
                    hit = _in_poly(float(p["lon"][n_ // 2]), float(p["lat"][n_ // 2]), h["lon"], h["lat"])
                else:
                    hit = float(_project(np, qla, qlo, la, lo, s_)[1].max()) <= HINT_NEAR_M
                if not hit:
                    continue
                w = float(p["s"][-1])
                if h["type"]:
                    tscore[h["type"]] = tscore.get(h["type"], 0.0) + w
                if h["name"]:
                    nscore[h["name"]] = nscore.get(h["name"], 0.0) + w
        t = max(tscore.items(), key=lambda kv: kv[1])[0] if tscore else "beam"
        nm = max(nscore.items(), key=lambda kv: kv[1])[0] if nscore else ""
        for k in grp:
            pieces[k]["btype"] = t
            if nm and not pieces[k].get("bname"):
                pieces[k]["bname"] = nm


def auto_entry(np, ax, t, name) -> dict:
    """Запись «как в справочнике» для моста с типом из OSM, без справочника: главный пролёт -- над самым глубоким местом
    (наибольший просвет настила над землёй/водой), размеры -- по типовым пропорциям (ОЦЕНКА, не обмер)."""
    L = float(ax["s"][-1])
    clr = ax["deck"] - ax["g"]
    deep = clr >= 0.7 * float(clr.max())                             # самое глубокое место -- середина его самого длинного куска
    best, cur, k0 = (0, 0), None, 0
    for k, v in enumerate(list(deep) + [False]):
        if v and cur is None:
            cur = k
        elif not v and cur is not None:
            if k - cur > best[1] - best[0]:
                best = (cur, k)
            cur = None
    sm = 0.5 * (float(ax["s"][best[0]]) + float(ax["s"][max(best[1] - 1, best[0])])) if best[1] > best[0] else L / 2
    k0 = int(np.argmin(np.abs(ax["s"] - sm)))
    e = {"name": name or "", "type": t, "lat": float(ax["lat"][k0]), "lon": float(ax["lon"][k0]), "auto": True}
    if t == "arch":
        e["main_span_m"] = min(0.7 * L, 260.0)
        e["rise_m"] = 0.18 * e["main_span_m"]
    elif t == "truss":                                               # ферма -- над руслом (самое глубокое место), на своих опорах
        run = float(ax["s"][max(best[1] - 1, best[0])] - ax["s"][best[0]]) if best[1] > best[0] else L
        e["main_span_m"] = float(np.clip(1.15 * run, min(60.0, 0.96 * L), 0.96 * L))
        e["rise_m"] = float(np.clip(0.12 * (_pier_step(L) or 60.0), 8.0, 16.0))
    elif t == "cable_stayed":
        e["main_span_m"] = min(0.45 * L, 320.0)
        e["cables"], e["cable_n"] = "fan", 9
    elif t == "suspension":
        e["main_span_m"] = min(0.6 * L, 600.0)
    return e


NAME_NEAR_M = 15.0                # имя линии данных -- куску, если середина и четверти куска не дальше этого от неё
BRIDGE_WORDS = ("міст", "мост", "bridge", "шляхопровід", "путепровод", "естакада", "эстакада")


def assign_names(np, pieces, groups, named):
    """Имена мостов из данных: named = [(имя, [(lat, lon)])]. Куску -- имя ближайшей линии (по середине и четвертям),
    мосту -- самое частое по длине; имя со словом «міст»/«мост»/«bridge» -- важнее имени улицы."""
    lines = []
    for name, pts in named:
        if len(pts) < 2:
            continue
        la, lo = np.array([q[0] for q in pts], float), np.array([q[1] for q in pts], float)
        s_, _kx = _cum(np, la, lo)
        lines.append((name, la, lo, s_, (la.min() - 0.0005, lo.min() - 0.0008, la.max() + 0.0005, lo.max() + 0.0008)))
    if not lines:
        return
    for grp in groups:
        score = {}
        for k in grp:
            p = pieces[k]
            n_ = len(p["lat"])
            idx = sorted({n_ // 4, n_ // 2, (3 * n_) // 4})
            qla, qlo = p["lat"][idx], p["lon"][idx]
            best = None
            for name, la, lo, s_, bb in lines:
                if qla.max() < bb[0] or qla.min() > bb[2] or qlo.max() < bb[1] or qlo.min() > bb[3]:
                    continue
                _sv, d = _project(np, qla, qlo, la, lo, s_)
                dm = float(d.max())
                if dm <= NAME_NEAR_M and (best is None or dm < best[1]):
                    best = (name, dm)
            if best:
                w = float(p["s"][-1]) * (3.0 if any(x in best[0].lower() for x in BRIDGE_WORDS) else 1.0)
                score[best[0]] = score.get(best[0], 0.0) + w
        if score:
            nm = max(score.items(), key=lambda kv: kv[1])[0]
            for k in grp:
                pieces[k]["bname"] = nm


def smooth_deck(np, grp):
    """Настил моста -- как стол аэродрома: никаких волн по рельефу. Раньше у каждого куска был свой профиль, и он шёл
    за землёй под ним (кочки SRTM, острова, насыпи), а стык кусков посреди реки садился на воду. Теперь на весь мост
    (все его полотна и продолжения) -- одна гладкая линия: прямая между настоящими концами моста и, если нужен просвет
    над рекой/дорогой, одна плавная дуга (парабола) поверх неё. Пешеходные переходы -- ровные (свой расчёт)."""
    ps = [p for p in grp if p["kind"] != "foot"]
    if not ps:
        return
    from geodata.osm_obstacles import BRIDGE_MAX_ABOVE_M, BRIDGE_OVERPASS_CLEAR_M, BRIDGE_VALLEY_M
    la = np.concatenate([p["lat"] for p in ps])
    lo = np.concatenate([p["lon"] for p in ps])
    g = np.concatenate([p["g"] for p in ps])
    kx = 111320.0 * math.cos(math.radians(float(la.mean())))
    ex, ny = (lo - lo.mean()) * kx, (la - la.mean()) * 110540.0
    if len(la) > 2:
        w_, v_ = np.linalg.eigh(np.cov(np.stack([ex, ny])))
        u = v_[:, int(np.argmax(w_))]
    else:
        u = np.array([1.0, 0.0])
    along = ex * u[0] + ny * u[1]
    a0, a1 = float(along.min()), float(along.max())
    L = a1 - a0
    if L < 5.0:
        return
    end = lambda a: float(np.median(g[np.abs(along - a) <= max(8.0, 0.02 * L)]))   # берег -- земля у самого конца
    h0, h1 = end(a0), end(a1)
    t = (along - a0) / L
    lin = h0 + (h1 - h0) * t
    dep = float(np.max(lin - g))
    clear = BRIDGE_OVERPASS_CLEAR_M if dep < BRIDGE_VALLEY_M else min(40.0, 5.0 + 0.02 * L, dep + 15.0)
    shape = 4.0 * t * (1.0 - t)                                      # дуга: 0 на концах, 1 посередине
    mid = (t > 0.1) & (t < 0.9)
    need = g + clear * np.sin(np.pi * t)                             # к берегам просвет сходит на нет
    A = float(np.max((need - lin)[mid] / shape[mid])) if mid.any() else 0.0
    A = min(max(A, 0.0), BRIDGE_MAX_ABOVE_M)
    k0 = 0
    for p in ps:                                                     # каждому полотну -- его точки той же линии
        n = len(p["lat"])
        p["deck"] = lin[k0:k0 + n] + A * shape[k0:k0 + n]
        k0 += n


def _own_piers(p):
    L = float(p["s"][-1])
    st = _pier_step(L)
    n = int(round(L / st)) if st else 0
    inner = [L * k / n for k in range(1, n)] if n >= 2 else []
    if p.get("kind") == "foot":                                      # переход: башни лестниц/лифтов по концам
        e = min(1.5, L / 4.0)
        return [e] + inner + [L - e]
    return inner


FOOT_DECK_M = 6.5                 # пешеходный переход: верх настила над дорогой (просвет ~5.5 м + настил)


def crosses_traffic(np, pts, lines, grid=None):
    """Пересекает ли ломаная pts [(lat, lon)] какую-нибудь из линий lines [[(lat, lon)]] (дороги, пути)? grid -- индекс
    из traffic_grid(lines) (быстрее на сотнях переходов)."""
    G = 0.005
    grid = traffic_grid(lines) if grid is None else grid
    segs = list(zip(pts, pts[1:]))
    cand = set()
    for a, b in segs:
        for i in range(int(min(a[0], b[0]) // G), int(max(a[0], b[0]) // G) + 1):
            for j in range(int(min(a[1], b[1]) // G), int(max(a[1], b[1]) // G) + 1):
                cand |= grid.get((i, j), set())

    def ccw(A, B, C):
        return (C[0] - A[0]) * (B[1] - A[1]) - (B[0] - A[0]) * (C[1] - A[1])
    for k in cand:
        ln = lines[k]
        for c, d in zip(ln, ln[1:]):
            for a, b in segs:
                if ccw(a, b, c) * ccw(a, b, d) < 0 and ccw(c, d, a) * ccw(c, d, b) < 0:
                    return True
    return False


def traffic_grid(lines):
    G = 0.005
    grid = {}
    for k, ln in enumerate(lines):
        for a, b in zip(ln, ln[1:]):
            for i in range(int(min(a[0], b[0]) // G), int(max(a[0], b[0]) // G) + 1):
                for j in range(int(min(a[1], b[1]) // G), int(max(a[1], b[1]) // G) + 1):
                    grid.setdefault((i, j), set()).add(k)
    return grid


def _shared_piers(np, grp):
    """Опоры кусков одного моста без справочника: у полотен, идущих рядом (дорога туда и обратно, метро), -- по рядам
    самого длинного полотна (как у настоящего моста: общий ростверк поперёк), у остальных (съезды, продолжения) -- свои."""
    free = [p for p in grp if p["piers"] is None]
    if not free:
        return
    ax = max(free, key=lambda p: p["s"][-1])
    ax["piers"] = _own_piers(ax)
    for p in free:
        if p is ax:
            continue
        ps, dist = _project(np, p["lat"], p["lon"], ax["lat"], ax["lon"], ax["s"])
        if float(np.median(dist)) > GROUP_ACROSS_M or len(ax["piers"]) == 0:
            p["piers"] = _own_piers(p)
            continue
        order = np.argsort(ps)
        lo_, hi_ = float(ps.min()), float(ps.max())
        p["piers"] = [float(np.interp(sv, ps[order], p["s"][order])) for sv in ax["piers"] if lo_ + 5.0 < sv < hi_ - 5.0]


GROUP_ACROSS_M = 45.0             # полотна одного моста: рядом поперёк (дорога в обе стороны, метро, колея)
CHAIN_GAP_M = 25.0                # и продолжения: конец к концу (длинный мост в данных -- несколькими кусками)


def _end_dir(np, p, end):
    k = (0, min(3, len(p["s"]) - 1)) if end == 0 else (max(len(p["s"]) - 4, 0), len(p["s"]) - 1)
    kx = 111320.0 * math.cos(math.radians(float(p["lat"][0])))
    ex, ny = (p["lon"][k[1]] - p["lon"][k[0]]) * kx, (p["lat"][k[1]] - p["lat"][k[0]]) * 110540.0
    n = math.hypot(ex, ny) or 1.0
    return ex / n, ny / n


def group_pieces(np, pieces) -> List[List[int]]:
    """Куски -> мосты (списки индексов). Один мост: параллельные полотна рядом (поперёк <= GROUP_ACROSS_M на большей
    части короткого) и продолжения (конец к концу <= CHAIN_GAP_M, по одной линии). Поиск соседей -- по сетке ~300 м."""
    n = len(pieces)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    G = 0.003
    grid = {}
    for i, p in enumerate(pieces):
        cells = {(int(a // G), int(b // G)) for a, b in zip(p["lat"][::5].tolist() + [float(p["lat"][-1])],
                                                            p["lon"][::5].tolist() + [float(p["lon"][-1])])}
        for c in cells:
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    grid.setdefault((c[0] + di, c[1] + dj), set()).add(i)
    pairs = set()
    for ids in grid.values():
        ids = sorted(ids)
        for a in range(len(ids)):
            for b in range(a + 1, len(ids)):
                pairs.add((ids[a], ids[b]))
    for i, j in pairs:
        if find(i) == find(j):
            continue
        a, b = (pieces[i], pieces[j]) if pieces[i]["s"][-1] >= pieces[j]["s"][-1] else (pieces[j], pieces[i])
        _ps, dist = _project(np, b["lat"], b["lon"], a["lat"], a["lon"], a["s"])
        same = float(np.mean(dist <= GROUP_ACROSS_M)) >= 0.5 and float(b["s"][-1]) >= 10.0
        if same:                                                     # рядом -- ещё и параллельно (не путепровод поперёк)
            ua, ub = _end_dir(np, a, 0), _end_dir(np, b, 0)
            kx = 111320.0 * math.cos(math.radians(float(a["lat"][0])))
            va = ((a["lon"][-1] - a["lon"][0]) * kx, (a["lat"][-1] - a["lat"][0]) * 110540.0)
            vb = ((b["lon"][-1] - b["lon"][0]) * kx, (b["lat"][-1] - b["lat"][0]) * 110540.0)
            na, nb_ = math.hypot(*va) or 1.0, math.hypot(*vb) or 1.0
            same = abs((va[0] * vb[0] + va[1] * vb[1]) / (na * nb_)) >= 0.9 or abs(ua[0] * ub[0] + ua[1] * ub[1]) >= 0.9
        if not same:                                                 # продолжение: конец к концу, по одной линии
            kx = 111320.0 * math.cos(math.radians(float(a["lat"][0])))
            for ea in (0, -1):
                for eb in (0, -1):
                    d = math.hypot((a["lat"][ea] - b["lat"][eb]) * 110540.0, (a["lon"][ea] - b["lon"][eb]) * kx)
                    if d <= CHAIN_GAP_M:
                        da, db = _end_dir(np, a, ea), _end_dir(np, b, eb)
                        if abs(da[0] * db[0] + da[1] * db[1]) >= 0.9:
                            same = True
        if same:
            parent[find(i)] = find(j)
    out = {}
    for i in range(n):
        out.setdefault(find(i), []).append(i)
    return list(out.values())


KIND_NAMES = {"rail": "колії", "road": "дорога", "foot": "пішохідний"}


def objects(np, pieces, structures) -> List[dict]:
    """Сущности «мост» из кусков с пометкой bridge (build_models): id, имя, тип, длина, ширина, полотна, центр, рамка."""
    by = {}
    for p in pieces:
        by.setdefault(p.get("bridge") or ("b" + p["id"]), []).append(p)
    sts = {}
    for st in structures:
        sts.setdefault(st.get("bridge", ""), []).append(st)
    out = []
    for bid, grp in by.items():
        la = np.concatenate([p["lat"] for p in grp])
        lo = np.concatenate([p["lon"] for p in grp])
        kx = 111320.0 * math.cos(math.radians(float(la.mean())))
        ex, ny = (lo - lo.mean()) * kx, (la - la.mean()) * 110540.0
        cov = np.cov(np.stack([ex, ny])) if len(la) > 2 else np.eye(2)
        w_, v_ = np.linalg.eigh(cov)
        u = v_[:, int(np.argmax(w_))]                                # главное направление моста
        along, across = ex * u[0] + ny * u[1], -ex * u[1] + ny * u[0]
        mine = sts.get(bid, [])
        wd = max(float(p["width"]) for p in grp)
        width = max([wd, float(across.max() - across.min()) + wd] + [float(s_.get("width", 0.0)) for s_ in mine])
        kinds = {}
        for p in grp:
            kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
        out.append({"id": bid, "name": grp[0].get("bname", "") or (mine[0].get("name", "") if mine else ""),
                    "type": mine[0].get("type", "beam") if mine else grp[0].get("btype", "beam"),
                    "length": round(float(along.max() - along.min()), 1), "width": round(width, 1),
                    "kinds": kinds, "pieces": [p["id"] for p in grp],
                    "deck_max": round(float(max(np.max(p["deck"]) for p in grp)), 1),
                    "lat": round(float(la.mean()), 7), "lon": round(float(lo.mean()), 7),
                    "bbox": [round(float(la.min()), 7), round(float(lo.min()), 7), round(float(la.max()), 7), round(float(lo.max()), 7)]})
    return out


def _structure(np, e, mine, ground_fn):
    """Конструкция по записи справочника e над кусками mine: ось, опоры, главный пролёт, настил; куски получают опоры."""
    clat, clon = float(e["lat"]), float(e["lon"])
    ax = max(mine, key=lambda p: p["s"][-1])                     # ось конструкции -- самый длинный кусок
    alat, alon = ax["lat"].copy(), ax["lon"].copy()
    frm = e.get("spans_from", "west")
    flip = {"west": alon[0] > alon[-1], "east": alon[0] < alon[-1],
            "south": alat[0] > alat[-1], "north": alat[0] < alat[-1]}.get(frm, False)
    if flip:
        alat, alon = alat[::-1], alon[::-1]
    as_, _ = _cum(np, alat, alon)
    L = float(as_[-1])
    if e.get("spans"):
        sp = [float(x) for x in e["spans"]]
        sup = np.concatenate([[0.0], np.cumsum(sp)])
        sup *= L / sup[-1]                                       # данные и линия чуть расходятся по длине
        mi = int(np.argmax(sp))
        main = (float(sup[mi]), float(sup[mi + 1]))
    else:
        sc, _d = _project(np, np.array([clat]), np.array([clon]), alat, alon, as_)
        ms = float(e.get("main_span_m", min(200.0, 0.3 * L)))
        c = min(max(float(sc[0]), ms / 2.0), L - ms / 2.0)
        main = (c - ms / 2.0, c + ms / 2.0)
        st = _pier_step(L) or ms
        left = list(np.arange(main[0], 0.0, -st))[::-1]
        right = list(np.arange(main[1], L, st))
        if e.get("type") == "truss":                                 # ферма: опоры под ней -- с обычным шагом
            nn = max(1, int(round((main[1] - main[0]) / st)))
            left += [main[0] + (main[1] - main[0]) * q / nn for q in range(1, nn)]
        sup = np.array(sorted(set([0.0] + [float(x) for x in left + right] + [L])))
        if e.get("type") == "cable_stayed" and "pylon_at" not in e:   # пилон -- у ближнего к центру края главного пролёта
            edge = main[0] if abs(main[0] - L / 2) <= abs(main[1] - L / 2) else main[1]
            e = dict(e, pylon_at=int(np.argmin(np.abs(sup - edge))))
    # настил кусков: просвет главного пролёта (clearance_m) -- плавно, на соседних пролётах -- переход
    clr = e.get("clearance_m")
    decks = []
    for p in mine:
        ps, dist = _project(np, p["lat"], p["lon"], alat, alon, as_)
        if clr:
            water = float(np.min(p["g"]))
            span = main[1] - main[0]
            t = np.clip(1.0 - np.maximum(np.maximum(main[0] - ps, ps - main[1]), 0.0) / (1.5 * span), 0.0, 1.0)
            w = 0.5 - 0.5 * np.cos(np.pi * t)                     # 1 в главном пролёте, к краям перехода -- 0
            p["deck"] = np.maximum(p["deck"], p["deck"] * (1 - w) + (water + float(clr)) * w)
        p["piers"] = [float(np.interp(sv, ps if ps[0] <= ps[-1] else ps[::-1],
                                      p["s"] if ps[0] <= ps[-1] else p["s"][::-1]))
                      for sv in sup[1:-1] if min(ps) + 5.0 < sv < max(ps) - 5.0
                      and (e.get("type") == "truss" or not (main[0] + 1.0 < sv < main[1] - 1.0))]
        if e.get("type") == "cable_stayed":                      # опора пилона -- это сам пилон
            spy = float(sup[int(e.get("pylon_at", 1))])
            p["piers"] = [x for x in p["piers"] if abs(float(np.interp(x, p["s"], ps)) - spy) > 5.0]
        decks.append((ps, p["deck"]))
    adeck = np.max([np.interp(as_, *((d[0], d[1]) if d[0][0] <= d[0][-1] else (d[0][::-1], d[1][::-1])))
                    for d in decks], axis=0)
    ag = np.asarray(ground_fn(alat, alon), float)
    if e.get("type") == "cable_stayed" and "height_m" not in e:      # высота пилона: над настилом ~0.3 главного пролёта
        k = int(np.argmin(np.abs(as_ - sup[int(e.get("pylon_at", 1))])))
        e = dict(e, height_m=float(adeck[k] - ag[k]) + 0.3 * (main[1] - main[0]))
    width = float(e.get("width_m", 0.0)) if len(mine) > 1 or e.get("type") == "cable_stayed" else 0.0
    return ({k: v for k, v in e.items() if k not in ("lat", "lon")} |
            {"lat": alat, "lon": alon, "s": as_, "deck": adeck, "g": ag,
             "width": width or float(sum(p["width"] for p in mine)), "supports": sup.tolist(), "main": main})


# ------------------------------------------------------------------------------------------------- «балки»: камера и удар
def _at(np, m, sv):
    """(широта, долгота, настил, земля) в точке s оси куска или конструкции."""
    return (float(np.interp(sv, m["s"], m["lat"])), float(np.interp(sv, m["s"], m["lon"])),
            float(np.interp(sv, m["s"], m["deck"])), float(np.interp(sv, m["s"], m["g"])))


def _dir(np, m, sv):
    """Единичное направление оси в точке s (east, north)."""
    k = int(np.clip(np.searchsorted(m["s"], sv), 1, len(m["s"]) - 1))
    kx = 111320.0 * math.cos(math.radians(float(m["lat"][k])))
    ex, ny = (m["lon"][k] - m["lon"][k - 1]) * kx, (m["lat"][k] - m["lat"][k - 1]) * 110540.0
    n = math.hypot(ex, ny) or 1.0
    return ex / n, ny / n


def _off(lat, lon, east, north):
    return lat + north / 110540.0, lon + east / (111320.0 * math.cos(math.radians(lat)))


def beams(np, pieces, structures) -> List[tuple]:
    """Конструкции мостов как «балки»: (lat0, lon0, z0, lat1, lon1, z1, радиус м, часть). Опоры, пилоны, ванты, арки,
    подвески. Настил -- отдельно (лента по профилю куска)."""
    out = []
    for p in pieces:                                                 # опоры: столб (узкий мост) или два столба
        foot = p.get("kind") == "foot"
        th = FOOT_DECK_TH_M if foot else 1.8
        for sv in p["piers"]:
            la, lo, d, g = _at(np, p, sv)
            if d - th - g < PIER_MIN_UNDER_M:
                continue
            ux, uy = _dir(np, p, sv)
            offs = (0.0,) if p["width"] < 16.0 else (-p["width"] * 0.3, p["width"] * 0.3)
            for o in offs:
                a, b = _off(la, lo, -uy * o, ux * o)
                out.append((a, b, g - 2.0, a, b, d - th, FOOT_PIER_R_M if foot else PIER_R_M, "pier"))
    for st in structures:
        t = st.get("type")
        hw = st["width"] / 2.0
        if t == "cable_stayed":
            sp = float(st["supports"][int(st.get("pylon_at", 1))])
            la, lo, d, g = _at(np, st, sp)
            ux, uy = _dir(np, st, sp)
            top = g + float(st.get("height_m", 100.0))
            legs = []
            for side in (-1, 1):
                bl, bo = _off(la, lo, -uy * side * (hw + 3.0), ux * side * (hw + 3.0))
                if st.get("pylon_form") == "A":
                    tl, to = la, lo
                else:
                    tl, to = _off(la, lo, -uy * side * (hw + 1.5), ux * side * (hw + 1.5))
                out.append((bl, bo, g - 2.0, tl, to, top, PYLON_R_M, "pylon"))
                legs.append((bl, bo, tl, to))
            (a0, b0, _x, _y), (a1, b1, _x2, _y2) = legs
            out.append((a0, b0, d - 3.0, a1, b1, d - 3.0, 2.0, "pylon"))  # ригель под настилом
            # ванты: две плоскости по краям настила, в обе стороны от пилона; «арфа» -- параллельные (выше на пилоне --
            # дальше на настиле), «веер» -- все из-под верха пилона
            n = int(st.get("cable_n", 10))
            harp = st.get("cables", "harp") == "harp"
            main_len = st["main"][1] - st["main"][0]
            dirm = 1.0 if abs(st["main"][0] - sp) <= abs(st["main"][1] - sp) else -1.0   # главный пролёт: вперёд или назад
            room_back = (sp if dirm > 0 else st["s"][-1] - sp) * 0.95
            sides = ((dirm, 0.92 * main_len), (-dirm, min(0.92 * main_len, room_back)))
            h0, h1 = d + 0.35 * (top - d), top - 4.0
            for k in range(n):
                f = (k + 1) / n
                hz = h0 + (h1 - h0) * f if harp else h1 - 1.5 * (n - 1 - k) / max(n - 1, 1)
                reach = (hz - d) / (h1 - d) if harp else f
                for sgn, Ls in sides:
                    if Ls <= 5.0:
                        continue
                    s_a = min(max(sp + sgn * Ls * reach, 0.0), float(st["s"][-1]))
                    ala, alo, ad, _ag = _at(np, st, s_a)
                    aux, auy = _dir(np, st, s_a)
                    for side in (-1, 1):
                        pla, plo = _off(la, lo, -uy * side * (hw + 1.5), ux * side * (hw + 1.5))
                        dla, dlo = _off(ala, alo, -auy * side * hw, aux * side * hw)
                        out.append((pla, plo, hz, dla, dlo, ad, CABLE_R_M, "cable"))
        elif t == "suspension":                                      # висячий: две башни, главный трос дугой, подвески
            s0, s1 = st["main"]
            span = s1 - s0
            sag = float(st.get("sag_m", span / 10.0))
            Ltot = float(st["s"][-1])
            tops = []
            for sv in (s0, s1):
                la, lo, d, g = _at(np, st, sv)
                ux, uy = _dir(np, st, sv)
                top = d + sag + 6.0
                legs = []
                for side in (-1, 1):
                    a, b = _off(la, lo, -uy * side * (hw + 2.0), ux * side * (hw + 2.0))
                    out.append((a, b, g - 2.0, a, b, top, PYLON_R_M * 0.8, "pylon"))
                    legs.append((a, b))
                for zz in (d - 3.0, top - 2.0):                       # ригели башни: под настилом и наверху
                    out.append((legs[0][0], legs[0][1], zz, legs[1][0], legs[1][1], zz, 1.5, "pylon"))
                tops.append(top)
            n = 24
            ss = np.linspace(s0, s1, n + 1)
            for side in (-1, 1):
                P = []
                for sv in ss:
                    la, lo, d, _g = _at(np, st, sv)
                    ux, uy = _dir(np, st, sv)
                    tt = (sv - s0) / span
                    z = (tops[0] * (1 - tt) + tops[1] * tt) - 4.0 * sag * tt * (1.0 - tt)
                    a, b = _off(la, lo, -uy * side * (hw + 1.0), ux * side * (hw + 1.0))
                    P.append((a, b, z, d))
                for (a0, b0, z0, _d0), (a1, b1, z1, _d1) in zip(P, P[1:]):
                    out.append((a0, b0, z0, a1, b1, z1, 0.6, "cable"))
                for a, b, z, d in P[1:-1]:
                    out.append((a, b, d, a, b, z, HANGER_R_M, "hanger"))
                for sv, top, sgn in ((s0, tops[0], -1.0), (s1, tops[1], 1.0)):   # оттяжки к анкерам на берегах
                    anc = min(max(sv + sgn * 0.35 * span, 0.0), Ltot)
                    la, lo, d, _g = _at(np, st, sv)
                    ala, alo, ad, _ag = _at(np, st, anc)
                    ux, uy = _dir(np, st, sv)
                    a0, b0 = _off(la, lo, -uy * side * (hw + 1.0), ux * side * (hw + 1.0))
                    a1, b1 = _off(ala, alo, -uy * side * (hw + 1.0), ux * side * (hw + 1.0))
                    out.append((a0, b0, top, a1, b1, ad, 0.6, "cable"))
        elif t in ("arch", "truss"):
            s0, s1 = st["main"]
            rise = float(st.get("rise_m", 0.2 * (s1 - s0))) if t == "arch" else float(st.get("rise_m", 10.0))
            nseg = 24
            ss = np.linspace(s0, s1, nseg + 1)
            pts = {-1: [], 1: []}
            for sv in ss:
                la, lo, d, _g = _at(np, st, sv)
                ux, uy = _dir(np, st, sv)
                tt = (sv - s0) / (s1 - s0)
                z = d + (4.0 * rise * tt * (1.0 - tt) if t == "arch" else rise)
                for side in (-1, 1):
                    a, b = _off(la, lo, -uy * side * (hw + 0.5), ux * side * (hw + 0.5))
                    pts[side].append((a, b, z, d))
            for side in (-1, 1):
                P = pts[side]
                for (a0, b0, z0, d0), (a1, b1, z1, _d1) in zip(P, P[1:]):
                    out.append((a0, b0, z0, a1, b1, z1, ARCH_R_M if t == "arch" else 0.8, "arch" if t == "arch" else "truss"))
                for k, (a, b, z, d) in enumerate(P):              # подвески (арка) или стойки (ферма)
                    if 0 < k < len(P) - 1:
                        out.append((a, b, d, a, b, z, HANGER_R_M if t == "arch" else 0.5, "hanger"))
                if t == "truss":
                    for (a0, b0, z0, d0), (a1, b1, _z1, d1) in zip(P, P[1:]):
                        out.append((a0, b0, d0, a1, b1, _z1, 0.4, "truss"))
            for k in range(2, nseg - 1, 3):                          # связи между арками сверху
                (a0, b0, z0, _), (a1, b1, z1, _) = pts[-1][k], pts[1][k]
                out.append((a0, b0, z0, a1, b1, z1, 0.5, "arch" if t == "arch" else "truss"))
    return out


def beam_hit(bs, lat: float, lon: float, alt: float, margin: float = 1.0):
    """Борт ближе radius+margin к какой-нибудь «балке»? -> (часть, высота верха) или None."""
    kx = 111320.0 * math.cos(math.radians(lat))
    for la0, lo0, z0, la1, lo1, z1, r, part in bs:
        if min(la0, la1) - 0.003 > lat or lat > max(la0, la1) + 0.003 or min(lo0, lo1) - 0.004 > lon or lon > max(lo0, lo1) + 0.004:
            continue
        ax, ay, az = (lo0 - lon) * kx, (la0 - lat) * 110540.0, z0 - alt
        bx, by, bz = (lo1 - lon) * kx, (la1 - lat) * 110540.0, z1 - alt
        dx, dy, dz = bx - ax, by - ay, bz - az
        L2 = dx * dx + dy * dy + dz * dz
        t = 0.0 if L2 <= 0 else max(0.0, min(1.0, -(ax * dx + ay * dy + az * dz) / L2))
        cx, cy, cz = ax + t * dx, ay + t * dy, az + t * dz
        if cx * cx + cy * cy + cz * cz <= (r + margin) ** 2:
            return part, max(z0, z1)
    return None


# ------------------------------------------------------------------------------------------------- хранение: мосты -- отдельно
STORE_VERSION = 1


def store_path(folder: str, i: int, j: int) -> str:
    """Файл мостов клетки карты (те же клетки, что у модели земли): мост целиком -- в клетке своего центра."""
    return os.path.join(folder, f"bridges_v{STORE_VERSION}_{i}_{j}.json")


def save_cells(np, folder, cells, pieces, structures, cell_deg) -> int:
    """Записать мосты по клеткам (мост не режется: куски и конструкции -- в клетку центра моста). Пустой файл -- тоже
    (значит: мостов в клетке нет, а не «не считали»)."""
    obs = objects(np, pieces, structures)
    home = {o["id"]: (int(o["lat"] // cell_deg), int(o["lon"] // cell_deg)) for o in obs}
    os.makedirs(folder, exist_ok=True)
    n = 0
    for i, j in cells:
        ids = {b for b, c in home.items() if c == (i, j)}
        doc = json.loads(to_json(np, [p for p in pieces if p.get("bridge") in ids],
                                 [s_ for s_ in structures if s_.get("bridge") in ids]))
        doc.update(version=STORE_VERSION, built=time.time(), bridges=[o for o in obs if o["id"] in ids])
        tmp = store_path(folder, i, j) + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False)
        os.replace(tmp, store_path(folder, i, j))
        n += len(ids)
    return n


def load_cell(np, folder, i, j):
    """(куски, конструкции, мосты) клетки или None -- файла нет или он другой версии."""
    try:
        with open(store_path(folder, i, j), encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    if int(d.get("version", 0)) != STORE_VERSION:
        return None
    pcs, sts = from_json(np, json.dumps({"pieces": d.get("pieces", []), "structures": d.get("structures", [])}))
    return pcs, sts, list(d.get("bridges", []))


def bridges_json_path() -> str:
    import sys
    dirs = [os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else None,
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.getcwd()]
    dirs = [d for d in dirs if d]
    for d in dirs:
        if os.path.isfile(os.path.join(d, "bridges.json")):
            return os.path.join(d, "bridges.json")
    return os.path.join(dirs[0], "bridges.json")


def rename(bridge_id: str, name: str, lat: float, lon: float) -> str:
    """Имя моста -- в bridges.json: запись с этим bridge_id получает имя (с конструкцией -- остаётся с ней), иначе новая
    запись только с именем. Координаты и форма не меняются."""
    path = bridges_json_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = [e for e in json.load(fh) if isinstance(e, dict)]
    except (OSError, ValueError):
        data = []
    for e in data:
        if e.get("bridge_id") == bridge_id:
            e["name"] = name
            break
    else:
        data.append({"name": name, "bridge_id": bridge_id, "lat": lat, "lon": lon})
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


NAME_NEAR_M = 150.0     # имя моста из bridges.json -- к мосту, чей центр ближе этого (если id не совпал)


def apply_names(objs):
    """Имена из bridges.json (по bridge_id) -- поверх записей при чтении: переименование видно сразу."""
    try:
        with open(bridges_json_path(), encoding="utf-8") as fh:
            ents = [e for e in json.load(fh) if isinstance(e, dict) and e.get("bridge_id") and e.get("name")]
    except (OSError, ValueError):
        ents = []
    names = {e["bridge_id"]: e["name"] for e in ents}
    for o in objs:
        if o.get("id") in names:
            o["name"] = names[o["id"]]
        elif ents and "lat" in o:                                     # id сменился после докачки -- имя держится за место
            kx = 111320.0 * math.cos(math.radians(float(o["lat"])))
            best = min(((math.hypot((float(e.get("lat", 0)) - o["lat"]) * 110540.0,
                                    (float(e.get("lon", 0)) - o["lon"]) * kx), e) for e in ents if "lat" in e),
                       key=lambda t: t[0], default=None)
            if best and best[0] <= NAME_NEAR_M:
                o["name"] = best[1]["name"]
    return objs


def list_all(folder) -> List[dict]:
    """Все мосты из файлов мостов папки (для списка на вкладке «Карта»)."""
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in sorted(names):
        if name.startswith(f"bridges_v{STORE_VERSION}_") and name.endswith(".json"):
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as fh:
                    out += list(json.load(fh).get("bridges", []))
            except (OSError, ValueError):
                continue
    return apply_names(out)


# ------------------------------------------------------------------------------------------------- хранение (модель земли)
def to_json(np, pieces, structures) -> str:
    def enc(m):
        out = {}
        for k, v in m.items():
            if hasattr(v, "tolist"):
                v = [round(float(x), 7 if k in ("lat", "lon") else 2) for x in v.tolist()]
            elif isinstance(v, tuple):
                v = list(v)
            out[k] = v
        return out
    return json.dumps({"pieces": [enc(p) for p in pieces], "structures": [enc(s) for s in structures]}, ensure_ascii=False)


def from_json(np, text: str):
    d = json.loads(text) if text else {"pieces": [], "structures": []}

    def dec(m):
        for k in ("lat", "lon", "s", "deck", "g"):
            if k in m:
                m[k] = np.asarray(m[k], float)
        if "main" in m:
            m["main"] = tuple(m["main"])
        return m
    return [dec(p) for p in d.get("pieces", [])], [dec(s) for s in d.get("structures", [])]
