"""Совмещение данных карты со снимком подложки -- часть КАРТЫ, а не камеры.

Зачем. Дороги, пути, вода, аэродромы OSM обведены по другим снимкам (ESRI, Bing, Maxar разных лет), дома Overture -- то из
OSM, то найдены нейросетью по снимкам Bing; подложка камеры -- Google. Снимки разных поставщиков сдвинуты друг от друга на
метры, местами на десятки метров (трасса Киев -- Борисполь: ~30 м). Без совмещения асфальт рисуется по лесу рядом с
настоящей трассой, ровный коридор земли лежит не под дорогой, «дом на дороге» не отсеивается.

Как. На вкладке «Карта» при загрузке района (слой «Суміщення зі знімком») снимок качается блоками 4 x 4 тайла зума 16
(~1.5 км, ~1.5 м на пиксель) -- только там, где есть главные дороги, пути или дома. В каждом блоке ищется сдвиг:
  * линий (дороги, пути): «где на снимке похоже на дорогу» (гладко и серо среди пёстрого и цветного) и края полос --
    берётся более уверенный способ, в пределах LINES_SHIFT_MAX_M;
  * домов: края контуров против краёв на снимке, в пределах BLD_SHIFT_MAX_M.
Результат -- файл align_v1_<поставщик снимков>.json рядом с кэшем ЛЭП и ориентиров: {блок: [dx, dy, q, bdx, bdy, bq]}
(метры на восток и север). Загрузчики сдвигают геометрию при загрузке (между блоками -- плавно, билинейно), и все
потребители -- камера, проверка удара, вкладка «Карта» -- получают уже совмещённые данные. Скачанный кэш не меняется:
поменяли снимки или правило -- пересчитали совмещение, данные OSM перекачивать не нужно."""
from __future__ import annotations

import json
import math
import os
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


ALIGN_VERSION = 1
ALIGN_Z = 16                     # зум снимков совмещения (тот же, что у ближнего слоя камеры)
ALIGN_BLOCK = 4                  # блок = ALIGN_BLOCK x ALIGN_BLOCK тайлов (~1.5 км)
LINES_SHIFT_MAX_M = 40.0         # сдвиг линий ищется в этих пределах (у трасс в лесу встречается 25-35 м)
BLD_SHIFT_MAX_M = 20.0           # сдвиг домов -- в этих
SHIFT_MIN_Q = 1.25               # пик совпадения должен быть во столько раз выше среднего, иначе сдвиг не принимается
LINES_MIN_PX = 1500              # меньше пикселей линий в блоке -- сдвиг линий не оцениваем
BLD_MIN_N = 25                   # меньше домов -- сдвиг домов не оцениваем
NEAR_BLOCKS = 2                  # блок без оценки -- берётся ближайший оценённый не дальше стольких блоков
ALIGN_BUILDINGS = False          # сдвиг домов под снимок: выключен -- от домов нужно только не стоять на дорогах и воде
                                 # (это делает отсев домов); совмещение по домам над городом -- самая долгая часть


# --------------------------------------------------------------------------------------------- координаты блоков
def _tile_xy(lat, lon, z=ALIGN_Z):
    """Дробные координаты тайла (x, y) -- скаляры или массивы numpy."""
    import numpy as np
    n = 1 << z
    lat, lon = np.asarray(lat, dtype=float), np.asarray(lon, dtype=float)
    return (lon + 180.0) / 360.0 * n, (1.0 - np.arcsinh(np.tan(np.radians(lat))) / math.pi) / 2.0 * n


def block_of(lat: float, lon: float) -> Tuple[int, int]:
    x, y = _tile_xy(lat, lon)
    return int(math.floor(float(x) / ALIGN_BLOCK)), int(math.floor(float(y) / ALIGN_BLOCK))


def block_bbox(bx: int, by: int) -> Tuple[float, float, float, float]:
    """(юг, запад, север, восток) блока."""
    n = 1 << ALIGN_Z
    x0, y0 = bx * ALIGN_BLOCK, by * ALIGN_BLOCK
    lon0, lon1 = x0 / n * 360.0 - 180.0, (x0 + ALIGN_BLOCK) / n * 360.0 - 180.0
    lat0 = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y0 + ALIGN_BLOCK) / n))))
    lat1 = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y0 / n))))
    return lat0, lon0, lat1, lon1


def blocks_in_bbox(s: float, w: float, n_: float, e: float) -> List[Tuple[int, int]]:
    bx0, by1 = block_of(s, w)
    bx1, by0 = block_of(n_, e)
    return [(bx, by) for bx in range(bx0, bx1 + 1) for by in range(by0, by1 + 1)]


# --------------------------------------------------------------------------------------------- оценка сдвига
def _box_mean(np, a, r):
    """Среднее по квадрату (2r+1)^2 -- интегральным изображением (без scipy)."""
    p = np.pad(a, r + 1, mode="edge").astype(np.float64)
    c = p.cumsum(0).cumsum(1)
    k = 2 * r + 1
    s = c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]
    return (s / (k * k))[:a.shape[0], :a.shape[1]]


def _peak(np, corr, size, r):
    """Пик взаимной корреляции в окне +-r пикселей: (сдвиг y, сдвиг x с долями пикселя, качество = пик / среднее |окна|)."""
    idx = np.r_[0:r + 1, -r:0]
    win = corr[np.ix_(idx % size, idx % size)]
    iy, ix = np.unravel_index(int(np.argmax(win)), win.shape)
    sy, sx = float(idx[iy]), float(idx[ix])
    base = float(np.mean(np.abs(win)))
    q = float(win[iy, ix]) / base if base > 0 else 0.0
    c0 = corr[int(sy) % size, int(sx) % size]                        # доли пикселя: парабола по соседям пика
    for axis in (0, 1):
        a = corr[(int(sy) - (1 if axis == 0 else 0)) % size, (int(sx) - (1 if axis == 1 else 0)) % size]
        b = corr[(int(sy) + (1 if axis == 0 else 0)) % size, (int(sx) + (1 if axis == 1 else 0)) % size]
        den = a - 2.0 * c0 + b
        off = 0.5 * (a - b) / den if den < 0 else 0.0
        if axis == 0:
            sy += max(-0.5, min(0.5, off))
        else:
            sx += max(-0.5, min(0.5, off))
    return sy, sx, q


def _px_to_m(sx, sy, m_px):
    """Сдвиг в пикселях мозаики (x вправо, y вниз) -> метры (восток, север)."""
    return float(sx * m_px), float(-sy * m_px)


# Вода на снимках Google тёмная (яркость ~10-25), гладкая и почти серая -- для «похоже на дорогу» это идеал: блок у
# Днепра тянуло к воде (Южный мост: линии на 20 м севернее настила, хотя в данных лежали верно). Асфальт светлее
# (50-120): тёмное до DARK_LO -- «не дорога», дальше плавно до DARK_LO + DARK_SPAN.
DARK_LO = 30.0
DARK_SPAN = 25.0
BLOCKS_VERSION = 2                 # 2: тёмная вода -- не дорога; блоки старой версии считаются заново


def water_blocks(np, waters, rivers, min_area_km2: float = 0.05) -> set:
    """Блоки совмещения у воды: по которым идут реки и берега водоёмов (больше min_area_km2) или чей центр -- в воде.
    Их и только их пересчитывать после исправления «вода -- не дорога» (BLOCKS_VERSION 2)."""
    out = set()

    def mark_line(pts, step=100.0):
        la, lo = densify(np, [q[0] for q in pts], [q[1] for q in pts], step)
        for a, b in zip(la.tolist(), lo.tolist()):
            out.add(block_of(a, b))
    for _w, pts in rivers:
        if len(pts) >= 2:
            mark_line(pts)
    for ring, _holes in waters:
        if len(ring) < 3:
            continue
        la = np.array([q[0] for q in ring], float)
        lo = np.array([q[1] for q in ring], float)
        kx = 111320.0 * math.cos(math.radians(float(la.mean())))
        area = 0.5 * abs(float(np.dot(lo * kx, np.roll(la, 1) * 110540.0) - np.dot(la * 110540.0, np.roll(lo, 1) * kx))) / 1e6
        if area < min_area_km2:
            continue
        mark_line(ring)
        for bk in blocks_in_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max())):
            s_, w_, n_, e_ = block_bbox(*bk)
            cy, cx = (s_ + n_) / 2.0, (w_ + e_) / 2.0
            j = np.roll(np.arange(len(la)), 1)
            cross = ((la > cy) != (la[j] > cy)) & (cx < (lo[j] - lo) * (cy - la) / np.where(la[j] - la == 0, 1e-12, la[j] - la) + lo)
            if np.count_nonzero(cross) % 2 == 1:
                out.add(bk)
    return out


def estimate_lines_shift(np, rgb, z, tx0, ty0, roads, rails, lat_c, polys=()):
    """Сдвиг линий OSM под снимок: (dx, dy, качество) в метрах (восток, север) -- или None.
    rgb -- мозаика (size x size x 3, uint8), её левый верхний угол -- тайл (tx0, ty0) зума z.
    roads -- [(класс, ширина, точки (lat, lon))], rails -- [(широты, долготы)].
    Два способа, берётся более уверенный: 1) «где на снимке похоже на дорогу» -- асфальт гладкий и серый, лес и поля
    пёстрые и цветные (работает и в лесу); 2) края нарисованных полос против краёв на снимке (хорош в городе)."""
    from PIL import Image, ImageDraw
    size = rgb.shape[0]
    n = 1 << z
    rgb = np.asarray(rgb, dtype=np.float32)
    img = rgb.mean(-1)
    m_px = 40075016.7 * math.cos(math.radians(lat_c)) / (n * 256.0)
    fill = Image.new("L", (size, size), 0)
    mask = Image.new("L", (size, size), 0)
    df, d = ImageDraw.Draw(fill), ImageDraw.Draw(mask)

    def px(lat, lon):
        x, y = _tile_xy(lat, lon, z)
        return (x - tx0) * 256.0, (y - ty0) * 256.0
    for _cls, width, pts in roads:
        if len(pts) >= 2:
            x, y = px([q[0] for q in pts], [q[1] for q in pts])
            seg = list(zip(x.tolist(), y.tolist()))
            w = max(2, int(round(width / m_px)))
            d.line(seg, fill=255, width=w, joint="curve")
            df.line(seg, fill=255, width=w, joint="curve")
    for la, lo in rails:
        if len(la) >= 2:
            x, y = px(la, lo)
            d.line(list(zip(x.tolist(), y.tolist())), fill=255, width=max(2, int(round(3.4 / m_px))), joint="curve")
    for ring in polys:                                               # перроны аэродромов: бетон -- залитой площадью
        if len(ring) >= 3:
            x, y = px([q[0] for q in ring], [q[1] for q in ring])
            pr = list(zip(x.tolist(), y.tolist()))
            d.polygon(pr, fill=255)
            df.polygon(pr, fill=255)
    M = np.asarray(mask, dtype=np.float32) / 255.0
    if M.sum() < LINES_MIN_PX:
        return None
    r = max(2, int(math.ceil(LINES_SHIFT_MAX_M / m_px)))
    cands = []
    F = np.asarray(fill, dtype=np.float32) / 255.0
    if F.sum() >= LINES_MIN_PX:                                      # способ 1: где на снимке «похоже на дорогу»
        k = max(1, int(round(2.5 / m_px)))
        mu = _box_mean(np, img, k)
        sd = np.sqrt(np.maximum(_box_mean(np, img * img, k) - mu * mu, 0.0))
        sat = rgb.max(-1) - rgb.min(-1)
        R = np.exp(-sd / 12.0) * np.exp(-sat / 40.0)                 # гладко и серо -> 1, пёстро или цветно -> 0
        R *= np.clip((img - DARK_LO) / DARK_SPAN, 0.0, 1.0)          # тёмная вода гладкая и серая -- но не дорога
        R -= R.mean()
        Fz = F - F.mean()
        c1 = np.fft.irfft2(np.fft.rfft2(R) * np.conj(np.fft.rfft2(Fz)), s=R.shape)
        cands.append(_peak(np, c1, size, r) + (c1,))
    gy_, gx_ = np.gradient(img)                                      # способ 2: края
    G = np.hypot(gx_, gy_)
    G -= G.mean()
    my, mx = np.gradient(M)
    E = np.hypot(mx, my)
    c2 = np.fft.irfft2(np.fft.rfft2(G) * np.conj(np.fft.rfft2(E)), s=G.shape)
    cands.append(_peak(np, c2, size, r) + (c2,))
    sy, sx, q, corr = max(cands, key=lambda c: c[2])
    if q < SHIFT_MIN_Q:
        return None
    # вдоль прямой дороги совпадение одинаково при любом сдвиге (ничего не держит): если линии блока идут почти в одну
    # сторону И пик корреляции вдоль них плоский (нет перрона, перекрёстка, конца полосы), берётся только сдвиг ПОПЕРЁК,
    # вдоль -- ноль (иначе пик вдоль дороги случайный)
    jxx, jyy, jxy = float((mx * mx).sum()), float((my * my).sum()), float((mx * my).sum())
    tr, det = jxx + jyy, jxx * jyy - jxy * jxy
    if tr > 0:
        l1 = tr / 2 + math.sqrt(max(tr * tr / 4 - det, 0.0))
        l2 = tr - l1
        if l2 < 0.15 * l1:                                           # одно направление: нормаль -- собственный вектор l1
            nx_, ny_ = (jxy, l1 - jxx) if abs(jxy) > 1e-9 else ((1.0, 0.0) if jxx >= jyy else (0.0, 1.0))
            nn = math.hypot(nx_, ny_)
            nx_, ny_ = nx_ / nn, ny_ / nn
            tx_, ty_ = -ny_, nx_                                     # вдоль линий
            peak = corr[int(round(sy)) % size, int(round(sx)) % size]
            along = [corr[int(round(sy + k * ty_)) % size, int(round(sx + k * tx_)) % size]
                     for k in range(-r, r + 1) if abs(k) >= max(3, r // 4)]
            if not along or max(along) >= peak - 0.15 * (peak - float(np.median(along))):   # вдоль -- плато, не пик
                p_ = sx * nx_ + sy * ny_
                sx, sy = p_ * nx_, p_ * ny_
    dx, dy = _px_to_m(sx, sy, m_px)
    return dx, dy, q


def estimate_building_shift(np, rgb, z, tx0, ty0, rings, lat_c):
    """Сдвиг домов под снимок: (dx, dy, качество, домов) в метрах -- или None. rings -- [(широты, долготы)] контуров."""
    from PIL import Image, ImageDraw
    size = rgb.shape[0]
    n = 1 << z
    img = np.asarray(rgb, dtype=np.float32).mean(-1)
    gy_, gx_ = np.gradient(img)
    G = np.hypot(gx_, gy_)
    G -= G.mean()
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    used = 0
    for la, lo in rings:
        x, y = _tile_xy(la, lo, z)
        px_, py_ = (x - tx0) * 256.0, (y - ty0) * 256.0
        if px_.max() < 0 or py_.max() < 0 or px_.min() > size or py_.min() > size:
            continue
        pts = list(zip(px_.tolist(), py_.tolist()))
        draw.line(pts + pts[:1], fill=255, width=1)
        used += 1
    if used < BLD_MIN_N:
        return None
    M = np.asarray(mask, dtype=np.float32) / 255.0
    corr = np.fft.irfft2(np.fft.rfft2(G) * np.conj(np.fft.rfft2(M)), s=G.shape)
    m_px = 40075016.7 * math.cos(math.radians(lat_c)) / (n * 256.0)
    sy, sx, q = _peak(np, corr, size, max(2, int(math.ceil(BLD_SHIFT_MAX_M / m_px))))
    if q < SHIFT_MIN_Q:
        return None
    dx, dy = _px_to_m(sx, sy, m_px)
    return dx, dy, q, used


# --------------------------------------------------------------------------------------------- хранение и применение
CONSIST_M = 8.0                    # сдвиг блока дальше медианы соседей -- ложный пик (настоящий сдвиг снимка плавный)
CONSIST_R = 2                      # соседи -- в стольких блоках вокруг
CONSIST_MIN_N = 3                  # меньше соседей с оценкой -- блок одинокий ...
ISOLATED_MAX_M = 10.0              # ... и ему верим, только если сдвиг небольшой


def consistent_blocks(blocks) -> set:
    """Блоки с ложным сдвигом линий: несогласные с соседями. Снимок Google смещён относительно OSM плавно -- на метры,
    одинаково на километры вокруг. А ложный пик корреляции (настил моста, берег, тень) даёт 20-40 м в случайную
    сторону, и у соседнего блока -- в другую (Южный мост: соседи +28.6/+32.9, -21.5/+31.4, -3.9/-28.6, а рядом
    +1.0/-0.4, +0.6/+0.1). Такой блок не используется -- линии берут сдвиг от согласных соседей. Два прохода: после
    первого медиана считается уже без отброшенных."""
    import numpy as np
    bad = set()
    for _pass in range(2):
        new = set()
        for k, v in blocks.items():
            if v[2] <= 0:
                continue
            nb = [blocks[(k[0] + a, k[1] + b)] for a in range(-CONSIST_R, CONSIST_R + 1) for b in range(-CONSIST_R, CONSIST_R + 1)
                  if (a or b) and (k[0] + a, k[1] + b) in blocks and blocks[(k[0] + a, k[1] + b)][2] > 0
                  and (k[0] + a, k[1] + b) not in bad]
            if len(nb) >= CONSIST_MIN_N:
                mx, my = float(np.median([n[0] for n in nb])), float(np.median([n[1] for n in nb]))
                if math.hypot(v[0] - mx, v[1] - my) > CONSIST_M:
                    new.add(k)
            elif math.hypot(v[0], v[1]) > ISOLATED_MAX_M:
                new.add(k)
        bad = new
    return bad


class AlignMap:
    """Сдвиги по блокам: {(bx, by): (dx, dy, q, bdx, bdy, bq)}; q = 0 -- блок проверен, оценки нет (мало дорог или
    неуверенно). shift(lat, lon, kind) -- сдвиг в точках (между блоками -- билинейно по оценённым)."""

    def __init__(self, folder: Optional[str] = None, provider: str = "google_satellite"):
        self.folder, self.provider = folder, provider
        self.blocks: Dict[Tuple[int, int], Tuple[float, float, float, float, float, float]] = {}
        self.runways: List[Tuple[float, ...]] = []                   # подогнанные ВПП: (исходные концы 4, новые концы 4, ширина, q)
        self.fine = FineField()                                      # тонкая подгонка дорог, путей и мостов (поверх блоков)
        self.fine_blocks: set = set()                                # блоки, для которых тонкая подгонка уже посчитана
        self.blocks_old = False                                      # блоки посчитаны старой версией (см. BLOCKS_VERSION)
        self.mtime = None
        if folder:
            self.load()

    @property
    def path(self) -> Optional[str]:
        return os.path.join(self.folder, f"align_v{ALIGN_VERSION}_{self.provider}.json") if self.folder else None

    def load(self) -> "AlignMap":
        p = self.path
        try:
            with open(p, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self.blocks = {tuple(map(int, k.split("_"))): tuple(float(x) for x in v) for k, v in raw.get("blocks", {}).items()}
            self.runways = [tuple(float(x) for x in r) for r in raw.get("runways", [])]
            self.fine = FineField(raw.get("fine", []))
            self.fine_blocks = {tuple(map(int, k.split("_"))) for k in raw.get("fine_blocks", [])}
            if int(raw.get("fine_version", 1)) != FINE_VERSION:          # подгонка старой версии: «Карта» досчитает заново
                self.fine_blocks = set()
            # блоки старой версии: у воды их тянуло к воде -- «Карта» пересчитает только блоки у рек и водоёмов
            self.blocks_old = int(raw.get("blocks_version", 1)) != BLOCKS_VERSION
            self.mtime = os.path.getmtime(p)
        except (OSError, ValueError, TypeError, AttributeError):
            self.blocks, self.runways, self.fine, self.fine_blocks = {}, [], FineField(), set()
        return self

    def changed_on_disk(self) -> bool:
        try:
            return self.path is not None and os.path.getmtime(self.path) != self.mtime
        except OSError:
            return False

    def save(self) -> None:
        if not self.folder:
            return
        os.makedirs(self.folder, exist_ok=True)
        data = {"version": ALIGN_VERSION, "provider": self.provider, "z": ALIGN_Z, "block": ALIGN_BLOCK,
                "saved": time.strftime("%Y-%m-%d %H:%M:%S"),
                "blocks": {f"{k[0]}_{k[1]}": [round(x, 2) for x in v] for k, v in self.blocks.items()},
                "runways": [[round(x, 8) for x in r] for r in self.runways],
                "fine": self.fine.rows(), "fine_blocks": [f"{k[0]}_{k[1]}" for k in sorted(self.fine_blocks)],
                "fine_version": FINE_VERSION, "blocks_version": BLOCKS_VERSION - (1 if self.blocks_old else 0)}
        tmp = self.path + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, self.path)
        self.mtime = os.path.getmtime(self.path)

    def __len__(self):
        return sum(1 for v in self.blocks.values() if v[2] > 0 or v[5] > 0) + len(self.runways) + (1 if len(self.fine) else 0)

    SIG_MARGIN_DEG = 0.03             # подпись клетки: и соседние блоки (согласованность блоков смотрит на соседей)

    def cell_sig(self, s: float, w: float, n: float, e: float) -> str:
        """Подпись совмещения в рамке (юг, запад, север, восток): сдвиги блоков, ВПП и точки тонкой подгонки рядом.
        Модель земли клетки хранит её и устаревает, только когда совмещение изменилось ИМЕННО там (а не где угодно --
        раньше любая докачка в другом районе делала устаревшей всю модель)."""
        import hashlib
        import numpy as np
        m = self.SIG_MARGIN_DEG
        s, w, n, e = s - m, w - m, n + m, e + m
        h = hashlib.md5()
        for k in sorted(self.blocks):
            bs, bw, bn, be = block_bbox(*k)
            if bn >= s and bs <= n and be >= w and bw <= e:
                h.update(f"{k}:{','.join(f'{x:.2f}' for x in self.blocks[k])};".encode())
        for r in self.runways:
            if any(s <= r[a] <= n and w <= r[a + 1] <= e for a in (0, 2)):
                h.update(",".join(f"{x:.6f}" for x in r).encode())
        if len(self.fine):
            sel = (self.fine.lat >= s) & (self.fine.lat <= n) & (self.fine.lon >= w) & (self.fine.lon <= e)
            if sel.any():
                a = np.round(np.stack([self.fine.lat[sel], self.fine.lon[sel], self.fine.dn[sel], self.fine.de[sel]], -1), 5)
                h.update(np.ascontiguousarray(a[np.lexsort((a[:, 1], a[:, 0]))]).tobytes())
        return h.hexdigest()

    def apply_runways(self, elements: List[dict]) -> List[dict]:
        """ВПП, подогнанные по снимку: линия -- по найденным концам, ширина -- тегом; такой элемент больше не сдвигается."""
        if not self.runways:
            return elements
        out = []
        for el in elements:
            tags = el.get("tags") or {}
            if el.get("type") == "way" and tags.get("aeroway") == "runway" and el.get("geometry"):
                pts = [(g["lat"], g["lon"]) for g in el["geometry"]]
                f = _match_runway(self.runways, pts)
                if f is not None:
                    el = dict(el, geometry=[{"lat": f[4], "lon": f[5]}, {"lat": f[6], "lon": f[7]}],
                              tags=dict(tags, width=f"{f[8]:.0f}"), _fitted=True)
            out.append(el)
        return out

    @property
    def eff(self):
        """Блоки, которыми совмещаются линии: несогласные с соседями (consistent_blocks) -- без оценки (q = 0)."""
        key = (id(self.blocks), len(self.blocks), getattr(self, "_eff_n", None))
        if getattr(self, "_eff_key", None) != key:
            bad = consistent_blocks(self.blocks)
            self.rejected = bad
            self._eff = {k: ((0.0, 0.0, 0.0) + tuple(v[3:]) if k in bad else v) for k, v in self.blocks.items()}
            self._eff_key = key
        return self._eff

    def _eff_arrays(self):
        """eff массивами для поиска searchsorted: (ключи int64 по возрастанию, значения (n, 6))."""
        import numpy as np
        eff = self.eff
        if getattr(self, "_effa_src", None) is not eff:
            if eff:
                items = sorted((_bkey(i, j), v) for (i, j), v in eff.items())
                k = np.array([a for a, _v in items], np.int64)
                v = np.array([tuple(b[:6]) + (0.0,) * (6 - len(b[:6])) for _a, b in items], float)
            else:
                k, v = np.zeros(0, np.int64), np.zeros((0, 6))
            self._effa, self._effa_src = (k, v), eff
        return self._effa

    def shift(self, lat, lon, kind: str = "lines"):
        """Сдвиг (dx, dy) в метрах для точек: kind "lines" -- дороги, пути, вода, аэродромы, ЛЭП; "buildings" -- дома.
        Билинейно между центрами соседних оценённых блоков (вес -- и уверенность); вокруг ни одного -- ближайший
        оценённый в пределах NEAR_BLOCKS, иначе 0."""
        import numpy as np
        lat, lon = np.atleast_1d(np.asarray(lat, dtype=float)), np.atleast_1d(np.asarray(lon, dtype=float))
        dx, dy = np.zeros(lat.shape), np.zeros(lat.shape)
        if not self.blocks or not lat.size:
            return dx, dy
        o = 0 if kind == "lines" else 3
        x, y = _tile_xy(lat, lon)
        fx, fy = x / ALIGN_BLOCK - 0.5, y / ALIGN_BLOCK - 0.5
        i0, j0 = np.floor(fx).astype(np.int64), np.floor(fy).astype(np.int64)
        tx, ty = fx - i0, fy - j0
        wsum = np.zeros(lat.shape)
        ek, ev = self._eff_arrays()
        for di, dj, w in ((0, 0, (1 - tx) * (1 - ty)), (1, 0, tx * (1 - ty)), (0, 1, (1 - tx) * ty), (1, 1, tx * ty)):
            vals = _lookup(np, ek, ev, i0 + di, j0 + dj)[:, o:o + 3]    # массивами: раньше -- словарь по точке (миллионы
            ok = vals[:, 2] > 0                                            # точек -- секунды процессора и рывки окна)
            ww = w * ok * np.minimum(vals[:, 2], 3.0)
            dx += ww * vals[:, 0]
            dy += ww * vals[:, 1]
            wsum += ww
        good = wsum > 0
        dx[good] /= wsum[good]
        dy[good] /= wsum[good]
        for k in np.nonzero(~good)[0]:                               # рядом нет оценок -- ближайший оценённый блок
            bi, bj = int(math.floor(x[k] / ALIGN_BLOCK)), int(math.floor(y[k] / ALIGN_BLOCK))
            best = None
            for r in range(1, NEAR_BLOCKS + 1):
                for a in range(-r, r + 1):
                    for b in range(-r, r + 1):
                        v = self.eff.get((bi + a, bj + b))
                        if v is not None and v[o + 2] > 0 and (best is None or a * a + b * b < best[0]):
                            best = (a * a + b * b, v[o], v[o + 1])
                if best is not None:
                    break
            if best is not None:
                dx[k], dy[k] = best[1], best[2]
        return dx, dy

    def shift_points(self, pts, kind: str = "lines"):
        """[(lat, lon), ...] -> сдвинутые (тем же типом списка кортежей)."""
        if not self.blocks or not pts:
            return pts
        la = [p[0] for p in pts]
        lo = [p[1] for p in pts]
        nla, nlo = self.apply(la, lo, kind)
        return [(a, b) for a, b in zip(nla.tolist(), nlo.tolist())]

    def apply(self, lat, lon, kind: str = "lines"):
        """Сдвинутые широты и долготы (массивы)."""
        import numpy as np
        lat, lon = np.atleast_1d(np.asarray(lat, dtype=float)), np.atleast_1d(np.asarray(lon, dtype=float))
        dx, dy = self.shift(lat, lon, kind)
        return lat + dy / 110540.0, lon + dx / (111320.0 * np.cos(np.radians(lat)))

    def shift_elements(self, elements: List[dict], kind: str = "lines") -> List[dict]:
        """Элементы вида OSM (way с geometry, node, ovt_water с outer/inner) -> копии со сдвинутой геометрией.
        Все линии и точки -- ОДНИМ вызовом сдвига и тонкой подгонки (раньше -- по элементу: на десятках тысяч дорог
        района это минуты процессора, и окно программы дёргалось). Результат тот же."""
        import numpy as np
        if not self.blocks and not len(self.fine):
            return elements
        fine = kind == "lines" and len(self.fine) > 0
        out = list(elements)
        ways, nodes, waters = [], [], []
        for n, el in enumerate(elements):
            if el.get("_fitted"):                                    # подогнана по снимку -- уже на месте
                continue
            t = el.get("type")
            try:
                if t == "way" and el.get("geometry"):
                    g = el["geometry"]
                    ways.append((n, [float(p["lat"]) for p in g], [float(p["lon"]) for p in g]))
                elif t == "node" and "lat" in el:
                    nodes.append((n, float(el["lat"]), float(el["lon"])))
                elif t == "ovt_water" and el.get("outer"):
                    waters.append(n)
            except (KeyError, TypeError, ValueError):
                pass
        if nodes:
            nla, nlo = self.apply([q[1] for q in nodes], [q[2] for q in nodes], kind)
            for (n, _a, _b), x, y in zip(nodes, nla.tolist(), nlo.tolist()):
                out[n] = dict(elements[n], lat=x, lon=y)
        if ways:
            cnt = np.array([len(w[1]) for w in ways])
            off = np.concatenate([[0], np.cumsum(cnt)])
            la = np.concatenate([np.asarray(w[1], float) for w in ways])
            lo = np.concatenate([np.asarray(w[2], float) for w in ways])
            sla, slo = self.apply(la, lo, kind)
            res = [(sla[off[k]:off[k + 1]], slo[off[k]:off[k + 1]]) for k in range(len(ways))]
            if fine:
                pick = []
                for k, (n, wl, wo) in enumerate(ways):
                    tags = elements[n].get("tags") or {}
                    if (tags.get("highway") or tags.get("railway")) and cnt[k] >= 2 and (wl[0], wo[0]) != (wl[-1], wo[-1]):
                        pick.append(k)
                if pick:
                    fl = self.fine.apply_lines([res[k][0] for k in pick], [res[k][1] for k in pick])
                    for k, (fla, flo) in zip(pick, fl):
                        nla, nlo = res[k]
                        if len(fla) != len(nla) or (fla != nla).any() or (flo != nlo).any():
                            res[k] = (fla, flo)
            for (n, _wl, _wo), (nla, nlo) in zip(ways, res):
                out[n] = dict(elements[n], geometry=[{"lat": x, "lon": y} for x, y in zip(nla.tolist(), nlo.tolist())])
        for n in waters:
            el = elements[n]
            try:
                def ring(r):
                    nla, nlo = self.apply([p[0] for p in r], [p[1] for p in r], kind)
                    return [[x, y] for x, y in zip(nla.tolist(), nlo.tolist())]
                out[n] = dict(el, outer=ring(el["outer"]), inner=[ring(r) for r in el.get("inner", []) if len(r)])
            except (KeyError, TypeError, ValueError, IndexError):
                pass
        return out

    def shift_cell(self, cell):
        """Дома (BuildingCell) -> та же ячейка со сдвинутыми контурами (каждый дом -- по сдвигу в его центре)."""
        import numpy as np
        if not ALIGN_BUILDINGS or not self.blocks or cell is None or not len(cell):
            return cell                                              # сдвиг домов выключен (старые оценки в файле не действуют)
        bb = cell.bbox
        clat, clon = (bb[:, 0] + bb[:, 2]) / 2.0, (bb[:, 1] + bb[:, 3]) / 2.0
        dx, dy = self.shift(clat, clon, "buildings")
        if not (np.any(dx) or np.any(dy)):
            return cell
        counts = np.diff(cell.offsets)
        vdx, vdy = np.repeat(dx, counts), np.repeat(dy, counts)
        lat = cell.lat + vdy / 110540.0
        lon = cell.lon + vdx / (111320.0 * np.cos(np.radians(cell.lat)))
        return type(cell)(lat, lon, cell.offsets, cell.height, cell.assumed)


# --------------------------------------------------------------------------------------------- расчёт блока (вкладка «Карта»)
def align_block(np, get_tile, bx: int, by: int, roads, rails, rings, polys=()) -> Tuple[float, float, float, float, float, float]:
    """Один блок: мозаика 4 x 4 тайлов зума 16 (get_tile(z, x, y) -> байты или None) и оценки сдвигов.
    Возвращает (dx, dy, q, bdx, bdy, bq); q = 0 -- оценки нет."""
    from PIL import Image
    import io
    size = ALIGN_BLOCK * 256
    tx0, ty0 = bx * ALIGN_BLOCK, by * ALIGN_BLOCK
    mosaic = Image.new("RGB", (size, size), (0, 0, 0))
    got = 0
    for i in range(ALIGN_BLOCK):
        for j in range(ALIGN_BLOCK):
            data = get_tile(ALIGN_Z, tx0 + i, ty0 + j)
            if not data:
                continue
            try:
                im = Image.open(io.BytesIO(data)).convert("RGB")
                if im.size != (256, 256):
                    im = im.resize((256, 256))
                mosaic.paste(im, (i * 256, j * 256))
                got += 1
            except Exception:
                pass
    if got < ALIGN_BLOCK * ALIGN_BLOCK // 2:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    rgb = np.asarray(mosaic, dtype=np.uint8)
    s, w, n_, e = block_bbox(bx, by)
    lat_c = (s + n_) / 2.0
    lr = estimate_lines_shift(np, rgb, ALIGN_Z, tx0, ty0, roads, rails, lat_c, polys) if (roads or rails or polys) else None
    br = estimate_building_shift(np, rgb, ALIGN_Z, tx0, ty0, rings, lat_c) if rings else None
    return ((lr[0], lr[1], lr[2]) if lr else (0.0, 0.0, 0.0)) + ((br[0], br[1], br[2]) if br else (0.0, 0.0, 0.0))


def index_by_block(roads, rails, rings):
    """Раскладка данных по блокам (с соседями -- линия у края блока видна и из соседнего):
    {блок: (дороги, пути, дома, есть ли в самом блоке линии)}.
    Чтобы блок рисовал только своё, а не все дороги района (и было ясно, в каких блоках есть что совмещать)."""
    import numpy as np
    idx: Dict[Tuple[int, int], Tuple[list, list, list]] = {}

    def blocks(la, lo):
        x, y = _tile_xy(la, lo)
        bx, by = np.floor(x / ALIGN_BLOCK).astype(np.int64), np.floor(y / ALIGN_BLOCK).astype(np.int64)
        return set(zip(bx.tolist(), by.tolist()))

    own = set()                                                      # блоки, где линии лежат сами (а не у соседа)

    def put(keys, slot, item):
        own.update(keys)
        for k in keys:
            for a in (-1, 0, 1):
                for b in (-1, 0, 1):
                    idx.setdefault((k[0] + a, k[1] + b), ([], [], []))[slot].append(item)
    for r in roads:
        pts = r[2]
        if len(pts) >= 2:
            put(blocks([p[0] for p in pts], [p[1] for p in pts]), 0, r)
    for la, lo in rails:
        if len(la) >= 2:
            put(blocks(la, lo), 1, (la, lo))
    for la, lo in rings:
        if len(la):
            k = blocks(la[:1], lo[:1])
            for kk in k:
                idx.setdefault(kk, ([], [], []))[2].append((la, lo))
    for k in list(idx):
        r_, l_, b_ = idx[k]
        idx[k] = (r_, l_, b_, k in own)
    return idx


def block_has_data(entry) -> bool:
    """Есть ли в блоке что совмещать (тогда стоит качать снимки): запись index_by_block."""
    _roads, _rails, rings, own = entry
    return bool(own) or len(rings) >= BLD_MIN_N


def index_buildings(cells, cap: int = 2000):
    """Дома по блокам: {блок: [(широты, долготы) контура]} -- массивно по центрам, не больше cap на блок."""
    import numpy as np
    out: Dict[Tuple[int, int], list] = {}
    for cell in cells:
        if cell is None or not len(cell):
            continue
        bb = cell.bbox
        x, y = _tile_xy((bb[:, 0] + bb[:, 2]) / 2.0, (bb[:, 1] + bb[:, 3]) / 2.0)
        bx, by = np.floor(x / ALIGN_BLOCK).astype(np.int64), np.floor(y / ALIGN_BLOCK).astype(np.int64)
        order = np.lexsort((by, bx))
        keys = np.stack([bx[order], by[order]], -1)
        if not len(keys):
            continue
        cut = np.nonzero(np.any(np.diff(keys, axis=0) != 0, axis=1))[0] + 1
        for grp in np.split(order, cut):
            k = (int(bx[grp[0]]), int(by[grp[0]]))
            lst = out.setdefault(k, [])
            for i in grp[:max(0, cap - len(lst))].tolist():
                lst.append(cell.ring(int(i)))
    return out


def run_align(np, amap: AlignMap, blocks, roads, rails, bld_cells, get_tile, progress=None, cancel=None, save_every: int = 20,
              aprons=(), runways=(), fine_lines_in=(), fine_progress=None):
    """Совмещение блоков района: где есть что совмещать -- снимок и оценка, где нечего -- отметка «проверен».
    roads -- дороги и полосы аэродромов (вид, ширина, точки); aprons -- контуры перронов.
    progress(сделано, всего, оценено) -- для строки хода; cancel() -> True -- остановиться (сделанное сохранится)."""
    import numpy as _np
    idx = index_by_block(roads, rails, [])
    bidx = index_buildings(bld_cells) if ALIGN_BUILDINGS else {}
    pidx: Dict[Tuple[int, int], list] = {}                           # перроны -- по блокам (с соседями), свои блоки -- «есть что»
    pown = set()
    for ring in aprons:
        if len(ring) < 3:
            continue
        x, y = _tile_xy([q[0] for q in ring], [q[1] for q in ring])
        ks = set(zip(_np.floor(x / ALIGN_BLOCK).astype(int).tolist(), _np.floor(y / ALIGN_BLOCK).astype(int).tolist()))
        pown.update(ks)
        for k in ks:
            for a in (-1, 0, 1):
                for b in (-1, 0, 1):
                    pidx.setdefault((k[0] + a, k[1] + b), []).append(ring)
    todo = [k for k in blocks if k not in amap.blocks]               # посчитанные блоки не трогаем
    done = found = 0
    for k in todo:
        if cancel is not None and cancel():
            break
        e = idx.get(k, ([], [], [], False))
        rings = bidx.get(k, []) if ALIGN_BUILDINGS else []
        polys = pidx.get(k, [])
        if e[3] or k in pown or len(rings) >= BLD_MIN_N:
            try:
                res = align_block(np, get_tile, k[0], k[1], e[0], e[1], rings, polys)
            except Exception:
                res = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        else:
            res = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)                    # нечего совмещать -- блок проверен, снимки не качались
        amap.blocks[k] = tuple(float(v) for v in res)
        amap._eff_key = None                                         # согласованность блоков -- пересчитать
        done += 1
        found += 1 if (res[2] > 0 or res[5] > 0) else 0
        if progress is not None:
            progress(done, len(todo), found)
        if done % save_every == 0:
            amap.save()
    for pts in runways:                                              # ВПП -- подогнать по снимку (ширина, концы, ось)
        if cancel is not None and cancel():
            break
        if len(pts) < 2 or _match_runway(amap.runways, pts) is not None:
            continue
        try:
            f = fit_runway(np, get_tile, pts)
        except Exception:
            f = None
        if f is not None:
            amap.runways.append((pts[0][0], pts[0][1], pts[-1][0], pts[-1][1]) + tuple(f))
            found += 1
    # тонкая подгонка линий -- после блоков (по уже сдвинутым линиям); заново, если блоки добавились или её ещё нет
    # ТОЛЬКО блоки этого района без тонкой подгонки; точки других районов и готовых блоков остаются как есть (раньше
    # любая новая клетка пересчитывала всё и заменяла поле целиком -- терялись поправки других районов)
    new_fb = {k for k in blocks if k in amap.blocks and k not in amap.fine_blocks}
    if fine_lines_in and new_fb and not (cancel is not None and cancel()):
        amap.save()
        near = {(k[0] + a, k[1] + b) for k in new_fb for a in (-1, 0, 1) for b in (-1, 0, 1)}
        shifted = []
        for ln in fine_lines_in:                                     # (ширина, широты, долготы[, поиск м -- у мостов])
            w, la, lo = ln[0], ln[1], ln[2]
            if len(la) < 2:
                continue
            x, y = _tile_xy(la, lo)
            ks = set(zip(_np.floor(x / ALIGN_BLOCK).astype(int).tolist(), _np.floor(y / ALIGN_BLOCK).astype(int).tolist()))
            if not ks & near:
                continue                                             # линия не касается новых блоков -- её поправки готовы
            nla, nlo = amap.apply(la, lo)
            shifted.append((float(w), nla, nlo) + tuple(ln[3:4]))
        ff = fine_lines(np, shifted, get_tile, progress=fine_progress, cancel=cancel)
        if not (cancel is not None and cancel()):
            def blk(f):
                if not len(f):
                    return _np.zeros(0, bool)
                x, y = _tile_xy(f.lat, f.lon)
                bx, by = _np.floor(x / ALIGN_BLOCK).astype(int), _np.floor(y / ALIGN_BLOCK).astype(int)
                return _np.array([(int(a), int(b)) in new_fb for a, b in zip(bx, by)], bool)
            old, new = amap.fine.rows(), ff.rows()
            m_old, m_new = blk(amap.fine), blk(ff)
            amap.fine = FineField([r for r, inb in zip(old, m_old) if not inb] + [r for r, inb in zip(new, m_new) if inb])
            amap.fine_blocks |= new_fb
    amap.save()
    return done, found


# --------------------------------------------------------------------------------------------- тонкая подгонка линий
# Сдвиг блока (~1.5 км) двигает всю сеть блока как одну картинку, и форма линии остаётся как в OSM: поворот, обведённый
# хордами, середина моста, кусок трассы, обведённый по другому снимку, -- лежат в 5-15 м от себя на снимке (Южный мост,
# трасса на Борисполь в лесу). Поэтому после блоков -- второй шаг, по каждой линии: точки через FINE_STEP_M, в каждой --
# поиск ПОПЕРЁК линии (до FINE_MAX_M), где на снимке её полотно: пара краёв на ширине полотна (асфальт в лесу, настил над
# водой) и «похоже на дорогу» внутри. Неуверенные точки не участвуют; поправки сглаживаются вдоль линии и «притягиваются»
# к нулю там, где уверенных точек мало (остаётся сдвиг блока). Итог -- поле смещений (точка, смещение, направление
# линии); при загрузке дороги, пути и мосты сгущаются до FINE_STEP_M и каждая точка сдвигается по ближайшим точкам поля
# той же линии (по направлению: переезд и развязка не тянут чужую дорогу).
FINE_VERSION = 3                   # 3: по согласованным блокам (consistent_blocks); старше -- досчитать заново
FINE_STEP_M = 20.0                 # шаг точек вдоль линии (и сгущения геометрии при загрузке)
FINE_MAX_M = 12.0                  # поправка поперёк -- не больше (дальше -- соседняя дорога, а не эта)
FINE_ALONG_M = 15.0                # снимок вдоль линии усредняется на ±столько (одна точка -- полоса 30 м)
FINE_T_STEP_M = 0.75               # шаг поперёк
FINE_MIN_S = 5.5                   # сила пика (в «шумах» профиля) -- меньше: точка неуверенная (пёстрый лес без дороги: до ~5)
FINE_MIN_MARGIN = 3.0              # пик выше второго (дальше ширины полотна) хотя бы на столько -- иначе две похожие полосы
FINE_SIGMA_M = 60.0                # сглаживание поправок вдоль линии (гаусс): самое длинное -- где поправка ровная
FINE_SIGMA_MIN_M = 15.0            # ... самое короткое -- где поправка быстро меняется (поворот, концы моста)
FINE_ADAPT_TOL_M = 1.0             # окно -- такое, на котором поправка меняется не больше чем на столько
FINE_PRIOR = 0.3                  # «вес нуля»: одиночная уверенная точка сдвигает на ~3/4, несколько подряд -- почти полностью
FINE_AGREE_M = 60.0                # точку подтверждают соседи в ±столько м вдоль линии ...
FINE_AGREE_TOL_M = 2.0             # ... с поправкой не дальше этого ...
FINE_AGREE_N = 2                   # ... не меньше стольких (в пёстром лесу случайные пики не согласуются)
FINE_OUTLIER_M = 3.0               # точка дальше медианы соседей (±100 м) -- выброс
FINE_APPLY_R_M = 25.0              # точка геометрии берёт поправку от точек поля не дальше этого
FINE_DIR_COS = 0.85                # ... и только той же линии по направлению (|cos| угла)
FINE_GRID_DEG = 0.0003             # ячейка индекса поля (~30 м)
FINE_RAIL_W_M = 6.0                # полотно одного пути на снимке (балласт)


def _bkey(i, j):
    """Номер блока (i, j) -> один int64 (для поиска массивами)."""
    return ((int(i) + (1 << 30)) << 32) | ((int(j) + (1 << 30)) & 0xFFFFFFFF)


def _lookup(np, keys, vals, i, j):
    """Значения блоков (i, j) массивами; нет блока -- нули."""
    q = ((np.asarray(i, np.int64) + (1 << 30)) << 32) | ((np.asarray(j, np.int64) + (1 << 30)) & 0xFFFFFFFF)
    out = np.zeros((len(q), vals.shape[1] if vals.ndim == 2 else 6))
    if not len(keys):
        return out
    pos = np.clip(np.searchsorted(keys, q), 0, len(keys) - 1)
    hit = keys[pos] == q
    out[hit] = vals[pos[hit]]
    return out


def _grad_lines(np, v, off):
    """np.gradient по каждой линии [off[k], off[k+1]) (длина >= 2) -- массивами."""
    g = np.empty_like(v)
    n = len(v)
    if n == 0:
        return g
    nxt = np.minimum(np.arange(n) + 1, n - 1)
    prv = np.maximum(np.arange(n) - 1, 0)
    g[:] = (v[nxt] - v[prv]) / 2.0
    first, lastp = off[:-1], off[1:] - 1
    g[first] = v[first + 1] - v[first]
    g[lastp] = v[lastp] - v[lastp - 1]
    return g


def densify_many(np, lats, lons, step=FINE_STEP_M):
    """densify для многих линий сразу (массивами, без цикла по отрезкам) -> [(широты, долготы)], то же, что densify."""
    out = [None] * len(lats)
    idx = [k for k in range(len(lats)) if len(lats[k]) >= 2]
    for k in range(len(lats)):
        if len(lats[k]) < 2:
            out[k] = (np.asarray(lats[k], float), np.asarray(lons[k], float))
    if not idx:
        return out
    LA = [np.asarray(lats[k], float) for k in idx]
    LO = [np.asarray(lons[k], float) for k in idx]
    cnt = np.array([len(a) for a in LA])
    la, lo = np.concatenate(LA), np.concatenate(LO)
    start = np.concatenate([[0], np.cumsum(cnt)[:-1]])
    lid = np.repeat(np.arange(len(idx)), cnt)
    mlat = np.add.reduceat(la, start) / cnt
    kx = 111320.0 * np.cos(np.radians(mlat))
    last = np.zeros(len(la), bool)
    last[start + cnt - 1] = True
    seg = np.nonzero(~last)[0]                                       # отрезок i -> i+1 внутри линии
    d = np.hypot((la[seg + 1] - la[seg]) * 110540.0, (lo[seg + 1] - lo[seg]) * kx[lid[seg]])
    n = np.maximum(1, np.ceil(d / step).astype(np.int64))
    seg_line = lid[seg]
    rep = np.repeat(np.arange(len(seg)), n)
    t = (np.arange(len(rep)) - np.repeat(np.cumsum(n) - n, n) + 1) / np.repeat(n, n)
    pla = la[seg][rep] + (la[seg + 1] - la[seg])[rep] * t
    plo = lo[seg][rep] + (lo[seg + 1] - lo[seg])[rep] * t
    per_line = np.bincount(seg_line, weights=n, minlength=len(idx)).astype(np.int64)
    off = np.concatenate([[0], np.cumsum(per_line)])
    for q, k in enumerate(idx):
        a, b = off[q], off[q + 1]
        s0 = start[q]
        out[k] = (np.concatenate([la[s0:s0 + 1], pla[a:b]]), np.concatenate([lo[s0:s0 + 1], plo[a:b]]))
    return out


def densify(np, lat, lon, step=FINE_STEP_M):
    """Точки линии через ~step м (исходные вершины сохраняются): (широты, долготы)."""
    lat, lon = np.asarray(lat, float), np.asarray(lon, float)
    if len(lat) < 2:
        return lat, lon
    kx = 111320.0 * math.cos(math.radians(float(lat.mean())))
    out_la, out_lo = [lat[:1]], [lon[:1]]
    for i in range(len(lat) - 1):
        d = math.hypot((lat[i + 1] - lat[i]) * 110540.0, (lon[i + 1] - lon[i]) * kx)
        n = max(1, int(math.ceil(d / step)))
        t = np.arange(1, n + 1) / n
        out_la.append(lat[i] + (lat[i + 1] - lat[i]) * t)
        out_lo.append(lon[i] + (lon[i + 1] - lon[i]) * t)
    return np.concatenate(out_la), np.concatenate(out_lo)


def _resample(np, lat, lon, step=FINE_STEP_M):
    """Ровно через step м вдоль линии: (широты, долготы, s м, направление east, north)."""
    lat, lon = np.asarray(lat, float), np.asarray(lon, float)
    kx = 111320.0 * math.cos(math.radians(float(lat.mean())))
    x, y = (lon - lon[0]) * kx, (lat - lat[0]) * 110540.0
    seg = np.hypot(np.diff(x), np.diff(y))
    s = np.concatenate([[0.0], np.cumsum(seg)])
    if s[-1] < 1.0:
        return None
    ss = np.arange(0.0, s[-1] + 1e-6, step)
    if ss[-1] < s[-1] - 0.3 * step:
        ss = np.append(ss, s[-1])
    if len(ss) < 3:                                                  # кусок короче ~2 шагов -- подгонять нечего
        return None
    xs, ys = np.interp(ss, s, x), np.interp(ss, s, y)
    ux, uy = np.gradient(xs), np.gradient(ys)
    nn = np.maximum(np.hypot(ux, uy), 1e-9)
    return lat[0] + ys / 110540.0, lon[0] + xs / kx, ss, ux / nn, uy / nn


def _road_like(np, rgb, m_px):
    """«Похоже на дорогу» (гладко и серо -> 1) -- как у сдвига блока."""
    img = rgb.mean(-1)
    k = max(1, int(round(2.5 / m_px)))
    mu = _box_mean(np, img, k)
    sd = np.sqrt(np.maximum(_box_mean(np, img * img, k) - mu * mu, 0.0))
    sat = rgb.max(-1) - rgb.min(-1)
    return np.exp(-sd / 12.0) * np.exp(-sat / 40.0) * np.clip((img - DARK_LO) / DARK_SPAN, 0.0, 1.0)


def _bilinear(np, A, px, py):
    H, W = A.shape
    x0 = np.clip(np.floor(px).astype(int), 0, W - 2)
    y0 = np.clip(np.floor(py).astype(int), 0, H - 2)
    fx, fy = np.clip(px - x0, 0, 1), np.clip(py - y0, 0, 1)
    return (A[y0, x0] * (1 - fx) * (1 - fy) + A[y0, x0 + 1] * fx * (1 - fy)
            + A[y0 + 1, x0] * (1 - fx) * fy + A[y0 + 1, x0 + 1] * fx * fy)


def estimate_line_offsets(np, gray, rl, z, tx0, ty0, lat, lon, ux, uy, width, dbg=None, maxoff=FINE_MAX_M):
    """Поправка поперёк линии в каждой точке (м, «влево» = к нормали (-uy, ux)) и уверенность: (смещение, годна).
    gray, rl -- яркость и «похоже на дорогу» мозаики, её угол -- тайл (tx0, ty0) зума z."""
    lat, lon = np.asarray(lat, float), np.asarray(lon, float)
    n = len(lat)
    h = max(2.0, width / 2.0)
    T = maxoff + h + 6.0
    a = np.arange(-FINE_ALONG_M, FINE_ALONG_M + 1e-6, 1.5)
    t = np.arange(-T, T + 1e-6, FINE_T_STEP_M)
    kx = 111320.0 * np.cos(np.radians(lat))
    vx, vy = -uy, ux
    E = (a[None, :, None] * ux[:, None, None] + t[None, None, :] * vx[:, None, None])        # м на восток
    N = (a[None, :, None] * uy[:, None, None] + t[None, None, :] * vy[:, None, None])        # м на север
    pla = lat[:, None, None] + N / 110540.0
    plo = lon[:, None, None] + E / kx[:, None, None]
    px, py = _tile_xy(pla, plo, z)
    px, py = (px - tx0) * 256.0, (py - ty0) * 256.0
    P = _bilinear(np, gray, px, py).mean(1)                          # (n, nt): поперечный профиль яркости
    R = _bilinear(np, rl, px, py).mean(1)                            # ... и «похоже на дорогу»
    dP = np.abs(np.gradient(P, axis=1))
    dP = (dP + np.roll(dP, 1, axis=1) + np.roll(dP, -1, axis=1)) / 3.0
    offs = np.arange(-maxoff, maxoff + 1e-6, FINE_T_STEP_M)
    hi = int(round(h / FINE_T_STEP_M))
    band = max(2, int(round(4.0 / FINE_T_STEP_M)))
    c0 = int(round(T / FINE_T_STEP_M))
    oi = c0 + np.round(offs / FINE_T_STEP_M).astype(int)
    se = dP[:, oi - hi] + dP[:, oi + hi]                             # пара краёв полотна
    cs = np.concatenate([np.zeros((n, 1)), np.cumsum(R, axis=1)], axis=1)
    seg = lambda i0, i1: (cs[:, i1] - cs[:, i0]) / np.maximum(i1 - i0, 1)
    inner = seg(oi - hi + 1, oi + hi)
    outer = 0.5 * (seg(oi - hi - band, oi - hi) + seg(oi + hi + 1, oi + hi + 1 + band))
    sb = inner - outer                                               # внутри «похоже на дорогу» больше, чем по краям

    def z_(s):
        med = np.median(s, axis=1, keepdims=True)
        mad = np.median(np.abs(s - med), axis=1, keepdims=True) * 1.4826
        return (s - med) / (mad + 1e-3 + 0.05 * np.abs(med))
    S = z_(se) + np.clip(z_(sb), -3.0, None)
    best = np.argmax(S, axis=1)
    sb_ = S[np.arange(n), best]
    far = np.abs(offs[None, :] - offs[best][:, None]) >= h + 3.0
    second = np.where(far, S, -1e9).max(axis=1)
    ok = (sb_ >= FINE_MIN_S) & (sb_ - second >= FINE_MIN_MARGIN) & (np.abs(offs[best]) < maxoff - FINE_T_STEP_M)   # на краю поиска -- не пик
    if dbg is not None:
        dbg.update(S=S, offs=offs, best=sb_, second=second)
    return offs[best], ok


def smooth_offsets(np, s, o, ok):
    """Поправки вдоль линии: выбросы прочь, гаусс по уверенным, «притяжение к нулю» там, где их мало."""
    o = np.where(ok, o, 0.0).astype(float)
    good = ok.copy()
    idx = np.flatnonzero(good)
    for i in idx:                                                    # согласие соседей: случайный пик в пёстром лесу
        nb = idx[(np.abs(s[idx] - s[i]) <= FINE_AGREE_M) & (idx != i)]   # соседи не подтверждают -- не верим
        if np.count_nonzero(np.abs(o[nb] - o[i]) <= FINE_AGREE_TOL_M) < FINE_AGREE_N:
            good[i] = False
    idx = np.flatnonzero(good)
    for i in idx:                                                    # выбросы: дальше медианы соседей
        nb = idx[(np.abs(s[idx] - s[i]) <= 100.0) & (idx != i)]
        if len(nb) >= 2 and abs(o[i] - float(np.median(o[nb]))) > FINE_OUTLIER_M:
            good[i] = False
    if not good.any():
        return np.zeros(len(s)), np.zeros(len(s))
    # длина сглаживания -- по месту: где поправка меняется быстро (поворот хордами, концы моста), окно короче и линия
    # успевает за формой; где ровная (прямая трасса, сдвинутая целиком) -- длиннее и гасит шум. Окно -- то, на котором
    # поправка меняется не больше чем на FINE_ADAPT_TOL_M
    gs, go = s[good], o[good]
    grad = np.zeros(len(s))
    for i in range(len(s)):
        nb = np.abs(gs - s[i]) <= 100.0
        if np.count_nonzero(nb) >= 3:
            grad[i] = abs(np.polyfit(gs[nb], go[nb], 1)[0])           # м поправки на м линии
    sig = np.clip(FINE_ADAPT_TOL_M / np.maximum(grad, 1e-6), FINE_SIGMA_MIN_M, FINE_SIGMA_M)
    K = np.exp(-0.5 * ((s[:, None] - s[None, good]) / sig[:, None]) ** 2)
    wsum = K.sum(1)
    f = (K * o[None, good]).sum(1) / (wsum + FINE_PRIOR)
    return f, wsum


class FineField:
    """Поле тонких поправок: точки (широта, долгота), смещение (м на север, восток), направление линии (east, north)."""

    def __init__(self, rows=()):
        import numpy as np
        a = np.asarray(list(rows), float).reshape(-1, 6)
        self.lat, self.lon, self.dn, self.de, self.ux, self.uy = (a[:, i].copy() for i in range(6))
        self._index()

    def __len__(self):
        return len(self.lat)

    def rows(self):
        return [[round(float(a), 7), round(float(b), 7), round(float(c), 2), round(float(d), 2), round(float(e), 3),
                 round(float(f), 3)] for a, b, c, d, e, f in zip(self.lat, self.lon, self.dn, self.de, self.ux, self.uy)]

    def _index(self):
        import numpy as np
        self.K = 0
        if not len(self.lat):
            self.keys = np.zeros(0, np.int64)
            return
        k = self._key(np.floor(self.lat / FINE_GRID_DEG).astype(np.int64), np.floor(self.lon / FINE_GRID_DEG).astype(np.int64))
        order = np.argsort(k, kind="stable")
        uk, start, cnt = np.unique(k[order], return_index=True, return_counts=True)
        self.K = int(cnt.max())
        table = np.full((len(uk), self.K), -1, np.int64)
        for r, (s0, c) in enumerate(zip(start.tolist(), cnt.tolist())):
            table[r, :c] = order[s0:s0 + c]
        self.keys, self.table = uk, table

    @staticmethod
    def _key(ci, cj):
        return ci * 4_000_000 + cj

    def displacement(self, lat, lon, dx_dir, dy_dir):
        """Смещение (м на север, восток) точек геометрии с направлением линии (dx_dir, dy_dir) в них."""
        import numpy as np
        lat, lon = np.asarray(lat, float), np.asarray(lon, float)
        dn, de = np.zeros(len(lat)), np.zeros(len(lat))
        if not len(self.lat) or not len(lat):
            return dn, de
        ci, cj = np.floor(lat / FINE_GRID_DEG).astype(np.int64), np.floor(lon / FINE_GRID_DEG).astype(np.int64)
        cand = []
        for a in (-1, 0, 1):
            for b in (-1, 0, 1):
                q = self._key(ci + a, cj + b)
                pos = np.clip(np.searchsorted(self.keys, q), 0, len(self.keys) - 1)
                hit = self.keys[pos] == q
                rows = np.where(hit[:, None], self.table[pos], -1)
                cand.append(rows)
        C = np.concatenate(cand, axis=1)                             # (n, 9K) номера точек поля или -1
        valid = C >= 0
        Ci = np.where(valid, C, 0)
        kx = 111320.0 * np.cos(np.radians(lat))
        d = np.hypot((self.lat[Ci] - lat[:, None]) * 110540.0, (self.lon[Ci] - lon[:, None]) * kx[:, None])
        nn = np.maximum(np.hypot(dx_dir, dy_dir), 1e-9)
        cosang = np.abs(self.ux[Ci] * (dx_dir / nn)[:, None] + self.uy[Ci] * (dy_dir / nn)[:, None])
        use = valid & (d <= FINE_APPLY_R_M) & (cosang >= FINE_DIR_COS)
        w = np.where(use, 1.0 / (d + 5.0), 0.0)
        ws = w.sum(1)
        good = ws > 0
        dn[good] = (w * self.dn[Ci]).sum(1)[good] / ws[good]
        de[good] = (w * self.de[Ci]).sum(1)[good] / ws[good]
        return dn, de

    def apply_lines(self, lats, lons, chunk_cells=4_000_000):
        """Много линий сразу -> [(широты, долготы)] -- как apply_line по каждой, но одним расчётом поля (порциями)."""
        import numpy as np
        if not lats:
            return []
        dens = densify_many(np, lats, lons)
        out = [None] * len(dens)
        todo = [k for k, (la, _lo) in enumerate(dens) if len(la) >= 2 and len(self.lat)]
        todo_set = set(todo)
        for k in range(len(dens)):
            if k not in todo_set:
                out[k] = dens[k]
        if not todo:
            return out
        LA = np.concatenate([dens[k][0] for k in todo])
        LO = np.concatenate([dens[k][1] for k in todo])
        cnt = np.array([len(dens[k][0]) for k in todo])
        off = np.concatenate([[0], np.cumsum(cnt)])
        kx = 111320.0 * np.cos(np.radians(LA))
        X, Y = LO * kx, LA * 110540.0
        GX, GY = _grad_lines(np, X, off), _grad_lines(np, Y, off)    # градиент -- внутри своей линии, как np.gradient
        DN, DE = np.zeros(len(LA)), np.zeros(len(LA))
        step = max(1000, min(50_000, int(chunk_cells // max(1, 9 * self.K))))   # (точек x 9K) -- память на порцию
        for a in range(0, len(LA), step):
            b = min(a + step, len(LA))
            DN[a:b], DE[a:b] = self.displacement(LA[a:b], LO[a:b], GX[a:b], GY[a:b])
            time.sleep(0.003)                                        # фоновый расчёт: отдать процесс окну программы
        for k, i in enumerate(todo):
            a, b = off[k], off[k + 1]
            dn, de = DN[a:b], DE[a:b]
            if not (np.any(dn) or np.any(de)):                       # поля рядом нет -- геометрия как была (не сгущать)
                out[i] = (np.asarray(lats[i], float), np.asarray(lons[i], float))
            else:
                out[i] = (LA[a:b] + dn / 110540.0, LO[a:b] + de / kx[a:b])
        return out

    def apply_line(self, lat, lon):
        """Линия (вершины) -> сгущённая до FINE_STEP_M и сдвинутая по полю: (широты, долготы)."""
        import numpy as np
        la, lo = densify(np, lat, lon)
        if len(la) < 2 or not len(self.lat):
            return la, lo
        kx = 111320.0 * np.cos(np.radians(la))
        gx, gy = np.gradient(lo * kx), np.gradient(la * 110540.0)
        dn, de = self.displacement(la, lo, gx, gy)
        if not (np.any(dn) or np.any(de)):                           # поля рядом нет -- геометрия как была (не сгущать)
            return np.asarray(lat, float), np.asarray(lon, float)
        return la + dn / 110540.0, lo + de / kx


FINE_BRIDGE_MAX_M = 30.0           # мост: поиск настила шире -- над водой спутать не с чем (Южный: связка линий в ~20 м от настила)
BRIDGE_GROUP_M = 40.0              # куски моста (проезжие части, метро) ближе этого и параллельные -- один настил


def bridge_decks(np, bridges):
    """Мосты [(вид, ширина, точки, ...)] -> настилы для тонкой подгонки [(ширина, широты, долготы, поиск м)].
    На широком мосту несколько линий (проезжие части в разные стороны, метро) лежат на ОДНОМ настиле: у отдельной линии
    на снимке нет своих краёв, края -- у настила целиком. Поэтому параллельные куски рядом сводятся в один настил: ось --
    посередине связки, ширина -- от крайней линии до крайней плюс полполотна с каждой стороны."""
    items = []
    for br in bridges:
        pts = br[2]
        if len(pts) < 2:
            continue
        la, lo = np.array([q[0] for q in pts], float), np.array([q[1] for q in pts], float)
        kx = 111320.0 * math.cos(math.radians(float(la.mean())))
        ex, ny = (lo[-1] - lo[0]) * kx, (la[-1] - la[0]) * 110540.0
        L = math.hypot(ex, ny)
        if L < 1.0:
            continue
        items.append({"w": float(br[1]), "la": la, "lo": lo, "u": (ex / L, ny / L), "L": L, "kx": kx,
                      "mid": (float(np.interp(0.5, np.linspace(0, 1, len(la)), la)), float(np.interp(0.5, np.linspace(0, 1, len(lo)), lo)))})
    used = [False] * len(items)
    out = []
    for i in sorted(range(len(items)), key=lambda k: -items[k]["L"]):
        if used[i]:
            continue
        a = items[i]
        used[i] = True
        ux, uy = a["u"]
        offs = [(0.0, a["w"])]
        for j, b in enumerate(items):
            if used[j] or abs(ux * b["u"][0] + uy * b["u"][1]) < 0.95:
                continue
            dx, dy = (b["mid"][1] - a["mid"][1]) * a["kx"], (b["mid"][0] - a["mid"][0]) * 110540.0
            across, along = -uy * dx + ux * dy, ux * dx + uy * dy
            if abs(across) <= BRIDGE_GROUP_M and abs(along) <= 0.5 * a["L"] + 0.5 * b["L"]:
                used[j] = True
                offs.append((across, b["w"]))
        lo_ = min(o - w / 2 for o, w in offs)
        hi_ = max(o + w / 2 for o, w in offs)
        c = (lo_ + hi_) / 2.0                                        # ось настила -- посередине связки
        la_c = a["la"] + (ux * c) / 110540.0
        lo_c = a["lo"] + (-uy * c) / a["kx"]
        out.append((hi_ - lo_, la_c, lo_c, FINE_BRIDGE_MAX_M))
    return out


def fine_lines(np, lines, get_tile, progress=None, cancel=None, dbg=None):
    """Поле тонких поправок для линий [(ширина м, широты, долготы[, наибольшая поправка м])] (уже со сдвигом блоков):
    FineField. Мосты идут сюда настилом целиком (bridge_decks) и с поиском шире: над водой спутать не с чем.
    Снимки -- зума ALIGN_Z блоками ALIGN_BLOCK x ALIGN_BLOCK (тайлы уже в кэше после сдвига блоков)."""
    from PIL import Image
    import io
    res = []                                                         # по линии: (широты, долготы, s, ux, uy, смещение, годна)
    by_block: Dict[Tuple[int, int], list] = {}
    for li, ln in enumerate(lines):
        w, la, lo = ln[0], ln[1], ln[2]
        mx = float(ln[3]) if len(ln) > 3 else FINE_MAX_M
        if len(la) < 2:
            res.append(None)
            continue
        r = _resample(np, la, lo)
        if r is None or len(r[0]) < 2:
            res.append(None)
            continue
        rla, rlo, s, ux, uy = r
        res.append([rla, rlo, s, ux, uy, np.zeros(len(s)), np.zeros(len(s), bool)])
        x, y = _tile_xy(rla, rlo)
        bx, by = np.floor(x / ALIGN_BLOCK).astype(int), np.floor(y / ALIGN_BLOCK).astype(int)
        for k in set(zip(bx.tolist(), by.tolist())):
            sel = np.flatnonzero((bx == k[0]) & (by == k[1]))
            by_block.setdefault(k, []).append((li, sel, w, mx))
    total, done = len(by_block), 0
    for (bx, by), items in by_block.items():
        if cancel is not None and cancel():
            break
        tx0, ty0 = bx * ALIGN_BLOCK - 1, by * ALIGN_BLOCK - 1          # блок + тайл запаса со всех сторон
        nt = ALIGN_BLOCK + 2
        img = Image.new("RGB", (nt * 256, nt * 256), (0, 0, 0))
        got = 0
        for i in range(nt):
            for j in range(nt):
                data = get_tile(ALIGN_Z, tx0 + i, ty0 + j)
                if data:
                    try:
                        img.paste(Image.open(io.BytesIO(data)).convert("RGB").resize((256, 256)), (i * 256, j * 256))
                        got += 1
                    except Exception:
                        pass
        done += 1
        if got >= 0.8 * nt * nt:
            rgb = np.asarray(img, np.float32)
            lat_c = float(res[items[0][0]][0][items[0][1][0]])
            m_px = 40075016.7 * math.cos(math.radians(lat_c)) / ((1 << ALIGN_Z) * 256.0)
            gray, rl = rgb.mean(-1), _road_like(np, rgb, m_px)
            for li, sel, w, mx in items:
                r = res[li]
                o, ok = estimate_line_offsets(np, gray, rl, ALIGN_Z, tx0, ty0, r[0][sel], r[1][sel], r[3][sel], r[4][sel], w,
                                               maxoff=mx)
                r[5][sel], r[6][sel] = o, ok
        if progress is not None:
            progress(done, total)
    rows = []
    for r in res:
        if r is None:
            continue
        la, lo, s, ux, uy, o, ok = r
        f, wsum = smooth_offsets(np, s, o, ok)
        vx, vy = -uy, ux
        keep = wsum > 0.05
        if dbg is not None:
            dbg.setdefault("lines", []).append((la, lo, o, ok, f))
        for i in np.flatnonzero(keep):
            rows.append((la[i], lo[i], f[i] * vy[i], f[i] * vx[i], ux[i], uy[i]))
    return FineField(rows)


# --------------------------------------------------------------------------------------------- подгонка ВПП по снимку
# Совмещение сдвигает геометрию, но не меняет её размеров. У полос OSM/Overture размеры и концы часто не те: ширины
# в Overture нет (берётся по длине), линию рисуют по краю бетона вместе с концевыми полосами, а порог (там, где «зебра»)
# бывает смещён внутрь на сотни метров. Поэтому каждая ВПП подгоняется по снимку отдельно: вдоль её оси снимок
# «разворачивается» в полосу (вдоль x поперёк) и ищется её РАЗМЕТКА -- она одинакова на всех ВПП мира:
#   * боковые линии (белые, ~1 м) -- ось и ширина: пара ярких тонких линий, симметричных относительно оси. Яркость
#     бетона против травы не годится: асфальт бывает темнее травы, обочины светлее, разметка «рябит» (Жуляны);
#   * «зебра» порогов (полосы 1.8 м через 1.8 м вдоль оси, 30 м) -- концы: на снимке зума 18 у концов -- периодичность
#     поперёк оси с шагом ~3.6 м. Нет зума 18 или зебры не видно -- конец по тому, где кончаются боковые линии.
# Разметки не видно (грунтовая или стёртая полоса) -- по контрасту яркости «полоса -- обочина» (в любую сторону).
RWY_W_MIN, RWY_W_MAX = 18.0, 80.0          # ширина ищется в этих пределах
RWY_OFF_MAX = 45.0                          # боковой сдвиг оси -- до стольких метров
RWY_END_MAX = 400.0                         # конец полосы -- до стольких метров от конца линии OSM (в обе стороны)
RWY_STEP_M = 2.0                            # шаг выборки снимка вдоль оси
RWY_T_STEP_M = 1.0                          # шаг поперёк оси
RWY_EDGE_Q = 4.0                            # боковые линии: яркость пика над шумом профиля -- столько «шумов» и больше
RWY_MIN_Q = 0.12                            # без разметки: контраст «полоса -- обочина» (доля яркости), меньше -- не верим
RWY_EDGE_GAP_M = 400.0                      # боковые линии прерываются (рулёжки, стёрто) -- не дольше этого
RWY_ZEBRA_Z = 18                            # зум снимка для «зебры»
RWY_ZEBRA_WIN_M = 160.0                     # «зебра» ищется в стольких метрах от конца по боковым линиям (в обе стороны)
RWY_ZEBRA_Q = 0.25                          # доля энергии поперечного профиля на шаге 3-4.5 м -- больше: «зебра»
RWY_ZEBRA_IN_M = 6.0                        # зебра начинается в 6 м от торца (так её и рисует камера)
RWY_ZEBRA_TRUST_M = 60.0                    # «зебра» дальше этого от конца боковых линий -- не она
RWY_MATCH_M = 3.0                           # подогнанная полоса узнаётся по концам исходной линии с такой точностью


def _mosaic(np, get_tile, z, lats, lons, max_tiles=400):
    """Серая мозаика тайлов, покрывающая точки: (массив, tx0, ty0, получено, всего) или None (слишком много тайлов)."""
    from PIL import Image
    import io
    xs, ys = _tile_xy(lats, lons, z)
    tx0, tx1 = int(math.floor(min(xs))), int(math.floor(max(xs)))
    ty0, ty1 = int(math.floor(min(ys))), int(math.floor(max(ys)))
    total = (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
    if total > max_tiles:
        return None
    img = Image.new("RGB", ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256), (0, 0, 0))
    got = 0
    for i in range(tx0, tx1 + 1):
        for j in range(ty0, ty1 + 1):
            data = get_tile(z, i, j)
            if not data:
                continue
            try:
                img.paste(Image.open(io.BytesIO(data)).convert("RGB").resize((256, 256)), ((i - tx0) * 256, (j - ty0) * 256))
                got += 1
            except Exception:
                pass
    return img, tx0, ty0, got, total


def _unroll(np, gray, z, tx0, ty0, la0, lo0, ux, uy, kx, s, t):
    """Снимок вдоль оси: строки -- s (вдоль, м от начала линии), столбцы -- t (влево от оси, м); билинейно."""
    vx, vy = -uy, ux
    S, T = np.meshgrid(s, t, indexing="ij")
    px, py = _tile_xy(la0 + (S * uy + T * vy) / 110540.0, lo0 + (S * ux + T * vx) / kx, z)
    px, py = (px - tx0) * 256.0, (py - ty0) * 256.0
    H, W = gray.shape
    x0 = np.clip(np.floor(px).astype(int), 0, W - 2)
    y0 = np.clip(np.floor(py).astype(int), 0, H - 2)
    fx, fy = np.clip(px - x0, 0, 1), np.clip(py - y0, 0, 1)
    return (gray[y0, x0] * (1 - fx) * (1 - fy) + gray[y0, x0 + 1] * fx * (1 - fy)
            + gray[y0 + 1, x0] * (1 - fx) * fy + gray[y0 + 1, x0 + 1] * fx * fy)


def _smooth(np, v, k, axis=0):
    """Скользящее среднее ±k отсчётов вдоль оси (края -- повтором)."""
    if k < 1:
        return v
    pad = [(0, 0)] * v.ndim
    pad[axis] = (k, k)
    c = np.cumsum(np.pad(v, pad, mode="edge"), axis=axis, dtype=float)
    c = np.concatenate([np.zeros_like(np.take(c, [0], axis=axis)), c], axis=axis)
    n = v.shape[axis]
    return (np.take(c, np.arange(2 * k + 1, 2 * k + 1 + n), axis=axis) - np.take(c, np.arange(0, n), axis=axis)) / (2 * k + 1)


def _line_score(np, P, k):
    """Тонкая светлая линия: насколько точка ярче точек в k отсчётах по ОБЕ стороны (ступенька яркости -- не линия)."""
    out = np.full(P.shape, -1e9, dtype=float)
    out[..., k:-k] = np.minimum(P[..., k:-k] - P[..., :-2 * k], P[..., k:-k] - P[..., 2 * k:])
    return out


def _runs(np, mask, gap):
    """Отрезки True [(i0, i1)], слитые через промежутки не длиннее gap отсчётов."""
    idx = np.flatnonzero(mask)
    if not idx.size:
        return []
    runs, a, b = [], int(idx[0]), int(idx[0])
    for i in idx[1:]:
        if i - b - 1 <= gap:
            b = int(i)
        else:
            runs.append((a, b))
            a = b = int(i)
    runs.append((a, b))
    return runs


def _zebra_end(np, get_tile, la0, lo0, ux, uy, kx, s_end, c, half, outward, dbg, tag):
    """Уточнить конец по «зебре» (снимок зума 18 у конца): s торца (м по оси) или None. outward -- +1 / -1: куда «наружу»."""
    z = RWY_ZEBRA_Z
    s = np.arange(s_end - RWY_ZEBRA_WIN_M, s_end + RWY_ZEBRA_WIN_M, 0.5)
    tw = max(6.0, half - 3.0)
    t = np.arange(c - tw, c + tw, 0.3)
    corner_s, corner_t = [s[0], s[0], s[-1], s[-1]], [t[0], t[-1], t[0], t[-1]]
    vx, vy = -uy, ux
    m = _mosaic(np, get_tile, z, [la0 + (a * uy + b * vy) / 110540.0 for a, b in zip(corner_s, corner_t)],
                [lo0 + (a * ux + b * vx) / kx for a, b in zip(corner_s, corner_t)], max_tiles=60)
    if m is None or m[3] < 0.7 * m[4]:
        dbg[f"zebra_{tag}"] = "немає знімків зуму 18"
        return None
    img, tx0, ty0, _g, _n = m
    gray = np.asarray(img, np.float32).mean(-1)
    U = _unroll(np, gray, z, tx0, ty0, la0, lo0, ux, uy, kx, s, t)
    U = U - U.mean(1, keepdims=True)
    F = np.abs(np.fft.rfft(U * np.hanning(U.shape[1])[None, :], axis=1)) ** 2
    f = np.fft.rfftfreq(U.shape[1], 0.3)                             # циклы на метр
    band = (f >= 1 / 4.5) & (f <= 1 / 3.0)
    ratio = F[:, band].sum(1) / (F[:, f > 1 / 40.0].sum(1) + 1e-6)
    r = _smooth(np, ratio, 10)                                       # ±5 м
    dbg[f"zebra_{tag}_ratio"] = (s, r)
    k30 = int(30 / 0.5)
    win = _smooth(np, ratio, k30 // 2)                               # среднее по 30 м
    i = int(np.argmax(win))
    if win[i] < RWY_ZEBRA_Q:
        dbg[f"zebra_{tag}"] = f"зебри не видно (найбільше {win[i]:.2f} < {RWY_ZEBRA_Q})"
        return None
    on = r >= 0.5 * win[i]                                           # края зебры: где периодичность падает вдвое
    a = i
    while a > 0 and on[a - 1]:
        a -= 1
    b = i
    while b < len(s) - 1 and on[b + 1]:
        b += 1
    if not 15.0 <= s[b] - s[a] <= 60.0:
        dbg[f"zebra_{tag}"] = f"довжина «зебри» {s[b] - s[a]:.0f} м -- не схоже"
        return None
    outer = float(s[b]) if outward > 0 else float(s[a])
    dbg[f"zebra_{tag}"] = f"зебра {s[a]:.0f}..{s[b]:.0f} м, сила {win[i]:.2f}"
    return outer + outward * RWY_ZEBRA_IN_M


def fit_runway(np, get_tile, pts, z: int = ALIGN_Z, dbg: Optional[dict] = None):
    """Подгонка ВПП (прямая линия pts [(lat, lon), ...], концы -- первая и последняя точка) по снимку:
    (lat0, lon0, lat1, lon1, ширина м, качество) или None. Концы -- пороги (по «зебре»), где она видна.
    Качество: по разметке -- сила боковых линий в «шумах» профиля, без разметки -- контраст яркости (0..1).
    dbg (словарь) -- для диагностики: причина отказа ("why"), развёртка снимка и найденные параметры."""
    if dbg is None:
        dbg = {}
    (la0, lo0), (la1, lo1) = pts[0], pts[-1]
    latc = (la0 + la1) / 2.0
    kx = 111320.0 * math.cos(math.radians(latc))
    ex, ey = (lo1 - lo0) * kx, (la1 - la0) * 110540.0                 # ось в метрах (восток, север)
    L = math.hypot(ex, ey)
    dbg["L"] = L
    if L < 300.0:
        dbg["why"] = f"лінія смуги коротка: {L:.0f} м"
        return None
    ux, uy = ex / L, ey / L                                          # вдоль
    vx, vy = -uy, ux                                                 # поперёк (влево)
    pad = RWY_END_MAX + 100.0
    wide = RWY_W_MAX + RWY_OFF_MAX + 40
    cs_, ct_ = [-pad, -pad, L + pad, L + pad], [-wide, wide, -wide, wide]
    m = _mosaic(np, get_tile, z, [la0 + (a * uy + b * vy) / 110540.0 for a, b in zip(cs_, ct_)],
                [lo0 + (a * ux + b * vx) / kx for a, b in zip(cs_, ct_)])
    if m is None:
        dbg["why"] = "забагато тайлів"
        return None
    mosaic, tx0, ty0, got, total = m
    dbg.update(tx0=tx0, ty0=ty0, z=z, m_px=40075016.7 * math.cos(math.radians(latc)) / ((1 << z) * 256.0),
               tiles=(got, total), mosaic=mosaic)
    if got < total * 0.7:
        dbg["why"] = f"знімків мало: {got} з {total}"
        return None
    gray = np.asarray(mosaic, np.float32).mean(-1)
    s = np.arange(-pad, L + pad, RWY_STEP_M)
    tmax = RWY_W_MAX / 2 + RWY_OFF_MAX + 30.0
    t = np.arange(-tmax, tmax + 1e-6, RWY_T_STEP_M)
    U = _unroll(np, gray, z, tx0, ty0, la0, lo0, ux, uy, kx, s, t)
    inside_s = (s >= 0.2 * L) & (s <= 0.8 * L)
    P = U[inside_s].mean(0)                                          # средний поперечный профиль яркости
    dbg.update(s=s, t=t, U=U, P=P)
    c0 = len(t) // 2
    kl = max(2, int(round(3.0 / RWY_T_STEP_M)))
    oi_max, h_min, h_max = int(RWY_OFF_MAX / RWY_T_STEP_M), int(RWY_W_MIN / 2 / RWY_T_STEP_M), int(RWY_W_MAX / 2 / RWY_T_STEP_M)

    # 1) боковые линии разметки: пара тонких ярких линий c±h
    Lk = _line_score(np, P, kl)
    Lm = np.maximum(np.maximum(Lk, np.roll(Lk, 1)), np.roll(Lk, -1))
    valid = Lk[kl:-kl]
    noise = 1.4826 * float(np.median(np.abs(valid - np.median(valid)))) + 0.5
    best = None
    for oi in range(-oi_max, oi_max + 1):
        for h in range(h_min, h_max + 1):
            a, b = c0 + oi - h, c0 + oi + h
            if a < kl + 1 or b > len(t) - kl - 2:
                continue
            # пара боковых линий; осевая посередине -- в плюс (иначе пара «осевая -- край» равна паре краёв)
            q = (min(Lm[a], Lm[b]) + 0.5 * max(Lm[c0 + oi], 0.0)) / noise - 0.005 * abs(oi)
            if best is None or q > best[0]:
                best = (q, oi, h)
    dbg["edge"] = best
    if best is not None and best[0] >= RWY_EDGE_Q:
        q, oi, h = best
        off, half = oi * RWY_T_STEP_M, h * RWY_T_STEP_M + 1.5         # линия -- в 1.5 м от края полосы
        mode = "розмітка"
        # где боковые линии есть вдоль оси: по строкам, сглаженным на ±20 м
        Us = _smooth(np, U, int(round(20.0 / RWY_STEP_M)), axis=0)
        ca, cb = c0 + oi - h, c0 + oi + h
        ea, eb = (np.max([_line_score(np, Us[:, cc + d - kl: cc + d + kl + 1], kl)[:, kl] for d in (-1, 0, 1)], 0)
                  for cc in (ca, cb))
        lo_, hi_ = np.minimum(ea, eb), np.maximum(ea, eb)
        thr = max(2.0 * noise, 0.4 * float(np.median(lo_[inside_s])))
        thr1 = max(4.0 * noise, 0.6 * float(np.median(hi_[inside_s])))
        present = (lo_ >= thr) | (hi_ >= thr1)                       # у рулёжек одна линия прерывается
        dbg["edge_along"] = (ea, eb, thr)
    else:
        # 2) разметки не видно: полоса по контрасту яркости (в любую сторону)
        cs = np.concatenate([[0.0], np.cumsum(P)])
        seg = lambda a, b: (cs[b] - cs[a]) / max(b - a, 1)
        sh = int(round(12.0 / RWY_T_STEP_M))
        bb = None
        for h in range(h_min, h_max + 1):
            for oi in range(-oi_max, oi_max + 1):
                a, b = c0 + oi - h, c0 + oi + h
                if a - sh < 0 or b + sh > len(t):
                    continue
                inn, out = seg(a, b), 0.5 * (seg(a - sh, a) + seg(b, b + sh))
                q = abs(inn - out) / max(inn, out, 1.0) - 0.002 * abs(oi)
                if bb is None or q > bb[0]:
                    bb = (q, oi, h)
        dbg["contrast"] = bb
        if bb is None or bb[0] < RWY_MIN_Q:
            dbg["why"] = (f"розмітки не видно (бокові лінії {best[0] if best else 0:.1f} < {RWY_EDGE_Q}) і контраст смуги "
                          f"малий ({bb[0] if bb else 0:.3f} < {RWY_MIN_Q})")
            return None
        q, oi, h = bb
        off, half = oi * RWY_T_STEP_M, h * RWY_T_STEP_M
        mode = "контраст"
        a, b = c0 + oi - h, c0 + oi + h
        sgn = 1.0 if seg(a, b) >= 0.5 * (seg(a - sh, a) + seg(b, b + sh)) else -1.0
        row = sgn * (U[:, a:b].mean(1) - 0.5 * (U[:, a - sh:a].mean(1) + U[:, b:b + sh].mean(1)))
        Qs = _smooth(np, row, int(round(30.0 / RWY_STEP_M)))
        present = Qs >= 0.5 * float(np.median(Qs[inside_s]))
    # концы: отрезок «полоса есть», накрывающий середину (с промежутками до RWY_EDGE_GAP_M)
    i_mid = int(np.argmin(np.abs(s - L / 2)))
    run = next(((a, b) for a, b in _runs(np, present, int(RWY_EDGE_GAP_M / RWY_STEP_M)) if a <= i_mid <= b), None)
    if run is None:
        dbg["why"] = f"{mode}: посередині смуги її не видно"
        return None
    s0, s1 = float(s[run[0]]), float(s[run[1]])
    dbg.update(mode=mode, width=2 * half, off=off, q=float(q), s0_lines=s0, s1_lines=s1)
    if s1 - s0 < 0.5 * L:                                            # полоса «кончилась» посреди -- не верим
        dbg["why"] = f"{mode}: смуга уривається -- знайдено {s1 - s0:.0f} м із {L:.0f}"
        return None
    if mode == "розмітка":
        # боковые линии кончаются у «зебры» (рядом с ней их на снимке зума 16 не отличить): торец -- в ~24 м за концом
        # линий (Жуляны: 12 м ошибки, ровная разметка по ИКАО: столько же в другую сторону); точнее -- по самой
        # «зебре» на снимке зума 18, если она там видна и не дальше RWY_ZEBRA_TRUST_M от этой оценки
        ends = []
        for tag, s_e, outward in (("0", s0, -1.0), ("1", s1, 1.0)):
            guess = s_e + outward * (18.0 + RWY_ZEBRA_IN_M)
            try:
                zs = _zebra_end(np, get_tile, la0, lo0, ux, uy, kx, guess, off, half, outward, dbg, tag)
            except Exception as exc:
                zs, dbg[f"zebra_{tag}"] = None, f"{type(exc).__name__}: {exc}"
            if zs is not None and abs(zs - guess) > RWY_ZEBRA_TRUST_M:
                dbg[f"zebra_{tag}"] += f" -- далеко від кінця бокових ліній ({zs - guess:+.0f} м), не беру"
                zs = None
            ends.append(guess if zs is None else zs)
        s0, s1 = ends
    dbg.update(s0=s0, s1=s1)
    p0 = (la0 + (s0 * uy + off * vy) / 110540.0, lo0 + (s0 * ux + off * vx) / kx)
    p1 = (la0 + (s1 * uy + off * vy) / 110540.0, lo0 + (s1 * ux + off * vx) / kx)
    return p0[0], p0[1], p1[0], p1[1], 2 * half, float(q)


def _match_runway(fits, pts):
    """Подогнанная запись для линии полосы pts (по исходным концам, в любом направлении) или None."""
    if not fits or len(pts) < 2:
        return None
    (a0, b0), (a1, b1) = pts[0], pts[-1]
    kx = 111320.0 * math.cos(math.radians(a0))
    near = lambda p, q: math.hypot((p[0] - q[0]) * 110540.0, (p[1] - q[1]) * kx) <= RWY_MATCH_M
    for f in fits:
        o0, o1 = (f[0], f[1]), (f[2], f[3])
        if (near(o0, (a0, b0)) and near(o1, (a1, b1))) or (near(o0, (a1, b1)) and near(o1, (a0, b0))):
            return f
    return None
