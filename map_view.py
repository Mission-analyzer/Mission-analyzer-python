"""
map_view.py — отрисовка маршрута поверх офлайн-тайлов Mission Planner
на tk.Canvas, плюс бонусный экспорт маршрута в автономный HTML (Leaflet,
нужен браузер и интернет — на случай если он всё же есть).
"""

from __future__ import annotations

import io
import json
import tkinter as tk

from analyzer import MissionAnalyzer
from geo import TILE_SIZE, lonlat_to_tile_xy, lonlat_to_pixel
import i18n

# Tkinter из коробки умеет только PNG/GIF. Многие провайдеры карт в кэше
# Mission Planner (например GoogleSatelliteMap) хранят тайлы в JPEG — без
# Pillow такие тайлы не декодировать. Pillow опционален: если он есть,
# используем его как запасной декодер для форматов, которые не осилил Tk.
try:
    from PIL import Image, ImageTk
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

# публічний, без підкреслення -- щоб mission_page.py міг перевірити ДО
# побудови UI, чи буде доступний масштабований (render_tiles_fit) режим,
# і зібрати відповідний варіант канваса (без скролбарів) чи старий
# (зі скролбарами, нативний розмір тайлів) -- а не з'ясовувати це вже
# по факту кривого рендеру, як щойно сталося.
HAS_PIL = _HAS_PIL


def _decode_tile_image(data: bytes):
    """Возвращает PhotoImage, пригодный для canvas.create_image, либо None."""
    try:
        return tk.PhotoImage(data=data)
    except tk.TclError:
        pass
    if _HAS_PIL:
        try:
            return ImageTk.PhotoImage(Image.open(io.BytesIO(data)))
        except Exception:
            return None
    return None


class MapTooLargeError(Exception):
    def __init__(self, total: int):
        self.total = total
        super().__init__(f"too many tiles: {total}")


def compute_tile_bounds(
    analyzer: MissionAnalyzer, zoom: int, max_tiles: int = 400,
    target_aspect: float | None = None,
):
    """
    Считает диапазон тайлов под маршрут. Без сети и без Tkinter — можно
    звать откуда угодно.

    Класична задача "вписати прямокутник маршруту в прямокутник канваса"
    БЕЗ спотворення пропорцій (єдиний коефіцієнт масштабу для X і Y):
    - визначаємо, який із двох прямокутників (маршрут / канвас)
      пропорційно "вертикальніший" -- порівнюємо їхні aspect ratio
      (ширина/висота) напряму, як числа, без категорій
      "портрет/ландшафт" (працює для будь-якої комбінації орієнтацій
      обох прямокутників);
    - якщо маршрут вертикальніший за канвас (route_aspect < canvas_
      aspect) -- прив'язуємось по ВИСОТІ (вона й так впритул), а
      ширину розширюємо, щоб дібрати запас по боках РЕАЛЬНОЮ картою
      (не білим/сірим) до потрібних пропорцій;
    - якщо маршрут горизонтальніший -- дзеркально, прив'язуємось по
      ШИРИНІ, розширюємо висоту (запас зверху/знизу);
    - якщо пропорції збігаються -- розширювати нічого не треба.

    target_aspect (canvas_width / canvas_height), якщо задано --
    розширює КОРОТШИЙ вимір діапазону тайлів (по X або Y, цілими
    тайлами, СИМЕТРИЧНО з обох боків) так, щоб підсумкові пропорції
    діапазону тайлів збігались із пропорціями канваса. Завдяки цьому
    подальше масштабування ОДНИМ коефіцієнтом (_compose_scaled_width)
    заповнює канвас повністю, без порожніх білих полів -- і БЕЗ
    геометричного спотворення карти (принципово важливо для
    планувальника місій: супутникові знімки/дороги не повинні виглядати
    розтягнутими). Розширення -- лише ДОДАЄ область показу реальною
    картою, ніколи не обрізає маршрут.
    """
    pts = analyzer.nav_wps
    if not pts:
        raise ValueError("no points with coordinates")

    lats = [wp.lat for wp in pts]
    lons = [wp.lon for wp in pts]
    # запас навколо маршруту без самої лінії -- 5% з кожного боку,
    # мінімум лишається достатнім, щоб точки на самому краю не впирались
    # у рамку канваса впритул
    pad_lat = max((max(lats) - min(lats)) * 0.05, 0.002)
    pad_lon = max((max(lons) - min(lons)) * 0.05, 0.002)
    lat_min, lat_max = min(lats) - pad_lat, max(lats) + pad_lat
    lon_min, lon_max = min(lons) - pad_lon, max(lons) + pad_lon

    tx1, ty1 = lonlat_to_tile_xy(lat_max, lon_min, zoom)  # north-west
    tx2, ty2 = lonlat_to_tile_xy(lat_min, lon_max, zoom)  # south-east
    tx_min, tx_max = min(tx1, tx2), max(tx1, tx2)
    ty_min, ty_max = min(ty1, ty2), max(ty1, ty2)

    if target_aspect is not None and target_aspect > 0:
        cur_w = tx_max - tx_min + 1
        cur_h = ty_max - ty_min + 1
        cur_aspect = cur_w / cur_h
        if cur_aspect < target_aspect:
            # маршрут вертикальніший за канвас -- прив'язка по висоті,
            # розширюємо ширину (запас по боках)
            needed_w = max(int(round(cur_h * target_aspect)), cur_w)
            extra = needed_w - cur_w
            left = extra // 2
            tx_min -= left
            tx_max += extra - left
        elif cur_aspect > target_aspect:
            # маршрут горизонтальніший за канвас -- прив'язка по ширині,
            # розширюємо висоту (запас зверху/знизу)
            needed_h = max(int(round(cur_w / target_aspect)), cur_h)
            extra = needed_h - cur_h
            top = extra // 2
            ty_min -= top
            ty_max += extra - top

        n = 2 ** zoom
        tx_min = max(0, tx_min)
        tx_max = min(n - 1, tx_max)
        ty_min = max(0, ty_min)
        ty_max = min(n - 1, ty_max)

    total = (tx_max - tx_min + 1) * (ty_max - ty_min + 1)
    if total > max_tiles:
        raise MapTooLargeError(total)

    return tx_min, tx_max, ty_min, ty_max, total


def compute_viewport_tile_bounds(
    center_lat: float, center_lon: float, zoom: int, canvas_w: int, canvas_h: int,
    buffer_factor: float = 2.0,
) -> tuple[int, int, int, int]:
    """
    Діапазон тайлів навколо КОНКРЕТНОЇ точки (не всього маршруту) --
    основа "як у Mission Planner": на високому зумі довантажуємо лише
    видиму (+запас buffer_factor) область навколо центру огляду, а не
    весь маршрут одразу. Завдяки цьому кількість тайлів залежить від
    розміру КАНВАСА, а не від довжини маршруту -- зум завжди доступний
    аж до 19, незалежно від того, наскільки протяжна місія.

    buffer_factor=2.0 -- запас для панорамування без миттєвого
    дозавантаження (див. _on_map_press_edit/_on_map_release_edit-подібну
    логіку панорамування в mission_page.py).
    """
    cx, cy = lonlat_to_pixel(center_lat, center_lon, zoom)
    half_w = canvas_w * buffer_factor / 2
    half_h = canvas_h * buffer_factor / 2

    n = 2 ** zoom
    max_px = n * TILE_SIZE
    px_min = max(0, cx - half_w)
    px_max = min(max_px, cx + half_w)
    py_min = max(0, cy - half_h)
    py_max = min(max_px, cy + half_h)

    tx_min = int(px_min // TILE_SIZE)
    tx_max = int(px_max // TILE_SIZE)
    ty_min = int(py_min // TILE_SIZE)
    ty_max = int(py_max // TILE_SIZE)
    return tx_min, tx_max, ty_min, ty_max


def fetch_tiles(
    tile_cache,
    tx_min: int, tx_max: int, ty_min: int, ty_max: int, zoom: int,
    progress_cb=None,
    cancel_event=None,
    max_workers: int = 6,
    coords: list[tuple[int, int]] | None = None,
    tile_ready_cb=None,
) -> tuple[dict, bool]:
    """
    Скачивает/читает все тайлы диапазона ПАРАЛЛЕЛЬНО (как это делает браузер).
    Никакого Tkinter здесь нет — безопасно звать из фонового потока, чтобы
    не подвешивать окно программы во время сетевых запросов.

    coords -- якщо задано, явний список конкретних пар (tx,ty) замість
    прямокутника tx_min..tx_max x ty_min..ty_max (наприклад, коли частина
    тайлів уже є з іншого джерела -- див. analysis_page._load_trajectory_map,
    де карта "Маршрут" довантажує лише те, чого бракує з карти "Місія",
    а не весь діапазон заново).

    tile_ready_cb(tx, ty, data) -- якщо задано, викликається ОДРАЗУ по
    готовності КОЖНОГО окремого тайла (data -- bytes|None), не чекаючи
    решти -- для прогресивної відмальовки (mission_page.py: кожен тайл
    з'являється на екрані одразу, а не всі разом лише в кінці). Той
    самий цикл as_completed, що вже й так обробляє тайли по мірі
    готовності (не по порядку) -- просто тепер ще й повідомляє про
    кожен окремо, а не лише лічильником done/total.

    Возвращает (словарь {(tx,ty): bytes|None}, отменено_ли).
    """
    import concurrent.futures

    if coords is None:
        coords = [(tx, ty) for tx in range(tx_min, tx_max + 1) for ty in range(ty_min, ty_max + 1)]
    total = len(coords)
    tiles: dict = {}
    done = 0
    cancelled = False

    if total == 0:
        return tiles, cancelled

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(tile_cache.get_tile, zoom, tx, ty): (tx, ty) for tx, ty in coords}
        for fut in concurrent.futures.as_completed(futures):
            tx, ty = futures[fut]
            try:
                data = fut.result()
            except Exception:
                data = None
            tiles[(tx, ty)] = data
            if tile_ready_cb:
                tile_ready_cb(tx, ty, data)
            done += 1
            if progress_cb:
                progress_cb(done, total)
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                for f in futures:
                    f.cancel()
                break

    return tiles, cancelled


def render_tiles(
    canvas: tk.Canvas,
    analyzer: MissionAnalyzer,
    zoom: int,
    tx_min: int, tx_max: int, ty_min: int, ty_max: int,
    tiles: dict,
    image_refs: list,
    overlay_polygons: list | None = None,
) -> tuple[int, int, int, int, int, int]:
    """
    Рисует уже скачанные тайлы (см. fetch_tiles), опциональный слой полигонов
    (например, оккупированных территорий) поверх них, и маршрут поверх всего.
    Трогает Tkinter — звать только из главного потока.

    Возвращает (отрисовано, всего, нет_в_кэше, не_декодировано,
    ширина_картинки, висота_картинки).
    """
    canvas.delete("all")
    image_refs.clear()

    origin_x = tx_min * TILE_SIZE
    origin_y = ty_min * TILE_SIZE

    found = 0
    undecodable = 0
    total = 0
    for tx in range(tx_min, tx_max + 1):
        for ty in range(ty_min, ty_max + 1):
            total += 1
            data = tiles.get((tx, ty))
            px = tx * TILE_SIZE - origin_x
            py = ty * TILE_SIZE - origin_y

            if data is not None:
                img = _decode_tile_image(data)
                if img is not None:
                    image_refs.append(img)
                    canvas.create_image(px, py, image=img, anchor="nw")
                    found += 1
                    continue
                undecodable += 1
                canvas.create_rectangle(px, py, px + TILE_SIZE, py + TILE_SIZE, fill="#ffe0b3", outline="#cc9955")
                canvas.create_text(
                    px + TILE_SIZE / 2, py + TILE_SIZE / 2,
                    text=i18n.t("map_jpeg_no_pillow"), font=("Arial", 8), fill="#996633",
                )
                continue

            canvas.create_rectangle(px, py, px + TILE_SIZE, py + TILE_SIZE, fill="#cccccc", outline="#aaaaaa")
            canvas.create_text(
                px + TILE_SIZE / 2, py + TILE_SIZE / 2,
                text=i18n.t("map_no_tile"), font=("Arial", 8), fill="#777777",
            )

    if overlay_polygons:
        _draw_polygon_overlay(canvas, overlay_polygons, zoom, origin_x, origin_y)

    grid_w = (tx_max - tx_min + 1) * TILE_SIZE
    grid_h = (ty_max - ty_min + 1) * TILE_SIZE

    route_px = _draw_route(canvas, analyzer, zoom, origin_x, origin_y)

    canvas.config(scrollregion=(0, 0, grid_w, grid_h))
    _center_on_route(canvas, route_px, grid_w, grid_h)

    missing = total - found - undecodable
    return found, total, missing, undecodable, grid_w, grid_h


def _compose_scaled_fit(tiles: dict, tx_min: int, tx_max: int, ty_min: int, ty_max: int,
                        target_w: int, target_h: int, bg_color: str = "#e8e8e8"):
    """
    Збирає тайли в одну мозаїку і масштабує РІВНОМІРНО (один коефіцієнт
    для X і Y, "letterbox"/"contain", без спотворення пропорцій) під
    (target_w, target_h) -- реальний розмір канваса. Той самий прийом,
    що й у overview_map.py для карт на сторінці "Аналіз" -- тут окрема
    копія навмисно (map_view.py не повинен залежати від overview_map.py
    і навпаки, щоб зміни в інтерактивній карті не чіпали read-only
    огляди, див. докстрінг overview_map.py).
    Повертає (PhotoImage, scale, offset_x, offset_y) або None, якщо
    Pillow не встановлено (тоді викликач має намалювати тайли в
    натуральну величину зі скролом як fallback).
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
    resized = mosaic.resize((draw_w, draw_h), Image.BILINEAR)

    canvas_img = Image.new("RGB", (target_w, target_h), bg_color)
    offset_x = (target_w - draw_w) // 2
    offset_y = (target_h - draw_h) // 2
    canvas_img.paste(resized, (offset_x, offset_y))

    photo = ImageTk.PhotoImage(canvas_img)
    return photo, scale, offset_x, offset_y


def _compose_scaled_width(tiles: dict, tx_min: int, tx_max: int, ty_min: int, ty_max: int,
                          target_w: int):
    """
    Збирає тайли в одну мозаїку і масштабує ЛИШЕ по ширині (scale =
    target_w / grid_w) -- на відміну від _compose_scaled_fit (contain,
    підганяє під МЕНШУ зі сторін target_w/target_h), тут висота ніяк не
    обмежується. Навмисно: якщо висоти вікна фізично не вистачає під
    "правильну" висоту для даної ширини (реальний випадок -- вікно
    ширше, ніж високе), contain-fit підганяв би під куций залишок
    висоти і лишав сірі поля З БОКІВ -- а ширина екрана важливіша.
    Тут ширина ЗАВЖДИ точно target_w, висота -- яка вийде; якщо вона
    більша за видиму область канваса, для цього лишається вертикальний
    скролбар (див. render_tiles_fit).
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
    draw_w = target_w
    draw_h = max(int(grid_h * scale), 1)
    # BILINEAR замість LANCZOS -- при сильному зменшенні (велика мозаїка
    # тайлів стискається під ширину екрана) LANCZOS давав приблизно
    # секунду тільки на цей крок (виміряно [timing]-логами), BILINEAR
    # у кілька разів швидший при практично непомітній різниці якості
    # для супутникових/OSM-тайлів на такому масштабі стиснення.
    resized = mosaic.resize((draw_w, draw_h), Image.BILINEAR)

    photo = ImageTk.PhotoImage(resized)
    return photo, scale


def begin_viewport_render(
    canvas: tk.Canvas,
    analyzer: MissionAnalyzer,
    zoom: int,
    center_lat: float, center_lon: float,
    tx_min: int, tx_max: int, ty_min: int, ty_max: int,
    image_refs: list,
    overlay_polygons: list | None = None,
    placeholder_bg: str = "#cccccc",
    placeholder_outline: str = "#aaaaaa",
) -> tuple[float, float]:
    """
    Перший крок прогресивної відмальовки "вікна в карту" (mission_page.py:
    кожен тайл з'являється на екрані одразу по готовності, а не всі
    разом лише в кінці -- уникає "порожнього білого екрана" на час
    завантаження). Викликається ОДИН раз, синхронно, ще ДО того, як
    почалось власне мережеве завантаження тайлів:
    - очищає канвас і малює ПЛЕЙСХОЛДЕРИ на місці КОЖНОГО тайла (щоб
      одразу було видно структуру сітки й маршрут, а не пустечу).
      placeholder_bg/placeholder_outline -- кольори ПІД ПОТОЧНУ ТЕМУ
      (передаються з mission_page.py, той самий колір, що й фон самого
      канваса, c["map_placeholder_bg"]) -- інакше в темній темі при
      кожному новому рендері мигав би контрастний СВІТЛО-СІРИЙ
      прямокутник на місці кожного ще не завантаженого тайла;
    - малює полігони окупованих територій і сам маршрут (лінія + точки)
      ОДРАЗУ, поверх плейсхолдерів -- це найважливіший вміст, він не
      повинен чекати завантаження тайлів;
    - тег "route_layer" на всіх елементах маршруту -- потрібен, щоб
      draw_single_tile() міг піднімати маршрут НАД щойно домальованим
      тайлом (інакше кожен новий тайл малювався б ПОВЕРХ лінії/точок,
      ховаючи їх -- порядок canvas-об'єктів = порядок створення).

    Повертає (screen_origin_gx, screen_origin_gy) -- зберегти й
    передавати в кожен наступний виклик draw_single_tile() для цього
    самого рендеру.
    """
    canvas.update_idletasks()
    canvas_w = max(canvas.winfo_width(), 100)
    canvas_h = max(canvas.winfo_height(), 100)

    canvas.delete("all")
    image_refs.clear()

    center_gx, center_gy = lonlat_to_pixel(center_lat, center_lon, zoom)
    screen_origin_gx = center_gx - canvas_w / 2
    screen_origin_gy = center_gy - canvas_h / 2

    for tx in range(tx_min, tx_max + 1):
        for ty in range(ty_min, ty_max + 1):
            px = tx * TILE_SIZE - screen_origin_gx
            py = ty * TILE_SIZE - screen_origin_gy
            canvas.create_rectangle(
                px, py, px + TILE_SIZE, py + TILE_SIZE,
                fill=placeholder_bg, outline=placeholder_outline, tags=(f"tile_{tx}_{ty}",),
            )

    if overlay_polygons:
        _draw_polygon_overlay(canvas, overlay_polygons, zoom, screen_origin_gx, screen_origin_gy)

    _draw_route(canvas, analyzer, zoom, screen_origin_gx, screen_origin_gy)
    canvas.addtag_withtag("route_layer", "wp_marker")
    canvas.addtag_withtag("route_layer", "wp_line")

    canvas.config(scrollregion=(0, 0, canvas_w, canvas_h))
    return screen_origin_gx, screen_origin_gy


def draw_single_tile(
    canvas: tk.Canvas,
    image_refs: list,
    tx: int, ty: int,
    data: bytes | None,
    screen_origin_gx: float, screen_origin_gy: float,
    raise_tag: str = "route_layer",
) -> str:
    """
    Другий крок прогресивної відмальовки -- викликається ОКРЕМО на
    КОЖЕН тайл по готовності (mission_page.py: self.after(0, ...) на
    кожен тайл, з перевіркою покоління рендеру ПЕРЕД викликом -- щоб
    застарілий результат від попереднього (вже неактуального) зуму не
    домалювався поверх нового). Замінює сірий плейсхолдер цієї позиції
    (тег "tile_{tx}_{ty}", проставлений в begin_viewport_render) на
    реальне зображення тайла -- чи на позначку помилки, якщо байтів
    немає чи вони не розпізнались.

    Піднімає raise_tag (за замовчуванням "route_layer" -- лінію/точки
    маршруту на "Місія"; для area-карт Взліт/Посадка передається
    "overlay_layer" -- компас/стрілки азимуту-вітру, там маршруту
    немає) НАД щойно доданим тайлом -- інакше кожен новий тайл
    малювався б поверх цього вмісту, ховаючи його (порядок canvas-
    об'єктів = порядок створення).

    Повертає "found" / "undecodable" / "missing" -- для підрахунку
    статистики в _on_render_finish (mission_page.py), той самий сенс,
    що й found/undecodable у старому render_viewport.
    """
    tag = f"tile_{tx}_{ty}"
    px = tx * TILE_SIZE - screen_origin_gx
    py = ty * TILE_SIZE - screen_origin_gy

    canvas.delete(tag)

    result = "missing"
    if data is not None:
        img = _decode_tile_image(data)
        if img is not None:
            image_refs.append(img)
            canvas.create_image(px, py, image=img, anchor="nw", tags=(tag,))
            result = "found"
        else:
            canvas.create_rectangle(
                px, py, px + TILE_SIZE, py + TILE_SIZE,
                fill="#ffe0b3", outline="#cc9955", tags=(tag,),
            )
            canvas.create_text(
                px + TILE_SIZE / 2, py + TILE_SIZE / 2,
                text=i18n.t("map_jpeg_no_pillow"), font=("Arial", 8), fill="#996633", tags=(tag,),
            )
            result = "undecodable"
    else:
        canvas.create_rectangle(
            px, py, px + TILE_SIZE, py + TILE_SIZE,
            fill="#cccccc", outline="#aaaaaa", tags=(tag,),
        )
        canvas.create_text(
            px + TILE_SIZE / 2, py + TILE_SIZE / 2,
            text=i18n.t("map_no_tile"), font=("Arial", 8), fill="#777777", tags=(tag,),
        )

    canvas.tag_raise(raise_tag)
    return result


def render_viewport(
    canvas: tk.Canvas,
    analyzer: MissionAnalyzer,
    zoom: int,
    center_lat: float, center_lon: float,
    tx_min: int, tx_max: int, ty_min: int, ty_max: int,
    tiles: dict,
    image_refs: list,
    overlay_polygons: list | None = None,
) -> tuple[int, int, int, int, int, int]:
    """
    "Вікно в карту" навколо (center_lat, center_lon) -- як у Mission
    Planner: тайли позиціонуються так, щоб ця геоточка опинилась РІВНО
    в центрі КАНВАСА. Синхронна, НЕ прогресивна версія -- всі тайли вже
    ГОТОВІ заздалегідь (tiles dict повний) і малюються одразу, без
    поетапного заповнення. Для нового мережевого завантаження (mission_
    page.py: worker() качає тайли й показує кожен одразу по готовності)
    використовуються begin_viewport_render()/draw_single_tile() окремо
    -- ця функція лишається як проста синхронна обгортка над ними, для
    місць, де прогресивність не потрібна (напр. analysis_page.py: карта
    "Маршрут" бере вже повністю готовий набір тайлів з "Місія" -- там
    попроцесно малювати нема сенсу, усе й так одразу в пам'яті).

    Повертає (відрисовано, всього, нет_в_кеші, не_декодировано,
    screen_origin_gx, screen_origin_gy) -- останні два: глобальні
    пікселі (у системі lonlat_to_pixel цього zoom), що відповідають
    ЕКРАННІЙ (0,0) канваса -- потрібні для перетворення координат миші
    в lat/lon (drag точок, підказка при наведенні тощо, той самий
    формат, що очікує mission_editor.py).
    """
    screen_origin_gx, screen_origin_gy = begin_viewport_render(
        canvas, analyzer, zoom, center_lat, center_lon,
        tx_min, tx_max, ty_min, ty_max, image_refs, overlay_polygons,
    )

    found = undecodable = 0
    total = (tx_max - tx_min + 1) * (ty_max - ty_min + 1)
    for tx in range(tx_min, tx_max + 1):
        for ty in range(ty_min, ty_max + 1):
            result = draw_single_tile(canvas, image_refs, tx, ty, tiles.get((tx, ty)), screen_origin_gx, screen_origin_gy)
            if result == "found":
                found += 1
            elif result == "undecodable":
                undecodable += 1

    missing = total - found - undecodable
    return found, total, missing, undecodable, int(screen_origin_gx), int(screen_origin_gy)


def render_tiles_fit(
    canvas: tk.Canvas,
    analyzer: MissionAnalyzer,
    zoom: int,
    tx_min: int, tx_max: int, ty_min: int, ty_max: int,
    tiles: dict,
    image_refs: list,
    overlay_polygons: list | None = None,
) -> tuple[int, int, int, int, int, int]:
    """
    Те саме, що render_tiles, але карта масштабується ТОЧНО ПО ШИРИНІ
    канваса, ОДНИМ коефіцієнтом (_compose_scaled_width, без спотворення
    пропорцій -- супутникові знімки/дороги не виглядають розтягнутими).

    Заповнення канваса без порожніх білих полів досягається РАНІШЕ, на
    рівні compute_tile_bounds(target_aspect=...): та функція вже
    розширює коротший вимір діапазону тайлів під пропорції канваса
    (симетрично з обох боків, реальною картою, не білим) -- тому
    просте масштабування "рівно по ширині" тут автоматично заповнює й
    висоту теж.

    Якщо Pillow не встановлено -- тихо повертається до старої поведінки
    (render_tiles, натуральний розмір + скрол), бо без Pillow нема чим
    масштабувати мозаїку тайлів.
    """
    canvas.update_idletasks()
    W = max(canvas.winfo_width(), 100)

    composed = _compose_scaled_width(tiles, tx_min, tx_max, ty_min, ty_max, W)
    if composed is None:
        # немає Pillow -- масштабувати нема чим, малюємо як раніше
        return render_tiles(
            canvas, analyzer, zoom, tx_min, tx_max, ty_min, ty_max, tiles, image_refs,
            overlay_polygons=overlay_polygons,
        )
    photo, scale = composed

    canvas.delete("all")
    image_refs.clear()

    image_refs.append(photo)
    canvas.create_image(0, 0, image=photo, anchor="nw")

    found = sum(1 for v in tiles.values() if v is not None)
    total = (tx_max - tx_min + 1) * (ty_max - ty_min + 1)
    missing = total - found
    undecodable = 0  # PIL сам мовчки пропускає нечитані тайли в мозаїці -- рахуємо як "відсутні"

    origin_x = tx_min * TILE_SIZE
    origin_y = ty_min * TILE_SIZE

    if overlay_polygons:
        _draw_polygon_overlay(canvas, overlay_polygons, zoom, origin_x, origin_y, scale=scale)

    _draw_route(canvas, analyzer, zoom, origin_x, origin_y, scale=scale)

    draw_w = photo.width()
    draw_h = photo.height()
    canvas.config(scrollregion=(0, 0, draw_w, draw_h))

    return found, total, missing, undecodable, draw_w, draw_h


def _draw_polygon_overlay(
    canvas: tk.Canvas,
    polygons: list,
    zoom: int,
    origin_x: float,
    origin_y: float,
    color: str = "#cc2222",
    stipple: str = "gray25",
    scale: float = 1.0,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
):
    """
    Рисует список полигонов (см. occupied_layer.extract_polygons) поверх
    тайлов. Дырки в полигонах игнорируются (только внешний контур) — это
    для общей наглядности, не для точных измерений границы.

    scale/offset_x/offset_y -- те саме масштабування, що й для тайлів у
    render_tiles_fit (за замовчуванням 1.0/0/0 -- без змін, для старого
    render_tiles із натуральним розміром).
    """
    for poly in polygons:
        if not poly:
            continue
        outer_ring = poly[0]
        if len(outer_ring) < 3:
            continue
        flat = []
        for lon, lat in outer_ring:
            px, py = lonlat_to_pixel(lat, lon, zoom)
            flat.extend([(px - origin_x) * scale + offset_x, (py - origin_y) * scale + offset_y])
        canvas.create_polygon(*flat, fill=color, outline=color, stipple=stipple, width=1)


def _draw_route(
    canvas: tk.Canvas, analyzer: MissionAnalyzer, zoom: int, origin_x: float, origin_y: float,
    scale: float = 1.0, offset_x: float = 0.0, offset_y: float = 0.0,
) -> list[tuple[float, float]]:
    """
    scale/offset_x/offset_y -- те саме масштабування "letterbox", що й
    для тайлів у render_tiles_fit (за замовчуванням 1.0/0/0 -- без змін,
    для старого render_tiles із натуральним розміром).

    Кожен маркер точки й кожен відрізок лінії між сусідніми точками
    отримують ВЛАСНІ теги ("wp_marker_<index>" / "wp_line_<index>") --
    потрібно для редактора місії (перетягування точки мишею): за тегом
    можна знайти й пересунути саме ці canvas-об'єкти напряму, без
    перемальовки всієї мозаїки тайлів на кожен рух миші.
    """
    issues_by_wp: dict[int, list[str]] = {}
    for it in analyzer.issues:
        issues_by_wp.setdefault(it["wp_index"], []).append(it["type"])

    route_px = []
    for wp in analyzer.nav_wps:
        gx, gy = lonlat_to_pixel(wp.lat, wp.lon, zoom)
        route_px.append(((gx - origin_x) * scale + offset_x, (gy - origin_y) * scale + offset_y, wp))

    for i in range(len(route_px) - 1):
        x1, y1, wp1 = route_px[i]
        x2, y2, wp2 = route_px[i + 1]
        canvas.create_line(
            x1, y1, x2, y2, fill="#1f77b4", width=3,
            tags=("wp_line", f"wp_line_{wp1.index}_{wp2.index}"),
        )

    for x, y, wp in route_px:
        color = "#d62728" if wp.index in issues_by_wp else "#1f77b4"
        marker_tag = f"wp_marker_{wp.index}"
        canvas.create_oval(
            x - 6, y - 6, x + 6, y + 6, fill=color, outline="white", width=1,
            tags=("wp_marker", marker_tag),
        )
        canvas.create_text(
            x, y - 12, text=str(wp.index), font=("Arial", 8, "bold"),
            tags=("wp_marker", marker_tag),
        )

    return [(x, y) for x, y, _ in route_px]


def _center_on_route(canvas: tk.Canvas, route_px: list[tuple[float, float]], grid_w: float, grid_h: float):
    """Прокручивает холст так, чтобы центр маршрута оказался в видимой области."""
    if not route_px:
        return
    canvas.update_idletasks()
    view_w = max(canvas.winfo_width(), 1)
    view_h = max(canvas.winfo_height(), 1)

    cx = sum(p[0] for p in route_px) / len(route_px)
    cy = sum(p[1] for p in route_px) / len(route_px)

    frac_x = (cx - view_w / 2) / grid_w if grid_w > 0 else 0
    frac_y = (cy - view_h / 2) / grid_h if grid_h > 0 else 0
    frac_x = min(max(frac_x, 0.0), max(1 - view_w / grid_w, 0.0)) if grid_w > view_w else 0.0
    frac_y = min(max(frac_y, 0.0), max(1 - view_h / grid_h, 0.0)) if grid_h > view_h else 0.0

    canvas.xview_moveto(frac_x)
    canvas.yview_moveto(frac_y)


def bind_pan(canvas: tk.Canvas):
    """Перетаскивание карты мышью (зажать левую кнопку и тащить)."""
    canvas.bind("<ButtonPress-1>", lambda e: canvas.scan_mark(e.x, e.y))
    canvas.bind("<B1-Motion>", lambda e: canvas.scan_dragto(e.x, e.y, gain=1))


# --------------------------------------------------------------------------
# Бонус: экспорт маршрута в автономный HTML с Leaflet (если есть интернет
# в браузере — необязательная функция, GUI её не вызывает по умолчанию).

def build_route_map_html(analyzer: MissionAnalyzer, out_path: str):
    pts = analyzer.nav_wps
    if not pts:
        raise ValueError("no points with coordinates to display on the map")

    issues_by_wp: dict[int, list[str]] = {}
    for it in analyzer.issues:
        issues_by_wp.setdefault(it["wp_index"], []).append(it["detail"])

    points = []
    for wp in pts:
        points.append({
            "lat": wp.lat,
            "lon": wp.lon,
            "index": wp.index,
            "alt": wp.alt,
            "command": wp.command,
            "critical": wp.index in issues_by_wp,
            "issues": issues_by_wp.get(wp.index, []),
        })

    data_json = json.dumps(points, ensure_ascii=False)

    html = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Маршрут миссии</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body { height: 100%; margin: 0; font-family: Arial, sans-serif; }
  #map { height: 100%; }
</style>
</head>
<body>
<div id="map"></div>
<script>
  const points = __DATA_JSON__;

  const map = L.map('map');
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors'
  }).addTo(map);

  const latlngs = points.map(p => [p.lat, p.lon]);
  L.polyline(latlngs, {color: '#1f77b4', weight: 3}).addTo(map);

  points.forEach(p => {
    const color = p.critical ? '#d62728' : '#1f77b4';
    const marker = L.circleMarker([p.lat, p.lon], {
      radius: 7, color: color, fillColor: color, fillOpacity: 0.9, weight: 2
    }).addTo(map);
    let popup = `<b>WP #${p.index}</b><br>Высота: ${p.alt} м<br>Команда MAVLink: ${p.command}`;
    if (p.issues.length) {
      popup += '<br><span style="color:#d62728">' + p.issues.join('<br>') + '</span>';
    }
    marker.bindPopup(popup);
  });

  if (latlngs.length === 1) {
    map.setView(latlngs[0], 14);
  } else {
    map.fitBounds(latlngs, {padding: [30, 30]});
  }
</script>
</body>
</html>
"""
    html = html.replace("__DATA_JSON__", data_json)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
