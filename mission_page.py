"""
mission_page.py — усе, що стосується сторінки "Місія" в App:
таблиця вейпоінтів, завантаження/збереження .waypoints, підключення
до ArduPilot (Read/Write через MAVLink), відмальовка карти маршруту.

MissionPageMixin підмішується до класу App (app.py) — методи звертаються
до self.* атрибутів, які App ініціалізує в _init_vars/_build_ui.
"""

from __future__ import annotations

import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from waypoints import parse_waypoints, command_name
from geo import haversine_m, bearing_deg, TILE_SIZE
from srtm import SRTMTerrain, SRTMError
from online_tiles import OnlineTileCache
from analyzer import MissionAnalyzer
import map_view
from map_view import compute_tile_bounds, fetch_tiles, render_tiles_fit, bind_pan, MapTooLargeError
from occupied_layer import fetch_occupied_geojson, extract_polygons
import i18n

# MAV_FRAME -> спрощена назва (як у Mission Planner): Absolute / Relative / Terrain
_MAV_FRAME_TERRAIN = {13, 14}  # GLOBAL_TERRAIN_ALT, GLOBAL_TERRAIN_ALT_INT


def _frame_name(frame: int) -> str:
    """Повертає текстову назву MAV_FRAME для колонки таблиці місії.

    0        -> Absolute
    13, 14   -> Terrain
    решта    -> Relative
    """
    frame = int(frame)
    if frame == 0:
        return "Absolute"
    if frame in _MAV_FRAME_TERRAIN:
        return "Terrain"
    return "Relative"



class MissionPageMixin:
    """Сторінка "Місія": таблиця, завантаження/збереження, ArduPilot, карта."""

    def _build_mission_page(self, content, pad):
        page_mission = ttk.Frame(content)
        page_mission.grid(row=0, column=0, sticky="nsew")
        self.pages["mission"] = page_mission

        # === страница "Місія" ===
        page_mission = ttk.Frame(content)
        page_mission.grid(row=0, column=0, sticky="nsew")
        self.pages["mission"] = page_mission

        btns = ttk.Frame(page_mission)
        btns.pack(fill="x", **pad)
        load_btn, save_btn = self._make_toggle_action_buttons(
            btns, [(i18n.t("btn_load"), self.load_mission), (i18n.t("btn_save"), self.save_csv)]
        )
        self._reg_i18n(load_btn, "text", "btn_load")
        self._reg_i18n(save_btn, "text", "btn_save")
        load_btn.pack(side="left")
        save_btn.pack(side="left", padx=6)

        # кнопки Info/Read/Write/Files SD для ArduPilot -- видимі тільки
        # коли підключено. Окрема підгрупа, притиснута до ПРАВОГО краю
        # рядка (side="right"), щоб не змішуватись з Завантажити/Зберегти
        # зліва -- порядок усередині групи зберігається зліва направо:
        # Info, Зчитати, Записати, Файли SD.
        ardu_btns_frame = ttk.Frame(btns)
        ardu_btns_frame.pack(side="right")
        self._ardu_info_btn, self._ardu_read_btn, self._ardu_write_btn, self._ardu_files_btn = \
            self._make_toggle_action_buttons(
                ardu_btns_frame, [
                    (i18n.t("btn_info"), self._show_flight_info),
                    (i18n.t("btn_read"), self._load_mission_from_mavlink),
                    (i18n.t("btn_write"), self._save_mission_to_mavlink),
                    (i18n.t("btn_sd_files"), self._show_sd_files),
                ]
            )
        self._reg_i18n(self._ardu_info_btn, "text", "btn_info")
        self._reg_i18n(self._ardu_read_btn, "text", "btn_read")
        self._reg_i18n(self._ardu_write_btn, "text", "btn_write")
        self._reg_i18n(self._ardu_files_btn, "text", "btn_sd_files")
        # спочатку сховані -- покажемо при підключенні
        self._ardu_btns_visible = False

        # тело страницы -- либо чёрный плейсхолдер с лого (пока ничего не
        # загружено), либо таблица+карта (после успешной загрузки миссии).
        # Пустая таблица/карта до загрузки не несут смысла, поэтому вместо
        # них показываем то же самое, что и на сплэш-экране при старте.
        mission_body = ttk.Frame(page_mission)
        mission_body.pack(fill="both", expand=True)
        mission_body.rowconfigure(0, weight=1)
        mission_body.columnconfigure(0, weight=1)

        self.mission_placeholder = tk.Frame(mission_body, bg="black")
        self.mission_placeholder.grid(row=0, column=0, sticky="nsew")
        logo_path = self._find_asset(("icon.png", "logo.png"))
        if logo_path:
            self._mission_placeholder_logo = self._load_logo_thumbnail(logo_path, target_h=170)
            if self._mission_placeholder_logo is not None:
                tk.Label(self.mission_placeholder, image=self._mission_placeholder_logo, bg="black").place(
                    relx=0.5, rely=0.5, anchor="center"
                )

        self.mission_content = ttk.Frame(mission_body)
        self.mission_content.grid(row=0, column=0, sticky="nsew")

        table_frame = ttk.Frame(self.mission_content)
        table_frame.pack(fill="x", **pad)
        table_columns = ("idx", "cmd", "p1", "p2", "p3", "p4", "lat", "lon", "alt", "frame", "dist", "az")
        self.mission_table = ttk.Treeview(
            table_frame, columns=table_columns, show="headings", height=7,
        )
        table_headings = {
            "idx": ("table_col_idx", 36),
            "cmd": ("table_col_command", 130),
            "p1": ("table_col_p1", 55),
            "p2": ("table_col_p2", 55),
            "p3": ("table_col_p3", 55),
            "p4": ("table_col_p4", 55),
            "lat": ("table_col_lat", 90),
            "lon": ("table_col_lon", 90),
            "alt": ("table_col_alt", 60),
            "frame": ("table_col_frame", 50),
            "dist": ("table_col_dist", 70),
            "az": ("table_col_az", 55),
        }
        for col, (key, width) in table_headings.items():
            self.mission_table.heading(col, text=i18n.t(key))
            self.mission_table.column(col, width=width, anchor="center", stretch=False)
        self._table_headings = table_headings  # для ретрансляції нижче

        def _retranslate_table_headers():
            for col, (key, _width) in self._table_headings.items():
                self.mission_table.heading(col, text=i18n.t(key))

        self._retranslate_callbacks.append(_retranslate_table_headers)
        table_vbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.mission_table.yview)
        self.mission_table.configure(yscrollcommand=table_vbar.set)
        self.mission_table.pack(side="left", fill="x", expand=True)
        table_vbar.pack(side="left", fill="y")

        map_ctrl2 = ttk.Frame(self.mission_content)
        map_ctrl2.pack(fill="x", padx=6)
        ttk.Label(map_ctrl2, textvariable=self.occupied_status_var, foreground="#555").pack(side="left")

        # Канвас займає ВСЕ місце, що лишилось на сторінці (fill="both",
        # expand=True) -- без спроб підігнати його форму під маршрут.
        # Спроба через pack_propagate(False)+configure(height=...) під
        # реальні пропорції маршруту НЕ спрацювала: коли висоти вікна не
        # вистачає під потрібну висоту, Tk просто обрізає канвас до того,
        # що фізично є в наявності -- і всі розрахунки пропорції йдуть
        # нанівець. Замість боротьби з висотою вікна -- ширина карти
        # ЗАВЖДИ точно по ширині канваса (render_tiles_fit масштабує
        # мозаїку лише по ширині, без обмеження по висоті, див.
        # map_view._compose_scaled_width), а вертикальний скролбар --
        # запасний варіант на випадок, якщо результуюча висота карти
        # виявиться більшою за видиму область.
        map_canvas_frame = ttk.Frame(self.mission_content)
        map_canvas_frame.pack(fill="both", expand=True, **pad)
        map_canvas_frame.rowconfigure(0, weight=1)
        map_canvas_frame.columnconfigure(0, weight=1)

        self.map_canvas = tk.Canvas(map_canvas_frame, bg="#dddddd", highlightthickness=0, bd=0)
        map_vbar = ttk.Scrollbar(map_canvas_frame, orient="vertical", command=self.map_canvas.yview)
        self.map_canvas.configure(yscrollcommand=map_vbar.set)

        self.map_canvas.grid(row=0, column=0, sticky="nsew")
        map_vbar.grid(row=0, column=1, sticky="ns")

        bind_pan(self.map_canvas)
        self.map_canvas.bind("<MouseWheel>", self._on_map_wheel)     # Windows / macOS
        self.map_canvas.bind("<Button-4>", self._on_map_wheel)       # Linux — колесо вверх
        self.map_canvas.bind("<Button-5>", self._on_map_wheel)       # Linux — колесо вниз

        # если ширина канваса меняется (пользователь потянул окно) --
        # перерисовываем карту точно под новую ширину, а не оставляем
        # старый масштаб
        self._last_map_render = None
        self._map_resize_after_id = None
        self.map_canvas.bind("<Configure>", self._on_map_canvas_resize)

        # по умолчанию -- плейсхолдер; если миссия уже была загружена до
        # перестройки интерфейса (например, при смене языка) -- ниже, после
        # полной сборки страниц, покажем контент вместо него
        self.mission_placeholder.tkraise()

    def _build_analyzer(self, path: str) -> bool:
        """Парсит файл миссии и создаёт self.analyzer (без вызова .analyze()). True при успехе."""
        if not path:
            messagebox.showwarning(i18n.t("msg_no_file_title"), i18n.t("msg_no_file_body"))
            return False
        if not os.path.isfile(path):
            messagebox.showerror(i18n.t("msg_file_not_found_title"), i18n.t("msg_file_not_found_body", path=path))
            return False

        try:
            alt_min = float(self.alt_min_var.get())
            turn_min = float(self.turn_min_var.get())
        except ValueError:
            messagebox.showerror(i18n.t("msg_bad_numbers_title"), i18n.t("msg_bad_numbers_body"))
            return False

        try:
            wps = parse_waypoints(path)
        except Exception as e:
            messagebox.showerror(i18n.t("msg_file_read_error_title"), str(e))
            return False

        terrain = None
        if self.use_srtm_var.get() and self.srtm_var.get().strip():
            try:
                terrain = SRTMTerrain(self.srtm_var.get().strip())
            except SRTMError as e:
                messagebox.showwarning(i18n.t("msg_srtm_unavailable_title"), i18n.t("msg_srtm_unavailable_body", err=e))

        self.analyzer = MissionAnalyzer(wps, alt_min=alt_min, turn_min=turn_min, terrain=terrain)
        self._populate_mission_table(wps)
        return True


    def _populate_mission_table(self, waypoints):
        """Заполняет таблицу миссии (страница «Місія») — як у Mission Planner:
        нульова точка Home (seq=0, команда 16, координати 0/0) не показується."""
        self.mission_table.delete(*self.mission_table.get_children())

        last_pos = None
        for wp in waypoints:
            # пропускаємо нульову Home-точку -- Mission Planner теж її не показує в таблиці
            if wp.index == 0:
                continue

            has_pos = wp.lat != 0 or wp.lon != 0
            dist_str = az_str = ""
            if has_pos and last_pos is not None:
                dist = haversine_m(last_pos[0], last_pos[1], wp.lat, wp.lon)
                az = bearing_deg(last_pos[0], last_pos[1], wp.lat, wp.lon)
                dist_str = f"{dist:.0f}"
                az_str = f"{az:.0f}"
            if has_pos:
                last_pos = (wp.lat, wp.lon)

            self.mission_table.insert("", "end", values=(
                wp.index,
                command_name(wp.command),
                f"{wp.param1:g}", f"{wp.param2:g}", f"{wp.param3:g}", f"{wp.param4:g}",
                f"{wp.lat:.7f}" if has_pos else "",
                f"{wp.lon:.7f}" if has_pos else "",
                f"{wp.alt:g}",
                _frame_name(wp.frame),
                dist_str,
                az_str,
            ))


    def load_mission(self):
        """«Завантажити»: завжди файловий діалог. ArduPilot -- через окрему кнопку Read."""
        self._load_mission_from_file()


    def _load_mission_from_file(self):
        path = filedialog.askopenfilename(
            title=i18n.t("dlg_choose_mission_title"),
            filetypes=[(i18n.t("filetype_waypoints"), "*.waypoints"), (i18n.t("filetype_all"), "*.*")],
        )
        if not path:
            return
        self.file_var.set(path)
        if not self._build_analyzer(path):
            return
        self._finish_load()

    def _finish_load(self):
        self.mission_content.tkraise()
        self._hide_analysis_tabs()
        # таблиця вже заповнена (_build_analyzer -> _populate_mission_table),
        # але без примусового update_idletasks() Tk не встигає її
        # перемалювати до того, як почнеться важкий analyze() нижче --
        # тому виглядає, ніби все "зависає" на кілька секунд
        self.status_var.set(i18n.t("status_analyzing"))
        self.update_idletasks()

        # analyzer.analyze() -- це і сегментна перевірка AGL вздовж усієї
        # траєкторії з запитами до SRTM, тому може бути повільним на
        # довгих місіях. Рахуємо у фоновому потоці, як і завантаження
        # тайлів карти нижче, щоб вікно не "замерзало".
        threading.Thread(target=self._analyze_worker, daemon=True).start()


    def _analyze_worker(self):
        self.analyzer.analyze()
        self.after(0, self._on_analysis_done)


    def _on_analysis_done(self):
        self._distribute_report_text(self._captured_report())
        # важкі елементи "Аналіз" (профілі висоти/кута/глісади -- SRTM-запити
        # на кожен крок маршруту, карта маршруту -- мережевий фетч тайлів)
        # рахуються ЛІНИВО, при першому реальному відкритті сторінки
        # (_ensure_analysis_built, викликається з _show_analysis_tabs), а
        # не одразу тут -- інакше кожне "Завантажити" на "Місія" зайво
        # рахувало б усе це, навіть якщо користувач "Аналіз" ще не відкривав
        self._analysis_built = False
        self.status_var.set(
            i18n.t("status_ready_fmt", n=len(self.analyzer.nav_wps), m=len(self.analyzer.issues))
        )
        self._compute_arrival_time()
        self._save_settings()
        self.render_map(auto_zoom=True)


    def save_csv(self):
        """«Зберегти»: завжди файловий діалог. ArduPilot -- через окрему кнопку Write."""
        if self.analyzer is None:
            messagebox.showwarning(i18n.t("msg_no_data_title"), i18n.t("msg_no_data_body"))
            return
        self._save_mission_to_file()


    def _save_mission_to_file(self):
        path = filedialog.asksaveasfilename(
            title=i18n.t("dlg_save_csv_title"),
            defaultextension=".csv",
            filetypes=[
                ("CSV", "*.csv"),
                (i18n.t("filetype_waypoints"), "*.waypoints"),
            ],
        )
        if not path:
            return
        if path.lower().endswith(".waypoints"):
            self._export_waypoints(path)
        else:
            self.analyzer.export_csv(path)
        messagebox.showinfo(i18n.t("msg_saved_title"), i18n.t("msg_saved_body", path=path))




    def _export_waypoints(self, path: str):
        """
        Пишет полный список точек (self.analyzer.all_wps) в формате
        QGC WPL 110 -- тот же текстовый .waypoints, который принимает
        Mission Planner и который мы сами читаем при загрузке.
        """
        wps = self.analyzer.all_wps
        with open(path, "w", encoding="utf-8") as f:
            f.write("QGC WPL 110\n")
            for wp in wps:
                current = 1 if wp.index == 0 else 0
                f.write(
                    "\t".join(
                        str(v)
                        for v in (
                            wp.index, current, wp.frame, wp.command,
                            wp.param1, wp.param2, wp.param3, wp.param4,
                            wp.lat, wp.lon, wp.alt, 1,
                        )
                    )
                    + "\n"
                )

    # ------------------------------------------------------------- график --


    def _on_map_canvas_resize(self, event):
        # дебаунс: при перетягивании окна <Configure> сыплется десятками
        # событий в секунду -- пересобирать мозаику на каждое дорого и
        # дёргано. Ждём короткую паузу после последнего события и
        # перерисовываем последнюю скачанную мозаику под новый размер
        # (без повторного похода в сеть за тайлами).
        if self._last_map_render is None:
            return
        if self._map_resize_after_id is not None:
            self.after_cancel(self._map_resize_after_id)
        self._map_resize_after_id = self.after(150, self._redraw_last_map_render)


    def _redraw_last_map_render(self):
        self._map_resize_after_id = None
        if self._last_map_render is None:
            return
        tiles, zoom, tx_min, tx_max, ty_min, ty_max, occupied_polygons = self._last_map_render
        render_tiles_fit(
            self.map_canvas, self.analyzer, zoom,
            tx_min, tx_max, ty_min, ty_max, tiles, self._map_images,
            overlay_polygons=occupied_polygons,
        )


    def _on_map_wheel(self, event):
        if self.analyzer is None:
            return
        # если сейчас уже идёт загрузка -- игнорируем, чтобы не наплодить потоки
        if self._map_loading:
            return

        # Windows/macOS: event.delta (+120/-120 обычно), Linux: Button-4/Button-5
        if getattr(event, "num", None) == 4:
            direction = 1
        elif getattr(event, "num", None) == 5:
            direction = -1
        else:
            direction = 1 if event.delta > 0 else -1

        new_zoom = max(1, min(19, self.zoom_var.get() + direction))
        if new_zoom == self.zoom_var.get():
            return
        self.zoom_var.set(new_zoom)
        self.render_map()


    def _find_safe_zoom(self, start_zoom: int, min_zoom: int = 1):
        """
        Ищет наибольший зум не выше start_zoom, при котором маршрут
        укладывается в лимит тайлов (см. MapTooLargeError). Чем мельче
        зум, тем крупнее тайлы в реальных метрах и тем меньше их нужно
        для покрытия одной и той же площади -- поэтому идём вниз.
        Возвращает None, если у маршрута вообще нет точек.
        """
        for z in range(start_zoom - 1, min_zoom - 1, -1):
            try:
                compute_tile_bounds(self.analyzer, z)
                return z
            except MapTooLargeError:
                continue
            except ValueError:
                return None
        return min_zoom


    def _find_best_fit_zoom(self, canvas_width: int, max_zoom: int = 19, min_zoom: int = 1):
        """
        Підбирає зум, на якому НАТИВНА ширина мозаїки тайлів (діапазон
        тайлів по X, помножений на TILE_SIZE, -- до будь-якого
        масштабування Pillow) якнайближче до реальної ширини канваса.

        Навіщо: render_tiles_fit усе одно розтягне/стисне будь-який зум
        точно під канвас (це вирішує проблему сірих полів), але ЯКІСТЬ
        картинки залежить від того, наскільки зум відповідає екрану --
        занадто високий зум означає дрібну ділянку, розтягнуту (блюр);
        занадто низький -- завелику ділянку, стиснуту (втрата деталей).
        Мета -- зум, де майже нема чого ані розтягувати, ані стискати.

        Повертає None, якщо у маршруту взагалі немає точок.
        """
        best_zoom = None
        best_diff = None
        for z in range(max_zoom, min_zoom - 1, -1):
            try:
                tx_min, tx_max, ty_min, ty_max, total = compute_tile_bounds(self.analyzer, z)
            except MapTooLargeError:
                continue
            except ValueError:
                return None
            native_w = (tx_max - tx_min + 1) * TILE_SIZE
            diff = abs(native_w - canvas_width)
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_zoom = z
        return best_zoom if best_zoom is not None else min_zoom


    def render_map(self, auto_zoom: bool = False):
        if self.analyzer is None:
            messagebox.showwarning(i18n.t("msg_no_data_title"), i18n.t("msg_no_data_body"))
            return

        if auto_zoom:
            # Нова місія -- підбираємо зум заново під сам маршрут І під
            # реальну ширину канваса (а НЕ беремо те, що лежить у
            # self.zoom_var -- це значення прийшло з settings.json від
            # попереднього перегляду, можливо зовсім іншої місії/масштабу).
            self.map_canvas.update_idletasks()
            canvas_w = max(self.map_canvas.winfo_width(), self.mission_content.winfo_width(), 100)
            zoom = self._find_best_fit_zoom(canvas_w)
            if zoom is None:
                messagebox.showwarning(i18n.t("msg_no_points_title"), i18n.t("msg_no_points_body"))
                return
            self.zoom_var.set(zoom)
            # окреме поле, яке НЕ чіпається подальшою ручною прокруткою
            # колесом миші на "Місія" (та лише крутить self.zoom_var) --
            # саме це значення "Маршрут" на "Аналіз" бере як своє, щоб не
            # рахувати зум заново під власний канвас і не залежати від
            # того, як користувач потім покрутив зум на "Місія".
            self._initial_auto_zoom = zoom
            tx_min, tx_max, ty_min, ty_max, total = compute_tile_bounds(self.analyzer, zoom)
        else:
            zoom = int(self.zoom_var.get())
            try:
                tx_min, tx_max, ty_min, ty_max, total = compute_tile_bounds(self.analyzer, zoom)
            except MapTooLargeError:
                # текущий зум слишком мелкий для площади маршрута -- вместо
                # предупреждения молча подбираем самый крупный зум, который
                # ещё укладывается в лимит тайлов, и рисуем картой сразу с ним
                safe_zoom = self._find_safe_zoom(zoom)
                if safe_zoom is None:
                    messagebox.showwarning(i18n.t("msg_no_points_title"), i18n.t("msg_no_points_body"))
                    return
                zoom = safe_zoom
                self.zoom_var.set(zoom)
                tx_min, tx_max, ty_min, ty_max, total = compute_tile_bounds(self.analyzer, zoom)
            except ValueError:
                messagebox.showwarning(i18n.t("msg_no_points_title"), i18n.t("msg_no_points_body"))
                return

        disk_cache = self.tilecache_var.get().strip() or None
        self.tile_cache = OnlineTileCache(provider=self.provider_key, disk_cache_dir=disk_cache)
        self._save_settings()

        self._cancel_event = threading.Event()
        self._map_loading = True
        self.map_status_var.set(i18n.t("status_loading_tiles_fmt", done=0, total=total))

        def progress_cb(done, tot):
            self.after(0, lambda: self.map_status_var.set(i18n.t("status_loading_tiles_fmt", done=done, total=tot)))

        def worker():
            tiles, cancelled = fetch_tiles(
                self.tile_cache, tx_min, tx_max, ty_min, ty_max, zoom,
                progress_cb=progress_cb, cancel_event=self._cancel_event,
            )
            occupied_polygons = None
            occupied_date = None
            if self.show_occupied_var.get() and not cancelled:
                occ_cache = self.tilecache_var.get().strip() or "map_cache"
                geojson, date_str = fetch_occupied_geojson(occ_cache)
                if geojson is not None:
                    occupied_polygons = extract_polygons(geojson)
                    occupied_date = date_str
            self.after(
                0,
                lambda: self._on_tiles_ready(
                    tiles, zoom, tx_min, tx_max, ty_min, ty_max, cancelled,
                    occupied_polygons, occupied_date,
                ),
            )

        threading.Thread(target=worker, daemon=True).start()


    def _on_tiles_ready(
        self, tiles, zoom, tx_min, tx_max, ty_min, ty_max, cancelled,
        occupied_polygons=None, occupied_date=None,
    ):
        self._map_loading = False

        if cancelled:
            self.map_status_var.set(i18n.t("status_map_cancelled"))
            return

        if self.show_occupied_var.get():
            if occupied_polygons is not None:
                d = f"{occupied_date[:4]}-{occupied_date[4:6]}-{occupied_date[6:]}" if occupied_date else "?"
                self.occupied_status_var.set(i18n.t("occupied_status_date_fmt", date=d))
            else:
                self.occupied_status_var.set(i18n.t("occupied_status_failed"))
        else:
            self.occupied_status_var.set("")

        found, total, missing, undecodable = render_tiles_fit(
            self.map_canvas, self.analyzer, zoom,
            tx_min, tx_max, ty_min, ty_max, tiles, self._map_images,
            overlay_polygons=occupied_polygons,
        )

        # запоминаем последний успешный рендер -- при изменении размера
        # окна просто перерисуем ту же мозаику под новый размер канваса
        # (см. _on_map_canvas_resize), не скачивая тайлы заново
        self._last_map_render = (tiles, zoom, tx_min, tx_max, ty_min, ty_max, occupied_polygons)

        status = i18n.t("status_rendered_fmt", found=found, total=total, missing=missing)
        if undecodable:
            status += i18n.t("status_undecodable_suffix_fmt", n=undecodable)
        self.map_status_var.set(status)

        if undecodable and not self._pil_warning_shown:
            self._pil_warning_shown = True
            messagebox.showinfo(i18n.t("msg_need_pillow_title"), i18n.t("msg_need_pillow_body", n=undecodable))


