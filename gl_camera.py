"""
gl_camera.py -- камера на видеокарте (OpenGL) и отдельное окно HUD.

Один источник картинки на оба экрана
  * Кадр камеры считается в ОДНОМ месте: на видеокарте, если она есть и годится, иначе -- прежним кодом на процессоре.
    Этот кадр со штатным HUD показывается на левом экране и, если включено, в отдельном окне.
  * Видеокарта: отдельный процесс без окна (своё ядро, свой GIL, сбой драйвера не роняет программу). Рабочий поток камеры
    присылает позу и размер кадра, процесс отвечает готовым кадром. Рельеф SRTM -- сетка треугольников, снимки -- текстуры с
    мипмапами, три слоя подробности (~3 / ~12 / ~50 км), дымка, небо, 3D-объекты сцены, тепловизор по той же модели, что
    nose_camera_thermal (ThermalParams): температура земли днём/ночью, небо, атмосфера, шумы матрицы, АРУ, палитры.
  * Проверка железа: процесс сам проверяет moderngl, OpenGL 3.3 и что это настоящая видеокарта, а не программный драйвер.
    Нет -- причина в журнале и строке состояния, кадры дальше считает процессор. Сбой посреди полёта -- то же самое.
  * Отдельное окно (галочка «HUD у окремому окні») -- просмотрщик: тот же кадр + тот же HUD (код hud_view.py «рисует в запись»
    по размеру окна, окно растеризует запись), красная точка, IMPACT. OpenGL ему не нужен: работает и без видеокарты.

Подключение: GlCameraMixin входит в SimulationPageMixin. Страница зовёт _gl_build_toggle(top); камера -- _gpu_start() (старт вида
«Вперед»), _gl_feed() (такт), _gpu_frame() (рабочий поток), _view_offer() (готовый кадр); HUD -- _gl_hud_send(); удар -- _gl_crash().
"""
from __future__ import annotations

import math
import os
import sys
import threading
import time

GL_LEVELS = ((16, 8), (14, 8), (12, 8))   # (зум, тайлов на сторону) -- от ближнего к дальнему; 8 тайлов = текстура 2048 px
GL_GRID = 129                              # вершин на сторону сетки слоя
GL_HFOV_DEG = 48.4                         # как у носовой камеры (Caddx 640)
GL_FAR_M = 60000.0
GL_FOG_M = 18000.0
GL_PROVIDER = "google_satellite"
SUN_AZ_DEG, SUN_EL_DEG = 135.0, 40.0       # условное солнце (юго-восток, 40 градусов) -- подсветка склонов


# =====================================================================================================================
# Главная программа
# =====================================================================================================================


# =====================================================================================================================
# HUD: «холст-запись» (главная программа) и растеризация записи (окно). Общая раскладка текста -- чтобы bbox совпадал.
# =====================================================================================================================
_FONT_CACHE = {}
_FONT_FILES = (("consola.ttf", "consolab.ttf"), ("DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf"),
               ("lucon.ttf", "lucon.ttf"), ("cour.ttf", "courbd.ttf"))


def _pil_font(font):
    """Tk-шрифт (семейство, -размер_px[, "bold"]) -> PIL-шрифт того же размера в пикселях (моно: Consolas / DejaVu Sans Mono / ...)."""
    from PIL import ImageFont
    if isinstance(font, (tuple, list)) and len(font) >= 2:
        px, bold = abs(int(font[1])), "bold" in font[2:]
    else:
        px, bold = 12, False
    key = (max(px, 6), bold)
    f = _FONT_CACHE.get(key)
    if f is None:
        for reg, bld in _FONT_FILES:
            try:
                f = ImageFont.truetype(bld if bold else reg, key[0])
                break
            except OSError:
                continue
        if f is None:
            try:
                f = ImageFont.load_default(key[0])
            except TypeError:
                f = ImageFont.load_default()
        _FONT_CACHE[key] = f
    return f


def _text_layout(x, y, text, font, anchor="center", width=None, justify="left"):
    """Как Tk раскладывает текст: [(x_строки, y_верха, строка), ...], bbox. width -- перенос по словам (пиксели)."""
    f = _pil_font(font)
    lines = []
    for para in str(text).split("\n"):
        if width and f.getlength(para) > width:
            cur = ""
            for word in para.split(" "):
                cand = (cur + " " + word) if cur else word
                if cur and f.getlength(cand) > width:
                    lines.append(cur)
                    cur = word
                else:
                    cur = cand
            lines.append(cur)
        else:
            lines.append(para)
    asc, desc = f.getmetrics()
    lh = asc + desc
    widths = [f.getlength(t) for t in lines]
    bw, bh = (max(widths) if widths else 0.0), lh * len(lines)
    a = anchor or "center"
    left = x - bw / 2 if a in ("center", "n", "s") else (x - bw if "e" in a else x)
    top = y - bh / 2 if a in ("center", "e", "w") else (y - bh if "s" in a else y)
    out = []
    for i, (t, lw) in enumerate(zip(lines, widths)):
        lx = left + ((bw - lw) / 2 if justify == "center" else (bw - lw) if justify == "right" else 0.0)
        out.append((lx, top + i * lh, t))
    return out, (left, top, left + bw, top + bh)


class _HudRecorder:
    """Минимальный «холст» для HudRenderer: create_* только записывают примитивы. find_withtag всегда пуст, поэтому HudRenderer
    каждый раз рисует кадр целиком; bbox текста считается той же раскладкой, что и при растеризации."""

    def __init__(self):
        self.items = []             # [id, вид, координаты, параметры]
        self._next = 1

    def _add(self, kind, coords, kw):
        kw.pop("tags", None)
        i = self._next
        self._next += 1
        flat = []
        for c in coords:
            flat.extend(c if isinstance(c, (list, tuple)) else (c,))
        self.items.append([i, kind, [float(v) for v in flat], kw])
        return i

    def create_text(self, *c, **kw): return self._add("text", c, kw)
    def create_line(self, *c, **kw): return self._add("line", c, kw)
    def create_rectangle(self, *c, **kw): return self._add("rectangle", c, kw)
    def create_polygon(self, *c, **kw): return self._add("polygon", c, kw)
    def create_oval(self, *c, **kw): return self._add("oval", c, kw)

    def find_withtag(self, _tag):
        return ()

    def delete(self, what):
        if isinstance(what, int):
            self.items = [it for it in self.items if it[0] != what]
        else:
            self.items = []

    def coords(self, item, *c):
        for it in self.items:
            if it[0] == item:
                it[2] = [float(v) for v in c]

    def itemconfigure(self, item, **kw):
        for it in self.items:
            if it[0] == item:
                it[3].update(kw)

    def tag_raise(self, *_a):
        pass

    def tag_lower(self, item, below):
        idx = {it[0]: n for n, it in enumerate(self.items)}
        if item in idx and below in idx and idx[item] > idx[below]:
            moved = self.items.pop(idx[item])
            self.items.insert(idx[below], moved)

    def bbox(self, item):
        for it in self.items:
            if it[0] == item and it[1] == "text":
                kw = it[3]
                _lines, bb = _text_layout(it[2][0], it[2][1], kw.get("text", ""), kw.get("font"), kw.get("anchor", "center"),
                                          kw.get("width"), kw.get("justify", "left"))
                return tuple(int(round(v)) for v in bb)
        return None

    def winfo_width(self):
        return 1

    def winfo_height(self):
        return 1

    def snapshot(self):
        return [(k, c, {key: v for key, v in kw.items()}) for _i, k, c, kw in self.items]


def _hud_raster(w, h, items, crash):
    """Запись примитивов -> RGBA (прозрачный фон) того же вида, что Tk рисует в главном окне. crash -- рамка и IMPACT."""
    from PIL import Image, ImageDraw

    def col(v, default):
        v = default if v is None else v
        return None if v in ("", None) else v
    im = Image.new("RGBA", (max(1, w), max(1, h)), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for kind, c, kw in items:
        try:
            wid = max(1, int(round(float(kw.get("width", 1) or 1)))) if kind != "text" else 1
            if kind == "text":
                lines, _bb = _text_layout(c[0], c[1], kw.get("text", ""), kw.get("font"), kw.get("anchor", "center"),
                                          kw.get("width"), kw.get("justify", "left"))
                f, fill = _pil_font(kw.get("font")), col(kw.get("fill"), "black")
                for lx, ty, t in lines:
                    d.text((lx, ty), t, font=f, fill=fill, anchor="la")
            elif kind == "line":
                pts = list(zip(c[0::2], c[1::2]))
                fill, dash = col(kw.get("fill"), "black"), kw.get("dash")
                if fill is None or len(pts) < 2:
                    continue
                if dash:
                    on, off = float(dash[0]), float(dash[1] if len(dash) > 1 else dash[0])
                    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                        L = math.hypot(x1 - x0, y1 - y0)
                        t = 0.0
                        while t < L:
                            t1 = min(t + on, L)
                            d.line([(x0 + (x1 - x0) * t / L, y0 + (y1 - y0) * t / L),
                                    (x0 + (x1 - x0) * t1 / L, y0 + (y1 - y0) * t1 / L)], fill=fill, width=wid)
                            t = t1 + off
                else:
                    d.line(pts, fill=fill, width=wid, joint="curve")
            elif kind in ("rectangle", "oval"):
                x0, y0, x1, y1 = c[:4]
                box = [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
                fill, outline = col(kw.get("fill"), ""), col(kw.get("outline"), "black")
                (d.rectangle if kind == "rectangle" else d.ellipse)(box, fill=fill, outline=outline, width=wid if outline else 0)
            elif kind == "polygon":
                pts = list(zip(c[0::2], c[1::2]))
                if len(pts) >= 3:
                    fill, outline = col(kw.get("fill"), "black"), col(kw.get("outline"), "")
                    d.polygon(pts, fill=fill, outline=outline, width=wid if outline else 0)
        except Exception:
            continue
    if crash:
        red = "#ff2222"
        d.rectangle([3, 3, w - 3, h - 3], outline=red, width=4)
        cx, cy, bw, bh = w / 2, h * 0.22, 260, 56
        d.rectangle([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], fill="#000000", outline=red, width=3)
        lines, _bb = _text_layout(cx, cy, "IMPACT", ("mono", -38, "bold"))
        for lx, ty, t in lines:
            d.text((lx, ty), t, font=_pil_font(("mono", -38, "bold")), fill=red, anchor="la")
    return im


# =====================================================================================================================
# Процесс окна (своё ядро, свой GIL). Ничего из Tk здесь нет.
# =====================================================================================================================
_VS_TERRAIN = """
#version 330
uniform mat4 mvp;
uniform float zbias;      // метры: дальние слои земли чуть ниже ближнего -- на стыке побеждает ближний. Сдвиг в МИРОВЫХ метрах:
                          // прежний сдвиг глубины (нелинейной) на 1 км был ~100 м и прятал под землёй дома, опоры и объекты
in vec3 in_pos; in vec2 in_uv; in float in_shade; in float in_forest;
out vec2 v_uv; out float v_shade; out vec3 v_pos; out float v_forest;
void main() {
    vec3 p = vec3(in_pos.xy, in_pos.z - zbias);
    gl_Position = mvp * vec4(p, 1.0);
    v_uv = in_uv; v_shade = in_shade; v_pos = p; v_forest = in_forest;
}
"""
_FS_TERRAIN = """
#version 330
uniform sampler2D tex;
uniform vec4 hole;          // xmin, ymin, xmax, ymax подробного слоя: здесь грубый не рисуется
uniform vec3 campos; uniform vec3 fogcol; uniform float fogdist;
in vec2 v_uv; in float v_shade; in vec3 v_pos; in float v_forest;
out vec4 frag;
void main() {
    if (v_pos.x > hole.x && v_pos.x < hole.z && v_pos.y > hole.y && v_pos.y < hole.w) discard;
    vec3 c = texture(tex, v_uv).rgb * v_shade * mix(1.0, 0.86, v_forest);     // полог чуть темнее: тень внутри крон
    float k = 1.0 - exp(-length(v_pos - campos) / fogdist);
    frag = vec4(mix(c, fogcol, k), 1.0);
}
"""
_VS_SKY = """
#version 330
out vec2 v_ndc;
void main() {
    vec2 p = vec2((gl_VertexID == 1) ? 3.0 : -1.0, (gl_VertexID == 2) ? 3.0 : -1.0);
    v_ndc = p; gl_Position = vec4(p, 0.0, 1.0);
}
"""
_FS_SKY = """
#version 330
uniform mat4 inv_vp; uniform vec3 fogcol; uniform vec3 zenith;
in vec2 v_ndc; out vec4 frag;
void main() {
    vec4 a = inv_vp * vec4(v_ndc, -1.0, 1.0); vec4 b = inv_vp * vec4(v_ndc, 1.0, 1.0);
    vec3 d = normalize(b.xyz / b.w - a.xyz / a.w);
    frag = vec4(mix(fogcol, zenith, smoothstep(0.0, 0.45, d.z)), 1.0);
}
"""


_VS_OBJ = """
#version 330
uniform mat4 mvp; uniform vec3 offset;      // положение основания объекта: от борта -- той же формулой, что у программы (красная точка совпадает)
in vec3 in_pos; in vec3 in_col; in vec3 in_nrm;
out vec3 v_pos; out vec3 v_col; out vec3 v_nrm;
void main() { vec3 p = in_pos + offset; gl_Position = mvp * vec4(p, 1.0); v_pos = p; v_col = in_col; v_nrm = in_nrm; }
"""
_FS_OBJ = """
#version 330
uniform vec3 campos; uniform vec3 fogcol; uniform float fogdist; uniform vec3 sun;
in vec3 v_pos; in vec3 v_col; in vec3 v_nrm; out vec4 frag;
void main() {
    float shade = 0.45 + 0.55 * abs(dot(normalize(v_nrm), sun));      // грани моделей бывают с любой стороны: свет с обеих
    float k = 1.0 - exp(-length(v_pos - campos) / fogdist);
    frag = vec4(mix(v_col * shade, fogcol, k), 1.0);
}
"""
# --- тепловизор: проход 1 -- «температура» сцены в буфер (R -- температура, G -- уровень объекта или -1) ---
_GLSL_GROUND_T = """
float t_day(vec3 c) {
    float y = dot(c, vec3(0.299, 0.587, 0.114));
    float veg = clamp((c.g - max(c.r, c.b)) * 6.0, 0.0, 1.0);
    float water = clamp((c.b - max(c.r, c.g)) * 6.0, 0.0, 1.0) * (1.0 - y);
    return clamp(0.30 + 0.50 * y - 0.16 * veg - 0.14 * water, 0.0, 1.0);
}
float t_night(vec3 c) {
    float y = dot(c, vec3(0.299, 0.587, 0.114));
    float sat = max(max(c.r, c.g), c.b) - min(min(c.r, c.g), c.b);
    float veg = clamp((c.g - max(c.r, c.b)) * 6.0, 0.0, 1.0);
    float water = clamp((c.b - max(c.r, c.g)) * 6.0, 0.0, 1.0) * (1.0 - y);
    float dark_gray = clamp(1.0 - sat * 5.0, 0.0, 1.0) * clamp(1.0 - 1.6 * y, 0.0, 1.0);
    float bright_bare = clamp(1.5 * y - 0.5, 0.0, 1.0) * (1.0 - veg) * (1.0 - water);
    return clamp(0.40 + 0.14 * water + 0.10 * dark_gray - 0.12 * bright_bare - 0.03 * veg, 0.0, 1.0);
}
"""
_FS_TERRAIN_T = """
#version 330
uniform sampler2D tex; uniform vec4 hole; uniform vec3 campos;
uniform float dayw; uniform float air; uniform float atm;
in vec2 v_uv; in float v_shade; in vec3 v_pos; in float v_forest;
out vec2 frag;
""" + _GLSL_GROUND_T + """
void main() {
    if (v_pos.x > hole.x && v_pos.x < hole.z && v_pos.y > hole.y && v_pos.y < hole.w) discard;
    vec3 c = texture(tex, v_uv).rgb;
    float tg = dayw * t_day(c) + (1.0 - dayw) * t_night(c);
    tg += v_forest * (dayw * -0.05 + (1.0 - dayw) * 0.10);     // лес: днём прохладнее открытой земли, ночью теплее (кроны держат тепло)
    float tau = exp(-length(v_pos - campos) / atm);
    frag = vec2(tg * tau + air * (1.0 - tau), -1.0);
}
"""
_FS_SKY_T = """
#version 330
uniform mat4 inv_vp; uniform float air; uniform float zenith_t; uniform float span_deg;
in vec2 v_ndc; out vec2 frag;
void main() {
    vec4 a = inv_vp * vec4(v_ndc, -1.0, 1.0); vec4 b = inv_vp * vec4(v_ndc, 1.0, 1.0);
    vec3 d = normalize(b.xyz / b.w - a.xyz / a.w);
    float elev = degrees(asin(clamp(d.z, -1.0, 1.0)));
    frag = vec2(air - (air - zenith_t) * clamp(elev / span_deg, 0.0, 1.0), -1.0);
}
"""
_FS_OBJ_T = """
#version 330
uniform vec3 sun; uniform float air; uniform float warm;     // warm >= 0 -- уровень палитры задан (здания), иначе -- по окраске
in vec3 v_pos; in vec3 v_col; in vec3 v_nrm; out vec2 frag;
void main() {                                   // объект -- как в обычной камере: серый тон своей окраски с освещением, через палитру
    float shade = 0.45 + 0.55 * abs(dot(normalize(v_nrm), sun));
    float lvl = clamp(dot(v_col * shade, vec3(0.299, 0.587, 0.114)), 0.0, 1.0);
    frag = vec2(air, warm >= 0.0 ? clamp(warm * (0.85 + 0.15 * shade), 0.0, 1.0) : lvl);
}
"""
# проход 2 -- матрица (размытие, виньетка, шумы), АРУ и палитра
_FS_THERMAL = """
#version 330
uniform sampler2D temp; uniform sampler2D fpn; uniform sampler2D lut;
uniform vec2 texel; uniform float blur; uniform float vignette;
uniform float lo; uniform float span; uniform float n_temporal; uniform float n_row; uniform float frame_no;
in vec2 v_uv; out vec4 frag;
float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
void main() {
    vec2 uv = vec2(v_uv.x, 1.0 - v_uv.y);
    vec2 o = texel * blur;
    float t = 0.4 * texture(temp, uv).r + 0.15 * (texture(temp, uv + vec2(o.x, 0)).r + texture(temp, uv - vec2(o.x, 0)).r
              + texture(temp, uv + vec2(0, o.y)).r + texture(temp, uv - vec2(0, o.y)).r);
    float obj = texelFetch(temp, ivec2(uv / texel), 0).g;
    vec2 q = (uv - 0.5) * 2.0;
    t *= 1.0 - vignette * dot(q, q) / 2.0;
    vec2 px = floor(uv / texel);
    t += texture(fpn, uv).r;
    t += (hash(px + frame_no * 1.37) - 0.5) * 3.4641 * n_temporal;
    t += (hash(vec2(px.y, frame_no * 2.11)) - 0.5) * 3.4641 * n_row;
    float level = obj >= 0.0 ? obj : clamp((t - lo) / span, 0.0, 1.0);
    frag = vec4(texture(lut, vec2(level * 255.0 / 256.0 + 0.5 / 256.0, 0.5)).rgb, 1.0);
}
"""
# выборка для АРУ: каждый STEP-й пиксель буфера температуры (как [::4, ::4] в обычной камере); объекты в АРУ не входят
_FS_AGC = """
#version 330
uniform sampler2D temp; uniform int step;
out vec2 frag;
void main() { vec2 v = texelFetch(temp, ivec2(gl_FragCoord.xy) * step, 0).rg; frag = vec2(v.g >= 0.0 ? -100.0 : v.r, 0.0); }
"""

_VS_HUD = """
#version 330
out vec2 v_uv;
void main() {
    vec2 p = vec2((gl_VertexID == 1) ? 3.0 : -1.0, (gl_VertexID == 2) ? 3.0 : -1.0);
    v_uv = vec2(p.x * 0.5 + 0.5, 1.0 - (p.y * 0.5 + 0.5)); gl_Position = vec4(p, 0.0, 1.0);
}
"""


def _object_arrays(o, base):
    """Модель объекта -> плоские треугольники ОТНОСИТЕЛЬНО точки объекта на карте: позиция (восток, север, высота AMSL), цвет (0..1),
    нормаль грани. Куда поставить точку объекта -- решается каждый кадр (uniform offset)."""
    import numpy as np
    v, f, c = o["v"].astype(np.float64), o["f"], o["c"].astype(np.float32) / 255.0
    a = math.radians(o["az"])
    ca, sa = math.cos(a), math.sin(a)
    north = v[:, 1] * ca - v[:, 0] * sa                 # как scene_project.model_to_ned: перёд модели -- по азимуту
    east = v[:, 1] * sa + v[:, 0] * ca
    P = np.stack([east, north, v[:, 2] + base], 1)
    tri = P[f]                                           # (M, 3, 3)
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    out = np.empty((len(f), 3, 9), dtype="f4")
    out[:, :, 0:3] = tri
    out[:, :, 3:6] = c[:, None, :]
    out[:, :, 6:9] = n[:, None, :]
    return out.reshape(-1, 9)


# =====================================================================================================================
# Препятствия OpenStreetMap (те же данные и размеры, по которым obstacles.py считает столкновение)
# =====================================================================================================================
OBST_COLORS = {"pylon": (150, 152, 158), "mast": (205, 205, 210), "tower": (175, 172, 165), "chimney": (150, 80, 60),
               "crane": (225, 180, 40), "turbine": (235, 236, 238), "wire": (70, 72, 76)}


def _prism(np, cx, cy, z0, z1, r0, r1, sides, rot=0.0):
    """Боковые грани правильной призмы/усечённой пирамиды (sides граней) + крышка: треугольники (N, 3, 3)."""
    a = rot + np.arange(sides) * 2 * math.pi / sides
    b0 = np.stack([cx + r0 * np.cos(a), cy + r0 * np.sin(a), np.full(sides, z0)], 1)
    b1 = np.stack([cx + r1 * np.cos(a), cy + r1 * np.sin(a), np.full(sides, z1)], 1)
    n0, n1 = np.roll(b0, -1, 0), np.roll(b1, -1, 0)
    side = np.concatenate([np.stack([b0, n0, n1], 1), np.stack([b0, n1, b1], 1)])
    c = np.array([cx, cy, z1])
    cap = np.stack([b1, n1, np.broadcast_to(c, b1.shape)], 1)
    return np.concatenate([side, cap])


def _box(np, x0, y0, z0, x1, y1, z1):
    """Параллелепипед по осям (12 треугольников)."""
    v = np.array([[x, y, z] for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)], dtype=np.float64)
    f = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1), (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    return v[np.array(f)]


def _blade(np, cx, cy, hz, length, width, ang):
    """Лопасть ветряка в вертикальной плоскости «восток -- вверх» (направление ветра в OSM нет): вытянутый треугольник."""
    ux, uz = math.cos(ang), math.sin(ang)
    px, pz = -uz * width / 2, ux * width / 2
    tip = (cx + ux * length, cy, hz + uz * length)
    return np.array([[(cx + px, cy, hz + pz), (cx - px, cy, hz - pz), tip]], dtype=np.float64)


def _obstacle_tris(np, kind, x, y, g, h, r, hub, rotor):
    """Треугольники одного точечного препятствия в локальной системе (основание на земле g, верх g + h)."""
    if kind == "pylon":                                    # опора ЛЭП: сужающаяся четырёхгранная башня + траверса
        base = max(2.0 * r, 6.0) / 2.0
        parts = [_prism(np, x, y, g, g + h, base * 1.414, 0.9, 4, math.pi / 4),
                 _box(np, x - 7.0, y - 0.6, g + 0.86 * h, x + 7.0, y + 0.6, g + 0.86 * h + 1.0)]
    elif kind == "turbine":                                # ветряк: башня до ротора, гондола, три лопасти
        hub = hub if hub > 0 else h * 0.75
        rotor = rotor if rotor > 0 else max(h - hub, 20.0)
        parts = [_prism(np, x, y, g, g + hub, 2.4, 1.6, 10), _box(np, x - 2.0, y - 6.0, g + hub - 1.8, x + 2.0, y + 3.0, g + hub + 1.8)]
        parts += [_blade(np, x, y - 6.2, g + hub, rotor, 3.0, math.radians(90 + 120 * i)) for i in range(3)]
    elif kind == "crane":                                  # кран: башня + стрела
        parts = [_box(np, x - 1.2, y - 1.2, g, x + 1.2, y + 1.2, g + h),
                 _box(np, x - 1.0, y - 0.6 * h, g + h - 2.0, x + 1.0, y + 0.2 * h, g + h)]
    elif kind == "chimney":
        rr = max(r, 2.5)
        parts = [_prism(np, x, y, g, g + h, rr * 1.4, rr, 12)]
    elif kind == "tower":
        rr = max(r, 3.0)
        parts = [_prism(np, x, y, g, g + h, rr, rr * 0.8, 12)]
    else:                                                  # мачта и всё прочее высокое и тонкое
        rr = max(r, 1.0)
        parts = [_prism(np, x, y, g, g + h, rr, rr * 0.6, 8)]
    return np.concatenate(parts)


OBST_RADIUS_M = 15000.0      # препятствия строятся вокруг борта в этом радиусе (дальше их скрывает дымка)
OBST_REBUILD_M = 5000.0
NEAR_LINE_SEARCH_M = 20000.0  # строка «найближча ЛЕП» ищет в этом радиусе      # борт ушёл от центра прошлой сборки дальше -- пересборка в фоне


def _obstacle_arrays(np, points, lines, origin, ground_fn, center=None, radius=OBST_RADIUS_M):
    """Препятствия в радиусе от center (локальные м) -> (треугольники (N, 9), линии проводов (M, 9)): позиция, цвет 0..1, нормаль.
    Высота земли для всех опор и всех точек проводов -- ОДНИМ запросом (раньше тысячи отдельных запросов SRTM)."""
    cx, cy = center if center is not None else (0.0, 0.0)
    near = []
    for p in points:
        x, y = origin.xy(p[1], p[2])
        if center is None or (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius:
            near.append((p, x, y))
    k = 8                                                  # точек на пролёт провода
    tt = np.linspace(0.0, 1.0, k + 1)
    spans = []                                             # (sx, sy, la, lo, top, sag, nx, ny)
    for pts, support, band in lines:
        if len(pts) < 2:
            continue
        lat = np.array([q[0] for q in pts])
        lon = np.array([q[1] for q in pts])
        x, y = origin.xy(lat, lon)
        for i in range(len(pts) - 1):
            if center is not None and min((x[i] - cx) ** 2 + (y[i] - cy) ** 2, (x[i + 1] - cx) ** 2 + (y[i + 1] - cy) ** 2) > radius * radius:
                continue
            dx, dy = x[i + 1] - x[i], y[i + 1] - y[i]
            L = max(math.hypot(dx, dy), 1e-6)
            top = support * band[1] * 0.97
            sag = min(support * (band[1] - band[0]), 0.03 * L) * 4.0 * tt * (1.0 - tt)   # парабола: 3% пролёта, не ниже полосы
            spans.append((x[i] + dx * tt, y[i] + dy * tt, lat[i] + (lat[i + 1] - lat[i]) * tt, lon[i] + (lon[i + 1] - lon[i]) * tt,
                          top, sag, -dy / L, dx / L))
    q_lat = np.concatenate([np.array([p[1] for p, _x, _y in near])] + [sp[2] for sp in spans]) if (near or spans) else np.zeros(0)
    q_lon = np.concatenate([np.array([p[2] for p, _x, _y in near])] + [sp[3] for sp in spans]) if (near or spans) else np.zeros(0)
    g_all = ground_fn(q_lat, q_lon) if len(q_lat) else np.zeros(0)
    g_pts, g_lines = g_all[:len(near)], g_all[len(near):]
    tris, tri_cols, wires = [], [], []
    for ((kind, _la, _lo, h, r, hub, rotor), x, y), gz in zip(near, g_pts):
        t = _obstacle_tris(np, kind, x, y, float(gz), h, r, hub, rotor)
        tris.append(t)
        tri_cols.append(np.broadcast_to(np.array(OBST_COLORS.get(kind, OBST_COLORS["mast"]), dtype=np.float32) / 255.0, (len(t), 3)))
    for n, (sx, sy, _la, _lo, top, sag, nx, ny) in enumerate(spans):
        gz = g_lines[n * (k + 1):(n + 1) * (k + 1)]
        for off in (-5.0, 0.0, 5.0):                       # три провода
            pts3 = np.stack([sx + nx * off, sy + ny * off, gz + top - sag], 1)
            wires.append(np.stack([pts3[:-1], pts3[1:]], 1).reshape(-1, 3))

    def pack(pos, col, nrm):
        out = np.empty((len(pos), 9), dtype="f4")
        out[:, 0:3], out[:, 3:6], out[:, 6:9] = pos, col, nrm
        return out
    tri_out = np.zeros((0, 9), "f4")
    if tris:
        T = np.concatenate(tris)
        C = np.concatenate(tri_cols)
        nr = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
        nr /= np.maximum(np.linalg.norm(nr, axis=1, keepdims=True), 1e-12)
        tri_out = pack(T.reshape(-1, 3), np.repeat(C, 3, 0), np.repeat(nr, 3, 0))
    wire_out = np.zeros((0, 9), "f4")
    if wires:
        W = np.concatenate(wires)
        wire_out = pack(W, np.broadcast_to(np.array(OBST_COLORS["wire"], "f4") / 255.0, W.shape),
                        np.broadcast_to(np.array([0.0, 0.0, 1.0], "f4"), W.shape))
    return tri_out, wire_out, len(near), len(spans)


# =====================================================================================================================
# Здания OSM: стены и крыши из контуров (тот же кэш, что пишет BuildingLoader программы)
# =====================================================================================================================
BLD_RADIUS_M = 6000.0        # здания строятся вокруг борта в этом радиусе
BLD_REBUILD_M = 2000.0       # борт ушёл от центра прошлой сборки дальше -- пересборка в фоне
BLD_WALL = (205, 198, 186)
BLD_ROOFS = ((150, 82, 66), (118, 118, 124), (92, 98, 108), (160, 150, 132), (128, 72, 58))


def _ear_clip(np, x, y):
    """Треугольники невыпуклого многоугольника (индексы вершин); на сбое -- веер (лучше, чем дыра в крыше)."""
    n = len(x)
    idx = list(range(n))
    area = 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))
    sign = 1.0 if area > 0 else -1.0
    tris = []
    guard = 0
    while len(idx) > 3 and guard < 4 * n:
        guard += 1
        m = len(idx)
        for k in range(m):
            a, b, c = idx[k - 1], idx[k], idx[(k + 1) % m]
            cross = (x[b] - x[a]) * (y[c] - y[a]) - (y[b] - y[a]) * (x[c] - x[a])
            if cross * sign <= 0:
                continue                                   # вогнутый угол -- не «ухо»
            ok = True
            for o in idx:
                if o in (a, b, c):
                    continue
                d1 = (x[b] - x[a]) * (y[o] - y[a]) - (y[b] - y[a]) * (x[o] - x[a])
                d2 = (x[c] - x[b]) * (y[o] - y[b]) - (y[c] - y[b]) * (x[o] - x[b])
                d3 = (x[a] - x[c]) * (y[o] - y[c]) - (y[a] - y[c]) * (x[o] - x[c])
                if d1 * sign >= 0 and d2 * sign >= 0 and d3 * sign >= 0:
                    ok = False
                    break
            if ok:
                tris.append((a, b, c))
                idx.pop(k)
                break
        else:
            break
    if len(idx) == 3:
        tris.append(tuple(idx))
    elif len(idx) > 3:
        tris += [(idx[0], idx[k], idx[k + 1]) for k in range(1, len(idx) - 1)]
    return tris


BLD_CHUNK = 1000             # домов в порции сборки; между порциями процесс отдаётся потоку кадров


def _buildings_chunk(np, x, y, counts, z0b, z1b, bid0):
    """Порция домов (кольца подряд) -> треугольники (N, 9) float32. Сразу в готовый массив: меньше копий."""
    nb = len(counts)
    start = np.concatenate([[0], np.cumsum(counts)[:-1]])
    bid = np.repeat(np.arange(nb), counts)
    ar = np.arange(len(x))
    loc = ar - start[bid]
    nxt = np.where(loc + 1 < counts[bid], ar + 1, start[bid])
    prv = np.where(loc > 0, ar - 1, start[bid] + counts[bid] - 1)
    cr = (x - x[prv]) * (y[nxt] - y) - (y - y[prv]) * (x[nxt] - x)
    mn, mx = np.minimum.reduceat(cr, start), np.maximum.reduceat(cr, start)
    convex = (mn >= -1e-6) | (mx <= 1e-6)
    cb = np.nonzero(convex)[0]
    nt = counts[cb] - 2
    tb = np.repeat(cb, nt)
    k = np.arange(int(nt.sum())) - np.repeat(np.cumsum(nt) - nt, nt) + 1      # и когда выпуклых в порции нет
    roof_b, roof_i = [tb], [np.stack([start[tb], start[tb] + k, start[tb] + k + 1], 1)]
    for bi in np.nonzero(~convex)[0]:                               # невыпуклые -- по одному (их единицы)
        s0, m = int(start[bi]), int(counts[bi])
        tri = _ear_clip(np, x[s0:s0 + m], y[s0:s0 + m])
        if tri:
            roof_b.append(np.full(len(tri), bi))
            roof_i.append(np.asarray(tri, dtype=np.int64) + s0)
    rb, ri = np.concatenate(roof_b), np.concatenate(roof_i)
    nv = len(x)
    out = np.empty((2 * nv + len(rb), 3, 9), "f4")
    w1, w2 = out[:nv], out[nv:2 * nv]                                # стены: два треугольника на ребро
    z0, z1 = z0b[bid], z1b[bid]
    for tri, pts in ((w1, ((ar, z0), (nxt, z0), (nxt, z1))), (w2, ((ar, z0), (nxt, z1), (ar, z1)))):
        for q, (ix, zz) in enumerate(pts):
            tri[:, q, 0], tri[:, q, 1], tri[:, q, 2] = x[ix], y[ix], zz
    dx, dy = x[nxt] - x, y[nxt] - y                                  # нормаль стены -- горизонтальная, поперёк ребра
    L = np.maximum(np.hypot(dx, dy), 1e-9)
    nx, ny = (dy / L).astype("f4"), (-dx / L).astype("f4")
    for tri in (w1, w2):
        tri[:, :, 3:6] = np.array(BLD_WALL, "f4") / 255.0
        tri[:, :, 6], tri[:, :, 7], tri[:, :, 8] = nx[:, None], ny[:, None], 0.0
    rf = out[2 * nv:]
    zr = z1b[rb]
    for q in range(3):
        rf[:, q, 0], rf[:, q, 1], rf[:, q, 2] = x[ri[:, q]], y[ri[:, q]], zr
    pal = np.array(BLD_ROOFS, "f4") / 255.0
    rf[:, :, 3:6] = pal[((rb + bid0) * 2654435761) % len(pal)][:, None, :]
    rf[:, :, 6:9] = np.array([0.0, 0.0, 1.0], "f4")
    return out.reshape(-1, 9)


def _buildings_mesh(np, cells, origin, center, radius, ground_fn):
    """Здания в радиусе -> треугольники (N, 9): позиция, цвет 0..1, нормаль. Стены от земли (с запасом вниз) до крыши, крыша плоская.
    Массивами numpy и ПОРЦИЯМИ по BLD_CHUNK домов с паузой: прежний цикл по домам на 25 тысячах домов города занимал ~4 с
    и отнимал процесс видеокарты у кадров (толчки)."""
    X, Y, cnt, H, CLAT, CLON = [], [], [], [], [], []
    cx, cy = center
    for cell in cells:
        if not len(cell):
            continue
        bb = cell.bbox
        clat, clon = (bb[:, 0] + bb[:, 2]) / 2.0, (bb[:, 1] + bb[:, 3]) / 2.0
        bx, by = origin.xy(clat, clon)
        sel = np.nonzero((bx - cx) ** 2 + (by - cy) ** 2 <= radius * radius)[0]
        counts = np.diff(cell.offsets)[sel]
        sel, counts = sel[counts >= 3], counts[counts >= 3]
        if not len(sel):
            continue
        starts = cell.offsets[:-1][sel]
        vidx = np.repeat(starts - np.concatenate([[0], np.cumsum(counts)[:-1]]), counts) + np.arange(int(counts.sum()))
        x, y = origin.xy(cell.lat[vidx], cell.lon[vidx])
        X.append(x)
        Y.append(y)
        cnt.append(counts)
        H.append(cell.height[sel].astype(np.float64))
        CLAT.append(clat[sel])
        CLON.append(clon[sel])
    if not cnt:
        return np.zeros((0, 9), "f4"), 0
    x, y, counts = np.concatenate(X), np.concatenate(Y), np.concatenate(cnt)
    g = ground_fn(np.concatenate(CLAT), np.concatenate(CLON))
    z0b, z1b = g - 3.0, g + np.concatenate(H)
    vstart = np.concatenate([[0], np.cumsum(counts)])
    out = []
    for b0 in range(0, len(counts), BLD_CHUNK):
        b1 = min(b0 + BLD_CHUNK, len(counts))
        v0, v1 = vstart[b0], vstart[b1]
        out.append(_buildings_chunk(np, x[v0:v1], y[v0:v1], counts[b0:b1], z0b[b0:b1], z1b[b0:b1], b0))
        time.sleep(0.002)                                            # отдать процесс потоку кадров
    return np.concatenate(out), len(counts)


def _merc_tile(lat, lon, z):
    """Дробные координаты тайла Web Mercator (numpy или числа)."""
    import numpy as np
    n = 2.0 ** z
    lat = np.clip(lat, -85.05112878, 85.05112878)
    lr = np.radians(lat)
    return (np.asarray(lon) + 180.0) / 360.0 * n, (1.0 - np.log(np.tan(lr) + 1.0 / np.cos(lr)) / math.pi) / 2.0 * n


def _tile_latlon(tx, ty, z):
    import numpy as np
    n = 2.0 ** z
    lon = tx / n * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(math.pi * (1.0 - 2.0 * ty / n))))
    return lat, lon


class _Origin:
    """Локальная плоская система: x -- восток, y -- север (м) от точки начала; z -- высота AMSL."""

    def __init__(self, lat0, lon0):
        self.lat0, self.lon0 = lat0, lon0
        from scene_project import EARTH_R                  # тот же радиус, что у local_offset_m: земля и объекты там же, где их считает программа
        self.ky = EARTH_R * math.pi / 180.0
        self.kx = self.ky * math.cos(math.radians(lat0))

    def xy(self, lat, lon):
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky


def _build_layer(level, z, ntiles, lat_c, lon_c, origin, tiles, srtm, srtm_lock, home, forest=None, forest_ver=0):
    """Фоновый поток: мозаика снимков + сетка высот для одного слоя. Только numpy/PIL, без OpenGL."""
    import io
    import numpy as np
    from concurrent.futures import ThreadPoolExecutor
    from PIL import Image
    fx, fy = _merc_tile(lat_c, lon_c, z)
    tx0, ty0 = int(math.floor(float(fx) - ntiles / 2)), int(math.floor(float(fy) - ntiles / 2))
    size = ntiles * 256
    mosaic = Image.new("RGB", (size, size), (96, 104, 96))
    coords = [(tx0 + i, ty0 + j) for i in range(ntiles) for j in range(ntiles)]

    def get(c):
        try:
            return c, tiles.get_tile(z, c[0], c[1])
        except Exception:
            return c, None
    missing = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for (tx, ty), data in ex.map(get, coords):
            if not data:
                missing += 1
                continue
            try:
                im = Image.open(io.BytesIO(data)).convert("RGB")
                if im.size != (256, 256):
                    im = im.resize((256, 256), Image.BILINEAR)
                mosaic.paste(im, ((tx - tx0) * 256, (ty - ty0) * 256))
            except Exception:
                missing += 1
    # сетка: равномерно в координатах тайлов (тогда uv линейны), позиции -- в локальных метрах
    g = np.linspace(0.0, float(ntiles), GL_GRID)
    gx, gy = np.meshgrid(tx0 + g, ty0 + g)
    lat, lon = _tile_latlon(gx, gy, z)
    x, y = origin.xy(lat, lon)
    h = None
    if srtm is not None:
        with srtm_lock:
            try:
                srtm.preload_tiles(srtm.missing_tiles(lat, lon))
                h = srtm.get_elevations(lat, lon).astype(np.float64)
            except Exception:
                h = None
    if h is None or not np.isfinite(h).any():
        h = np.full(lat.shape, float(home or 0.0))
    else:
        h = np.where(np.isfinite(h), h, np.nanmedian(h))
    fr = np.zeros(lat.shape)
    if forest is not None:                                 # лес (карта ETH): поверхность поднимается на высоту крон (кромка -- склон сетки)
        try:
            spacing = float(abs(x[0, -1] - x[0, 0])) / (GL_GRID - 1)     # шаг сетки слоя: копия тайла не детальнее нужного
            canopy = forest.canopy(lat, lon, res_m=spacing)
            h = h + canopy
            fr = (canopy > 0).astype(np.float64)
        except Exception:
            pass
    # подсветка склонов (простая модель «солнце + рассеянный свет»)
    dx = (x[:, -1] - x[:, 0]).mean() / (GL_GRID - 1)
    dy = (y[0, 0] - y[-1, 0]) / (GL_GRID - 1)
    dzdx = np.gradient(h, axis=1) / max(abs(dx), 1e-6)
    dzdy = -np.gradient(h, axis=0) / max(abs(dy), 1e-6)            # строки идут с севера на юг
    nrm = np.stack([-dzdx, -dzdy, np.ones_like(h)], -1)
    nrm /= np.linalg.norm(nrm, axis=-1, keepdims=True)
    az, el = math.radians(SUN_AZ_DEG), math.radians(SUN_EL_DEG)
    sun = np.array([math.sin(az) * math.cos(el), math.cos(az) * math.cos(el), math.sin(el)])
    shade = 0.62 + 0.45 * np.clip(nrm @ sun, 0.0, 1.0)
    u, v = (gx - tx0) / ntiles, (gy - ty0) / ntiles
    verts = np.stack([x, y, h, u, v, shade, fr], -1).astype("f4").reshape(-1, 7)
    forest_frac = float(fr.mean())
    bounds = (float(x.min()), float(y.min()), float(x.max()), float(y.max()))
    cx, cy = origin.xy(lat_c, lon_c)
    return {"level": level, "verts": verts, "image": mosaic.tobytes(), "size": size, "bounds": bounds, "fver": forest_ver,
            "forest_frac": forest_frac,
            "center": (float(cx), float(cy)), "extent": bounds[2] - bounds[0], "origin": origin, "missing": missing}


def _grid_indices():
    import numpy as np
    n = GL_GRID
    i = np.arange(n * n, dtype="i4").reshape(n, n)
    a, b, c, d = i[:-1, :-1].ravel(), i[:-1, 1:].ravel(), i[1:, :-1].ravel(), i[1:, 1:].ravel()
    return np.stack([a, c, b, b, c, d], 1).astype("i4").ravel()


def _view_matrices(pos, yaw, pitch, roll, aspect):
    """mvp и обратная к ней. Борт: курс (по часовой от севера), тангаж (нос вверх +), крен (правое крыло вниз +)."""
    import numpy as np
    cy_, sy_ = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    cp, sp = math.cos(math.radians(pitch)), math.sin(math.radians(pitch))
    cr, sr = math.cos(math.radians(roll)), math.sin(math.radians(roll))
    fwd_ned = (cp * cy_, cp * sy_, -sp)
    right_ned = (sr * sp * cy_ - cr * sy_, sr * sp * sy_ + cr * cy_, sr * cp)
    down_ned = (cr * sp * cy_ + sr * sy_, cr * sp * sy_ - sr * cy_, cr * cp)

    def enu(v):
        return np.array([v[1], v[0], -v[2]])
    f, r, up = enu(fwd_ned), enu(right_ned), -enu(down_ned)
    p = np.asarray(pos, dtype=np.float64)
    view = np.identity(4)
    view[0, :3], view[1, :3], view[2, :3] = r, up, -f
    view[:3, 3] = -view[:3, :3] @ p
    vfov = 2.0 * math.atan(math.tan(math.radians(GL_HFOV_DEG) / 2.0) / aspect)
    near, far = 2.0, GL_FAR_M
    t = 1.0 / math.tan(vfov / 2.0)
    proj = np.zeros((4, 4))
    proj[0, 0], proj[1, 1] = t / aspect, t
    proj[2, 2], proj[2, 3] = (far + near) / (near - far), 2.0 * far * near / (near - far)
    proj[3, 2] = -1.0
    vp = proj @ view
    return vp, np.linalg.inv(vp)




# =====================================================================================================================
# Сцена на видеокарте (процесс без окна): слои земли, объекты, цветной и тепловой кадр в заданный буфер
# =====================================================================================================================
class _GlScene:
    def __init__(self, cfg, ctx, moderngl, np, send):
        from online_tiles import OnlineTileCache
        from nose_camera_thermal import ThermalParams, make_palette, day_weight
        self.ctx, self.mgl, self.np, self.send = ctx, moderngl, np, send
        self.day_weight = day_weight
        self.tiles = OnlineTileCache(provider=cfg["provider"], disk_cache_dir=cfg["tile_dir"])
        self.srtm = None
        if cfg.get("srtm_dir"):
            try:
                from srtm import SRTMTerrain
                self.srtm = SRTMTerrain(cfg["srtm_dir"])
            except Exception as exc:
                send("info", f"рельєф SRTM недоступний ({exc}) -- пласка земля")
        self.srtm_lock = threading.Lock()
        self.home = cfg.get("home_amsl")
        P = lambda vs, fs: ctx.program(vertex_shader=vs, fragment_shader=fs)
        self.prog, self.sky, self.oprog = P(_VS_TERRAIN, _FS_TERRAIN), P(_VS_SKY, _FS_SKY), P(_VS_OBJ, _FS_OBJ)
        self.prog_t, self.sky_t, self.oprog_t = P(_VS_TERRAIN, _FS_TERRAIN_T), P(_VS_SKY, _FS_SKY_T), P(_VS_OBJ, _FS_OBJ_T)
        self.therm_prog, self.agc_prog = P(_VS_HUD, _FS_THERMAL), P(_VS_HUD, _FS_AGC)
        self.sky_vao, self.sky_t_vao = ctx.vertex_array(self.sky, []), ctx.vertex_array(self.sky_t, [])
        self.therm_vao, self.agc_vao = ctx.vertex_array(self.therm_prog, []), ctx.vertex_array(self.agc_prog, [])
        self.ibo = ctx.buffer(_grid_indices().tobytes())
        try:
            self.max_aniso = float(ctx.info.get("GL_MAX_TEXTURE_MAX_ANISOTROPY", 1.0))
        except Exception:
            self.max_aniso = 1.0
        self.fogcol, self.zenith = (0.74, 0.80, 0.86), (0.36, 0.55, 0.80)
        az, el = math.radians(SUN_AZ_DEG), math.radians(SUN_EL_DEG)
        self.sun_vec = (math.sin(az) * math.cos(el), math.cos(az) * math.cos(el), math.sin(el))
        self.tp = ThermalParams()
        self.luts = {}
        for name in ("white_hot", "black_hot", "ironbow"):
            lt = ctx.texture((256, 1), 3, make_palette(name).astype("u1").tobytes())
            lt.filter = (moderngl.NEAREST, moderngl.NEAREST)
            self.luts[name] = lt
        self.therm = {"size": None, "lo": None, "hi": None, "t": time.monotonic(), "frame": 0, "palette": None}
        self.layers, self.building, self.results = {}, {}, []
        self.res_lock = threading.Lock()
        self.origin = None
        self.scene_objs, self.obj_vaos, self.obj_origin = [], [], None
        self.forest, self.forest_ver = None, 0   # высоты крон ETH (тайлы из папки); версия растёт, когда докачан тайл
        self.obst = ([], [])                     # (точки, линии) от главной программы
        self.obst_vao = self.obst_vao_t = self.obst_vbo = None
        self.wire_vao = self.wire_vao_t = self.wire_vbo = None
        self.obst_n = self.wire_n = 0
        self.obst_origin = None
        self.obst_center = None                  # (px, py) центра последней сборки
        self.obst_dirty = False                  # пришли новые данные
        self.obst_building = False
        self.bld_dir, self.bld_cells, self.bld_data = None, [], {}    # здания: папка кэша, загруженные у программы ячейки, данные
        self.bld_dirty, self.bld_busy, self.bld_center, self.bld_origin = False, False, None, None
        self.bld_vao = self.bld_vao_t = self.bld_vbo = None
        self.bld_n = 0
        self.targets = {}

    # ---- сообщения и фоновая подгрузка слоёв ----
    def handle(self, msg):
        if msg[0] == "scene":
            self.scene_objs, self.obj_origin = msg[1], None
        elif msg[0] == "obstacles":
            self.obst, self.obst_dirty = (msg[1], msg[2]), True
        elif msg[0] == "buildings":                                  # [(файл npz, рамка)] -- квадраты, уже загруженные программой
            self.bld_cells, self.bld_dirty = [(p_, tuple(b_)) for p_, b_ in msg[1]], True
        elif msg[0] == "forest":                                     # (папка тайлов ETH, какие тайлы в ней есть)
            from obstacles import CanopyMap
            self.forest = CanopyMap(msg[1])                          # заново: докачанные тайлы откроются
            self.forest_ver += 1                                     # слои перестроятся с высотами крон

    def _start_build(self, level, lat_c, lon_c):
        z, n = GL_LEVELS[level]
        origin = self.origin
        fver = self.forest_ver

        def work():
            try:
                out = _build_layer(level, z, n, lat_c, lon_c, origin, self.tiles, self.srtm, self.srtm_lock, self.home,
                                   self.forest, fver)
            except Exception as exc:
                out = {"level": level, "error": f"{type(exc).__name__}: {exc}"}
            with self.res_lock:
                self.results.append(out)
        th = threading.Thread(target=work, daemon=True)
        self.building[level] = th
        th.start()

    def pump(self):
        ctx, mgl = self.ctx, self.mgl
        with self.res_lock:
            ready, self.results[:] = list(self.results), []
        for out in ready:
            if out.get("obstacles"):
                self._upload_obstacles(out)
                continue
            if out.get("buildings"):
                self._upload_buildings(out)
                continue
            level = out["level"]
            self.building.pop(level, None)
            if "error" in out:
                self.send("info", f"шар {level}: {out['error']}")
                continue
            if out["origin"] is not self.origin:
                continue                                             # начало координат успело смениться
            old = self.layers.get(level)
            tex = ctx.texture((out["size"], out["size"]), 3, out["image"])
            tex.build_mipmaps()
            tex.filter = (mgl.LINEAR_MIPMAP_LINEAR, mgl.LINEAR)
            tex.repeat_x = tex.repeat_y = False
            if self.max_aniso > 1.0:
                tex.anisotropy = min(8.0, self.max_aniso)
            vbo = ctx.buffer(out["verts"].tobytes())
            fmt = [(vbo, "3f 2f 1f 1f", "in_pos", "in_uv", "in_shade", "in_forest")]
            self.layers[level] = {"vao": ctx.vertex_array(self.prog, fmt, self.ibo, skip_errors=True),
                                  "vao_t": ctx.vertex_array(self.prog_t, fmt, self.ibo, skip_errors=True),
                                  "vbo": vbo, "tex": tex, "bounds": out["bounds"], "center": out["center"], "extent": out["extent"],
                                  "fver": out["fver"]}
            if old is not None:
                self._release(old)
            if out["missing"]:
                self.send("info", f"шар {level}: {out['missing']} тайлів знімків недоступні")
            if level == 0 and out["fver"] != getattr(self, "_forest_logged_ver", None) and self.forest is not None:
                self._forest_logged_ver = out["fver"]
                self.send("info", f"ліс у ближньому шарі (~3 км): {out['forest_frac'] * 100:.0f}% площі")

    @staticmethod
    def _release(lay):
        for k in ("vao", "vao_t", "vbo", "tex"):
            lay[k].release()

    # ---- подготовка к кадру: начало координат, слои вокруг борта, объекты ----
    def prepare(self, lat, lon, yaw):
        """(px, py) борта в локальной системе или None, пока не готовы все слои земли (тогда кадр считает процессор)."""
        np = self.np
        if self.origin is None or max(abs(v) for v in self.origin.xy(lat, lon)) > 40000:
            self.origin = _Origin(lat, lon)                          # новое начало координат: всё строится заново
            for lay in self.layers.values():
                self._release(lay)
            self.layers.clear()
            self.obj_origin = None
        origin = self.origin
        px, py = origin.xy(lat, lon)
        for level, (z, n) in enumerate(GL_LEVELS):                   # слой, от которого борт (с упреждением по курсу) ушёл, -- вокруг новой точки
            if level in self.building:
                continue
            lay = self.layers.get(level)
            ext = lay["extent"] if lay else 40075000.0 * math.cos(math.radians(lat)) / 2 ** z * n
            ahead = 0.25 * ext
            ax, ay = px + ahead * math.sin(math.radians(yaw)), py + ahead * math.cos(math.radians(yaw))
            if lay is None or math.hypot(ax - lay["center"][0], ay - lay["center"][1]) > 0.2 * ext or lay["fver"] != self.forest_ver:
                self._start_build(level, origin.lat0 + ay / origin.ky, origin.lon0 + ax / origin.kx)
        if self.obj_origin is not origin:
            self._build_objects()
        if self.bld_cells and not self.bld_busy and (
                self.bld_dirty or self.bld_origin is not origin or self.bld_center is None
                or math.hypot(px - self.bld_center[0], py - self.bld_center[1]) > BLD_REBUILD_M):
            self._start_buildings(px, py)
        if (self.obst[0] or self.obst[1]) and not self.obst_building and (
                self.obst_dirty or self.obst_origin is not origin or self.obst_center is None
                or math.hypot(px - self.obst_center[0], py - self.obst_center[1]) > OBST_REBUILD_M):
            self._start_obstacles(px, py)
        if len(self.layers) < len(GL_LEVELS):
            return None
        return px, py

    def _build_objects(self):
        ctx, np = self.ctx, self.np
        for vao, vao_t, vbo, _la, _lo in self.obj_vaos:
            vao.release()
            vao_t.release()
            vbo.release()
        self.obj_vaos = []
        for o in self.scene_objs:
            base = o.get("base")
            if base is None and self.srtm is not None:               # высота основания неизвестна -- по SRTM под объектом
                with self.srtm_lock:
                    try:
                        la, lo = np.array([o["lat"]]), np.array([o["lon"]])
                        self.srtm.preload_tiles(self.srtm.missing_tiles(la, lo))
                        b = float(self.srtm.get_elevations(la, lo)[0])
                        base = b if math.isfinite(b) else None
                    except Exception:
                        base = None
            if base is None:
                base = float(self.home or 0.0)
            try:
                vbo = ctx.buffer(_object_arrays(o, base).tobytes())
                fmt = [(vbo, "3f 3f 3f", "in_pos", "in_col", "in_nrm")]
                self.obj_vaos.append((ctx.vertex_array(self.oprog, fmt), ctx.vertex_array(self.oprog_t, fmt, skip_errors=True), vbo,
                                      o["lat"], o["lon"]))
            except Exception as exc:
                self.send("info", f"3D-об'єкт: {type(exc).__name__}: {exc}")
        self.obj_origin = self.origin

    def _ground(self, lat, lon):
        """Земля AMSL под точками (как _sim_ground_amsl программы: SRTM, без него -- уровень дома)."""
        np = self.np
        lat, lon = np.atleast_1d(np.asarray(lat, float)), np.atleast_1d(np.asarray(lon, float))
        h = None
        if self.srtm is not None:
            with self.srtm_lock:
                try:
                    self.srtm.preload_tiles(self.srtm.missing_tiles(lat, lon))
                    h = self.srtm.get_elevations(lat, lon).astype(float)
                except Exception:
                    h = None
        floor = float(self.home or 0.0)
        return np.full(lat.shape, floor) if h is None else np.where(np.isfinite(h), h, floor)

    def _start_obstacles(self, px, py):
        """Сборка препятствий в радиусе OBST_RADIUS_M в фоновом потоке: кадры идут, пока она считается (десятки тысяч опор)."""
        self.obst_building, self.obst_dirty = True, False
        origin, (points, lines), center = self.origin, self.obst, (px, py)

        def work():
            try:
                tris, wires, n_pts, n_spans = _obstacle_arrays(self.np, points, lines, origin, self._ground, center)
                out = {"obstacles": True, "origin": origin, "center": center, "tris": tris, "wires": wires,
                       "n_pts": n_pts, "n_spans": n_spans, "total": (len(points), len(lines))}
            except Exception as exc:
                out = {"obstacles": True, "error": f"{type(exc).__name__}: {exc}", "origin": origin, "center": center}
            with self.res_lock:
                self.results.append(out)
        threading.Thread(target=work, daemon=True).start()

    def _start_buildings(self, px, py):
        """Сборка зданий вокруг борта в фоновом потоке: ячейки читаются из того же кэша, что пишет программа."""
        self.bld_busy, self.bld_dirty = True, False
        origin, center, cells = self.origin, (px, py), list(self.bld_cells)

        def work():
            try:
                from obstacles import BuildingLoader
                lat_c, lon_c = origin.lat0 + center[1] / origin.ky, origin.lon0 + center[0] / origin.kx
                rdeg_lat, rdeg_lon = BLD_RADIUS_M / origin.ky, BLD_RADIUS_M / origin.kx
                use = []
                for path, (s_, w_, n_, e_) in cells:             # только квадраты, задевающие радиус
                    if n_ < lat_c - rdeg_lat or s_ > lat_c + rdeg_lat or e_ < lon_c - rdeg_lon or w_ > lon_c + rdeg_lon:
                        continue
                    if path not in self.bld_data:
                        cell = BuildingLoader.load_file(path)
                        if cell is not None:
                            self.bld_data[path] = cell
                    if path in self.bld_data:
                        use.append(self.bld_data[path])
                tris, n = _buildings_mesh(self.np, use, origin, center, BLD_RADIUS_M, self._ground)
                out = {"buildings": True, "origin": origin, "center": center, "tris": tris, "n": n}
            except Exception as exc:
                out = {"buildings": True, "origin": origin, "center": center, "error": f"{type(exc).__name__}: {exc}"}
            with self.res_lock:
                self.results.append(out)
        threading.Thread(target=work, daemon=True).start()

    def _upload_buildings(self, out):
        self.bld_busy = False
        if "error" in out:
            self.send("info", f"будівлі OSM: {out['error']}")
            return
        if out["origin"] is not self.origin:
            return
        for o in (self.bld_vao, self.bld_vao_t, self.bld_vbo):
            if o is not None:
                o.release()
        self.bld_vao = self.bld_vao_t = self.bld_vbo = None
        self.bld_n = 0
        self.bld_origin, self.bld_center = out["origin"], out["center"]
        tris = out["tris"]
        if len(tris):
            self.bld_vbo = self.ctx.buffer(tris.tobytes())
            fmt = [(self.bld_vbo, "3f 3f 3f", "in_pos", "in_col", "in_nrm")]
            self.bld_vao = self.ctx.vertex_array(self.oprog, fmt)
            self.bld_vao_t = self.ctx.vertex_array(self.oprog_t, fmt, skip_errors=True)
            self.bld_n = len(tris)
        self.send("bstats", out["n"])

    def _upload_obstacles(self, out):
        ctx = self.ctx
        self.obst_building = False
        if "error" in out:
            self.send("info", f"перешкоди OSM: {out['error']}")
            return
        if out["origin"] is not self.origin:
            return                                               # начало координат сменилось: соберётся заново
        for o in (self.obst_vao, self.obst_vao_t, self.obst_vbo, self.wire_vao, self.wire_vao_t, self.wire_vbo):
            if o is not None:
                o.release()
        self.obst_vao = self.obst_vao_t = self.obst_vbo = self.wire_vao = self.wire_vao_t = self.wire_vbo = None
        self.obst_n = self.wire_n = 0
        self.obst_origin, self.obst_center = out["origin"], out["center"]
        fmt = lambda vbo: [(vbo, "3f 3f 3f", "in_pos", "in_col", "in_nrm")]
        tris, wires = out["tris"], out["wires"]
        if len(tris):
            self.obst_vbo = ctx.buffer(tris.tobytes())
            self.obst_vao = ctx.vertex_array(self.oprog, fmt(self.obst_vbo))
            self.obst_vao_t = ctx.vertex_array(self.oprog_t, fmt(self.obst_vbo), skip_errors=True)
            self.obst_n = len(tris)
        if len(wires):
            self.wire_vbo = ctx.buffer(wires.tobytes())
            self.wire_vao = ctx.vertex_array(self.oprog, fmt(self.wire_vbo))
            self.wire_vao_t = ctx.vertex_array(self.oprog_t, fmt(self.wire_vbo), skip_errors=True)
            self.wire_n = len(wires)
        tp, tl = out["total"]
        self.send("info", f"перешкоди OSM у кадрі (радіус {OBST_RADIUS_M / 1000:.0f} км): {out['n_pts']} споруд з {tp}, "
                          f"{out['n_spans']} прольотів ЛЕП")
        self.send("stats", out["n_pts"], out["n_spans"])

    def _draw_obstacles(self, pr, thermal):
        """Препятствия в абсолютных координатах окна: смещение 0. Провода -- линии в 1 пиксель (видны и тогда, когда тоньше пикселя)."""
        mgl = self.mgl
        self._setu(pr, "offset", (0.0, 0.0, 0.0))
        vao, wvao = (self.obst_vao_t, self.wire_vao_t) if thermal else (self.obst_vao, self.wire_vao)
        if vao is not None:
            vao.render(mgl.TRIANGLES, vertices=self.obst_n)
        if wvao is not None:
            wvao.render(mgl.LINES, vertices=self.wire_n)

    # ---- кадр ----
    @staticmethod
    def _setu(pr, name, value):
        if name in pr:
            if isinstance(value, bytes):
                pr[name].write(value)
            else:
                pr[name].value = value

    def _target(self, w, h):
        t = self.targets.get((w, h))
        if t is None:
            for old in self.targets.values():
                for o in old:
                    o.release()
            self.targets.clear()
            color = self.ctx.renderbuffer((w, h), 4)
            depth = self.ctx.depth_renderbuffer((w, h))
            t = self.targets[(w, h)] = (self.ctx.framebuffer([color], depth), color, depth)
        return t[0]

    def _hole(self, pr, level):
        finer = self.layers.get(level - 1) if level > 0 else None
        if finer is not None:
            b = finer["bounds"]
            m = (b[2] - b[0]) / (GL_GRID - 1) * 2.0                  # перекрытие в 2 ячейки: без щелей на стыке
            self._setu(pr, "hole", (b[0] + m, b[1] + m, b[2] - m, b[3] - m))
        else:
            self._setu(pr, "hole", (1.0, 1.0, -1.0, -1.0))
        self._setu(pr, "zbias", 1.0 * level)                     # слой 0 (ближний) -- 0 м, дальние -- на 1 и 2 м ниже

    def _obj_offset(self, px, py, olat, olon):
        from scene_project import local_offset_m
        n, e = local_offset_m(self.pose_ll[0], self.pose_ll[1], olat, olon)
        return (px + e, py + n, 0.0)

    def render(self, lat, lon, amsl, yaw, pitch, roll, w, h, palette, sun_el):
        """RGB-байты кадра w x h (строки снизу вверх, как читает OpenGL) или None, если земля ещё не готова."""
        self.pump()
        pos = self.prepare(lat, lon, yaw)
        if pos is None:
            return None
        px, py = pos
        self.pose_ll = (lat, lon)
        vp, inv_vp = _view_matrices((px, py, amsl), yaw, pitch, roll, w / h)
        target = self._target(w, h)
        if palette in self.luts:
            self._draw_thermal(target, vp, inv_vp, px, py, amsl, w, h, palette, sun_el)
        else:
            self._draw_color(target, vp, inv_vp, px, py, amsl)
        return target.read(components=3, alignment=1)

    def _draw_color(self, target, vp, inv_vp, px, py, amsl):
        mgl, S = self.mgl, self._setu
        target.use()
        target.clear(*self.fogcol, 1.0, depth=1.0)
        self.ctx.disable(mgl.DEPTH_TEST)
        S(self.sky, "inv_vp", inv_vp.T.astype("f4").tobytes())
        S(self.sky, "fogcol", self.fogcol)
        S(self.sky, "zenith", self.zenith)
        self.sky_vao.render(mgl.TRIANGLES, vertices=3)
        self.ctx.enable(mgl.DEPTH_TEST)
        S(self.prog, "mvp", vp.T.astype("f4").tobytes())
        S(self.prog, "campos", (px, py, amsl))
        S(self.prog, "fogcol", self.fogcol)
        S(self.prog, "fogdist", GL_FOG_M)
        for level in sorted(self.layers, reverse=True):              # от дальнего к ближнему
            lay = self.layers[level]
            self._hole(self.prog, level)
            lay["tex"].use(0)
            S(self.prog, "tex", 0)
            lay["vao"].render(mgl.TRIANGLES)
        if self.obj_vaos or self.obst_n or self.wire_n or self.bld_n:
            S(self.oprog, "mvp", vp.T.astype("f4").tobytes())
            S(self.oprog, "campos", (px, py, amsl))
            S(self.oprog, "fogcol", self.fogcol)
            S(self.oprog, "fogdist", GL_FOG_M)
            S(self.oprog, "sun", self.sun_vec)
            self._draw_obstacles(self.oprog, False)
            if self.bld_vao is not None:
                self.bld_vao.render(mgl.TRIANGLES, vertices=self.bld_n)
            for vao, _vao_t, _vbo, olat, olon in self.obj_vaos:
                S(self.oprog, "offset", self._obj_offset(px, py, olat, olon))
                vao.render(mgl.TRIANGLES)
        self.ctx.disable(mgl.DEPTH_TEST)

    def _ensure_thermal(self, tw, th):
        """Буфер «температуры» с разрешением матрицы (640 по ширине), шум матрицы и буфер выборки АРУ."""
        T, ctx, mgl, np, tp = self.therm, self.ctx, self.mgl, self.np, self.tp
        if T["size"] == (tw, th):
            return
        for k in ("tex", "depth", "fbo", "fpn", "agc_tex", "agc_fbo"):
            if T.get(k) is not None:
                T[k].release()
        tex = ctx.texture((tw, th), 2, dtype="f4")
        tex.filter = (mgl.LINEAR, mgl.LINEAR)
        tex.repeat_x = tex.repeat_y = False
        depth = ctx.depth_renderbuffer((tw, th))
        rng = np.random.default_rng(1)                       # одна и та же «матрица» при каждом запуске, как в обычной камере
        fpn = (rng.normal(0.0, tp.noise_fixed, (th, tw)) + rng.normal(0.0, tp.noise_column, (1, tw))).astype("f4")
        fpn_tex = ctx.texture((tw, th), 1, fpn.tobytes(), dtype="f4")
        fpn_tex.filter = (mgl.NEAREST, mgl.NEAREST)
        aw, ah = max(1, tw // AGC_STEP), max(1, th // AGC_STEP)
        agc_tex = ctx.texture((aw, ah), 2, dtype="f4")
        T.update(size=(tw, th), tex=tex, depth=depth, fbo=ctx.framebuffer([tex], depth), fpn=fpn_tex,
                 agc_tex=agc_tex, agc_fbo=ctx.framebuffer([agc_tex]))

    def _draw_thermal(self, target, vp, inv_vp, px, py, amsl, w, h, palette, sun_el):
        """Сцена -> «температура» (640 px по ширине) -> АРУ по выборке -> матрица, шумы и палитра в кадр."""
        T, ctx, mgl, np, tp, S = self.therm, self.ctx, self.mgl, self.np, self.tp, self._setu
        if palette != T["palette"]:
            T["palette"], T["lo"], T["hi"] = palette, None, None     # новая палитра: АРУ заново, как при старте обычной камеры
        tw, th = 640, max(64, int(round(640 * h / max(w, 1))))
        self._ensure_thermal(tw, th)
        fbo = T["fbo"]
        fbo.use()
        fbo.clear(tp.air_temp, -1.0, 0.0, 0.0, depth=1.0)
        ctx.disable(mgl.DEPTH_TEST)
        S(self.sky_t, "inv_vp", inv_vp.T.astype("f4").tobytes())
        S(self.sky_t, "air", tp.air_temp)
        S(self.sky_t, "zenith_t", tp.sky_zenith)
        S(self.sky_t, "span_deg", tp.sky_elev_span_deg)
        self.sky_t_vao.render(mgl.TRIANGLES, vertices=3)
        ctx.enable(mgl.DEPTH_TEST)
        S(self.prog_t, "mvp", vp.T.astype("f4").tobytes())
        S(self.prog_t, "campos", (px, py, amsl))
        S(self.prog_t, "dayw", 1.0 if sun_el is None else self.day_weight(sun_el))
        S(self.prog_t, "air", tp.air_temp)
        S(self.prog_t, "atm", tp.atm_range_m)
        for level in sorted(self.layers, reverse=True):
            lay = self.layers[level]
            self._hole(self.prog_t, level)
            lay["tex"].use(0)
            S(self.prog_t, "tex", 0)
            lay["vao_t"].render(mgl.TRIANGLES)
        if self.obj_vaos or self.obst_n or self.wire_n or self.bld_n:
            S(self.oprog_t, "mvp", vp.T.astype("f4").tobytes())
            S(self.oprog_t, "sun", self.sun_vec)
            S(self.oprog_t, "air", tp.air_temp)
            S(self.oprog_t, "warm", -1.0)
            self._draw_obstacles(self.oprog_t, True)
            if self.bld_vao_t is not None:                       # здания: днём чуть теплее земли, ночью -- самые тёплые в сцене
                dayw = 1.0 if sun_el is None else self.day_weight(sun_el)
                S(self.oprog_t, "warm", 0.88 - 0.30 * dayw)
                S(self.oprog_t, "offset", (0.0, 0.0, 0.0))
                self.bld_vao_t.render(mgl.TRIANGLES, vertices=self.bld_n)
                S(self.oprog_t, "warm", -1.0)
            for _vao, vao_t, _vbo, olat, olon in self.obj_vaos:
                S(self.oprog_t, "offset", self._obj_offset(px, py, olat, olon))
                vao_t.render(mgl.TRIANGLES)
        ctx.disable(mgl.DEPTH_TEST)
        # АРУ: перцентили по выборке (объекты не считаются), с инерцией; скорость -- на время между кадрами (в обычной камере ~15 к/с)
        T["agc_fbo"].use()
        T["tex"].use(0)
        S(self.agc_prog, "temp", 0)
        S(self.agc_prog, "step", AGC_STEP)
        self.agc_vao.render(mgl.TRIANGLES, vertices=3)
        vals = np.frombuffer(T["agc_fbo"].read(components=2, dtype="f4"), dtype="f4")[0::2]
        vals = vals[vals > -50.0]
        now = time.monotonic()
        if vals.size:
            lo, hi = np.percentile(vals, (tp.agc_low_pct, tp.agc_high_pct))
            if T["lo"] is None:
                T["lo"], T["hi"] = float(lo), float(hi)
            else:
                k = 1.0 - (1.0 - tp.agc_rate) ** (min(max(now - T["t"], 0.0), 1.0) * 15.0)
                T["lo"] += k * (float(lo) - T["lo"])
                T["hi"] += k * (float(hi) - T["hi"])
        T["t"] = now
        lo_, hi_ = (T["lo"], T["hi"]) if T["lo"] is not None else (0.0, 1.0)
        mid, span = (lo_ + hi_) / 2.0, max(hi_ - lo_, tp.agc_min_span)
        target.use()
        T["tex"].use(0)
        T["fpn"].use(2)
        self.luts[palette].use(3)
        T["frame"] += 1
        for name, val in (("temp", 0), ("fpn", 2), ("lut", 3), ("texel", (1.0 / tw, 1.0 / th)), ("blur", tp.blur_px),
                          ("vignette", tp.vignette), ("lo", mid - span / 2.0), ("span", span), ("n_temporal", tp.noise_temporal),
                          ("n_row", tp.noise_row), ("frame_no", float(T["frame"] % 10007))):
            S(self.therm_prog, name, val)
        self.therm_vao.render(mgl.TRIANGLES, vertices=3)


AGC_STEP = 8


def gpu_process_main(conn, cfg):
    """Процесс видеокарты: проверка железа, затем кадры по запросам ("render", ...). Любая ошибка -> ("error", текст)."""
    def send(*msg):
        try:
            conn.send(msg)
        except Exception:
            pass
    try:
        import numpy as np
        try:
            import moderngl
        except ImportError:
            send("error", "немає бібліотеки moderngl")
            return
        try:
            ctx = moderngl.create_standalone_context(require=330)
        except Exception as exc:
            send("error", f"OpenGL 3.3 недоступний ({exc})")
            return
        renderer = ctx.info.get("GL_RENDERER", "?")
        if ("GDI Generic" in renderer or "llvmpipe" in renderer.lower() or "softpipe" in renderer.lower()) \
                and not os.environ.get("MA_GL_ALLOW_SOFTWARE"):
            send("error", f"лише програмний OpenGL ({renderer})")
            return
        scene = _GlScene(cfg, ctx, moderngl, np, send)
        send("ready", renderer, ctx.info.get("GL_VERSION", "?"))
        while True:
            if conn.poll(0.02):
                msg = conn.recv()
                if msg[0] == "quit":
                    return
                if msg[0] == "render":
                    _k, rid, lat, lon, amsl, yaw, pitch, roll, w, h, palette, sun_el = msg
                    data = scene.render(lat, lon, amsl, yaw, pitch, roll, w, h, palette, sun_el)
                    conn.send(("frame", rid, w, h, data))
                else:
                    scene.handle(msg)
            scene.pump()
    except (EOFError, OSError):
        return                                                       # программа закрылась
    except Exception as exc:
        import traceback
        send("error", f"{type(exc).__name__}: {exc} | {traceback.format_exc().splitlines()[-2].strip()[:160]}")


# =====================================================================================================================
# Отдельное окно HUD (просмотрщик): кадр + HUD + IMPACT. Только pygame, без OpenGL.
# =====================================================================================================================
def view_process_main(conn, cfg):
    def send(*msg):
        try:
            conn.send(msg)
        except Exception:
            pass
    try:
        try:
            import pygame
        except ImportError:
            send("error", "немає бібліотеки pygame")
            return
        pygame.display.init()
        screen = pygame.display.set_mode((1024, 800), pygame.RESIZABLE)
        pygame.display.set_caption(cfg.get("title", "HUD"))
        if cfg.get("icon"):
            try:
                pygame.display.set_icon(pygame.image.load(cfg["icon"]))
            except Exception:
                pass
        send("ready")
        frame = None                    # (w, h, rgb-байты)
        hud_items, crash = [], False
        hud_img = None                  # (W, H, rgba-байты)
        job = {"want": None, "done": None}
        cv = threading.Condition()

        def raster():
            while True:
                with cv:
                    while job["want"] is None:
                        cv.wait()
                    w_, h_, items, cr = job["want"]
                    job["want"] = None
                try:
                    img = _hud_raster(w_, h_, items, cr)
                    with cv:
                        job["done"] = (w_, h_, img.tobytes())
                except Exception as exc:
                    send("info", f"HUD: {type(exc).__name__}: {exc}")
        threading.Thread(target=raster, daemon=True).start()

        def want_hud():
            W_, H_ = screen.get_size()
            with cv:
                job["want"] = (W_, H_, hud_items, crash)
                cv.notify()
        sent_size, dirty = None, True
        clock = pygame.time.Clock()
        while True:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    return
                if ev.type in (pygame.VIDEORESIZE, getattr(pygame, "WINDOWSIZECHANGED", -1)):
                    dirty = True
            W, H = screen.get_size()
            if (W, H) != sent_size:
                sent_size = (W, H)
                send("size", W, H)
            while conn.poll():
                msg = conn.recv()
                if msg[0] == "quit":
                    return
                if msg[0] == "frame":
                    frame, dirty = msg[1:], True
                    send("ack")
                elif msg[0] == "hud":
                    hud_items = msg[1]
                    want_hud()
                elif msg[0] == "crash":
                    crash = bool(msg[1])
                    want_hud()
            with cv:
                done, job["done"] = job["done"], None
            if done is not None:
                hud_img, dirty = done, True
            if dirty:
                dirty = False
                screen.fill((0, 0, 0))
                if frame is not None:
                    fw, fh, data = frame
                    surf = pygame.image.frombuffer(data, (fw, fh), "RGB")
                    k = min(W / fw, H / fh)
                    size = (max(1, int(fw * k)), max(1, int(fh * k)))
                    screen.blit(pygame.transform.smoothscale(surf, size) if size != (fw, fh) else surf,
                                ((W - size[0]) // 2, (H - size[1]) // 2))
                if hud_img is not None and (hud_img[0], hud_img[1]) == (W, H):
                    screen.blit(pygame.image.frombuffer(hud_img[2], (W, H), "RGBA"), (0, 0))
                pygame.display.flip()
            clock.tick(120)
    except (EOFError, OSError):
        return
    except Exception as exc:
        import traceback
        send("error", f"{type(exc).__name__}: {exc} | {traceback.format_exc().splitlines()[-2].strip()[:160]}")
    finally:
        try:
            import pygame
            pygame.quit()
        except Exception:
            pass


# =====================================================================================================================
# Главная программа
# =====================================================================================================================
class _Child:
    """Процесс (видеокарта или окно) + поток чтения его сообщений: ответы приходят сразу, а не по таймеру интерфейса."""

    def __init__(self, target, cfg):
        import multiprocessing as mp
        import queue
        ctx = mp.get_context("spawn")
        self.conn, child = ctx.Pipe()
        self.proc = ctx.Process(target=target, args=(child, cfg), daemon=True)
        self.state, self.error, self.info = "starting", "", ()
        self.logs = queue.Queue()
        self.replies = queue.Queue(maxsize=8)
        self.size = None
        self.stats = None                       # (сооружений, пролётов ЛЭП) в последней сборке препятствий видеокарты
        self.bstats = None                      # зданий в последней сборке видеокарты
        self.acked = threading.Event()
        self.acked.set()
        self._lock = threading.Lock()
        self.proc.start()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            try:
                msg = self.conn.recv()
            except (EOFError, OSError):
                if self.state != "failed":
                    self.state = "closed" if self.state == "ready" else "failed"
                    self.error = self.error or "процес завершився"
                self.acked.set()
                return
            kind = msg[0]
            if kind == "ready":
                self.info, self.state = tuple(msg[1:]), "ready"
            elif kind == "error":
                self.error, self.state = str(msg[1]), "failed"
            elif kind == "info":
                self.logs.put(str(msg[1]))
            elif kind == "size":
                self.size = (int(msg[1]), int(msg[2]))
            elif kind == "ack":
                self.acked.set()
            elif kind == "stats":
                self.stats = (int(msg[1]), int(msg[2]))
            elif kind == "bstats":
                self.bstats = int(msg[1])
            elif kind == "frame":
                try:
                    self.replies.put_nowait(msg)
                except Exception:
                    pass

    def send(self, msg):
        with self._lock:
            self.conn.send(msg)

    @property
    def alive(self):
        return self.proc.is_alive() and self.state in ("starting", "ready")

    def stop(self):
        try:
            self.send(("quit",))
        except Exception:
            pass
        self.proc.join(timeout=1.5)
        if self.proc.is_alive():
            self.proc.terminate()


class GlCameraMixin:
    _gpu = None                     # _Child процесса видеокарты
    _gpu_note_done = False          # итог проверки видеокарты уже показан
    _gpu_rid = 0
    _gpu_timeouts = 0
    _gpu_scene_sig = None
    _gpu_obst_sig = None
    _gpu_obst_at = 0.0
    _view = None                    # _Child отдельного окна
    _view_hud = None                # HudRenderer, рисующий в запись (_HudRecorder)
    _view_hud_at = 0.0
    _view_frame_pose = None         # поза кадра, отданного окну (для красной точки)
    _view_frame_size = None
    _view_crash_at = None
    _gl_timer = None

    # ---------------------------------------------------------------- общее
    def _gl_dirs(self):
        """(папка SRTM или None, папка кэша тайлов камеры, высота дома AMSL или None) -- те же источники, что у обычной камеры."""
        an = getattr(self, "analyzer", None)
        terr = getattr(an, "terrain", None)
        srtm_dir = str(getattr(terr, "srtm_dir", "") or "") or None
        disk_cache = self.tilecache_var.get().strip() if hasattr(self, "tilecache_var") else ""
        if not disk_cache:
            base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
            disk_cache = os.path.join(base, "sim_map_cache")
        return srtm_dir, disk_cache, getattr(an, "home_amsl", None)

    def _gl_icon_path(self):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(base, "icon.png")
        return path if os.path.isfile(path) else None

    def _gl_log(self, text):
        if hasattr(self, "_sim_cmd_log"):
            try:
                self._sim_cmd_log(time.strftime("%H:%M:%S") + "  " + text)
            except Exception:
                pass

    def _gl_watch(self):
        """Таймер интерфейса: итог проверки видеокарты, сообщения процессов в журнал, закрытие окна крестиком."""
        self._gl_timer = None
        gpu, view = self._gpu, self._view
        for child, tag in ((gpu, "[GPU]"), (view, "[HUD-вікно]")):
            if child is not None:
                while not child.logs.empty():
                    self._gl_log(f"{tag} {child.logs.get_nowait()}")
        if gpu is not None and not self._gpu_note_done and gpu.state != "starting":
            self._gpu_note_done = True
            if gpu.state == "ready":
                self._gl_log(f"[GPU] камера на відеокарті: {gpu.info[0]} | OpenGL {gpu.info[1]}")
                self.sim_status_var.set(_t("status_gl_ready_fmt", renderer=gpu.info[0]))
            else:
                self._gl_log(f"[GPU] відеокарта недоступна: {gpu.error} -- кадри рахує процесор")
                self.sim_status_var.set(_t("status_gl_failed_fmt", error=gpu.error[:200]))
        if view is not None and not view.alive:
            if view.state == "failed":
                self._gl_log(f"[HUD-вікно] {view.error}")
            self._view = None
            self.sim_gl_var.set(False)
        if gpu is not None or self._view is not None:
            self._gl_timer = self.after(300, self._gl_watch)

    def _gl_watch_start(self):
        if self._gl_timer is None:
            self._gl_timer = self.after(300, self._gl_watch)

    # ---------------------------------------------------------------- кадр на видеокарте
    def _gpu_start(self):
        """Старт вида «Вперед»: процесс видеокарты запускается один раз за работу программы. Не годится -- кадры считает процессор."""
        if self._gpu is not None:
            if self._gpu.state in ("starting", "ready") and self._gpu.proc.is_alive():
                return
            if self._gpu.state == "failed" and "завершився" not in self._gpu.error:
                return                                               # видеокарта не годится: проверка не повторяется
        srtm_dir, disk_cache, home = self._gl_dirs()
        cfg = {"srtm_dir": srtm_dir, "tile_dir": disk_cache, "provider": GL_PROVIDER, "home_amsl": home}
        try:
            self._gpu = _Child(gpu_process_main, cfg)
        except Exception as exc:
            self._gl_log(f"[GPU] процес не запустився: {exc}")
            return
        self._gpu_note_done, self._gpu_scene_sig, self._gpu_timeouts = False, None, 0
        self._gpu_obst_sig = None
        self._gpu_forest_sig = None
        self._gpu_bld_sig = None
        self._gl_log("[GPU] перевірка відеокарти…")
        self._gl_watch_start()

    def _gpu_frame(self, pose, w, h, palette, sun_el):
        """Рабочий поток камеры: кадр от видеокарты (PIL RGB) или None -- тогда кадр считает процессор."""
        gpu = self._gpu
        if gpu is None or gpu.state != "ready" or getattr(pose, "z_amsl", None) is None:
            return None
        if time.monotonic() < getattr(self, "_gpu_pause_until", 0.0) or not gpu.proc.is_alive():
            return None
        from PIL import Image
        while not gpu.replies.empty():                               # хвосты прошлых запросов
            gpu.replies.get_nowait()
        self._gpu_rid += 1
        rid = self._gpu_rid
        try:
            gpu.send(("render", rid, float(pose.lat), float(pose.lon), float(pose.z_amsl), float(pose.yaw_deg),
                      float(pose.pitch_deg), float(pose.roll_deg), int(w), int(h), palette, sun_el))
        except Exception as exc:
            gpu.state, gpu.error = "failed", f"зв'язок з процесом: {exc}"
            return None
        deadline = time.monotonic() + 1.0
        while True:
            try:
                msg = gpu.replies.get(timeout=max(0.01, deadline - time.monotonic()))
            except Exception:
                self._gpu_timeouts += 1                              # занята (большая сборка) или подвисла: пара секунд на процессоре
                self._gpu_pause_until = time.monotonic() + 2.0
                self._gpu_slow_log(f"[GPU] кадр не прийшов за 1 с -- 2 с кадри рахує процесор (усього таких: {self._gpu_timeouts})")
                return None
            if msg[1] == rid:
                break
        self._gpu_timeouts = 0
        _k, _rid, fw, fh, data = msg
        waited = (time.monotonic() - (deadline - 1.0)) * 1000.0
        if waited > 150.0:
            self._gpu_slow_log(f"[GPU] кадр {fw}x{fh} чекав {waited:.0f} мс")
        if data is None:
            return None                                              # земля в процессе ещё грузится: этот кадр -- процессор
        return Image.frombytes("RGB", (fw, fh), data).transpose(Image.FLIP_TOP_BOTTOM)

    def _gpu_slow_log(self, text):
        """Из рабочего потока: в журнал -- через главный поток (журнал пишет интерфейс)."""
        try:
            self.after(0, lambda: self._gl_log(text))
        except Exception:
            pass

    def _gl_feed(self, s=None, roll_rad=None, pitch_rad=None):
        """Такт камеры: сцена -> видеокарта (только при изменении); после удара новый полёт убирает IMPACT в окне."""
        gpu = self._gpu
        if gpu is not None and gpu.state == "ready":
            loaded = getattr(self, "_scene_loaded", None) or []
            sig = tuple((id(it.mesh), it.base_amsl, it.obj.lat, it.obj.lon, it.obj.azimuth_deg, it.obj.enabled) for it in loaded)
            if sig != self._gpu_scene_sig:
                self._gpu_scene_sig = sig
                objs = [{"lat": it.obj.lat, "lon": it.obj.lon, "az": it.obj.azimuth_deg, "base": it.base_amsl,
                         "v": it.mesh.vertices.astype("f4"), "f": it.mesh.faces.astype("i4"), "c": it.mesh.colors.astype("u1")}
                        for it in loaded if it.obj.enabled]
                try:
                    gpu.send(("scene", objs))
                except Exception:
                    pass
            forest = getattr(self, "_sim_forest", None)
            if forest is not None:                                     # тайлы ETH: видеокарта читает их из той же папки
                fsig = (forest.dir, tuple(n for n in getattr(forest, "needed", ()) if forest.has(n)))
                if fsig != getattr(self, "_gpu_forest_sig", None) and fsig[1]:
                    self._gpu_forest_sig = fsig
                    try:
                        gpu.send(("forest", fsig[0], len(fsig[1])))
                    except Exception:
                        pass
            bl = getattr(self, "_sim_buildings", None)
            if bl is not None:                                         # здания: видеокарта читает ячейки из того же кэша на диске
                bsig = (bl.dir, len(bl.data))
                bnow = time.time()
                if bsig != getattr(self, "_gpu_bld_sig", None) and bl.data and \
                        (bl.finished or bnow - getattr(self, "_gpu_bld_at", 0.0) > 10.0):
                    self._gpu_bld_sig, self._gpu_bld_at = bsig, bnow
                    try:
                        gpu.send(("buildings", [(p_, list(b_)) for p_, b_ in bl.loaded()]))
                    except Exception:
                        pass
            loader = getattr(self, "_sim_obstacles", None)
            oset = loader.current_set() if loader is not None else None
            now = time.time()
            if oset is not None and now - self._gpu_obst_at > 10.0:    # ячейки OSM дозагружаются в фоне: набор растёт
                sig = (id(oset), len(oset))
                if sig != self._gpu_obst_sig:
                    self._gpu_obst_sig, self._gpu_obst_at = sig, now
                    pts = [(p.kind, p.lat, p.lon, float(p.height_m), float(p.radius_m), float(p.hub_m), float(p.rotor_r_m))
                           for p in oset.points]
                    lns = [(tuple(l.points), float(l.support_h_m), tuple(l.band)) for l in oset.lines]
                    try:
                        gpu.send(("obstacles", pts, lns))
                    except Exception:
                        pass
        if self._view_crash_at is not None and time.time() - self._view_crash_at > 2.0:
            self._view_crash_at = None
            self._view_send(("crash", False))

    # ---------------------------------------------------------------- отдельное окно HUD
    def _gl_build_toggle(self, parent):
        import tkinter as tk
        from tkinter import ttk
        self.sim_gl_var = tk.BooleanVar(value=False)
        cb = ttk.Checkbutton(parent, variable=self.sim_gl_var, command=self._gl_on_toggle)
        self._reg_i18n(cb, "text", "chk_sim_gpu_window")
        cb.pack(side="left", padx=(16, 0))
        self.sim_gl_cb = cb

    def _gl_on_toggle(self):
        if self.sim_gl_var.get():
            if self._view is None or not self._view.alive:
                try:
                    self._view = _Child(view_process_main, {"title": "Mission Analyzer — HUD", "icon": self._gl_icon_path()})
                except Exception as exc:
                    self._gl_log(f"[HUD-вікно] не запустилось: {exc}")
                    self.sim_gl_var.set(False)
                    return
                self._view_hud = None
                self._gl_watch_start()
                last = getattr(self, "_sim_fpv_latest", None)        # сразу показать последний кадр, если он есть
                if last is not None:
                    self._view_offer(last)
        else:
            view, self._view = self._view, None
            if view is not None:
                view.stop()

    def _view_send(self, msg):
        view = self._view
        if view is not None and view.alive:
            try:
                view.send(msg)
            except Exception:
                pass

    def _view_offer(self, image):
        """Готовый кадр (рабочий поток) -> окно. Следующий уходит, только когда окно приняло прошлый: ничего не копится."""
        view = self._view
        if view is None or not view.alive or not view.acked.is_set():
            return
        try:
            img = image
            if view.size:                                            # не больше окна: меньше байтов по каналу
                W, H = view.size
                k = min(W / img.width, H / img.height, 1.0)
                if k < 0.999:
                    img = img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))))
            if img.mode != "RGB":
                img = img.convert("RGB")
            self._view_frame_pose = getattr(image, "info", {}).get("scene_pose")
            self._view_frame_size = image.size
            view.acked.clear()
            view.send(("frame", img.width, img.height, img.tobytes()))
        except Exception:
            view.acked.set()

    def _gl_hud_send(self):
        """Из отрисовки штатного HUD: тот же HUD по размеру окна (+ красная точка) -> окно. До 30 раз/с."""
        self._gl_render_info()
        view = self._view
        if view is None or not view.alive or view.size is None:
            return
        now = time.time()
        if now - self._view_hud_at < 1.0 / 30.0:
            return
        self._view_hud_at = now
        tel, cfg = getattr(self, "_sim_hud_tel", None), getattr(self, "_sim_hud_cfg", None)
        if tel is None or cfg is None:
            return
        try:
            from hud_view import HudRenderer
            import nose_camera as nc
            W, H = view.size
            fs = self._view_frame_size or (nc.NOSE_CAMERA.width, nc.NOSE_CAMERA.height)
            k = min(W / fs[0], H / fs[1])
            video_h = fs[1] * k                                      # высота кадра в окне: масштаб линии тангажа, как на левом экране
            if self._view_hud is None or self._view_hud.cfg is not cfg:
                self._view_hud = HudRenderer(_HudRecorder(), cfg)
                self._view_hud._family = "mono"
            cam = nc.NOSE_CAMERA
            self._view_hud.render(tel, x0=0, y0=0, width=W, height=H, camera=cam if self._view_frame_size else None,
                                  video_height_px=video_h if self._view_frame_size else None)
            rec = self._view_hud.canvas
            self._view_dots(rec, cfg, cam, W, H, video_h)
            view.send(("hud", rec.snapshot()))
        except Exception as exc:
            if not getattr(self, "_view_hud_err", None):
                self._view_hud_err = True
                self._gl_log(f"[HUD-вікно] HUD: {type(exc).__name__}: {exc}")

    def _view_dots(self, rec, cfg, cam, W, H, video_h):
        """Красная точка на 3D-объектах -- как на левом экране (та же галочка, тот же размер), по позе кадра в окне."""
        pose = self._view_frame_pose
        items = [it for it in (getattr(self, "_scene_loaded", None) or []) if it.obj.enabled]
        if not cfg.is_on("object_dot") or pose is None or not items or not self._view_frame_size:
            return
        from scene_renderer import object_center_pixel, ZNEAR
        k = float(video_h) / cam.height
        r = float(getattr(self, "OBJECT_DOT_RADIUS", 2))
        for it in items:
            u, v, depth = object_center_pixel(it, pose, cam)
            if depth > ZNEAR and 0.0 <= u <= cam.width and 0.0 <= v <= cam.height:
                x, y = W / 2.0 + (u - cam.cx) * k, H / 2.0 + (v - cam.cy) * k
                rec.create_oval(x - r, y - r, x + r, y + r, fill="#ff2020", outline="")

    def _gl_render_info(self):
        """Строка во втором ряду страницы (раз в секунду): лес (карта ETH) -- тайлы, загрузка, что под бортом, ошибки."""
        var = getattr(self, "sim_render_info_var", None)
        now = time.time()
        if now - getattr(self, "_gl_info_at", 0.0) < 1.0:
            return
        self._gl_info_at = now
        loader = getattr(self, "_sim_obstacles", None)
        if loader is not None:
            self._gl_log_osm_errors("ЛЕП/споруди", loader)
        if var is None:
            return
        forest = getattr(self, "_sim_forest", None)
        if forest is None:
            if not hasattr(self, "_sim_canopy_at"):
                txt = _t("info_forest_old_file")
            elif getattr(self, "_sim_state", None) is not None:
                txt = _t("info_forest_failed_fmt", err=str(getattr(self, "_sim_forest_error", None) or "?")[:120])
            else:
                txt = _t("info_forest_off")
            bl = getattr(self, "_sim_buildings", None)
            var.set(txt + ("  |  " + self._gl_buildings_info(bl) if bl is not None else ""))
            return
        needed = list(getattr(forest, "needed", ()))
        have = [n for n in needed if forest.has(n)]
        parts = [_t("info_forest_fmt", have=len(have), total=len(needed))]
        dl = getattr(self, "_sim_canopy_dl", None)
        cur = dl.current if dl is not None else None
        if cur is not None:
            name, got, total = cur
            pct = f"{got * 100 / total:.0f}%" if total else f"{got / 1e6:.0f} МБ"
            parts.append(_t("info_forest_dl_fmt", tile=_short_tile(name), pct=pct))
        st = getattr(self, "_sim_state", None)
        if st is not None:
            from obstacles import canopy_tile_name
            name = canopy_tile_name(st["lat"], st["lon"])
            canopy = self._sim_canopy_at(st["lat"], st["lon"])
            if not forest.has(name):
                parts.append(_t("info_forest_tile_wait_fmt", tile=_short_tile(name)))
            elif canopy > 0:
                parts.append(_t("info_forest_here_fmt", m=canopy))
            else:
                parts.append(_t("info_forest_not_here"))
        errs = dict(getattr(forest, "errors", {}) or {})
        if dl is not None:
            errs.update(dl.failed)
        if errs:
            name, err = list(errs.items())[-1]
            parts.append(_t("info_forest_err_fmt", n=len(errs), err=f"{_short_tile(name)}: {err}"[:140]))
            missing = [n for n in needed if not forest.has(n)]
            if missing and dl is not None and not dl.queue and dl.current is None:   # качать вручную: имя файла и папка
                parts.append(_t("info_forest_manual_fmt", name=missing[0], folder=forest.dir))
        elif needed and len(have) == len(needed):
            parts.append(_t("info_forest_done_fmt", folder=forest.dir))
        bl = getattr(self, "_sim_buildings", None)
        if bl is not None:
            parts.append(self._gl_buildings_info(bl))
        var.set("  |  ".join(parts))

    def _gl_buildings_info(self, bl):
        """Часть строки про здания OSM: ячейки, сколько зданий, ход загрузки, что в кадре, последняя ошибка."""
        s_ = _t("info_bld_fmt", done=bl.cells_done, total=len(bl.cells), n=bl.total())
        gpu = self._gpu
        if gpu is not None and gpu.state == "ready" and getattr(gpu, "bstats", None) is not None:
            s_ += _t("info_bld_frame_fmt", n=gpu.bstats, km=BLD_RADIUS_M / 1000.0)
        if bl.part:
            s_ += f", {_t('info_bld_part_fmt', cell=bl.part[0], part=bl.part[1])}"
        if bl.last_error:
            s_ += _t("info_bld_err_fmt", n=bl.parts_failed, err=str(bl.last_error)[:100])
        return s_

    @staticmethod
    def _gl_nearest_line_km(oset, lat, lon):
        """Расстояние до ближайшего провода ЛЭП (км) в пределах NEAR_LINE_SEARCH_M или None: куда смотреть, чтобы её увидеть."""
        try:
            _pts, segs = oset.near(lat, lon, NEAR_LINE_SEARCH_M)
        except Exception:
            return None
        kx = 111320.0 * math.cos(math.radians(lat))
        best = None
        for line, i in segs:
            (la1, lo1), (la2, lo2) = line.points[i], line.points[i + 1]
            ax, ay = (lo1 - lon) * kx, (la1 - lat) * 110540.0
            bx, by = (lo2 - lon) * kx, (la2 - lat) * 110540.0
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            t = 0.0 if L2 <= 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L2))
            d = math.hypot(ax + dx * t, ay + dy * t)
            best = d if best is None or d < best else best
        return None if best is None or best > NEAR_LINE_SEARCH_M else best / 1000.0

    def _gl_log_osm_errors(self, what, loader):
        """Причины неудачных ячеек OSM -- в журнал, каждая ячейка один раз."""
        seen = self.__dict__.setdefault("_gl_osm_logged", set())
        for i, j, err in list(loader.failed):
            key = (what, id(loader), i, j)
            if key not in seen:
                seen.add(key)
                self._gl_log(f"[OSM] {what}: клітинка {i},{j} не завантажена: {err}")

    def _gl_crash(self):
        """Удар: красная рамка и IMPACT в окне (как на левом экране)."""
        if self._view is not None:
            self._view_crash_at = time.time()
            self._view_send(("crash", True))


def _short_tile(name):
    """ETH_GlobalCanopyHeight_10m_2020_N51E030_Map.tif -> N51E030."""
    try:
        return name.split("2020_")[1].split("_Map")[0]
    except IndexError:
        return name


def _t(key, **kw):
    import i18n
    return i18n.t(key, **kw)
