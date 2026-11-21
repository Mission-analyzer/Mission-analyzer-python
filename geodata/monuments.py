"""Высокие памятники -- ориентиры, которых нет в данных о зданиях: там у «Батьківщини-матері» есть только постамент
(музей), а статуи нет. В открытых данных (Overture) класса «памятник» с высотой нет, поэтому -- справочник высоких
(от MIN_TALL_M) и monuments.json рядом с программой (свои, в том же виде). Низкие памятники не берём.

Справочник -- geodata/monuments.geojson (GeoJSON, части -- по схеме OSM Simple 3D Buildings, см. load_catalog); правится
в QGIS / на geojson.io. Ниже -- поля записи после чтения и формы (shape) для monuments.json и фигур, которые части не описывают.

Запись: name, city (населённый пункт -- для списка на «Карте»), lat, lon, shape, base_m (высота постамента над землёй -- верх здания-постамента, если он нарисован),
height_m (полная высота над землёй), facing_deg (куда смотрит, для фигур). Формы:
  * statue_sword_shield -- фигура с поднятым мечом и щитом (Батьківщина-мати): statue_m, sword_m, shield [ширина, высота];
  * column -- колонна со статуей наверху (Монумент Незалежності): статуя statue_m, опора-храм base_m;
  * obelisk -- сужающаяся стела;
  * bell_tower -- ярусная колокольня с главой: width_m (сторона основания), color (стены), top (глава);
  * church -- храм: объём, барабан, купол, крест; width_m, color, top;
  * twin_spires -- готический храм с двумя башнями-шпилями по фасаду (facing_deg -- куда смотрит фасад).
Форма -> «балки» (отрезки с радиусом, как у мостов: geodata.bridges.beams) -- по ним одинаково строится картинка и
считается удар. Размеры фигуры -- типовые пропорции (ОЦЕНКА), высоты -- справочные.
Своя 3D-модель: "model" -- папка в models3d (как у 3D-объектов сцены), "model_units" (m/cm/mm), "model_up" (Y/Z), повёрнута
по facing_deg. Если модель на месте -- памятник ставится ею (в камере, ударах, на карте), упрощённая фигура не рисуется;
нет папки -- рисуется фигура. "shape": "model" -- только скрыть фигуру."""
from __future__ import annotations

import json
import math
import os
import sys
from typing import List

MIN_TALL_M = 25.0

CATALOG_FILE = "monuments.geojson"   # справочник: GeoJSON, части -- по схеме OSM Simple 3D Buildings (рядом с модулем)


def _ring_m(coords):
    """Контур [[долг, шир], ...] -> (центр шир, центр долг, радиус равной площади, м)."""
    pts = [(float(q[1]), float(q[0])) for q in coords[:-1] if len(q) >= 2] if coords and coords[0] == coords[-1] else \
        [(float(q[1]), float(q[0])) for q in coords]
    la0 = sum(p[0] for p in pts) / len(pts)
    lo0 = sum(p[1] for p in pts) / len(pts)
    kx = 111320.0 * math.cos(math.radians(la0))
    xy = [((p[1] - lo0) * kx, (p[0] - la0) * 110540.0) for p in pts]
    area = abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(xy, xy[1:] + xy[:1]))) / 2.0
    return la0, lo0, math.sqrt(area / math.pi)


def _num(v, default):
    """«12», «12.5 m», «12,5» -> число; иначе default."""
    try:
        return float(str(v).replace(",", ".").split()[0])
    except (TypeError, ValueError, IndexError):
        return default


def load_catalog(path=None) -> List[dict]:
    """monuments.geojson -> записи справочника: name, city, lat, lon, height_m, source, parts [(шир, долг, низ, верх, радиус,
    цвет, крыша)]; поля программы ma:* -- без приставки (shape, statue_m, model ...)."""
    try:
        with open(path or os.path.join(os.path.dirname(os.path.abspath(__file__)), CATALOG_FILE), encoding="utf-8") as fh:
            feats = json.load(fh).get("features", [])
    except (OSError, ValueError, AttributeError):
        return []
    out, parts = [], {}
    for f in feats:
        p, g = f.get("properties") or {}, f.get("geometry") or {}
        try:
            if p.get("type") == "landmark" and g.get("type") == "Point":
                lo, la = g["coordinates"][:2]
                e = {"name": str(p["name"]), "city": str(p.get("addr:city", "")), "lat": float(la), "lon": float(lo),
                     "height_m": float(p.get("height", 0)), "source": str(p.get("ma:source", ""))}
                for k, v in p.items():
                    if k.startswith("ma:") and k != "ma:source":
                        e[k[3:]] = v
                out.append(e)
            elif p.get("building:part") and g.get("type") == "Polygon":
                ring = g["coordinates"][0]
                la, lo, r = _ring_m(ring)
                parts.setdefault(str(p.get("ma:landmark", "")), []).append(
                    (la, lo, _num(p.get("min_height"), 0.0), _num(p.get("height"), 0.0), r,
                     str(p.get("building:colour", "#b4b4b4")), str(p.get("roof:shape", "flat")),
                     [(float(q[1]), float(q[0])) for q in ring], _num(p.get("roof:height"), None),
                     str(p.get("roof:colour", "")) or None))
        except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError):
            continue
    for e in out:
        if e["name"] in parts and "shape" not in e:
            e["shape"], e["parts"] = "parts", parts[e["name"]]
    return out


CATALOG = load_catalog()

COLORS = {"steel": (196, 200, 206), "marble": (236, 234, 228), "gold": (214, 176, 72), "stone": (160, 156, 148),
          "white": (232, 230, 222), "blue": (122, 168, 206), "brick": (168, 92, 72), "green": (112, 162, 132)}


CSS_COLORS = {"white": (240, 240, 236), "black": (40, 40, 40), "gray": (128, 128, 128), "grey": (128, 128, 128),
              "silver": (192, 192, 192), "gold": (214, 176, 72), "yellow": (230, 210, 90), "green": (80, 140, 90),
              "darkgreen": (30, 90, 50), "blue": (70, 110, 190), "lightblue": (160, 200, 230), "red": (170, 60, 50),
              "brown": (120, 80, 50), "beige": (225, 210, 180), "orange": (220, 140, 60), "maroon": (110, 40, 40)}


def colour_rgb(c, default=(180, 180, 180)):
    """Цвет части: имя из COLORS / CSS («white», «gold» -- как пишут в OSM) или «#rrggbb» (building:colour по S3DB)."""
    if c in COLORS:
        return COLORS[c]
    if str(c).lower() in CSS_COLORS:
        return CSS_COLORS[str(c).lower()]
    try:
        c = str(c).strip()
        if c.startswith("#") and len(c) == 7:
            return tuple(int(c[k:k + 2], 16) for k in (1, 3, 5))
    except ValueError:
        pass
    return default


def _dirs():
    return [os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else None,
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.getcwd()]


def load_all() -> List[dict]:
    """Справочник + monuments.json (запись с тем же именем -- заменяет справочную). Только высокие."""
    out = {e["name"]: dict(e) for e in CATALOG}
    for d in dict.fromkeys(x for x in _dirs() if x):
        try:
            with open(os.path.join(d, "monuments.json"), encoding="utf-8") as fh:
                for e in json.load(fh):
                    if isinstance(e, dict) and e.get("name") and "lat" in e and "lon" in e:
                        out[e["name"]] = dict(out.get(e["name"], {}), **e)
            break
        except (OSError, ValueError):
            continue
    return [e for e in out.values() if float(e.get("height_m", 0)) >= MIN_TALL_M or e.get("parts")]   # с частями -- любые (вокзалы)


UNIT_FACTORS = {"mm": 0.001, "cm": 0.01, "m": 1.0}


def model_path(m: dict):
    """Своя 3D-модель памятника из справочника: поле "model" -- папка (или файл) в models3d, как у 3D-объектов сцены.
    Путь к файлу модели или None (поля нет, папки нет, в ней нет .obj/.glb/.gltf) -- тогда рисуется упрощённая фигура."""
    ref = str(m.get("model") or "").strip()
    if not ref:
        return None
    try:
        import scene_library
        path = scene_library.resolve_model(ref)
    except Exception:
        return None
    return path if path and os.path.isfile(path) else None


def scene_object(m: dict):
    """Памятник со своей моделью -> 3D-объект сцены (scene_object.SceneObject) в точке справочника: стоит, рисуется и бьётся
    как объекты с «Конфігурації», но в их таблицу и scene_objects.json не попадает (landmark=True; красной точки нет).
    Азимут -- facing_deg, единицы -- model_units (m/cm/mm, по умолчанию m), ось «вверх» OBJ -- model_up (Y/Z, по умолчанию Y)."""
    if model_path(m) is None:
        return None
    from scene_object import SceneObject
    o = SceneObject(name=str(m["name"]), lat=float(m["lat"]), lon=float(m["lon"]),
                    azimuth_deg=float(m.get("facing_deg", 0.0)) % 360.0, model_path=str(m["model"]).strip(),
                    scale=UNIT_FACTORS.get(str(m.get("model_units", "m")).lower(), 1.0),
                    up_axis=str(m.get("model_up", "Y")).upper())
    o.landmark = True
    return o


def scene_objects(objs=None):
    """Все памятники справочника, у которых есть своя модель, -- объектами сцены."""
    out = []
    for m in (load_all() if objs is None else objs):
        o = scene_object(m)
        if o is not None:
            out.append(o)
    return out


def drawn(objs, *_ignored):
    """Что рисует камера и с чем считается удар упрощённой фигурой: без памятников со своей моделью (поле "model" в
    справочнике или monuments.json и модель на месте, либо "shape": "model"). Их рисует и бьёт сама модель. На карте и в
    поиске памятники остаются."""
    return [e for e in objs if e.get("shape") != "model" and model_path(e) is None]


def _off(lat, lon, de, dn):
    return lat + dn / 110540.0, lon + de / (111320.0 * math.cos(math.radians(lat)))


def beams(m: dict, ground: float):
    """Памятник -> [(lat0, lon0, z0, lat1, lon1, z1, радиус, часть)] (z -- AMSL; ground -- земля у памятника)."""
    la, lo = float(m["lat"]), float(m["lon"])
    g = float(ground)
    H = float(m["height_m"])
    base = float(m.get("base_m", 0.0))
    out = []
    shape = m.get("shape", "obelisk")
    if shape == "parts":                                             # части по S3DB: столбы от min_height до height
        return [(q[0], q[1], g + q[2], q[0], q[1], g + q[3], q[4], q[5]) for q in m["parts"] if q[3] > q[2]]
    if shape == "statue_sword_shield":
        S = float(m.get("statue_m", H - base))
        z0 = g + base
        f = math.radians(float(m.get("facing_deg", 0.0)))
        fx, fy = math.sin(f), math.cos(f)                            # вперёд
        rx, ry = fy, -fx                                             # вправо
        # statue_m -- до острия меча (поднятая рука). Фигура (до макушки) ~0.64 S, плечо ~0.52 S, кисть -- S минус меч
        prof = ((0.00, 7.5), (0.12, 6.5), (0.28, 5.0), (0.42, 4.0), (0.52, 3.4), (0.56, 2.0))
        for (t0, r0), (t1, _r1) in zip(prof, prof[1:]):
            out.append((la, lo, z0 + t0 * S, la, lo, z0 + t1 * S, r0 * 0.85, "steel"))
        out.append((la, lo, z0 + 0.56 * S, la, lo, z0 + 0.64 * S, 1.8, "steel"))   # голова
        sw = float(m.get("sword_m", 16.0))
        sh_r = _off(la, lo, rx * 3.5, ry * 3.5)                     # правое плечо -> рука вверх -> меч
        hand = _off(la, lo, rx * 4.5 + fx * 1.0, ry * 4.5 + fy * 1.0)
        zs, zh = z0 + 0.52 * S, z0 + S - sw
        out.append((sh_r[0], sh_r[1], zs, hand[0], hand[1], zh, 1.2, "steel"))
        out.append((hand[0], hand[1], zh, hand[0], hand[1], z0 + S, 0.6, "steel"))
        w, h = (m.get("shield") or [13.0, 8.0])[:2]                 # щит в левой руке: овал -- короткая толстая «балка»
        c = _off(la, lo, -rx * 6.5 + fx * 1.5, -ry * 6.5 + fy * 1.5)
        zc = z0 + 0.40 * S
        out.append((c[0], c[1], zc - float(w) / 2 + float(h) / 2, c[0], c[1], zc + float(w) / 2 - float(h) / 2,
                    float(h) / 2, "steel"))
    elif shape == "column":
        S = float(m.get("statue_m", 10.0))
        out.append((la, lo, g, la, lo, g + base, 7.0, "marble"))    # постамент-храм
        out.append((la, lo, g + base, la, lo, g + H - S, 2.2, "marble"))   # колонна
        out.append((la, lo, g + H - S, la, lo, g + H - S + 1.0, 2.8, "marble"))   # капитель
        out.append((la, lo, g + H - S + 1.0, la, lo, g + H, 1.3, "gold"))  # статуя
    elif shape == "bell_tower":                                      # 4 яруса, каждый уже предыдущего, глава и крест
        r0 = float(m.get("width_m", 0.25 * H)) / 2.0
        wall, top = m.get("color", "white"), m.get("top", "gold")
        z, zt = g, g + 0.74 * H
        for k, f in enumerate((0.34, 0.26, 0.22, 0.18)):
            z1 = z + (zt - g) * f / 1.0
            out.append((la, lo, z, la, lo, z1, r0 * (1.0 - 0.17 * k), wall))
            z = z1
        out.append((la, lo, z, la, lo, z + 0.08 * H, r0 * 0.38, wall))          # барабан
        out.append((la, lo, z + 0.08 * H, la, lo, z + 0.15 * H, r0 * 0.34, top))  # глава
        out.append((la, lo, z + 0.15 * H, la, lo, g + H, 0.5, top))             # шпиль и крест
    elif shape == "church":
        r = float(m.get("width_m", 0.6 * H)) / 2.0 * 0.85
        wall, top = m.get("color", "white"), m.get("top", "gold")
        out.append((la, lo, g, la, lo, g + 0.45 * H, r, wall))                   # объём храма
        out.append((la, lo, g + 0.45 * H, la, lo, g + 0.70 * H, r * 0.36, wall))  # барабан
        out.append((la, lo, g + 0.70 * H, la, lo, g + 0.88 * H, r * 0.40, top))   # купол
        out.append((la, lo, g + 0.88 * H, la, lo, g + H, 0.4, top))               # крест
    elif shape == "twin_spires":
        w = float(m.get("width_m", 0.7 * H))
        wall = m.get("color", "brick")
        out.append((la, lo, g, la, lo, g + 0.40 * H, w / 2.0 * 0.8, wall))       # неф
        f = math.radians(float(m.get("facing_deg", 0.0)))
        fx, fy = math.sin(f), math.cos(f)
        rx, ry = fy, -fx
        for sgn in (1.0, -1.0):                                      # башни по краям фасада
            t = _off(la, lo, fx * w * 0.38 + sgn * rx * w * 0.28, fy * w * 0.38 + sgn * ry * w * 0.28)
            out.append((t[0], t[1], g, t[0], t[1], g + 0.68 * H, 4.0, wall))
            out.append((t[0], t[1], g + 0.68 * H, t[0], t[1], g + 0.88 * H, 2.2, wall))
            out.append((t[0], t[1], g + 0.88 * H, t[0], t[1], g + H, 0.8, wall))
    else:                                                            # стела: сужается кверху
        r0 = max(1.5, 0.06 * H)
        for k in range(4):
            out.append((la, lo, g + base + (H - base) * k / 4, la, lo, g + base + (H - base) * (k + 1) / 4,
                        r0 * (1.0 - 0.2 * k), "stone"))
    return out


def near(objs, lat, lon, km):
    kx = 111.32 * math.cos(math.radians(lat))
    return [m for m in objs if math.hypot((m["lat"] - lat) * 110.54, (m["lon"] - lon) * kx) <= km]


def part_polys(objs=None):
    """Контуры частей ориентиров, которые камера рисует сама (части S3DB, без своей модели) -- [(контур, ())]: дома
    Overture с центром внутри не рисуются (иначе коробка здания закрывает купола и ярусы)."""
    out = []
    for m in drawn(load_all() if objs is None else objs):
        for q in m.get("parts") or []:
            if len(q) > 7 and len(q[7]) >= 4:
                out.append((tuple(q[7]), ()))
    return out
