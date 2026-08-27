"""
overview_map.py — READ-ONLY карти для сторінки «Аналіз»: квадратні 4×4 км
навколо точок старту/посадки (Зліт/Глісада) і карта всього маршруту
«вигляд згори» (Траєкторія). Завжди на всю ширину вкладки, без зуму,
без панелі керування і БЕЗ можливості редагування місії.

Свідомо відокремлений від map_view.py: там — інтерактивна карта сторінки
«Місія» (з контролем зуму, у планах — редактор місії з перетягуванням
точок). Зміни в тій карті не повинні випадково зачепити ці, статичні,
огляди. Спільна лише "чиста" математика тайлів без стану --
compute_tile_bounds/fetch_tiles/_decode_tile_image беруться з map_view.py,
самі функції відмальовки тут повністю свої.
"""

from __future__ import annotations

import io
import math
import tkinter as tk

import i18n
from analyzer import MissionAnalyzer
from geo import TILE_SIZE, lonlat_to_tile_xy, lonlat_to_pixel
from map_view import _decode_tile_image, MapTooLargeError, draw_single_tile

try:
    from PIL import Image, ImageTk
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False


def compute_area_tile_bounds(lat: float, lon: float, zoom: int,
                              half_km: float = 2.0, max_tiles: int = 400):
    """
    Повертає діапазон тайлів для квадрата half_km*2 × half_km*2 км
    з центром у точці (lat, lon). За замовчуванням — 4×4 км (half_km=2).
    """
    dlat = half_km / 111.0
    dlon = half_km / (111.0 * math.cos(math.radians(lat)))

    lat_min, lat_max = lat - dlat, lat + dlat
    lon_min, lon_max = lon - dlon, lon + dlon

    tx1, ty1 = lonlat_to_tile_xy(lat_max, lon_min, zoom)  # north-west
    tx2, ty2 = lonlat_to_tile_xy(lat_min, lon_max, zoom)  # south-east
    tx_min, tx_max = min(tx1, tx2), max(tx1, tx2)
    ty_min, ty_max = min(ty1, ty2), max(ty1, ty2)

    total = (tx_max - tx_min + 1) * (ty_max - ty_min + 1)
    if total > max_tiles:
        raise MapTooLargeError(total)

    return tx_min, tx_max, ty_min, ty_max, total



def _compose_scaled_fit(tiles: dict, tx_min: int, tx_max: int, ty_min: int, ty_max: int,
                        target_w: int, target_h: int, bg_color: str = "#e8e8e8"):
    """
    Те саме, що _compose_scaled, але масштабує РІВНОМІРНО (один
    коефіцієнт для X і Y, "letterbox"/"contain"), без спотворення
    пропорцій. Потрібно там, де геометрична область НЕ квадратна за
    задумом (напр. bounding box усього маршруту -- compute_tile_bounds,
    на відміну від compute_area_tile_bounds, який завжди робить
    квадрат 4×4 км) -- інакше карта виглядає розтягнутою.
    Повертає (PhotoImage, scale, offset_x, offset_y) або None.
    """
    if not _HAS_PIL:
        return None

    grid_w = (tx_max - tx_min + 1) * TILE_SIZE
    grid_h = (ty_max - ty_min + 1) * TILE_SIZE

    mosaic = Image.new("RGB", (grid_w, grid_h), "#cccccc")
    for tx in range(tx_min, tx_max + 1):
        for ty in range(ty_min, ty_max + 1):
            data = tiles.get((tx, ty))
            if not data:
                continue
            try:
                tile_img = Image.open(io.BytesIO(data)).convert("RGB")
            except Exception:
                continue
            px = (tx - tx_min) * TILE_SIZE
            py = (ty - ty_min) * TILE_SIZE
            mosaic.paste(tile_img, (px, py))

    target_w = max(int(target_w), 1)
    target_h = max(int(target_h), 1)
    scale = min(target_w / grid_w, target_h / grid_h)
    draw_w = max(int(grid_w * scale), 1)
    draw_h = max(int(grid_h * scale), 1)
    resized = mosaic.resize((draw_w, draw_h), Image.LANCZOS)

    canvas_img = Image.new("RGB", (target_w, target_h), bg_color)
    offset_x = (target_w - draw_w) // 2
    offset_y = (target_h - draw_h) // 2
    canvas_img.paste(resized, (offset_x, offset_y))

    photo = ImageTk.PhotoImage(canvas_img)
    return photo, scale, offset_x, offset_y


def _draw_tiles(canvas: tk.Canvas, tiles: dict, image_refs: list,
                tx_min: int, tx_max: int, ty_min: int, ty_max: int,
                origin_x: float, origin_y: float):
    for tx in range(tx_min, tx_max + 1):
        for ty in range(ty_min, ty_max + 1):
            data = tiles.get((tx, ty))
            px = tx * TILE_SIZE - origin_x
            py = ty * TILE_SIZE - origin_y
            if data:
                img = _decode_tile_image(data)
                if img:
                    image_refs.append(img)
                    canvas.create_image(px, py, image=img, anchor="nw")
                    continue
            canvas.create_rectangle(px, py, px + TILE_SIZE, py + TILE_SIZE,
                                    fill="#cccccc", outline="#aaaaaa")


def begin_area_render(
    canvas: tk.Canvas, lat: float, lon: float, zoom: int,
    tx_min: int, tx_max: int, ty_min: int, ty_max: int,
    image_refs: list,
    placeholder_bg: str = "#cccccc", placeholder_outline: str = "#aaaaaa",
    flight_az: float | None = None,
    wind_dir: float | None = None,
    wind_spd: float | None = None,
) -> tuple[float, float]:
    """
    Перший крок прогресивної відмальовки area-карти (Взліт/Посадка --
    квадрат навколо ОДНІЄЇ точки, не весь маршрут) -- та сама ідея, що
    й begin_viewport_render у map_view.py: спершу плейсхолдери на
    місці кожного тайла + все, що не залежить від мережі (сама точка,
    компас N/E/S/W, стрілки азимуту/вітру), а тайли домальовуються
    ОКРЕМО по готовності (draw_single_tile з map_view.py, той самий,
    що й на "Місія" -- переиспользується напряму, лише з іншим
    raise_tag: "overlay_layer" замість "route_layer", тут-бо немає
    маршруту, лише компас/стрілки).

    Тайли малюються 1:1, БЕЗ PIL-масштабування під точний розмір
    канваса (як робив старий render_area_map через _compose_scaled) --
    невелика неточність охоплення (може вийти трохи більше/менше за
    номінальні 4х4 км) прийнятна для огляду однієї точки, натомість
    відмальовка миттєва й прогресивна, без "секунди на PIL.resize".

    Повертає (screen_origin_gx, screen_origin_gy) -- зберегти й
    передавати в кожен наступний виклик draw_single_tile() для цього
    самого рендеру.
    """
    canvas.update_idletasks()
    W = max(canvas.winfo_width(), 100)
    H = max(canvas.winfo_height(), 100)

    canvas.delete("all")
    image_refs.clear()

    center_gx, center_gy = lonlat_to_pixel(lat, lon, zoom)
    screen_origin_gx = center_gx - W / 2
    screen_origin_gy = center_gy - H / 2

    for tx in range(tx_min, tx_max + 1):
        for ty in range(ty_min, ty_max + 1):
            px = tx * TILE_SIZE - screen_origin_gx
            py = ty * TILE_SIZE - screen_origin_gy
            canvas.create_rectangle(
                px, py, px + TILE_SIZE, py + TILE_SIZE,
                fill=placeholder_bg, outline=placeholder_outline, tags=(f"tile_{tx}_{ty}",),
            )

    cx, cy = W / 2, H / 2
    R = min(W, H) // 2 - 16

    canvas.create_oval(
        cx - 6, cy - 6, cx + 6, cy + 6,
        fill="#FFFFFF", outline="#000000", width=2, tags="overlay_layer",
    )

    for ang, lbl in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):
        rad = math.radians(ang)
        x = cx + (R + 14) * math.sin(rad)
        y = cy - (R + 14) * math.cos(rad)
        canvas.create_text(
            x, y, text=lbl, fill="#000000",
            font=("Segoe UI", 8, "bold"), tags="overlay_layer",
        )

    def arrow(az_deg: float, length: float, color: str, width: int, lbl: str, lbl_color: str):
        rad = math.radians(az_deg)
        ex = cx + length * math.sin(rad)
        ey = cy - length * math.cos(rad)
        canvas.create_line(
            cx, cy, ex, ey, fill=color, width=width,
            arrow="last", arrowshape=(12, 14, 5), tags="overlay_layer",
        )
        lx = cx + (length + 20) * math.sin(rad)
        ly = cy - (length + 20) * math.cos(rad)
        canvas.create_text(
            lx, ly, text=lbl, fill=lbl_color,
            font=("Segoe UI", 8, "bold"), tags="overlay_layer",
        )

    if flight_az is not None:
        arrow(flight_az, R * 0.70, "#39FF14", 3, f"Az {flight_az:.0f}°", "#39FF14")

    if wind_dir is not None:
        wind_to = (wind_dir + 180) % 360
        arrow(
            wind_to, R * 0.60, "#00BFFF", 3,
            f"{wind_spd:.0f}{i18n.t('unit_kmh_short')}\n{wind_dir:.0f}°", "#00BFFF",
        )

        if flight_az is not None:
            diff = abs((wind_dir - flight_az + 360) % 360)
            if diff > 180:
                diff = 360 - diff
            cross = abs(90 - abs(diff - 90))
            color = "#FF4444" if cross > 30 else "#44FF88"
            canvas.create_rectangle(
                4, H - 22, W - 4, H - 4,
                fill="#000000", outline="", stipple="gray50", tags="overlay_layer",
            )
            canvas.create_text(
                W // 2, H - 12, fill=color, font=("Segoe UI", 8, "bold"),
                text=i18n.t("weather_crosswind_map_label_fmt", cross=cross), tags="overlay_layer",
            )

    canvas.config(scrollregion=(0, 0, W, H))
    return screen_origin_gx, screen_origin_gy


def render_area_map(canvas: tk.Canvas, lat: float, lon: float, zoom: int,
                    tiles: dict, image_refs: list,
                    tx_min: int, tx_max: int, ty_min: int, ty_max: int,
                    flight_az: float | None = None,
                    wind_dir: float | None = None,
                    wind_spd: float | None = None):
    """
    Синхронна (НЕ прогресивна) обгортка над begin_area_render()/
    draw_single_tile() -- для місць, де всі тайли вже готові заздалегідь
    і прогресивність не потрібна. Для нового мережевого завантаження
    (analysis_page.py) використовуються ці дві функції окремо, кожен
    тайл малюється одразу по готовності.
    """
    screen_origin_gx, screen_origin_gy = begin_area_render(
        canvas, lat, lon, zoom, tx_min, tx_max, ty_min, ty_max, image_refs,
        flight_az=flight_az, wind_dir=wind_dir, wind_spd=wind_spd,
    )
    for tx in range(tx_min, tx_max + 1):
        for ty in range(ty_min, ty_max + 1):
            draw_single_tile(
                canvas, image_refs, tx, ty, tiles.get((tx, ty)),
                screen_origin_gx, screen_origin_gy, raise_tag="overlay_layer",
            )


def _compose_scaled_width(tiles: dict, tx_min: int, tx_max: int, ty_min: int, ty_max: int,
                          target_w: int):
    """
    Те саме, що _compose_scaled_fit, але масштабує ЛИШЕ по ширині
    (scale = target_w / grid_w), без обмеження по висоті. Contain-fit
    (_compose_scaled_fit) підганяє під МЕНШУ зі сторін target_w/target_h
    -- якщо контейнер хоч трохи ширший за реальні пропорції маршруту
    (a так майже завжди, бо вгадати точну висоту наперед неможливо),
    висота "перемагає", і по боках лишається сірий letterbox. Тут
    ширина ЗАВЖДИ точно target_w; якщо висота вийде більшою за видиму
    область канваса -- для цього є вертикальний скролбар (як на "Місія").
    Повертає (PhotoImage, scale) або None, якщо Pillow не встановлено.
    """
    if not _HAS_PIL:
        return None

    grid_w = (tx_max - tx_min + 1) * TILE_SIZE
    grid_h = (ty_max - ty_min + 1) * TILE_SIZE

    mosaic = Image.new("RGB", (grid_w, grid_h), "#cccccc")
    for tx in range(tx_min, tx_max + 1):
        for ty in range(ty_min, ty_max + 1):
            data = tiles.get((tx, ty))
            if not data:
                continue
            try:
                tile_img = Image.open(io.BytesIO(data)).convert("RGB")
            except Exception:
                continue
            px = (tx - tx_min) * TILE_SIZE
            py = (ty - ty_min) * TILE_SIZE
            mosaic.paste(tile_img, (px, py))

    target_w = max(int(target_w), 1)
    scale = target_w / grid_w
    draw_h = max(int(grid_h * scale), 1)
    resized = mosaic.resize((target_w, draw_h), Image.LANCZOS)

    photo = ImageTk.PhotoImage(resized)
    return photo, scale


def render_route_overview(canvas: tk.Canvas, analyzer: MissionAnalyzer, zoom: int,
                          tx_min: int, tx_max: int, ty_min: int, ty_max: int,
                          tiles: dict, image_refs: list):
    """
    Карта всього маршруту «вигляд згори» для вкладки «Траєкторія».
    Read-only: лінія маршруту + точки, БЕЗ панелі керування зумом і БЕЗ
    можливості перетягувати/редагувати точки (на відміну від майбутнього
    редактора місій на сторінці «Місія» -- це навмисно окрема функція).

    Масштабується ЛИШЕ по ширині (_compose_scaled_width) -- як і на
    сторінці "Місія": контейнер більше не намагається підлаштувати
    власну висоту під пропорції маршруту (це ненадійно -- висота вікна
    не гумова), тому carta завжди рівно на ширину блока, без сірих
    полів по боках.
    """
    canvas.delete("all")
    image_refs.clear()

    canvas.update_idletasks()
    W = max(canvas.winfo_width(), 100)

    origin_x = tx_min * TILE_SIZE
    origin_y = ty_min * TILE_SIZE

    composed = _compose_scaled_width(tiles, tx_min, tx_max, ty_min, ty_max, W)
    if composed:
        photo, scale = composed
        image_refs.append(photo)
        canvas.create_image(0, 0, image=photo, anchor="nw")
    else:
        _draw_tiles(canvas, tiles, image_refs, tx_min, tx_max, ty_min, ty_max, origin_x, origin_y)
        scale = 1.0
    offset_x = offset_y = 0

    issues_by_wp: dict[int, list[str]] = {}
    for it in analyzer.issues:
        issues_by_wp.setdefault(it["wp_index"], []).append(it["type"])

    route_px = []
    for wp in analyzer.nav_wps:
        gx, gy = lonlat_to_pixel(wp.lat, wp.lon, zoom)
        route_px.append(((gx - origin_x) * scale + offset_x, (gy - origin_y) * scale + offset_y, wp))

    for i in range(len(route_px) - 1):
        x1, y1, _ = route_px[i]
        x2, y2, _ = route_px[i + 1]
        canvas.create_line(x1, y1, x2, y2, fill="#1f77b4", width=3)

    for x, y, wp in route_px:
        color = "#d62728" if wp.index in issues_by_wp else "#1f77b4"
        canvas.create_oval(x - 6, y - 6, x + 6, y + 6, fill=color, outline="white", width=1)
        canvas.create_text(x, y - 12, text=str(wp.index), font=("Arial", 8, "bold"))

    if composed:
        canvas.config(scrollregion=(0, 0, photo.width(), photo.height()))
        return photo.width(), photo.height()
    else:
        grid_w = (tx_max - tx_min + 1) * TILE_SIZE
        grid_h = (ty_max - ty_min + 1) * TILE_SIZE
        canvas.config(scrollregion=(0, 0, grid_w, grid_h))
        return grid_w, grid_h
