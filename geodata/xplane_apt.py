"""Аэродром из X-Plane (apt.dat): полосы (торцы, ширина) и рулёжки (сеть руления ATC) -> запись справочника.

apt.dat X-Plane 12: «Global Scenery/Global Airports/Earth nav data/apt.dat» (все аэродромы мира) или
«Custom Scenery/<пакет>/Earth nav data/apt.dat» (отдельные сценарии). Формат -- строки с кодом в начале:
  1 / 16 / 17  -- заголовок аэродрома: «1 высота_фт 0 0 ICAO название»;
  100          -- полоса: ширина м, ..., затем по торцу: номер, широта, долгота, ... (9 полей на торец);
  1201         -- узел сети руления: широта долгота направление номер;
  1202         -- ребро: узел узел oneway|twoway вид [название]; вид taxiway_A..F (размер по ICAO) или runway.
Перроны и разметка (110 / 120 ...) не читаются."""
from __future__ import annotations

import math
from typing import Optional

TAXI_WIDTH_M = {"A": 7.5, "B": 10.5, "C": 15.0, "D": 18.0, "E": 23.0, "F": 25.0}
TAXI_DEFAULT_M = 18.0
NEAR_M = 5000.0                    # без ICAO: аэродром apt.dat, чья полоса ближе этого к месту строки


def _dist(a, b):
    return math.hypot((a[0] - b[0]) * 110540.0, (a[1] - b[1]) * 111320.0 * math.cos(math.radians(a[0])))


def find_airport(path: str, icao: str = "", near=None) -> Optional[dict]:
    """Найти аэродром в apt.dat: по ICAO, а если ICAO нет -- ближайший к near (шир, долг) не дальше NEAR_M.
    -> {"icao", "name", "elev_m", "runways": [...], "taxiways": [...]} или None."""
    icao = (icao or "").strip().upper()
    best, cur, lines = None, None, None

    def close():
        nonlocal best
        if cur is None:
            return
        if icao and cur["icao"].upper() == icao:
            best = (0.0, cur, lines)
        elif not icao and near is not None and cur["rw"]:
            d = min(_dist(near, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)) for a, b in cur["rw"])
            if d <= NEAR_M and (best is None or d < best[0]):
                best = (d, cur, lines)

    with open(path, encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            c = ln[:5]
            if c.startswith(("1 ", "16 ", "17 ")):
                close()
                if icao and best is not None:
                    break
                t = ln.split(None, 5)
                cur = {"icao": t[4] if len(t) > 4 else "", "name": t[5].strip() if len(t) > 5 else "",
                       "elev_ft": float(t[1]) if len(t) > 1 else 0.0, "rw": []}
                lines = [] if (not icao or cur["icao"].upper() == icao) else None
                continue
            if c.startswith("99"):
                break
            if cur is None:
                continue
            if c.startswith("1302 ") and ln.split()[1:2] == ["icao_code"] and icao:
                code = (ln.split() + [""])[2].upper()           # у малых аэродромов ICAO -- только в метаданных
                if code == icao and lines is None:
                    lines = []
                    cur["icao"] = code
            if lines is None:
                continue
            if c.startswith("100 "):
                t = ln.split()
                try:
                    cur["rw"].append(((float(t[9]), float(t[10])), (float(t[18]), float(t[19]))))
                except (IndexError, ValueError):
                    pass
            if ln.split(None, 1)[:1] and ln.split(None, 1)[0] in KEEP:
                lines.append(ln)
    close()
    if best is None:
        return None
    return _parse(best[1], best[2])


def find_airports(path: str, icaos) -> dict:
    """Несколько аэродромов за один проход файла: {ICAO: запись как у find_airport}; ненайденных нет в ответе."""
    want = {c.strip().upper() for c in icaos if c and c.strip()}
    found, cur, lines = {}, None, None

    def close():
        if cur is not None and lines is not None and cur["icao"].upper() in want:
            found[cur["icao"].upper()] = _parse(cur, lines)

    with open(path, encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            c = ln[:5]
            if c.startswith(("1 ", "16 ", "17 ")):
                close()
                if len(found) == len(want):
                    break
                t = ln.split(None, 5)
                cur = {"icao": t[4] if len(t) > 4 else "", "name": t[5].strip() if len(t) > 5 else "",
                       "elev_ft": float(t[1]) if len(t) > 1 else 0.0, "rw": []}
                lines = [] if cur["icao"].upper() in want else None
                continue
            if c.startswith("99"):
                break
            if cur is None:
                continue
            if c.startswith("1302 ") and lines is None:
                t = ln.split()
                if t[1:2] == ["icao_code"] and len(t) > 2 and t[2].upper() in want:
                    lines, cur["icao"] = [], t[2].upper()
            if lines is not None and ln.split(None, 1)[:1] and ln.split(None, 1)[0] in KEEP:
                lines.append(ln)
    close()
    return found


def locate() -> list:
    """Где лежит общий apt.dat X-Plane 12 (и 11): стандартные папки и все библиотеки Steam на всех дисках."""
    import os
    import re
    import string
    rel = (os.path.join("Global Scenery", "Global Airports", "Earth nav data", "apt.dat"),
           os.path.join("Resources", "default scenery", "default apt dat", "Earth nav data", "apt.dat"))
    roots = []
    drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")] if os.name == "nt" else ["/"]
    for d in drives:
        for sub in ("X-Plane 12", "X-Plane 11", os.path.join("Program Files", "X-Plane 12"),
                    os.path.join("Program Files (x86)", "Steam", "steamapps", "common", "X-Plane 12"),
                    os.path.join("Program Files", "Steam", "steamapps", "common", "X-Plane 12"),
                    os.path.join("SteamLibrary", "steamapps", "common", "X-Plane 12")):
            roots.append(os.path.join(d, sub))
    for vdf in (r"C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf", r"C:\Program Files\Steam\steamapps\libraryfolders.vdf"):
        try:
            with open(vdf, encoding="utf-8", errors="replace") as fh:
                for lib in re.findall(r'"path"\s+"([^"]+)"', fh.read()):
                    roots.append(os.path.join(lib.replace("\\\\", "\\"), "steamapps", "common", "X-Plane 12"))
        except OSError:
            pass
    out = []
    for r in dict.fromkeys(roots):
        for x in rel:
            f = os.path.join(r, x)
            if os.path.isfile(f) and f not in out:
                out.append(f)
    return out


KEEP = {"100", "1201", "1202", "110", "111", "112", "113", "114", "115", "116", "120", "130"}   # код строки, не префикс: «130» бывает без названия
SOFT = {3, 4, 5, 12, 13, 14, 15}     # покрытия «не бетон»: трава, грунт, гравий, дно озера, вода, снег, прозрачное
BEZ_N = 8                           # точек на кривой Безье


def _bezier(a, b):
    """Отрезок контура X-Plane: узел a -> узел b; у узла (шир, долг, контр. шир, контр. долг или None).
    Контрольная точка узла -- «вперёд», назад -- зеркальная. -> точки без последней."""
    p0, p3 = (a[0], a[1]), (b[0], b[1])
    p1 = (a[2], a[3]) if a[2] is not None else p0
    p2 = (2 * b[0] - b[2], 2 * b[1] - b[3]) if b[2] is not None else p3
    if p1 == p0 and p2 == p3:
        return [p0]
    out = []
    for i in range(BEZ_N):
        t = i / BEZ_N
        u = 1 - t
        out.append(tuple(u ** 3 * p0[k] + 3 * u * u * t * p1[k] + 3 * u * t * t * p2[k] + t ** 3 * p3[k] for k in (0, 1)))
    return out


def _ring(nodes):
    pts = []
    for k in range(len(nodes)):
        pts += _bezier(nodes[k], nodes[(k + 1) % len(nodes)])
    pts.append(pts[0])
    return pts


def keyhole(outer, holes):
    """Контур с дырами -> один контур (дыры соединены с внешним «разрезом» нулевой ширины): заливка многоугольника
    оставляет дыры пустыми, а всё остальное в программе (перроны) работает с одним контуром, как раньше."""
    ring = list(outer[:-1])
    for h in holes:
        h = list(h[:-1])
        if len(h) < 3:
            continue
        kx = math.cos(math.radians(h[0][0]))
        best = None
        for i, p in enumerate(ring):
            for j, q in enumerate(h):
                d = (p[0] - q[0]) ** 2 + ((p[1] - q[1]) * kx) ** 2
                if best is None or d < best[0]:
                    best = (d, i, j)
        _d, i, j = best
        loop = h[j:] + h[:j] + [h[j]]
        ring = ring[:i + 1] + loop + ring[i:]
    ring.append(ring[0])
    return ring


def _pavements(lines) -> list:
    """Площадки покрытия (110): бетон/асфальт рулёжек и перронов -> [{"surface", "ring": [[шир, долг], ...]}]."""
    out, cur, rings, ring = [], None, [], []

    def flush():
        if cur is not None and rings and cur not in SOFT:
            r = keyhole(_ring(rings[0]), [_ring(h) for h in rings[1:]])
            out.append({"surface": cur, "ring": [[round(a, 7), round(b, 7)] for a, b in r]})
    for ln in lines:
        t = ln.split()
        c = t[0]
        if c in ("110", "120", "130"):
            flush()
            cur, rings, ring = (int(float(t[1])) if c == "110" and len(t) > 1 else None), [], []
            if c != "110":
                cur = None
            continue
        if cur is None or c not in ("111", "112", "113", "114"):
            continue
        try:
            la, lo = float(t[1]), float(t[2])
            ctl = (float(t[3]), float(t[4])) if c in ("112", "114") else (None, None)
        except (IndexError, ValueError):
            continue
        ring.append((la, lo) + ctl)
        if c in ("113", "114"):
            if len(ring) >= 3:
                rings.append(ring)
            ring = []
    flush()
    return out


def _parse(head, lines) -> dict:
    runways, nodes, edges = [], {}, []
    for ln in lines:
        t = ln.split()
        try:
            if t[0] == "100":
                runways.append({"ref": f"{t[8]}/{t[17]}", "width": round(float(t[1]), 1),
                                "ends": [[float(t[9]), float(t[10])], [float(t[18]), float(t[19])]]})
            elif t[0] == "1201":
                nodes[t[4]] = (float(t[1]), float(t[2]))
            elif t[0] == "1202" and len(t) >= 5 and t[4].startswith("taxiway"):
                size = t[4].split("_")[1] if "_" in t[4] else ""
                edges.append((t[1], t[2], TAXI_WIDTH_M.get(size, TAXI_DEFAULT_M), t[5] if len(t) > 5 else ""))
        except (IndexError, ValueError):
            continue
    return {"icao": head["icao"], "name": head["name"], "elev_m": round(head["elev_ft"] * 0.3048, 1),
            "runways": runways, "taxiways": _chain(nodes, edges), "pavements": _pavements(lines)}


def _chain(nodes, edges) -> list:
    """Рёбра сети руления -> осевые рулёжек: цепочки через узлы, где сходятся ровно два ребра одной ширины и имени."""
    adj = {}
    for k, (a, b, w, nm) in enumerate(edges):
        if a in nodes and b in nodes and a != b:
            adj.setdefault(a, []).append(k)
            adj.setdefault(b, []).append(k)
    used, out = set(), []

    def through(node, k):
        e = adj.get(node, [])
        if len(e) != 2:
            return None
        k2 = e[0] if e[1] == k else e[1]
        if k2 in used or edges[k2][2:] != edges[k][2:]:
            return None
        return k2

    for k, (a, b, w, nm) in enumerate(edges):
        if k in used or a not in nodes or b not in nodes or a == b:
            continue
        used.add(k)
        seq = [a, b]
        for forward in (True, False):                                # продлить в обе стороны
            kk = k
            while True:
                end = seq[-1] if forward else seq[0]
                k2 = through(end, kk)
                if k2 is None:
                    break
                used.add(k2)
                a2, b2 = edges[k2][0], edges[k2][1]
                nxt = b2 if a2 == end else a2
                if forward:
                    seq.append(nxt)
                else:
                    seq.insert(0, nxt)
                kk = k2
        out.append({"ref": nm, "width": w, "pts": [[round(nodes[q][0], 7), round(nodes[q][1], 7)] for q in seq]})
    return out
