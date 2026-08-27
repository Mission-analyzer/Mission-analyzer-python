"""
mission_editor.py — редактор місії на сторінці "Місія": перетягування
точок на карті мишею, профіль висот під картою (з рельєфом SRTM,
перетягування висоти окремо від позиції), редагування комірок таблиці
(подвійний клік), додавання/видалення точок і службових команд (правий
клік на рядку таблиці або на точці на карті).

MissionEditorMixin підмішується до класу App (app.py) поряд із
MissionPageMixin -- методи звертаються до self.* атрибутів сторінки
"Місія" (self.analyzer, self.mission_table, self.map_canvas,
self.alt_profile_canvas тощо, створених у MissionPageMixin._build_mission_page)
та до self._populate_mission_table / self._redraw_last_map_render
з mission_page.py.

Свідомо відокремлений від mission_page.py: та частина відповідає за
перегляд/завантаження/збереження місії, ця -- лише за живе редагування.
Виріс за один день до ~900 рядків саме за рахунок цього блоку (drag по
карті, профіль висот, редактор комірок таблиці) -- тому винесення в
окремий файл підтримує навігацію читабельною, той самий принцип, що й
для ardupilot_link.py раніше.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from waypoints import Waypoint, command_name
from geo import haversine_m, pixel_to_lonlat
from srtm import SRTMError
from analyzer import MissionAnalyzer
from mission_page import _frame_name
import i18n


class MissionEditorMixin:
    """Редактор місії: перетягування точок на карті й на профілі висот,
    редагування таблиці, додавання/видалення точок і команд."""

    # ============================================================== --
    # Редактор місії: перетягування точок на карті + редагування таблиці.
    # Обидва джерела правди -- self.analyzer.all_wps (список Waypoint) --
    # будь-яка зміна (drag, комірка таблиці, додавання/видалення рядка)
    # завжди йде через _on_mission_edited(), яка перебудовує таблицю,
    # перемальовує карту (З КЕШОВАНИХ тайлів, без мережі) і запускає
    # ВІДКЛАДЕНИЙ повторний аналіз (SRTM/термінал-перевірки можуть бути
    # повільними -- не варто ганяти їх на кожен символ у полі).
    # ============================================================== --

    _FULL_MAV_CMD_LIST = None  # кешується при першому зверненні

    # Тільки команди, що РЕАЛЬНО валідні як елемент місії ArduPilot (той
    # самий перелік, що показує сам Mission Planner у випадаючому списку
    # команд редактора місії -- ~40 замість усіх ~188 MAV_CMD). Решта
    # (PREFLIGHT_*, ARM/DISARM, запити телеметрії/потокового відео,
    # точки geofence -- то окремий механізм, не елемент .waypoints) --
    # реальні протокольні/налаштувальні команди, які ніколи не
    # з'являються як рядок у .waypoints-файлі.
    _MISSION_VALID_CMD_NAMES = {
        # навігаційні точки (геометрія маршруту)
        "NAV_WAYPOINT", "NAV_LOITER_UNLIM", "NAV_LOITER_TURNS", "NAV_LOITER_TIME",
        "NAV_RETURN_TO_LAUNCH", "NAV_LAND", "NAV_TAKEOFF",
        "NAV_CONTINUE_AND_CHANGE_ALT", "NAV_LOITER_TO_ALT", "NAV_SPLINE_WAYPOINT",
        "NAV_VTOL_TAKEOFF", "NAV_VTOL_LAND", "NAV_GUIDED_ENABLE",
        "NAV_DELAY", "NAV_PAYLOAD_PLACE",
        # умовні команди
        "CONDITION_DELAY", "CONDITION_DISTANCE", "CONDITION_YAW",
        # службові команди (DO_*) -- усталений набір, що реально
        # використовується в місіях ArduPilot
        "DO_JUMP", "DO_CHANGE_SPEED", "DO_SET_HOME",
        "DO_SET_RELAY", "DO_REPEAT_RELAY", "DO_SET_SERVO", "DO_REPEAT_SERVO",
        "DO_RETURN_PATH_START", "DO_LAND_START",
        "DO_SET_ROI", "DO_SET_ROI_LOCATION", "DO_SET_ROI_WPNEXT_OFFSET", "DO_SET_ROI_NONE",
        "DO_DIGICAM_CONFIGURE", "DO_DIGICAM_CONTROL",
        "DO_MOUNT_CONTROL", "DO_SET_CAM_TRIGG_DIST", "DO_SET_CAM_TRIGG_INTERVAL",
        "DO_FENCE_ENABLE", "DO_PARACHUTE", "DO_INVERTED_FLIGHT", "DO_GRIPPER",
        "DO_AUTOTUNE_ENABLE", "DO_SET_RESUME_REPEAT_DIST", "DO_SPRAYER",
        "DO_GUIDED_LIMITS", "DO_ENGINE_CONTROL",
        "JUMP_TAG", "DO_JUMP_TAG",
    }

    @classmethod
    def _mav_cmd_options(cls) -> list[tuple[str, int]]:
        """[(назва_без_MAV_CMD_, код), ...] -- лише команди, валідні як
        елемент місії (_MISSION_VALID_CMD_NAMES), відсортовані за
        назвою. Коди беруться з pymavlink (не захардкоджені напряму --
        так безпечніше при різних версіях діалекту), фільтр -- за
        назвою. Кешується -- список не змінюється протягом роботи
        програми."""
        if cls._FULL_MAV_CMD_LIST is None:
            from pymavlink import mavutil
            options = []
            for code, entry in mavutil.mavlink.enums["MAV_CMD"].items():
                name = entry.name.replace("MAV_CMD_", "")
                if name not in cls._MISSION_VALID_CMD_NAMES:
                    continue
                options.append((name, code))
            options.sort(key=lambda pair: pair[0])
            cls._FULL_MAV_CMD_LIST = options
        return cls._FULL_MAV_CMD_LIST

    # (назва у комбобоксі, код MAV_FRAME, що підставляється при виборі)
    _FRAME_OPTIONS = [("Absolute", 0), ("Relative", 3), ("Terrain", 13)]


    def _toggle_edit_mode(self):
        if self._edit_mode:
            self._exit_edit_mode()
        else:
            self._enter_edit_mode()


    def _enter_edit_mode(self):
        if self.analyzer is None:
            return
        self._edit_mode = True
        self._edit_drag = None
        self.edit_btn.configure(text=i18n.t("btn_stop_editing"), bg="#C8E6C9")

        # карта: перетягування точки замість панорамування, + подвійний/
        # правий клік додає нову точку в кінець маршруту
        self.map_canvas.unbind("<ButtonPress-1>")
        self.map_canvas.unbind("<B1-Motion>")
        self.map_canvas.bind("<ButtonPress-1>", self._on_map_press_edit)
        self.map_canvas.bind("<B1-Motion>", self._on_map_motion_edit)
        self.map_canvas.bind("<ButtonRelease-1>", self._on_map_release_edit)
        self.map_canvas.bind("<Double-Button-1>", self._on_map_add_point)
        self.map_canvas.bind("<Button-3>", self._on_map_right_click)

        # профіль висот: перетягування маркера точки по вертикалі -- міняє
        # лише висоту (X/дистанція не чіпається); правий клік -- додає
        # нову точку в місце маршруту під X-координатою кліку
        self.alt_profile_canvas.bind("<ButtonPress-1>", self._on_alt_press_edit)
        self.alt_profile_canvas.bind("<B1-Motion>", self._on_alt_motion_edit)
        self.alt_profile_canvas.bind("<ButtonRelease-1>", self._on_alt_release_edit)
        self.alt_profile_canvas.bind("<Button-3>", self._on_alt_right_click)

        self._enable_table_editing()


    def _exit_edit_mode(self):
        self._edit_mode = False
        self._edit_drag = None
        self.edit_btn.configure(text=i18n.t("btn_edit_mission"), bg="#DEE3E8")

        self.map_canvas.unbind("<ButtonPress-1>")
        self.map_canvas.unbind("<B1-Motion>")
        self.map_canvas.unbind("<ButtonRelease-1>")
        self.map_canvas.unbind("<Double-Button-1>")
        self.map_canvas.unbind("<Button-3>")
        # відновлюємо ЄДИНІ обробники панорамування з mission_page.py (не
        # старий bind_pan) -- вони самі знають, обзорний зараз режим карти
        # чи "віконний" (Mission Planner-подібний)
        self.map_canvas.bind("<ButtonPress-1>", self._on_map_pan_press)
        self.map_canvas.bind("<B1-Motion>", self._on_map_pan_motion)
        self.map_canvas.bind("<ButtonRelease-1>", self._on_map_pan_release)

        self.alt_profile_canvas.unbind("<ButtonPress-1>")
        self.alt_profile_canvas.unbind("<B1-Motion>")
        self.alt_profile_canvas.unbind("<ButtonRelease-1>")
        self.alt_profile_canvas.unbind("<Button-3>")
        self._alt_drag = None
        self._alt_zoom_drag = None
        self.alt_profile_canvas.delete("zoom_select_rect")

        self._disable_table_editing()


    def _retranslate_edit_button(self):
        if hasattr(self, "edit_btn"):
            key = "btn_stop_editing" if self._edit_mode else "btn_edit_mission"
            self.edit_btn.configure(text=i18n.t(key))


    # ------------------------------------------------------- карта: drag --

    def _current_map_geom(self):
        """(zoom, origin_x, origin_y, scale) для поточного рендеру карти.

        Обидва режими ("overview" -- початковий вигляд "весь маршрут в
        екран", і "viewport" -- користувач попанорамував/призумив)
        малюються тайл-за-тайлом (render_viewport, як у Mission Planner)
        -- origin вже готові глобальні пікселі з render_viewport
        (origin_gx/gy), масштаб завжди 1.0 (жодного додаткового
        масштабування картинки немає)."""
        if self._last_map_render is None:
            return None
        r = self._last_map_render
        return r["zoom"], r["origin_gx"], r["origin_gy"], 1.0


    def _wp_by_index(self, index: int):
        return next((w for w in self.analyzer.all_wps if w.index == index), None)


    def _on_map_press_edit(self, event):
        cx = self.map_canvas.canvasx(event.x)
        cy = self.map_canvas.canvasy(event.y)
        item = self.map_canvas.find_closest(cx, cy)
        tags = self.map_canvas.gettags(item) if item else ()
        wp_index = None
        for t in tags:
            if t.startswith("wp_marker_"):
                wp_index = int(t.rsplit("_", 1)[-1])
                break

        if wp_index is not None and self._wp_by_index(wp_index) is not None:
            self._edit_drag = {"wp_index": wp_index, "last_cx": cx, "last_cy": cy}
        else:
            self._edit_drag = None
            self._on_map_pan_press(event)


    def _on_map_motion_edit(self, event):
        if self._edit_drag is None:
            self._on_map_pan_motion(event)
            return

        cx = self.map_canvas.canvasx(event.x)
        cy = self.map_canvas.canvasy(event.y)
        dx = cx - self._edit_drag["last_cx"]
        dy = cy - self._edit_drag["last_cy"]
        self._edit_drag["last_cx"] = cx
        self._edit_drag["last_cy"] = cy

        wp_index = self._edit_drag["wp_index"]
        marker_tag = f"wp_marker_{wp_index}"
        self.map_canvas.move(marker_tag, dx, dy)

        # підтягуємо кінці лінії(й), що з'єднують цю точку з сусідами --
        # решту карти (тайли, інші маркери) НЕ чіпаємо, тому це дешево
        nav_wps = self.analyzer.nav_wps
        pos = next((i for i, w in enumerate(nav_wps) if w.index == wp_index), None)
        if pos is None:
            return
        bbox = self.map_canvas.bbox(marker_tag)
        if bbox is None:
            return
        cx_new = (bbox[0] + bbox[2]) / 2
        cy_new = (bbox[1] + bbox[3]) / 2
        if pos > 0:
            prev_wp = nav_wps[pos - 1]
            line_tag = f"wp_line_{prev_wp.index}_{wp_index}"
            coords = self.map_canvas.coords(line_tag)
            if len(coords) == 4:
                self.map_canvas.coords(line_tag, coords[0], coords[1], cx_new, cy_new)
        if pos < len(nav_wps) - 1:
            next_wp = nav_wps[pos + 1]
            line_tag = f"wp_line_{wp_index}_{next_wp.index}"
            coords = self.map_canvas.coords(line_tag)
            if len(coords) == 4:
                self.map_canvas.coords(line_tag, cx_new, cy_new, coords[2], coords[3])


    def _on_map_release_edit(self, event):
        if self._edit_drag is None:
            self._on_map_pan_release(event)
            return
        wp_index = self._edit_drag["wp_index"]
        self._edit_drag = None

        geom = self._current_map_geom()
        wp = self._wp_by_index(wp_index)
        if geom is None or wp is None:
            return
        zoom, origin_x, origin_y, scale = geom

        cx = self.map_canvas.canvasx(event.x)
        cy = self.map_canvas.canvasy(event.y)
        gx = cx / scale + origin_x
        gy = cy / scale + origin_y
        lat, lon = pixel_to_lonlat(gx, gy, zoom)
        wp.lat = lat
        wp.lon = lon
        self._on_mission_edited()


    def _on_map_add_point(self, event):
        """Подвійний клік по карті -- та сама логіка додавання, що й
        кнопка «Додати точку» в таблиці (_add_waypoint_row): нова точка
        встає МІЖ обраною в таблиці й наступною за нею (середнє
        координат/висоти), а не в місці кліку. Клік по карті тут лише
        запускає дію -- саме позиціонування користувач потім робить
        перетягуванням (drag), як для будь-якої іншої точки."""
        if self.analyzer is None:
            return
        self._add_waypoint_row()


    def _on_map_right_click(self, event):
        """Правий клік по карті -- те саме контекстне меню (Додати точку /
        Додати команду / Видалити), що й на рядку таблиці. Якщо клік
        влучив у маркер точки на карті -- синхронізуємо виділення в
        таблиці на цю точку ПЕРЕД показом меню, щоб усі три дії (котрі
        орієнтуються на mission_table.selection()) працювали з тим
        самим вейпоінтом, на який клікнули."""
        if self.analyzer is None:
            return
        cx = self.map_canvas.canvasx(event.x)
        cy = self.map_canvas.canvasy(event.y)
        item = self.map_canvas.find_closest(cx, cy)
        tags = self.map_canvas.gettags(item) if item else ()
        wp_index = None
        for t in tags:
            if t.startswith("wp_marker_"):
                wp_index = int(t.rsplit("_", 1)[-1])
                break

        if wp_index is not None:
            for row_id in self.mission_table.get_children():
                if self.mission_table.set(row_id, "idx") == str(wp_index):
                    self.mission_table.selection_set(row_id)
                    break

        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label=i18n.t("ctx_add_waypoint"), command=self._add_waypoint_row)
        menu.add_command(label=i18n.t("ctx_add_command"), command=self._add_command_row)
        menu.add_separator()
        menu.add_command(label=i18n.t("btn_delete"), command=self._delete_selected_row)
        menu.tk_popup(event.x_root, event.y_root)


    def _redraw_route_only(self):
        """Перемальовує карту (тайли + маршрут) з УЖЕ завантажених тайлів
        (self._last_map_render) -- без мережі. Викликається після
        будь-якої зміни в редакторі місії."""
        if self._last_map_render is None:
            return
        self._redraw_last_map_render()


    def _get_terrain_profile(self, start_dist: float | None = None, end_dist: float | None = None):
        """Кешовані зразки рельєфу [(dist_м, elevation_AMSL_м), ...] вздовж
        маршруту -- рахуються ОДИН РАЗ на конкретну геометрію точок
        (lat/lon) І конкретний видимий діапазон, а не на кожен кадр
        перетягування ВИСОТИ -- рельєф не залежить від того, яку висоту
        польоту тягне користувач, лише від позицій точок і того, який
        відрізок маршруту зараз показано. Кеш явно скидається, коли
        позиції реально змінюються (route_key) чи змінюється видимий
        діапазон (range_key).

        start_dist/end_dist -- None означає "весь маршрут" (як і
        раніше). Якщо задано (напр. поточний зум графіка висот,
        _alt_zoom_range) -- ті самі ~600 семплів розподіляються РІВНОМІРНО
        в МЕЖАХ ЦЬОГО діапазону, а не по всій довжині маршруту -- принцип
        лупи: чим сильніший зум (менший видимий відрізок), тим щільніше
        (за метром вздовж маршруту) семпли рельєфу на цій самій ділянці,
        а не ті самі рідкі точки, розтягнуті на весь екран.

        Повертає (samples, total_dist); samples=None, якщо SRTM не
        підключено."""
        if self.analyzer is None or self.analyzer.terrain is None:
            return None, 0.0
        nav_wps = self.analyzer.nav_wps
        if not nav_wps:
            return None, 0.0

        route_key = tuple((wp.lat, wp.lon) for wp in nav_wps)

        dists = [0.0]
        for i in range(1, len(nav_wps)):
            d = haversine_m(nav_wps[i - 1].lat, nav_wps[i - 1].lon, nav_wps[i].lat, nav_wps[i].lon)
            dists.append(dists[-1] + d)
        total_dist = dists[-1] if dists[-1] > 0 else 1.0

        range_start = 0.0 if start_dist is None else max(0.0, start_dist)
        range_end = total_dist if end_dist is None else min(total_dist, end_dist)
        if range_end <= range_start:
            range_start, range_end = 0.0, total_dist
        range_key = (round(range_start, 1), round(range_end, 1))

        cache = getattr(self, "_terrain_profile_cache", None)
        if cache is not None and cache.get("route_key") == route_key and cache.get("range_key") == range_key:
            return cache["samples"], cache["total_dist"]

        range_span = range_end - range_start

        # ФІКСОВАНА кількість семплів (до 600) НЕЗАЛЕЖНО від довжини
        # видимого діапазону -- принцип лупи: скільки точок рельєфу
        # видно НА ЕКРАНІ має лишатись приблизно постійним, чим сильніший
        # зум (менший видимий відрізок) -- тим ЩІЛЬНІШЕ (за метром)
        # семпли саме на цій ділянці, а не розтягнуті рідкісні точки з
        # усього маршруту. Верхня межа -- ще й max(2, int(range_span)):
        # не більше 1 точки на метр -- семплувати частіше немає сенсу,
        # роздільна здатність SRTM однаково ~90м/піксель. SRTM-запити
        # після завантаження тайлу в пам'ять -- просто пошук у масиві,
        # тому більша щільність не б'є по швидкості так, як мережевий
        # запит (виміряно окремо -- див. чат: ~2мс на 600 запитів у вже
        # завантаженому тайлі).
        n_samples = int(min(600, max(2, range_span)))
        samples = []
        for i in range(n_samples + 1):
            target_d = range_start + range_span * i / n_samples
            seg = 0
            while seg < len(dists) - 2 and dists[seg + 1] < target_d:
                seg += 1
            d0 = dists[seg]
            d1 = dists[seg + 1] if seg + 1 < len(dists) else dists[seg]
            wp0 = nav_wps[seg]
            wp1 = nav_wps[seg + 1] if seg + 1 < len(nav_wps) else nav_wps[seg]
            frac = (target_d - d0) / (d1 - d0) if d1 > d0 else 0.0
            lat = wp0.lat + (wp1.lat - wp0.lat) * frac
            lon = wp0.lon + (wp1.lon - wp0.lon) * frac
            try:
                elev = self.analyzer.terrain.get_elevation(lat, lon)
            except SRTMError:
                elev = None
            samples.append((target_d, elev))

        self._terrain_profile_cache = {
            "route_key": route_key, "range_key": range_key,
            "samples": samples, "total_dist": total_dist,
        }
        return samples, total_dist


    def _amsl_to_relative_alt(self, wp, amsl: float) -> float:
        """Обернене до analyzer._absolute_alt() -- з нового AMSL (куди
        користувач перетягнув маркер) рахує, яке значення покласти у
        wp.alt (у власній системі відліку кадру цієї точки)."""
        if wp.frame in (0, 2):
            return amsl
        if wp.frame == 10:
            if self.analyzer.terrain is None:
                return amsl
            try:
                ground = self.analyzer.terrain.get_elevation(wp.lat, wp.lon)
                return amsl - ground
            except SRTMError:
                return amsl
        return amsl - (self.analyzer.home_amsl or 0.0)


    def _redraw_altitude_profile(self):
        """Профіль висот під картою: лінія польоту (AMSL, та сама
        логіка приведення, що й у перевірці AGL -- analyzer._absolute_alt)
        поверх зафарбованого профілю рельєфу (SRTM). Без рельєфу
        порівнювати висоту точки нема з чим -- сенс профілю пропадає,
        тому за відсутності SRTM явно попереджаємо, а не мовчки ховаємо.
        Маркери -- ті самі точки, що й на карті, з тегами
        alt_marker_<index> для перетягування по вертикалі в редакторі.

        self._alt_zoom_range -- (dist_start, dist_end) у метрах уздовж
        маршруту, якщо користувач виділив прямокутник лівою кнопкою
        (_on_alt_release_edit) -- None означає "весь маршрут". X-вісь
        мапить САМЕ цей видимий діапазон на ширину канваса (не завжди
        [0, total_dist]), і v_min/v_max (авто-масштаб висоти) рахуються
        ЛИШЕ з даних, що потрапляють у цей діапазон -- інакше зумована
        ділянка не виглядала б детальнішою, лише вужчою по горизонталі."""
        canvas = self.alt_profile_canvas
        canvas.delete("all")
        self._alt_profile_geom = None
        if self.analyzer is None:
            return
        nav_wps = self.analyzer.nav_wps
        if not nav_wps:
            return

        c = self._mission_colors()

        w = max(canvas.winfo_width(), 200)
        h = max(canvas.winfo_height(), 100)
        margin_l, margin_r, margin_t, margin_b = 55, 15, 15, 34
        plot_w = max(w - margin_l - margin_r, 10)
        plot_h = max(h - margin_t - margin_b, 10)

        dists = [0.0]
        for i in range(1, len(nav_wps)):
            d = haversine_m(nav_wps[i - 1].lat, nav_wps[i - 1].lon, nav_wps[i].lat, nav_wps[i].lon)
            dists.append(dists[-1] + d)
        total_dist = dists[-1] if dists[-1] > 0 else 1.0

        flight_amsl = []
        for wp in nav_wps:
            a = self.analyzer._absolute_alt(wp)
            flight_amsl.append(a if a is not None else wp.alt)

        # видимий діапазон (весь маршрут, чи активний зум) -- ВАЖЛИВО:
        # перевіряємо, що збережений діапазон досі коректний (напр. якщо
        # маршрут став коротшим після видалення точок -- старий zoom_end
        # міг вийти за нові межі). Рахуємо ДО _get_terrain_profile --
        # передаємо його туди, щоб рельєф семплювався щільніше саме на
        # цьому видимому відрізку (принцип лупи), а не по всьому маршруту.
        if self._alt_zoom_range is not None:
            view_start = max(0.0, min(self._alt_zoom_range[0], total_dist))
            view_end = max(view_start + 1.0, min(self._alt_zoom_range[1], total_dist))
        else:
            view_start, view_end = 0.0, total_dist
        view_span = view_end - view_start

        terrain_samples, _terrain_total = self._get_terrain_profile(view_start, view_end)

        # v_min/v_max -- ЛИШЕ з даних, що потрапляють у видимий діапазон
        # (інакше зум по X не давав би деталізації по висоті теж)
        visible_values = [
            a for d, a in zip(dists, flight_amsl) if view_start - 1e-6 <= d <= view_end + 1e-6
        ]
        if terrain_samples:
            visible_values += [
                e for d, e in terrain_samples
                if e is not None and view_start - 1e-6 <= d <= view_end + 1e-6
            ]
        if not visible_values:
            # діапазон зуму випадково не містить жодної точки/семпла --
            # запасний варіант, щоб не впасти на порожньому min()/max()
            visible_values = list(flight_amsl) or [0.0]
        v_min, v_max = min(visible_values), max(visible_values)
        if v_max - v_min < 1e-6:
            v_max = v_min + 1.0
        pad = (v_max - v_min) * 0.15
        v_min -= pad
        v_max += pad

        def X(d):
            return margin_l + (d - view_start) / view_span * plot_w

        def Y(v):
            return margin_t + (1 - (v - v_min) / (v_max - v_min)) * plot_h

        for i in range(5):
            val = v_min + (v_max - v_min) * i / 4
            y = Y(val)
            canvas.create_line(margin_l, y, w - margin_r, y, fill=c["alt_grid"])
            canvas.create_text(
                margin_l - 6, y, text=f"{val:.0f}", anchor="e", font=("Arial", 8), fill=c["alt_axis_label"],
            )
        # ліва вісь -- сама лінія + одиниця виміру (метри) над нею
        canvas.create_line(margin_l, margin_t, margin_l, margin_t + plot_h, fill=c["alt_axis_line"], width=1)
        canvas.create_text(
            margin_l, max(margin_t - 6, 6), text=i18n.t("unit_meters_axis"),
            anchor="s", font=("Arial", 7), fill=c["alt_axis_line"],
        )

        if terrain_samples:
            line_pts = []
            for d, e in terrain_samples:
                if e is None:
                    continue
                line_pts.extend([X(d), Y(e)])
            if len(line_pts) >= 4:
                # рельєф: лише синя лінія, без заливки -- "просто графік
                # рельєфу", без області під нею
                canvas.create_line(*line_pts, fill=c["alt_terrain_line"], width=2)
        else:
            canvas.create_text(
                w / 2, margin_t + 10, text=i18n.t("msg_terrain_unavailable"),
                font=("Arial", 8), fill=c["alt_no_terrain"],
            )

        points_px = [(X(d), Y(a), wp) for d, a, wp in zip(dists, flight_amsl, nav_wps)]

        # лінія польоту (AMSL) -- червона, як графік висоти в Mission Planner
        # (малюємо навіть сегменти, що частково/повністю виходять за межі
        # видимого діапазону -- Tkinter сам коректно показує лише видиму
        # частину, зайве просто не потрапляє в канвас)
        for i in range(len(points_px) - 1):
            x1, y1, wp1 = points_px[i]
            x2, y2, wp2 = points_px[i + 1]
            canvas.create_line(
                x1, y1, x2, y2, fill=c["alt_flight_line"], width=2,
                tags=("alt_line", f"alt_line_{wp1.index}_{wp2.index}"),
            )

        for x, y, wp in points_px:
            marker_tag = f"alt_marker_{wp.index}"
            canvas.create_oval(
                x - 5, y - 5, x + 5, y + 5, fill=c["alt_flight_line"], outline=c["alt_marker_outline"], width=1,
                tags=("alt_marker", marker_tag),
            )
            canvas.create_text(
                x, y - 12, text=str(wp.index), font=("Arial", 7, "bold"),
                fill=c["alt_marker_text"], tags=(marker_tag,),
            )

        # нижня вісь -- сама лінія + позначки-риски + підписи в кілометрах
        # (від view_start до view_end -- при активному зумі перша й
        # остання риска НЕ обов'язково 0 і total_dist)
        axis_y = margin_t + plot_h
        canvas.create_line(margin_l, axis_y, w - margin_r, axis_y, fill=c["alt_axis_line"], width=1)
        n_ticks = 5
        for i in range(n_ticks + 1):
            d = view_start + view_span * i / n_ticks
            x_tick = X(d)
            canvas.create_line(x_tick, axis_y, x_tick, axis_y + 4, fill=c["alt_axis_line"])
            canvas.create_text(
                x_tick, axis_y + 6, text=f"{d / 1000.0:.1f}", anchor="n", font=("Arial", 7), fill=c["alt_axis_label"],
            )
        canvas.create_text(
            w / 2, axis_y + 16, text=i18n.t("unit_km_axis"), anchor="n", font=("Arial", 7), fill=c["alt_unit_label"],
        )

        # margin_l/plot_w/view_start/view_span/dists/nav_wps -- потрібні
        # для ПРАВОГО кліку (додавання нової точки) і для виділення
        # прямокутника зуму лівою кнопкою: переводять X-координату кліку
        # назад у дистанцію вздовж маршруту, З УРАХУВАННЯМ поточного
        # видимого діапазону (не завжди [0, total_dist]).
        # -> між якими двома сусідніми точками ця відстань потрапляє ->
        # lat/lon лінійною інтерполяцією між ними за часткою відстані.
        self._alt_profile_geom = {
            "margin_t": margin_t, "plot_h": plot_h, "v_min": v_min, "v_max": v_max,
            "margin_l": margin_l, "plot_w": plot_w, "total_dist": total_dist,
            "view_start": view_start, "view_span": view_span,
            "dists": dists, "nav_wps": nav_wps,
        }

        # Числовий зум графіка -- відношення ВСЬОГО маршруту до
        # ВИДИМОГО (view_span) відрізка, округлене до цілого. Весь
        # маршрут видно -> view_span == total_dist -> зум = 1 (початкове
        # значення). Користувач виділив (зумив) третину маршруту ->
        # view_span = total_dist/3 -> зум = round(3) = 3. Виділив ще раз,
        # усередині вже зумленого (view_span стає ще меншим відносно
        # ТОГО САМОГО total_dist) -> зум ще зростає -- послідовний зум
        # природно накопичується сам, без окремого лічильника "скільки
        # разів зумили", просто через відношення поточного view_span до
        # незмінного total_dist.
        if hasattr(self, "_alt_zoom_label"):
            zoom_value = round(total_dist / view_span) if view_span > 1e-6 else 1
            self._alt_zoom_label.configure(text=str(max(1, zoom_value)))


    def _on_alt_press_edit(self, event):
        """Ліва кнопка на графіку висот -- ДВІ різні дії залежно від
        того, куди влучив клік:
        - на маркер існуючої точки -> перетягування цієї точки (як
          вертикально -- висота, так і горизонтально -- позиція вздовж
          ПРЯМОЇ між сусідніми точками маршруту, self._alt_drag);
        - на порожнє місце графіка -> починаємо виділення прямокутника
          зуму (self._alt_zoom_drag) -- малюється в _on_alt_motion_edit,
          застосовується в _on_alt_release_edit.

        prev_wp/next_wp/dist_prev/dist_next -- зберігаються ОДИН раз тут,
        на момент натискання, а НЕ перераховуються на кожен кадр motion:
        самі dists у geom змінюються при кожній перемальовці (бо lat/lon
        точки, що рухається, вже інші) -- якщо брати сусідів заново на
        кожен кадр, межі "між ким рухати" самі "пливли" б під час
        перетягування. Перша/остання точка маршруту не має сусіда з
        одного боку -- горизонтальне перетягування для них не
        застосовується (нема прямої, вздовж якої рухати), лишається
        тільки вертикальне (висота), як і раніше."""
        item = self.alt_profile_canvas.find_closest(event.x, event.y)
        tags = self.alt_profile_canvas.gettags(item) if item else ()
        wp_index = None
        for t in tags:
            if t.startswith("alt_marker_"):
                wp_index = int(t.rsplit("_", 1)[-1])
                break
        wp = self._wp_by_index(wp_index) if wp_index is not None else None
        if wp is not None:
            prev_wp = next_wp = None
            dist_prev = dist_next = None
            geom = self._alt_profile_geom
            if geom is not None:
                nav_wps = geom["nav_wps"]
                dists = geom["dists"]
                for pos, w in enumerate(nav_wps):
                    if w is wp:
                        if pos > 0:
                            prev_wp, dist_prev = nav_wps[pos - 1], dists[pos - 1]
                        if pos < len(nav_wps) - 1:
                            next_wp, dist_next = nav_wps[pos + 1], dists[pos + 1]
                        break
            self._alt_drag = {
                "wp_index": wp_index,
                "prev_wp": prev_wp, "next_wp": next_wp,
                "dist_prev": dist_prev, "dist_next": dist_next,
            }
            self._alt_zoom_drag = None
        else:
            self._alt_drag = None
            if self._alt_profile_geom is not None:
                self._alt_zoom_drag = {"start_x": event.x}


    def _on_alt_motion_edit(self, event):
        if self._alt_drag is not None:
            if self._alt_profile_geom is None:
                return
            drag = self._alt_drag
            wp = self._wp_by_index(drag["wp_index"])
            if wp is None:
                return
            geom = self._alt_profile_geom

            # вертикаль -- висота (як і раніше)
            y = max(geom["margin_t"], min(event.y, geom["margin_t"] + geom["plot_h"]))
            frac_y = 1 - (y - geom["margin_t"]) / geom["plot_h"]
            new_amsl = geom["v_min"] + frac_y * (geom["v_max"] - geom["v_min"])
            wp.alt = self._amsl_to_relative_alt(wp, new_amsl)

            # горизонталь -- позиція ВЗДОВЖ ПРЯМОЇ між сусідніми точками
            # (та сама лінійна інтерполяція, що й у _add_waypoint_row)
            # -- лише якщо ОБИДВА сусіди існують (перша/остання точка
            # маршруту -- лише вертикаль)
            if drag["prev_wp"] is not None and drag["next_wp"] is not None:
                click_dist = self._alt_click_to_dist(event, geom)
                span = drag["dist_next"] - drag["dist_prev"]
                t = (click_dist - drag["dist_prev"]) / span if span > 1e-9 else 0.5
                # невеликий відступ від самих сусідів (2%..98%) -- щоб
                # точка не "злипалась" точно в ту саму позицію, що й сусід
                t = max(0.02, min(0.98, t))
                prev_wp, next_wp = drag["prev_wp"], drag["next_wp"]
                wp.lat = prev_wp.lat + (next_wp.lat - prev_wp.lat) * t
                wp.lon = prev_wp.lon + (next_wp.lon - prev_wp.lon) * t

            # повний перемальовок дешевий (рельєф КЕШОВАНИЙ у
            # _get_terrain_profile -- тут перераховуються лише лінії/текст
            # польоту, без нових SRTM-запитів)
            self._redraw_altitude_profile()
            return

        if self._alt_zoom_drag is not None and self._alt_profile_geom is not None:
            geom = self._alt_profile_geom
            canvas = self.alt_profile_canvas
            canvas.delete("zoom_select_rect")
            x1 = self._alt_zoom_drag["start_x"]
            x2 = event.x
            top = geom["margin_t"]
            bottom = geom["margin_t"] + geom["plot_h"]
            canvas.create_rectangle(
                x1, top, x2, bottom,
                fill=self._mission_colors().get("alt_axis_line", "#888888"), stipple="gray25", outline="",
                tags=("zoom_select_rect",),
            )


    def _on_alt_release_edit(self, event):
        if self._alt_drag is not None:
            self._alt_drag = None
            self._on_mission_edited()
            return

        if self._alt_zoom_drag is not None:
            geom = self._alt_profile_geom
            start_x = self._alt_zoom_drag["start_x"]
            self._alt_zoom_drag = None
            self.alt_profile_canvas.delete("zoom_select_rect")
            if geom is None:
                return
            # захист від випадкового мікро-кліку (< 8px -- звичайний
            # клік по порожньому місцю, не виділення прямокутника)
            if abs(event.x - start_x) < 8:
                return
            dist1 = self._alt_x_to_dist(start_x, geom)
            dist2 = self._alt_x_to_dist(event.x, geom)
            new_start, new_end = min(dist1, dist2), max(dist1, dist2)
            if new_end - new_start < 1.0:
                return  # виділений діапазон менше 1м -- нема сенсу зумити
            # ПОСЛІДОВНИЙ зум -- новий діапазон обчислюється відносно
            # ВЖЕ ПОТОЧНОГО видимого діапазону (geom вже враховує
            # активний self._alt_zoom_range через view_start/view_span
            # у _redraw_altitude_profile), тому просто МІНЯЄМО
            # self._alt_zoom_range на щойно виділений -- кожен наступний
            # раз звужує його ще сильніше, скільки завгодно разів поспіль.
            self._alt_zoom_range = (new_start, new_end)
            self._redraw_altitude_profile()


    def _alt_x_to_dist(self, x: float, geom) -> float:
        """X-координата (пікселі канваса) -> дистанція вздовж маршруту
        (метри), з урахуванням поточного видимого діапазону. Те саме,
        що _alt_click_to_dist, але приймає X напряму (не event) --
        зручно для виділення прямокутника, де порівнюються ДВІ
        X-координати (початок і кінець drag), а не одна подія."""
        frac_x = (x - geom["margin_l"]) / geom["plot_w"]
        frac_x = max(0.0, min(1.0, frac_x))
        return geom["view_start"] + frac_x * geom["view_span"]


    def _on_alt_right_click(self, event):
        """Правий клік на графіку висот -- те саме контекстне меню
        (Додати точку / Додати команду / Видалити), що й на карті/в
        таблиці. Синхронізує виділення таблиці на НАЙБЛИЖЧУ (за
        X-координатою) точку маршруту ПЕРЕД показом меню -- щоб усі три
        дії (орієнтуються на mission_table.selection()) працювали з
        тим самим вейпоінтом, біля якого клікнули -- той самий принцип,
        що й на карті (_on_map_right_click).

        "Додати точку" тут -- ТА САМА _add_waypoint_row, що й на карті/
        в таблиці (завжди СЕРЕДИНА між обраною й наступною точкою,
        незалежно від точного місця кліку) -- єдина логіка на всіх трьох
        поверхнях. Раніше тут була окрема, "по точному місцю кліку"
        поведінка (_add_waypoint_at_click) -- прибрано на користь
        єдиноманітності."""
        if self.analyzer is None or self._alt_profile_geom is None:
            return
        geom = self._alt_profile_geom
        nav_wps = geom["nav_wps"]
        dists = geom["dists"]
        if not nav_wps:
            return

        click_dist = self._alt_click_to_dist(event, geom)
        # виключаємо Home (index == 0) з кандидатів -- вона фізично
        # бере участь у геометрії маршруту й видна на графіку (є в
        # nav_wps), але НЕ показується окремим рядком у таблиці
        # (_delete_selected_row: "Home не видаляється (і не показується
        # в таблиці взагалі)") -- якщо найближчою за X виявлялась саме
        # вона, синхронізація виділення мовчки провалювалась (жоден
        # рядок з idx="0" не існує), і "Додати команду"/"Видалити"
        # непередбачувано працювали з тим, що лишалось виділеним раніше.
        candidates = [i for i in range(len(nav_wps)) if nav_wps[i].index != 0] or list(range(len(nav_wps)))
        closest_i = min(candidates, key=lambda i: abs(dists[i] - click_dist))
        closest_wp = nav_wps[closest_i]
        for row_id in self.mission_table.get_children():
            if self.mission_table.set(row_id, "idx") == str(closest_wp.index):
                self.mission_table.selection_set(row_id)
                break

        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label=i18n.t("ctx_add_waypoint"), command=self._add_waypoint_row)
        menu.add_command(label=i18n.t("ctx_add_command"), command=self._add_command_row)
        menu.add_separator()
        menu.add_command(label=i18n.t("btn_delete"), command=self._delete_selected_row)
        menu.tk_popup(event.x_root, event.y_root)


    def _alt_click_to_dist(self, event, geom) -> float:
        """X-координата кліку на графіку висот -> дистанція вздовж
        маршруту (метри), З УРАХУВАННЯМ поточного видимого діапазону
        (view_start/view_span -- весь маршрут, чи активний зум). Спільна
        формула для _on_alt_right_click (пошук найближчої точки для
        синхронізації виділення таблиці) і горизонтального перетягування
        точки між сусідами (_on_alt_motion_edit)."""
        frac_x = (event.x - geom["margin_l"]) / geom["plot_w"]
        frac_x = max(0.0, min(1.0, frac_x))
        return geom["view_start"] + frac_x * geom["view_span"]


    def _schedule_reanalysis(self):
        """Відкладений повторний аналіз (SRTM/висота/кути можуть бути
        повільними) -- дебаунс тим самим прийомом, що й ресайз карти:
        кожна нова зміна скасовує попередній таймер."""
        if getattr(self, "_reanalyze_after_id", None) is not None:
            self.after_cancel(self._reanalyze_after_id)
        self._reanalyze_after_id = self.after(600, self._run_reanalysis)


    def _run_reanalysis(self):
        self._reanalyze_after_id = None
        if self.analyzer is None:
            return
        old = self.analyzer
        # ЗБЕРІГАЄМО всі параметри старого аналізатора -- інакше при
        # перестворенні губиться self.terrain (SRTM) і home_amsl, і всі
        # перевірки висоти над рельєфом ТИХО переставали б працювати
        # одразу після першої ж правки в редакторі
        self.analyzer = MissionAnalyzer(
            old.all_wps, alt_min=old.alt_min, turn_min=old.turn_min,
            angle_max=old.angle_max, terrain=old.terrain, home_amsl=old.home_amsl,
        )
        # НЕ рахуємо analyzer.analyze() тут -- це важка перевірка
        # (SRTM-запити на кожні 50м кожного відрізка), потрібна лише для
        # "Аналіз". Раніше рахувалась одразу після КОЖНОЇ правки точки
        # на "Місія" (навіть якщо користувач жодного разу не відкривав
        # "Аналіз") -- звідси й відчутні гальма редактора. Лише позначаємо
        # "Аналіз" застарілою -- _ensure_analysis_built() сама порахує
        # все заново, лінива, коли вкладку реально відкриють наступного разу.
        self._analysis_built = False


    def _renumber_waypoints(self):
        for i, wp in enumerate(self.analyzer.all_wps):
            wp.index = i


    def _on_mission_edited(self):
        self._renumber_waypoints()
        # ВАЖЛИВО: синхронізуємо analyzer.nav_wps з actual all_wps ОДРАЗУ,
        # тут -- не чекаючи на важке пересворення self.analyzer у
        # _run_reanalysis() (дебаунс 600мс, там же й перерахунок SRTM).
        # nav_wps -- звичайний список, порахований ОДИН раз у __init__
        # MissionAnalyzer, а НЕ властивість (property) -- сам по собі
        # НЕ підхоплює зміни в all_wps автоматично. Для ПЕРЕТЯГУВАННЯ
        # існуючої точки це непомітно (мутується той самий об'єкт, на
        # який nav_wps і так уже посилається) -- але для ДОДАВАННЯ/
        # ВИДАЛЕННЯ точки (склад списку міняється) без цього рядка карта
        # й графік висот малювали б СТАРИЙ маршрут ще секунду після
        # правки, поки не спрацює дебаунс -- і то лише тому, що
        # _run_reanalysis() пересоздає analyzer, а РЕАЛЬНОЇ повторної
        # перемальовки карти/графіка після цього перестворення не було
        # взагалі (ще один прихований бік цього самого бага).
        self.analyzer.nav_wps = [wp for wp in self.analyzer.all_wps if wp.is_nav_point]
        self._populate_mission_table(self.analyzer.all_wps)
        self._redraw_route_only()
        self._redraw_altitude_profile()
        self._schedule_reanalysis()


    # ----------------------------------------------------- таблиця: edit --

    def _enable_table_editing(self):
        self.mission_table.bind("<Double-1>", self._on_table_double_click)
        self.mission_table.bind("<Button-3>", self._on_table_right_click)
        self.mission_table.bind("<Delete>", self._delete_selected_row)

        self._table_context_menu = tk.Menu(self, tearoff=0)
        self._table_context_menu.add_command(
            label=i18n.t("ctx_add_waypoint"), command=self._add_waypoint_row,
        )
        self._table_context_menu.add_command(
            label=i18n.t("ctx_add_command"), command=self._add_command_row,
        )
        self._table_context_menu.add_separator()
        self._table_context_menu.add_command(
            label=i18n.t("btn_delete"), command=self._delete_selected_row,
        )


    def _disable_table_editing(self):
        self.mission_table.unbind("<Double-1>")
        self.mission_table.unbind("<Button-3>")
        self.mission_table.unbind("<Delete>")
        self._destroy_cell_editor()


    def _destroy_cell_editor(self):
        editor = getattr(self, "_active_cell_editor", None)
        if editor is not None:
            try:
                editor.destroy()
            except Exception:
                pass
            self._active_cell_editor = None


    def _on_table_right_click(self, event):
        row_id = self.mission_table.identify_row(event.y)
        if row_id:
            self.mission_table.selection_set(row_id)
        self._table_context_menu.tk_popup(event.x_root, event.y_root)


    def _add_waypoint_row(self):
        """Додає нову точку МІЖ обраною в таблиці й наступною за нею --
        координати й висота беруться як середнє між ними (а не копія
        останньої точки в кінець маршруту, як було раніше) -- інакше
        нова точка "висить" самотньо в кінці, ніяк не пов'язана з тим
        місцем, де користувач реально працює.

        Якщо в таблиці нічого не обрано -- вставляє між двома останніми
        точками маршруту (той самий принцип "без висячих точок").
        Якщо обрана точка -- остання в маршруті (немає "наступної") --
        ставить нову поруч із нею (той самий lat/lon/alt), користувач
        потім сам перетягне її, куди треба."""
        if self.analyzer is None:
            return
        nav_wps = self.analyzer.nav_wps
        if not nav_wps:
            return

        selected_wp = None
        sel = self.mission_table.selection()
        if sel:
            idx_str = self.mission_table.set(sel[0], "idx")
            try:
                selected_wp = self._wp_by_index(int(idx_str))
            except ValueError:
                selected_wp = None

        if selected_wp is None or selected_wp not in nav_wps:
            # нічого коректного не обрано -- працюємо з двома останніми
            # точками маршруту, щоб нова точка все одно лягла "в розрив",
            # а не самотньо в кінці
            selected_wp = nav_wps[-2] if len(nav_wps) >= 2 else nav_wps[-1]

        pos = next(i for i, w in enumerate(nav_wps) if w is selected_wp)
        next_wp = nav_wps[pos + 1] if pos + 1 < len(nav_wps) else None

        if next_wp is not None:
            mid_lat = (selected_wp.lat + next_wp.lat) / 2
            mid_lon = (selected_wp.lon + next_wp.lon) / 2
            # ВАЖЛИВО: усереднюємо AMSL (видиму на графіку висот
            # висоту), а НЕ сирий .alt -- сусідні точки можуть мати
            # РІЗНИЙ frame (напр. frame=0/2 -- .alt це вже AMSL
            # напряму, чи frame=10 -- .alt це висота НАД ЗЕМЛЕЮ саме в
            # ЇЇ точці) -- пряме усереднення сирих значень з РІЗНИХ
            # систем відліку дає число, що не має сенсу в жодній з них.
            # Нова точка завжди отримує frame=3 (relative до home),
            # тому AMSL-середнє переводимо назад тою самою формулою,
            # що й для home-frame (_amsl_to_relative_alt: amsl -
            # home_amsl -- та сама логіка, лише new_wp ще не існує на
            # момент виклику, тому рахуємо напряму, без допоміжної
            # функції, яка приймає готовий wp).
            amsl1 = self.analyzer._absolute_alt(selected_wp)
            amsl2 = self.analyzer._absolute_alt(next_wp)
            if amsl1 is None:
                amsl1 = selected_wp.alt
            if amsl2 is None:
                amsl2 = next_wp.alt
            mid_amsl = (amsl1 + amsl2) / 2
            mid_alt = mid_amsl - (self.analyzer.home_amsl or 0.0)
        else:
            # обрана точка -- остання в маршруті, "наступної" немає --
            # ставимо нову поруч (той самий AMSL, переведений у frame=3
            # нової точки -- та сама причина, що й вище: копіювання
            # СИРОГО .alt напряму дало б неправильну AMSL, якщо
            # selected_wp має інший frame, ніж 3)
            mid_lat, mid_lon = selected_wp.lat, selected_wp.lon
            amsl1 = self.analyzer._absolute_alt(selected_wp)
            mid_alt = (amsl1 if amsl1 is not None else selected_wp.alt) - (self.analyzer.home_amsl or 0.0)

        new_wp = Waypoint(
            index=-1,  # перенумерується в _on_mission_edited() -> _renumber_waypoints()
            current=0, frame=3, command=16,
            param1=0.0, param2=0.0, param3=0.0, param4=0.0,
            lat=mid_lat, lon=mid_lon, alt=mid_alt, autocontinue=1,
        )

        # вставляємо одразу ПІСЛЯ обраної точки за ПОЗИЦІЄЮ в all_wps
        # (ідентичність об'єкта, не .index() -- за значенням у Waypoint
        # автогенерується __eq__ по всіх полях, .index() міг би знайти
        # не той екземпляр при однакових координатах)
        insert_pos = next(
            i for i, w in enumerate(self.analyzer.all_wps) if w is selected_wp
        ) + 1
        self.analyzer.all_wps.insert(insert_pos, new_wp)
        self._on_mission_edited()


    def _prompt_command_choice(self) -> int | None:
        """Маленький модальний діалог -- readonly-комбобокс з УСІМА
        командами, валідними як елемент місії (_mav_cmd_options(), ~47
        замість повних ~188 MAV_CMD -- решта відфільтрована ще на рівні
        самого списку, окремого "часто використовуваних" шару більше
        нема сенсу тримати). Повертає обраний код команди, або None,
        якщо скасовано."""
        options = self._mav_cmd_options()
        names = [n for n, _c in options]
        code_by_name = dict(options)

        dlg = tk.Toplevel(self)
        dlg.title(i18n.t("dlg_choose_command_title"))
        dlg.transient(self)

        ttk.Label(dlg, text=i18n.t("dlg_choose_command_title")).pack(padx=12, pady=(12, 4))
        var = tk.StringVar(value=names[0])
        box = ttk.Combobox(dlg, textvariable=var, values=names, state="readonly", width=40)
        box.pack(padx=12, pady=4)
        box.focus_set()

        result = {"code": None}

        def on_ok(_event=None):
            name = var.get().strip()
            if name in code_by_name:
                result["code"] = code_by_name[name]
            dlg.destroy()

        def on_cancel(_event=None):
            dlg.destroy()

        btn_row = ttk.Frame(dlg)
        btn_row.pack(pady=(4, 12))
        ttk.Button(btn_row, text=i18n.t("btn_ok"), command=on_ok).pack(side="left", padx=4)
        ttk.Button(btn_row, text=i18n.t("btn_cancel"), command=on_cancel).pack(side="left", padx=4)
        box.bind("<Return>", on_ok)
        dlg.bind("<Escape>", on_cancel)
        dlg.protocol("WM_DELETE_WINDOW", on_cancel)

        dlg.grab_set()
        dlg.wait_window()
        return result["code"]


    def _add_command_row(self):
        """Додає СЛУЖБОВУ (не навігаційну) команду -- користувач обирає
        конкретний MAV_CMD у діалозі (_prompt_command_choice), одразу
        ПІСЛЯ обраного в таблиці рядка. На відміну від _add_waypoint_row,
        координати НЕ усереднюються -- службова команда типу
        DO_CHANGE_SPEED не є точкою маршруту й зазвичай без позиції
        (lat=lon=0), як і в звичайних .waypoints-файлах."""
        if self.analyzer is None:
            return
        code = self._prompt_command_choice()
        if code is None:
            return

        sel = self.mission_table.selection()
        anchor_wp = None
        if sel:
            idx_str = self.mission_table.set(sel[0], "idx")
            try:
                anchor_wp = self._wp_by_index(int(idx_str))
            except ValueError:
                anchor_wp = None

        new_wp = Waypoint(
            index=-1,  # перенумерується в _on_mission_edited()
            current=0, frame=0, command=code,
            param1=0.0, param2=0.0, param3=0.0, param4=0.0,
            lat=0.0, lon=0.0, alt=0.0, autocontinue=1,
        )

        if anchor_wp is not None:
            insert_pos = next(
                i for i, w in enumerate(self.analyzer.all_wps) if w is anchor_wp
            ) + 1
            self.analyzer.all_wps.insert(insert_pos, new_wp)
        else:
            self.analyzer.all_wps.append(new_wp)

        self._on_mission_edited()


    def _delete_selected_row(self, _event=None):
        sel = self.mission_table.selection()
        if not sel:
            return
        idx_str = self.mission_table.set(sel[0], "idx")
        try:
            wp_index = int(idx_str)
        except ValueError:
            return
        if wp_index == 0:
            return  # Home не видаляється (і не показується в таблиці взагалі)
        self.analyzer.all_wps = [w for w in self.analyzer.all_wps if w.index != wp_index]
        self._on_mission_edited()


    def _on_table_double_click(self, event):
        region = self.mission_table.identify_region(event.x, event.y)
        if region != "cell":
            return
        row_id = self.mission_table.identify_row(event.y)
        col_id = self.mission_table.identify_column(event.x)
        if not row_id or not col_id:
            return

        col_index = int(col_id.replace("#", "")) - 1
        columns = self.mission_table["columns"]
        if col_index < 0 or col_index >= len(columns):
            return
        col_name = columns[col_index]
        if col_name in ("idx", "dist", "az"):
            return  # обчислювані/нередаговані колонки

        idx_str = self.mission_table.set(row_id, "idx")
        try:
            wp_index = int(idx_str)
        except ValueError:
            return
        wp = self._wp_by_index(wp_index)
        if wp is None:
            return

        self._destroy_cell_editor()
        bbox = self.mission_table.bbox(row_id, col_id)
        if not bbox:
            return
        x, y, w, h = bbox

        if col_name == "cmd":
            self._open_command_editor(row_id, col_id, wp, x, y, w, h)
        elif col_name == "frame":
            self._open_frame_editor(row_id, col_id, wp, x, y, w, h)
        else:
            self._open_numeric_editor(row_id, col_id, wp, col_name, x, y, w, h)


    def _open_numeric_editor(self, row_id, col_id, wp, col_name, x, y, w, h):
        current = self.mission_table.set(row_id, col_name)
        var = tk.StringVar(value=current)
        entry = tk.Entry(self.mission_table, textvariable=var)
        entry.place(x=x, y=y, width=w, height=h)
        entry.select_range(0, "end")
        entry.focus_set()
        self._active_cell_editor = entry

        field_map = {
            "p1": "param1", "p2": "param2", "p3": "param3", "p4": "param4",
            "lat": "lat", "lon": "lon", "alt": "alt",
        }
        attr = field_map.get(col_name)

        def commit(_event=None):
            text = var.get().strip()
            if attr and text != "":
                try:
                    setattr(wp, attr, float(text))
                except ValueError:
                    pass
            self._destroy_cell_editor()
            self._on_mission_edited()

        def cancel(_event=None):
            self._destroy_cell_editor()

        entry.bind("<Return>", commit)
        entry.bind("<KP_Enter>", commit)
        entry.bind("<FocusOut>", commit)
        entry.bind("<Escape>", cancel)


    def _open_command_editor(self, row_id, col_id, wp, x, y, w, h):
        options = self._mav_cmd_options()
        names = [name for name, _code in options]
        code_by_name = dict(options)

        var = tk.StringVar(value=command_name(wp.command))
        box = ttk.Combobox(self.mission_table, textvariable=var, values=names, state="normal")
        box.place(x=x, y=y, width=max(w, 160), height=h)
        box.focus_set()
        self._active_cell_editor = box

        def commit(_event=None):
            name = var.get().strip()
            if name in code_by_name:
                wp.command = code_by_name[name]
            self._destroy_cell_editor()
            self._on_mission_edited()

        def cancel(_event=None):
            self._destroy_cell_editor()

        box.bind("<<ComboboxSelected>>", commit)
        box.bind("<Return>", commit)
        box.bind("<FocusOut>", commit)
        box.bind("<Escape>", cancel)


    def _open_frame_editor(self, row_id, col_id, wp, x, y, w, h):
        names = [name for name, _code in self._FRAME_OPTIONS]
        code_by_name = dict(self._FRAME_OPTIONS)

        var = tk.StringVar(value=_frame_name(wp.frame))
        box = ttk.Combobox(self.mission_table, textvariable=var, values=names, state="readonly")
        box.place(x=x, y=y, width=w, height=h)
        box.focus_set()
        self._active_cell_editor = box

        def commit(_event=None):
            name = var.get().strip()
            if name in code_by_name:
                wp.frame = code_by_name[name]
            self._destroy_cell_editor()
            self._on_mission_edited()

        box.bind("<<ComboboxSelected>>", commit)
        box.bind("<Return>", commit)
        box.bind("<FocusOut>", commit)
        box.bind("<Escape>", lambda _e: self._destroy_cell_editor())


