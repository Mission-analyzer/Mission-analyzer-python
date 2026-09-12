"""
optimization_page.py -- окрема сторінка "Оптимізація" (винесена з
analysis_page.py, де раніше була вкладкою всередині "Аналіз" -- тепер
власний пункт навбару, праворуч від "Аналіз"). Дві вкладки всередині:

    "Координати" -- горизонтальний обхід заборонних зон.

    "Висота" -- для кожної точки маршруту: рельєф у ній + цільова
    відносна висота зони (контрольована/окупована територія, "Тип
    маршруту" в Конфігурації), де вона фактично лежить (перевірка через
    державний кордон, country_borders.py). Ребро, де відбувається
    перетин кордону, отримує висоту переходу на обидві кінцеві точки.
    Після цього -- ітеративна перевірка кліренсу рельєфу уздовж кожного
    відрізка (крок 50м): якщо десь порушено, піднімається ближній кінець
    ребра чи вставляється нова точка, залежно від того, де саме
    найгірший кліренс.
"""

from __future__ import annotations

import threading
import math
import time
import dataclasses
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from geo import haversine_m, lonlat_to_pixel
from map_view import bind_pan, render_viewport
import populated_areas
import restricted_zones
import route_optimizer
import altitude_optimizer
import route_types
import occupied_layer
import country_borders
import aircraft_profiles
from waypoints import Waypoint, write_waypoints
from srtm import SRTMError
import theme
import i18n

from mission_page import MISSION_THEME_DARK, MISSION_THEME_LIGHT
from elevation_view import draw_altitude_before_after
from angle_view import draw_angle_profile


class OptimizationPageMixin:

    def _build_optimization_page(self, content, pad):
        """Сторінка "Оптимізація" -- notebook з двома вкладками
        ("Координати" -- вже реалізовано, "Висота" -- заглушка,
        реалізація окремим кроком)."""
        page_optimization = ttk.Frame(content)
        page_optimization.grid(row=0, column=0, sticky="nsew")
        self.pages["optimization"] = page_optimization

        self.opt_notebook = ttk.Notebook(page_optimization)
        self.opt_notebook.pack(fill="both", expand=True, **pad)

        self._optimization_outer_canvases = []  # для перефарбовки при зміні теми
        self._optimization_report_texts = []
        self._optimization_vbars = []

        initial_viewport_h = getattr(self, "_viewport_h", None) or 700

        def make_scroll_tab(tab_title_key: str):
            """Той самий паттерн, що й у analysis_page.py -- один
            вертикальний скрол на всю вкладку. Дубльовано тут навмисно
            (невеликий, суто layout-допоміжний код, не "значення", яке
            мало б бути єдиним джерелом істини на кшталт кольорів/
            шрифтів у theme.py)."""
            tab = ttk.Frame(self.opt_notebook)
            self.opt_notebook.add(tab, text=i18n.t(tab_title_key))
            self._retranslate_callbacks.append(
                lambda _t=tab, _k=tab_title_key: self.opt_notebook.tab(_t, text=i18n.t(_k))
            )

            outer = tk.Canvas(tab, highlightthickness=0, bg=self.palette["bg"])
            self._optimization_outer_canvases.append(outer)
            vbar = ttk.Scrollbar(tab, orient="vertical", command=outer.yview)
            self._optimization_vbars.append(vbar)
            outer.configure(yscrollcommand=vbar.set)
            outer.pack(side="left", fill="both", expand=True)
            vbar.pack(side="right", fill="y")

            inner = ttk.Frame(outer)
            inner_id = outer.create_window((0, 0), window=inner, anchor="nw")

            def _on_inner_configure(event, _outer=outer):
                _outer.configure(scrollregion=_outer.bbox("all"))

            def _on_outer_configure(event, _outer=outer, _id=inner_id):
                _outer.itemconfigure(_id, width=event.width)

            inner.bind("<Configure>", _on_inner_configure)
            outer.bind("<Configure>", _on_outer_configure)

            def _on_mousewheel(event, _outer=outer):
                _outer.yview_scroll(int(-1 * (event.delta / 120)), "units")

            outer.bind_all("<MouseWheel>", _on_mousewheel, add="+")

            return tab, inner

        def make_plain_text(parent, height: int):
            """Той самий паттерн, що в analysis_page.py -- звичайний
            tk.Text без власної смуги прокрутки."""
            c = MISSION_THEME_DARK if self._is_dark_theme() else MISSION_THEME_LIGHT
            widget = tk.Text(
                parent, wrap="word", font=("Consolas", 9),
                height=height, width=1, relief="solid", borderwidth=1,
                bg=c["table_bg"], fg=c["table_fg"], insertbackground=c["table_fg"],
            )
            theme.make_text_readonly(widget)
            self._optimization_report_texts.append(widget)
            return widget

        # --- «Координати» = обхід ВСІХ проблемних ребер маршруту одним розрахунком ---
        optimize_tab, optimize_inner = make_scroll_tab("tab_optimization_coordinates")

        # Вибір профілю літака -- заповнює поля НИЖЧЕ (крейсерська
        # швидкість/витрата/крен), ЛИШЕ для цього розрахунку. НЕ змінює
        # "поточний" профіль у "Конфігурації" -- вибір тут суто локальна
        # зручність, без побічного ефекту на решту програми. Заодно
        # користувач, який ніколи не заходив у "Конфігурація", тут
        # взагалі дізнається, що профілі існують.
        opt_profile_row = ttk.Frame(optimize_inner)
        opt_profile_row.pack(fill="x", pady=(0, 6))
        self._reg_i18n(ttk.Label(opt_profile_row), "text", "lbl_aircraft_profile").pack(side="left", padx=(0, 4))
        self.opt_profile_var = tk.StringVar(value="")
        self.opt_profile_box = ttk.Combobox(
            opt_profile_row, textvariable=self.opt_profile_var, state="readonly", width=22,
        )
        self.opt_profile_box.pack(side="left")
        self.opt_profile_box.bind("<<ComboboxSelected>>", self._on_optimize_profile_selected)
        # список профілів читається заново ПЕРЕД кожним відкриттям (не
        # тільки один раз при побудові сторінки) -- профіль міг бути
        # доданий у "Конфігурації" вже ПІСЛЯ того, як "Аналіз" збудувався
        self.opt_profile_box.bind("<Button-1>", self._refresh_optimize_profile_list)

        opt_controls = ttk.Frame(optimize_inner)
        opt_controls.pack(fill="x", pady=(0, 4))
        self._reg_i18n(ttk.Label(opt_controls), "text", "lbl_tank_capacity").pack(side="left", padx=(0, 4))
        self.opt_tank_capacity_var = tk.StringVar(value="80")
        ttk.Entry(opt_controls, textvariable=self.opt_tank_capacity_var, width=6).pack(side="left", padx=(0, 2))
        self._reg_i18n(ttk.Label(opt_controls), "text", "lbl_liters").pack(side="left", padx=(0, 12))

        self._reg_i18n(ttk.Label(opt_controls), "text", "lbl_cruise_consumption").pack(side="left", padx=(0, 4))
        self.opt_cruise_consumption_var = tk.StringVar(value="3.0")
        ttk.Entry(opt_controls, textvariable=self.opt_cruise_consumption_var, width=6).pack(side="left", padx=(0, 2))
        self._reg_i18n(ttk.Label(opt_controls), "text", "lbl_liters_per_hour").pack(side="left", padx=(0, 12))

        self._reg_i18n(ttk.Label(opt_controls), "text", "lbl_cruise_speed").pack(side="left", padx=(0, 4))
        self.opt_cruise_speed_var = tk.StringVar(value="90")
        ttk.Entry(opt_controls, textvariable=self.opt_cruise_speed_var, width=6).pack(side="left", padx=(0, 2))
        self._reg_i18n(ttk.Label(opt_controls), "text", "lbl_kmh").pack(side="left", padx=(0, 12))

        self._reg_i18n(ttk.Label(opt_controls), "text", "lbl_roll_limit").pack(side="left", padx=(0, 4))
        self.opt_roll_limit_var = tk.StringVar(value="25")
        ttk.Entry(opt_controls, textvariable=self.opt_roll_limit_var, width=6).pack(side="left", padx=(0, 2))
        self._reg_i18n(ttk.Label(opt_controls), "text", "lbl_deg").pack(side="left")

        opt_controls2 = ttk.Frame(optimize_inner)
        opt_controls2.pack(fill="x", pady=(0, 8))
        self._reg_i18n(ttk.Label(opt_controls2), "text", "lbl_exclude_landing_legs").pack(side="left", padx=(0, 4))
        self.opt_exclude_legs_var = tk.StringVar(value="2")
        ttk.Entry(opt_controls2, textvariable=self.opt_exclude_legs_var, width=4).pack(side="left", padx=(0, 12))
        self.optimize_route_btn = ttk.Button(opt_controls2, command=self._run_route_optimization)
        self._reg_i18n(self.optimize_route_btn, "text", "btn_optimize_route")
        self.optimize_route_btn.pack(side="left")
        self.save_optimized_btn = ttk.Button(
            opt_controls2, command=self._save_optimized_mission, state="disabled",
        )
        self._reg_i18n(self.save_optimized_btn, "text", "btn_save_optimized_mission")
        self.save_optimized_btn.pack(side="left", padx=(6, 0))

        # Якщо користувач ЗМІНЮЄ будь-яке з полів після успішної
        # оптимізації -- це сигнал "хочу спробувати з іншими
        # параметрами", тож "Оптимізувати" знову розблоковується.
        # Профіль (opt_profile_var) теж входить -- вибір іншого
        # профілю зі списку так само підставляє нові значення полів.
        def _on_optimize_settings_changed(*_args):
            if self.optimize_route_btn.cget("state") == "disabled":
                self.optimize_route_btn.configure(state="normal")

        for var in (
            self.opt_tank_capacity_var, self.opt_cruise_consumption_var,
            self.opt_cruise_speed_var, self.opt_roll_limit_var,
            self.opt_exclude_legs_var, self.opt_profile_var,
        ):
            var.trace_add("write", _on_optimize_settings_changed)

        # підтягуємо крейсерську швидкість/витрату/крен з ПОТОЧНОГО
        # профілю літака (Конфігурація), якщо такий є -- користувачу не
        # треба вводити ці значення вручну щоразу, вони вже задані один
        # раз на профіль. Поля лишаються звичайними editable Entry --
        # це лише ЗРУЧНА ПІДКАЗКА за замовчуванням, не жорстка
        # прив'язка, для одноразового тесту з іншими цифрами можна
        # просто перезаписати вручну.
        self._refresh_optimize_profile_list()
        self._prefill_optimization_from_profile()

        self.optimize_status_var = tk.StringVar(value="")
        ttk.Label(optimize_inner, textvariable=self.optimize_status_var, foreground="#888").pack(
            anchor="w", pady=(0, 4)
        )

        optimize_map_box = ttk.LabelFrame(optimize_inner, height=initial_viewport_h)
        self._reg_i18n(optimize_map_box, "text", "box_optimization_map")
        optimize_map_box.pack(fill="x", pady=(0, 8))
        optimize_map_box.pack_propagate(False)
        self._optimize_map_box = optimize_map_box

        self.optimize_map_canvas = tk.Canvas(optimize_map_box, bg=self._map_placeholder_bg(), highlightthickness=0, bd=0)
        self.optimize_map_canvas.pack(fill="both", expand=True)
        bind_pan(self.optimize_map_canvas)
        self._optimize_map_images = []
        self._route_optimization_result = None  # RouteOptimizationResult -- None, доки не запущено
        self._optimize_map_resize_after_id = None

        def _on_optimize_map_configure(event):
            if self._optimize_map_resize_after_id is not None:
                self.after_cancel(self._optimize_map_resize_after_id)
            self._optimize_map_resize_after_id = self.after(150, self._load_optimization_map)

        self.optimize_map_canvas.bind("<Configure>", _on_optimize_map_configure)

        optimize_report_box = ttk.LabelFrame(optimize_inner, height=initial_viewport_h)
        self._reg_i18n(optimize_report_box, "text", "box_optimization_report")
        optimize_report_box.pack(fill="x", pady=(0, 8))
        optimize_report_box.pack_propagate(False)
        self._optimize_report_box = optimize_report_box

        # ТОЧНА пікселева висота через pack_propagate(False) на самому
        # контейнері -- той самий метод, що вже гарантовано точно працює
        # для карти (populated_map_box/optimize_map_box): текст просто
        # ЗАПОВНЮЄ контейнер (fill="both", height=1 -- значення не має
        # значення, розмір диктує контейнер), а не навпаки (як робив
        # раніше -- рахував рядки тексту під висоту, це НАБЛИЖЕНО через
        # метрику шрифту, тому завжди трохи розходилось з реальним
        # пікселевим розміром карти вище -- саме тому "майже повний
        # екран" замість точно повного).
        optimize_report_inner = tk.Frame(optimize_report_box)
        optimize_report_inner.pack(fill="both", expand=True, padx=4, pady=4)
        self.optimize_report_text = make_plain_text(optimize_report_inner, height=1)
        optimize_report_scroll = ttk.Scrollbar(
            optimize_report_inner, orient="vertical", command=self.optimize_report_text.yview,
        )
        self.optimize_report_text.configure(yscrollcommand=optimize_report_scroll.set)
        self.optimize_report_text.pack(side="left", fill="both", expand=True)
        optimize_report_scroll.pack(side="right", fill="y")

        # --- «Висота» -- ЗАПЛАНОВАНА, ще НЕ реалізована оптимізація
        # висотного профілю (див. докстрінг модуля вище). Поки що --
        # чесна заглушка, а не порожня вкладка без пояснення.
        altitude_tab, altitude_inner = make_scroll_tab("tab_altitude_optimization")

        alt_route_type_row = ttk.Frame(altitude_inner)
        alt_route_type_row.pack(fill="x", pady=(0, 6))
        self._reg_i18n(ttk.Label(alt_route_type_row), "text", "lbl_route_type").pack(side="left", padx=(0, 4))
        self.alt_route_type_var = tk.StringVar(value="")
        self.alt_route_type_box = ttk.Combobox(
            alt_route_type_row, textvariable=self.alt_route_type_var, state="readonly", width=22,
        )
        self.alt_route_type_box.pack(side="left")
        # список типів читається заново ПЕРЕД кожним відкриттям (не
        # тільки при побудові сторінки) -- той самий принцип, що вже
        # є для профілю літака на вкладці "Координати": тип міг бути
        # доданий у "Конфігурації" вже ПІСЛЯ побудови цієї сторінки
        self.alt_route_type_box.bind("<Button-1>", self._refresh_altitude_route_type_list)
        self._refresh_altitude_route_type_list()

        alt_hint = ttk.Frame(altitude_inner)
        alt_hint.pack(fill="x", pady=(0, 8))
        self._reg_i18n(
            ttk.Label(alt_hint, foreground="#888", wraplength=600, justify="left"),
            "text", "hint_altitude_uses_coordinate_result",
        ).pack(anchor="w")

        alt_controls = ttk.Frame(altitude_inner)
        alt_controls.pack(fill="x", pady=(0, 8))
        self.optimize_altitude_btn = ttk.Button(alt_controls, command=self._run_altitude_optimization)
        self._reg_i18n(self.optimize_altitude_btn, "text", "btn_optimize_altitude")
        self.optimize_altitude_btn.pack(side="left")
        self.save_altitude_btn = ttk.Button(
            alt_controls, command=self._save_altitude_optimized_mission, state="disabled",
        )
        self._reg_i18n(self.save_altitude_btn, "text", "btn_save_optimized_mission")
        self.save_altitude_btn.pack(side="left", padx=(6, 0))

        self.alt_step_mode_var = tk.BooleanVar(value=False)
        self._reg_i18n(
            ttk.Checkbutton(alt_controls, variable=self.alt_step_mode_var), "text", "chk_altitude_step_mode",
        ).pack(side="left", padx=(12, 0))
        self.alt_step_next_btn = ttk.Button(
            alt_controls, command=self._on_altitude_step_next, state="disabled",
        )
        self._reg_i18n(self.alt_step_next_btn, "text", "btn_altitude_step_next")
        self.alt_step_next_btn.pack(side="left", padx=(6, 0))
        self.alt_cancel_btn = ttk.Button(
            alt_controls, command=self._on_altitude_cancel, state="disabled",
        )
        self._reg_i18n(self.alt_cancel_btn, "text", "btn_altitude_cancel")
        self.alt_cancel_btn.pack(side="left", padx=(6, 0))
        def _on_space_pressed(event):
            if not self.alt_step_mode_var.get():
                return  # покроковий режим вимкнено -- пробіл не наш, не чіпаємо
            focused = self.focus_get()
            if isinstance(focused, (tk.Entry, tk.Text, ttk.Entry, ttk.Combobox)):
                return  # фокус у полі вводу -- пробіл має вводити символ, не просувати крок
            self._on_altitude_step_next()
        # bind_all -- НЕ self.bind: останній спрацьовує лише коли
        # фокус клавіатури саме на головному вікні, а ПІСЛЯ кліку на
        # "Оптимізувати висоту" фокус лишається на самій кнопці, яка
        # перехоплює пробіл (стандартна поведінка Tkinter -- фокусна
        # кнопка сама реагує на пробіл, як на клік). bind_all працює
        # незалежно від того, який віджет має фокус -- але тому й
        # потребує захисту вище, щоб не заважати звичайному вводу тексту.
        self.bind_all("<space>", _on_space_pressed)

        self.altitude_status_var = tk.StringVar(value="")
        ttk.Label(altitude_inner, textvariable=self.altitude_status_var, foreground="#888").pack(
            anchor="w", pady=(0, 4)
        )

        altitude_chart_box = ttk.LabelFrame(altitude_inner, height=initial_viewport_h)
        self._reg_i18n(altitude_chart_box, "text", "box_altitude_chart")
        altitude_chart_box.pack(fill="x", pady=(0, 8))
        altitude_chart_box.pack_propagate(False)
        self.altitude_chart_canvas = tk.Canvas(
            altitude_chart_box, bg=self._graph_canvas_bg(), highlightthickness=0,
        )
        self.altitude_chart_canvas.pack(fill="both", expand=True)

        def _on_altitude_chart_configure(event):
            self._redraw_altitude_chart()
        self.altitude_chart_canvas.bind("<Configure>", _on_altitude_chart_configure)

        altitude_angle_chart_box = ttk.LabelFrame(altitude_inner, height=int(initial_viewport_h * 0.6))
        self._reg_i18n(altitude_angle_chart_box, "text", "box_altitude_angle_chart")
        altitude_angle_chart_box.pack(fill="x", pady=(0, 8))
        altitude_angle_chart_box.pack_propagate(False)
        self.altitude_angle_chart_canvas = tk.Canvas(
            altitude_angle_chart_box, bg=self._graph_canvas_bg(), highlightthickness=0,
        )
        self.altitude_angle_chart_canvas.pack(fill="both", expand=True)

        def _on_altitude_angle_chart_configure(event):
            self._redraw_altitude_angle_chart()
        self.altitude_angle_chart_canvas.bind("<Configure>", _on_altitude_angle_chart_configure)

        altitude_report_box = ttk.LabelFrame(altitude_inner, height=initial_viewport_h)
        self._reg_i18n(altitude_report_box, "text", "box_altitude_report")
        altitude_report_box.pack(fill="x", pady=(0, 8))
        altitude_report_box.pack_propagate(False)

        altitude_report_inner = tk.Frame(altitude_report_box)
        altitude_report_inner.pack(fill="both", expand=True, padx=4, pady=4)
        self.altitude_report_text = make_plain_text(altitude_report_inner, height=1)
        altitude_report_scroll = ttk.Scrollbar(
            altitude_report_inner, orient="vertical", command=self.altitude_report_text.yview,
        )
        self.altitude_report_text.configure(yscrollcommand=altitude_report_scroll.set)
        self.altitude_report_text.pack(side="left", fill="both", expand=True)
        altitude_report_scroll.pack(side="right", fill="y")

        self._altitude_optimization_result = None
        self._altitude_nav_wps = None

    def _run_route_optimization(self):
        """Кнопка "Оптимізувати маршрут" -- валідація введених даних,
        запуск у фоновому потоці (мережеві запити Overpass API на
        кожне ребро)."""
        if self.analyzer is None or not self.analyzer.nav_wps:
            messagebox.showwarning(i18n.t("msg_weather_title"), i18n.t("msg_load_mission_first_body"))
            return

        # радіус обльоту тепер ЗАЛЕЖИТЬ ВІД НАСЕЛЕННЯ конкретного НП
        # (з "Тип маршруту" у Конфігурації) -- ЗА ПРЯМОЮ ВКАЗІВКОЮ
        # користувача, замінює колишнє єдине поле-поріг на вкладці
        # "Аналіз". Без обраного типу маршруту координатна оптимізація
        # просто не має звідки взяти радіус -- явне попередження, а не
        # тиха робота з випадковим значенням за замовчуванням.
        route_type_store = route_types.load_route_types(route_types.default_route_types_path())
        route_type = route_type_store.get_current()
        if route_type is None:
            messagebox.showwarning(i18n.t("msg_weather_title"), i18n.t("msg_no_route_type_for_coords"))
            return

        try:
            tank_capacity = float(self.opt_tank_capacity_var.get().replace(",", "."))
            cruise_consumption = float(self.opt_cruise_consumption_var.get().replace(",", "."))
            cruise_speed = float(self.opt_cruise_speed_var.get().replace(",", "."))
            roll_limit = float(self.opt_roll_limit_var.get().replace(",", "."))
            exclude_legs = int(self.opt_exclude_legs_var.get())
            if (tank_capacity <= 0 or cruise_consumption <= 0
                    or cruise_speed <= 0 or roll_limit <= 0 or exclude_legs < 0):
                raise ValueError
        except ValueError:
            messagebox.showwarning(i18n.t("msg_weather_title"), i18n.t("msg_invalid_optimization_input_body"))
            return

        fuel_budget = route_optimizer.FuelBudget(
            tank_capacity_l=tank_capacity,
            cruise_consumption_lph=cruise_consumption,
            cruise_speed_kmh=cruise_speed,
        )

        self.optimize_route_btn.configure(state="disabled")
        self.save_optimized_btn.configure(state="disabled")
        self.optimize_status_var.set(i18n.t("status_optimizing_route"))

        # частковий стан для ПРОГРЕСИВНОГО малювання карти -- оригінальний
        # маршрут відомий одразу (без мережі, з nav_wps), малюємо його
        # негайно; new_route наповнюється по мірі готовності кожного
        # ребра (on_progress у _route_optimization_worker)
        self._route_optimization_result = None
        self._optimize_partial_new_route = [(self.analyzer.nav_wps[0].lat, self.analyzer.nav_wps[0].lon)]
        self._load_optimization_map()

        threading.Thread(
            target=self._route_optimization_worker,
            args=(route_type, exclude_legs, fuel_budget, roll_limit),
            daemon=True,
        ).start()


    def _route_optimization_worker(self, route_type, exclude_legs, fuel_budget, roll_limit):
        """Фоновий потік: викликає route_optimizer.optimize_route() з
        РЕАЛЬНИМ settlements_fetcher (populated_areas.fetch_settlements).
        НІЯКОГО звернення до Tkinter напряму -- лише через self.after(0, ...)."""
        try:
            def fetcher(lat_min, lat_max, lon_min, lon_max):
                # той самий шар заборонних зон, що й "Обліт НП"
                # (restricted_zones.py) -- гарантовано той самий набір
                # сіл для того самого маршруту, і мережа торкається лише
                # дійсно нових ділянок, не всього bbox щоразу заново
                settlements, all_covered = restricted_zones.get_settlements_for_bbox(
                    lat_min, lat_max, lon_min, lon_max,
                )
                # ТИМЧАСОВЕ діагностичне вивантаження (за прямою
                # вказівкою користувача) -- список сіл, які реально
                # прийшли для КОЖНОГО запиту bbox, дописується у файл
                # у поточній робочій директорії. НЕ чіпає інтерфейс.
                try:
                    import json as _json
                    dump_path = "debug_settlements_dump.json"
                    try:
                        with open(dump_path, "r", encoding="utf-8") as f:
                            dump_data = _json.load(f)
                    except (FileNotFoundError, ValueError):
                        dump_data = []
                    dump_data.append({
                        "bbox": [lat_min, lat_max, lon_min, lon_max],
                        "all_covered": all_covered,
                        "settlements": settlements,
                    })
                    with open(dump_path, "w", encoding="utf-8") as f:
                        _json.dump(dump_data, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass  # діагностичне вивантаження НІКОЛИ не має зривати сам розрахунок
                if not all_covered:
                    self.after(0, lambda: self.optimize_status_var.set(
                        i18n.t("status_zones_partial_coverage"),
                    ))
                return settlements

            def on_progress(done, total, leg_result):
                self.after(0, lambda: self.optimize_status_var.set(
                    i18n.t("status_optimizing_leg_fmt", done=done, total=total)
                ))
                # прогресивне домальовування карти -- ЦЕЙ саме потік
                # (фоновий) лише ДОПИСУЄ у список (проста atomic-подібна
                # операція append, безпечна без блокувань -- єдиний
                # writer тут, головний потік лише ЧИТАЄ через after());
                # саме малювання завжди відбувається в головному потоці
                # Tkinter, як і має бути -- Canvas не можна чіпати з
                # фонового потоку напряму
                self._optimize_partial_new_route.extend(leg_result.inserted_waypoints)
                for wp in self.analyzer.nav_wps[leg_result.leg_index + 1:leg_result.leg_index + 2]:
                    self._optimize_partial_new_route.append((wp.lat, wp.lon))
                self.after(0, self._load_optimization_map)

            # ЗА ПРЯМОЮ ВКАЗІВКОЮ користувача: ребро, що перетинає
            # державний кордон, НЕ обходить населені пункти -- щільні
            # точки обходу фізично несумісні з відстанню, потрібною
            # для плавної зміни висоти на переході (реальний баг: 57°
            # кут, коли перехід висоти намагався втиснутись у 129м
            # відрізок обходу). Замість примирення двох несумісних
            # вимог в одному місці -- на такому ребрі просто немає
            # обходу, як і для "зони посадки".
            exclude_leg_indices = set()
            try:
                border_polygons = self._get_country_border_polygon("RUS")
                if border_polygons:
                    nav_wps = self.analyzer.nav_wps
                    in_occupied = [
                        altitude_optimizer.point_in_any_polygon(wp.lat, wp.lon, border_polygons)
                        for wp in nav_wps
                    ]
                    for k in range(len(nav_wps) - 1):
                        if in_occupied[k] != in_occupied[k + 1]:
                            exclude_leg_indices.add(k)
            except Exception:
                pass  # відсутність кордону НЕ має зривати саму координатну оптимізацію

            result = route_optimizer.optimize_route(
                self.analyzer.nav_wps, fetcher, route_type.radius_for_population,
                exclude_last_n_legs=exclude_legs, exclude_leg_indices=exclude_leg_indices,
                fuel_budget=fuel_budget,
                roll_limit_deg=roll_limit,
                min_radius_km=min(r for _, r in route_type.population_radii_km),
                progress_callback=on_progress,
            )
            self.after(0, lambda: self._on_route_optimization_ready(result, None))
        except populated_areas.OverpassError as e:
            self.after(0, lambda: self._on_route_optimization_ready(None, str(e)))
        except Exception as e:
            self.after(0, lambda: self._on_route_optimization_ready(None, str(e)))


    def _on_route_optimization_ready(self, result, error):
        if error:
            # помилка -- РОЗБЛОКОВУЄМО "Оптимізувати" (нічого не вдалось,
            # користувач має змогу спробувати ще раз, напр. після
            # тимчасового мережевого збою). НЕ розблоковуємо при УСПІХУ
            # (нижче) -- оптимізовану місію нема сенсу оптимізувати ще
            # раз, доти, доки користувач сам не змінить якийсь параметр.
            self.optimize_route_btn.configure(state="normal")
            self.optimize_status_var.set(i18n.t("status_settlements_error_fmt", error=error))
            # статус-рядок (ttk.Label) НЕ підтримує виділення мишею взагалі
            # -- дублюємо повний текст помилки в optimize_report_text, де
            # вже є копіювання правою кнопкою (theme.make_text_readonly),
            # щоб користувач реально міг скопіювати й переслати текст
            self.optimize_report_text.configure(state="normal")
            self.optimize_report_text.delete("1.0", "end")
            self.optimize_report_text.insert("end", i18n.t("status_settlements_error_fmt", error=error))
            theme.make_text_readonly(self.optimize_report_text)
            return

        self._route_optimization_result = result
        self.save_optimized_btn.configure(state="normal")
        self.optimize_status_var.set(i18n.t(
            "status_optimization_done_fmt",
            added=result.added_distance_km, legs=len(result.legs),
        ))

        lines = [i18n.t("box_optimization_report"), "-" * 44, ""]
        lines.append(i18n.t("opt_distance_before_fmt", km=result.total_original_distance_km))
        lines.append(i18n.t("opt_distance_after_fmt", km=result.total_new_distance_km))
        lines.append(i18n.t("opt_distance_added_fmt", km=result.added_distance_km))
        lines.append("")
        lines.append(i18n.t("opt_waypoints_fmt", n=result.total_waypoints, limit=route_optimizer.MAX_MISSION_WAYPOINTS))
        if result.waypoint_limit_exceeded:
            lines.append("  " + i18n.t("opt_waypoint_limit_exceeded"))
        lines.append("")

        if result.fuel_check is not None:
            fc = result.fuel_check
            lines.append(i18n.t("opt_fuel_trip_fmt", l=fc.trip_fuel_l))
            lines.append(i18n.t("opt_fuel_reserve_fmt", l=fc.reserve_l))
            lines.append(i18n.t("opt_fuel_required_fmt", l=fc.required_total_l))
            lines.append(i18n.t("opt_fuel_tank_fmt", l=fc.tank_capacity_l))
            status_key = "opt_fuel_ok" if fc.feasible else "opt_fuel_deficit"
            lines.append(i18n.t(status_key, margin=abs(fc.margin_l)))
            lines.append("")

        if result.turn_check is not None:
            tc = result.turn_check
            lines.append(i18n.t("opt_turn_radius_fmt", r=tc.min_turn_radius_m, threshold=tc.threshold_m))
            turn_status_key = "opt_turn_ok" if tc.feasible else "opt_turn_deficit"
            lines.append(i18n.t(turn_status_key, margin=abs(tc.margin_m)))
            lines.append("")

        failed_legs = [lr for lr in result.legs if lr.failed]
        if failed_legs:
            lines.append(i18n.t("opt_failed_legs_header_fmt", n=len(failed_legs)))
            for lr in failed_legs:
                lines.append(f"  {i18n.t('settlement_col_leg')} {lr.leg_index}: {lr.failure_reason}")
            lines.append("")

        merged_legs = [lr for lr in result.legs if lr.merged_with_next]
        if merged_legs:
            lines.append(i18n.t("opt_merged_legs_header_fmt", n=len(merged_legs)))
            for lr in merged_legs:
                lines.append(i18n.t(
                    "opt_merged_leg_line_fmt",
                    leg1=lr.leg_index, leg2=lr.leg_index + 1, removed=lr.removed_waypoint_index,
                ))
            lines.append("")

        # --- таблиця "Було/Стало" по кожному НП -- пряме числове
        # підтвердження, що обхід реально спрацював: "було" (відстань
        # від ПРЯМОЇ лінії ребра) мала бути < порогу, "стало" (відстань
        # від НОВОГО шляху з обходом) -- >= порогу. Той самий формат
        # колонок, що й таблиця "Обліт НП" -- для узгодженості. Поріг
        # тепер ІНДИВІДУАЛЬНИЙ для кожного НП (obs.radius_km, залежить
        # від його населення) -- НЕ єдине число для всіх одразу.
        any_obstacles = any(
            populated_areas._point_to_segment_m(
                obs.lat, obs.lon,
                self.analyzer.nav_wps[lr.leg_index].lat, self.analyzer.nav_wps[lr.leg_index].lon,
                self.analyzer.nav_wps[
                    lr.removed_waypoint_index + 1 if lr.merged_with_next else lr.leg_index + 1
                ].lat,
                self.analyzer.nav_wps[
                    lr.removed_waypoint_index + 1 if lr.merged_with_next else lr.leg_index + 1
                ].lon,
            ) < obs.radius_km * 1000.0
            for lr in result.legs for obs in lr.obstacles_considered
        )
        lines.append(i18n.t("opt_before_after_header"))
        lines.append("-" * 90)
        if not any_obstacles:
            # ЯВНЕ повідомлення, а не тиха відсутність розділу -- інакше
            # неможливо відрізнити "жодного НП поруч, усе безпечно" від
            # "щось не спрацювало" (виявлено на практиці: користувач не
            # міг зрозуміти, чому таблиця відсутня)
            lines.append(i18n.t("opt_no_obstacles_considered"))
        else:
            col_num = i18n.t("settlement_col_num")
            col_leg = i18n.t("settlement_col_leg")
            col_name = i18n.t("settlement_col_name")
            col_before = i18n.t("opt_col_before")
            col_after = i18n.t("opt_col_after")
            lines.append(f"{col_num:<5}{col_leg:<7}{col_name:<28}{col_before:<15}{col_after}")

            row_num = 0
            for lr in result.legs:
                if not lr.obstacles_considered:
                    continue
                wp1 = self.analyzer.nav_wps[lr.leg_index]
                end_index = lr.removed_waypoint_index + 1 if lr.merged_with_next else lr.leg_index + 1
                wp2 = self.analyzer.nav_wps[end_index]
                new_path = [(wp1.lat, wp1.lon)] + lr.inserted_waypoints + [(wp2.lat, wp2.lon)]
                for obs in lr.obstacles_considered:
                    before_m = populated_areas._point_to_segment_m(
                        obs.lat, obs.lon, wp1.lat, wp1.lon, wp2.lat, wp2.lon,
                    )
                    # ПОКАЗУЄМО ТІЛЬКИ СПРАВЖНІ ПОРУШЕННЯ (Було < поріг) --
                    # obstacles_considered містить УСІ НП у радіусі 3×
                    # поріг (потрібно для побудови графа обходу -- щоб
                    # враховувати потенційно релевантні перешкоди, не
                    # тільки вже порушені), але показ ЦЬОГО ширшого
                    # контексту в таблиці лише спантеличує: "Було: 2843м"
                    # -- це взагалі НЕ порушення, і користувач не може
                    # відрізнити реальну проблему від шуму (звідси плутанина
                    # "Обліт НП знайшов 54, а тут 189" -- 189 містило й
                    # усе безпечне з 3х радіуса, не тільки порушення).
                    if before_m >= obs.radius_km * 1000.0:
                        continue
                    row_num += 1
                    after_m = min(
                        populated_areas._point_to_segment_m(
                            obs.lat, obs.lon,
                            new_path[i][0], new_path[i][1], new_path[i + 1][0], new_path[i + 1][1],
                        )
                        for i in range(len(new_path) - 1)
                    )
                    before_str = f"{before_m:.0f}м"
                    # для FAILED ребра "Стало" завжди == "Було" (жодних
                    # точок обходу не вставлено -- шлях лишився прямою
                    # лінією), АЛЕ це виглядало б ідентично випадку "тут
                    # і так було безпечно" -- явно позначаємо різницю,
                    # інакше користувач не зрозуміє, ЧОМУ немає покращення
                    if lr.failed:
                        after_str = i18n.t("opt_leg_failed_marker")
                    else:
                        after_str = f"{after_m:.0f}м"
                    lines.append(f"{row_num:<5}{lr.leg_index:<7}{obs.name:<28}{before_str:<15}{after_str}")

        self.optimize_report_text.configure(state="normal")
        self.optimize_report_text.delete("1.0", "end")
        self.optimize_report_text.insert("end", "\n".join(lines))
        theme.make_text_readonly(self.optimize_report_text)

        self._load_optimization_map()


    def _load_optimization_map(self):
        """Малює карту з ОБОМА маршрутами одна поверх одної: "було"
        (оригінальний, тонший/тьмяніший) і "стало" (оптимізований,
        яскравіший, поверх). Використовує ГОТОВИЙ знімок тайлів з
        "Місія" (_initial_map_render) -- та сама карта, що й "Обліт НП"
        і "Маршрут"."""
        if self.analyzer is None or not hasattr(self, "optimize_map_canvas"):
            return

        snapshot = getattr(self, "_initial_map_render", None)
        if snapshot is None:
            return

        zoom = snapshot["zoom"]
        tiles = snapshot["tiles"]
        tx_min, tx_max = snapshot["tx_min"], snapshot["tx_max"]
        ty_min, ty_max = snapshot["ty_min"], snapshot["ty_max"]
        center_lat, center_lon = snapshot["center_lat"], snapshot["center_lon"]

        result = render_viewport(
            self.optimize_map_canvas, self.analyzer, zoom, center_lat, center_lon,
            tx_min, tx_max, ty_min, ty_max, tiles, self._optimize_map_images,
        )
        screen_origin_gx, screen_origin_gy = result[4], result[5]

        opt_result = self._route_optimization_result

        def draw_polyline(points, color, width):
            coords = []
            for lat, lon in points:
                gx, gy = lonlat_to_pixel(lat, lon, zoom)
                coords.append(gx - screen_origin_gx)
                coords.append(gy - screen_origin_gy)
            if len(coords) >= 4:
                self.optimize_map_canvas.create_line(
                    *coords, fill=color, width=width, tags=("opt_route",),
                )

        if opt_result is not None:
            # фінальний результат готовий -- малюємо ПОВНІ обидва маршрути
            original_route = opt_result.original_route
            new_route = opt_result.new_route
            legs_for_markers = opt_result.legs
        else:
            # розрахунок ще триває -- оригінальний маршрут відомий ОДРАЗУ
            # (без мережі, прямо з nav_wps), новий -- лише те, що вже
            # встигли порахувати (self._optimize_partial_new_route,
            # наповнюється в on_progress по мірі готовності кожного ребра)
            partial = getattr(self, "_optimize_partial_new_route", None)
            if partial is None:
                return
            original_route = [(wp.lat, wp.lon) for wp in self.analyzer.nav_wps]
            new_route = partial
            legs_for_markers = []

        # "було" -- тьмяний, тонший, під "стало"
        draw_polyline(original_route, "#888888", 2)
        # "стало" -- яскравий, товщий, поверх (росте по мірі готовності)
        draw_polyline(new_route, "#00c853", 3)

        # позначки вставлених точок обходу (маленькі жовті кружечки)
        for leg in legs_for_markers:
            for lat, lon in leg.inserted_waypoints:
                gx, gy = lonlat_to_pixel(lat, lon, zoom)
                x, y = gx - screen_origin_gx, gy - screen_origin_gy
                self.optimize_map_canvas.create_oval(
                    x - 3, y - 3, x + 3, y + 3,
                    fill="#ffd600", outline="black", width=1, tags=("opt_route",),
                )

    def _compute_detour_altitude(self, wp1, wp2, inserted_points: list[tuple[float, float]]):
        """Висота вставлених точок обходу.

        Якщо обрано тип маршруту (Конфігурація) І вдалось отримати
        державний кордон -- НОВА, проста логіка: для кожної точки
        (рельєф_у_ЦІЙ_точці + цільова відносна висота ТІЄЇ зони, де
        вона фактично лежить), а для пари точок на самому ребрі, де
        відбувається перетин кордону -- висота переходу з типу
        маршруту. Жодного розрахунку маневру чи плавного переходу --
        свідомо просто, оцінка результату на графіку "Було/Стало"
        станеться ОКРЕМИМ кроком, після цього.

        Якщо тип маршруту НЕ обрано (чи кордон не вдалось отримати) --
        ЗАПАСНИЙ варіант, як і раніше: висота_рельєфу(нова_точка) +
        середнє_відносних_висот(wp1, wp2) -- НЕ лінійна інтерполяція
        абсолютної висоти (рельєф під дугою обходу може суттєво
        відрізнятись від прямої лінії між висотами кінців)."""
        analyzer = self.analyzer
        terrain = analyzer.terrain
        target_frame = wp1.frame
        home_amsl = analyzer.home_amsl or 0.0

        def _alt_for_frame(abs_alt, terrain_h, target_agl):
            if target_frame in (0, 2):
                return abs_alt
            if target_frame == 10:
                return target_agl if target_agl is not None else abs_alt - terrain_h
            return abs_alt - home_amsl

        # --- Спроба НОВОЇ, простої логіки (тип маршруту + кордон) ---
        try:
            route_type_store = route_types.load_route_types(route_types.default_route_types_path())
            route_type = route_type_store.get_current()
        except Exception:
            route_type = None

        polygons = self._get_country_border_polygon("RUS") if route_type is not None else None

        if route_type is not None and polygons:
            # РОЗДІЛЕНО (за прямою вказівкою користувача): обліт НП і
            # набір/зниження висоти на переході кордону -- ДВІ РІЗНІ
            # задачі, які НЕ МОЖНА змішувати. Точки обходу тут
            # розставлені щільно (десятки-сотні метрів між ними,
            # дискретизація дуги навколо перешкоди) -- вони НЕ мають
            # жодного стосунку до того, скільки відстані фізично
            # потрібно для плавної зміни висоти. РАНІШЕ тут ПОМИЛКОВО
            # шукався перехід кордону САМЕ всередині цих щільних точок
            # і туди ж напряму втискалась ціль border_crossing_m --
            # ВИЯВЛЕНО НА ПРАКТИЦІ: коли border_crossing_m суттєво
            # відрізняється від сусідньої зони, це давало кут нахилу
            # ~57° на відрізку в 129м -- фізично неможливий маневр.
            # Тепер КОЖНА точка обходу отримує ПРОСТО ціль своєї зони
            # (контрольована/окупована, БЕЗ спеціального "перехідного"
            # значення) -- плавний, з урахуванням дистанції, перехід
            # через сам кордон -- виключна відповідальність окремого,
            # правильного кроку "Висота" (Фаза 1: контроль кута).
            all_pts = [(wp1.lat, wp1.lon)] + list(inserted_points) + [(wp2.lat, wp2.lon)]
            in_occupied = [
                altitude_optimizer.point_in_any_polygon(lat, lon, polygons) for lat, lon in all_pts
            ]

            result = []
            for k, (lat, lon) in enumerate(all_pts[1:-1], start=1):  # тільки ВСТАВЛЕНІ (без wp1/wp2)
                try:
                    terrain_h = terrain.get_elevation(lat, lon)
                except SRTMError:
                    terrain_h = None
                if terrain_h is None:
                    continue  # немає рельєфу саме тут -- пропускаємо, не вигадуємо число
                target_agl = route_type.altitude_occupied_m if in_occupied[k] else route_type.altitude_controlled_m
                abs_alt = terrain_h + target_agl
                result.append(Waypoint(
                    index=0, current=0, frame=target_frame, command=16,
                    param1=0.0, param2=0.0, param3=0.0, param4=0.0,
                    lat=lat, lon=lon, alt=_alt_for_frame(abs_alt, terrain_h, target_agl), autocontinue=1,
                ))
            if len(result) == len(inserted_points):
                return result
            # якщо десь рельєфу не вистачило (пропущені точки вище) --
            # НЕ повертаємо частковий/неповний список, падаємо назад
            # на запасний варіант нижче для ВСІХ точок разом, чесніше,
            # ніж мовчки короткий список

        # --- ЗАПАСНИЙ варіант (тип маршруту не обрано чи немає кордону) ---
        abs1 = analyzer._absolute_alt(wp1)
        abs2 = analyzer._absolute_alt(wp2)
        try:
            terrain1 = terrain.get_elevation(wp1.lat, wp1.lon) if wp1.has_position else None
        except SRTMError:
            terrain1 = None
        try:
            terrain2 = terrain.get_elevation(wp2.lat, wp2.lon) if wp2.has_position else None
        except SRTMError:
            terrain2 = None

        rel1 = (abs1 - terrain1) if (abs1 is not None and terrain1 is not None) else None
        rel2 = (abs2 - terrain2) if (abs2 is not None and terrain2 is not None) else None

        if rel1 is None and rel2 is None:
            raise ValueError(i18n.t("msg_no_terrain_for_leg_fmt", leg=""))
        avg_rel = rel1 if rel2 is None else (rel2 if rel1 is None else (rel1 + rel2) / 2.0)

        fallback_terrain = None
        if terrain1 is not None and terrain2 is not None:
            fallback_terrain = (terrain1 + terrain2) / 2.0
        elif terrain1 is not None:
            fallback_terrain = terrain1
        elif terrain2 is not None:
            fallback_terrain = terrain2

        result = []
        for lat, lon in inserted_points:
            try:
                terrain_h = terrain.get_elevation(lat, lon)
            except SRTMError:
                if fallback_terrain is None:
                    raise ValueError(i18n.t("msg_no_terrain_for_leg_fmt", leg=""))
                terrain_h = fallback_terrain
            abs_alt = terrain_h + avg_rel
            result.append(Waypoint(
                index=0, current=0, frame=target_frame, command=16,
                param1=0.0, param2=0.0, param3=0.0, param4=0.0,
                lat=lat, lon=lon, alt=_alt_for_frame(abs_alt, terrain_h, None), autocontinue=1,
            ))
        return result


    def _build_optimized_waypoints(self) -> tuple[list, dict]:
        """Реконструює ПОВНИЙ список Waypoint (не тільки nav_wps) з
        вставленими точками обходу на правильних місцях -- зберігає всі
        інші елементи місії (home, зліт, DO_-команди тощо) на своїх
        позиціях відносно навігаційних точок, як у оригінальному файлі.

        Для ОБ'ЄДНАНИХ ребер (merged_with_next): вейпоінт МІЖ двома
        об'єднаними ребрами дійсно ВИДАЛЯЄТЬСЯ з фінального маршруту (не
        просто отримує вставлені точки ПЕРЕД собою, як було в
        попередній версії -- реальний баг, видалена точка лишалась у
        виводі). Будь-які НЕ-навігаційні команди, що стояли в оригіналі
        БЕЗПОСЕРЕДНЬО біля цієї точки (до чи після), переносяться на
        НАЙБЛИЖЧУ (за реальною географічною відстанню, не за порядком у
        списку) нову точку обходу -- а не мовчки губляться.

        Повертає (new_wps, summary) -- summary збирає ЛЮДСЬКИ читабельний
        підсумок усіх таких "непомітних" змін (перенесені команди,
        скориговані/перенаправлені DO_JUMP), щоб оператор бачив це явно
        в звіті після збереження, а не дізнавався постфактум із
        поведінки місії в польоті."""
        result_obj = self._route_optimization_result
        if result_obj is None:
            raise ValueError(i18n.t("msg_no_optimization_result"))
        analyzer = self.analyzer
        if analyzer.terrain is None:
            raise ValueError(i18n.t("msg_no_terrain_for_save"))

        summary = {"relocated_commands": [], "jump_redirected": [], "jump_renumbered": 0}

        nav_wps = analyzer.nav_wps
        legs_by_index = {lr.leg_index: lr for lr in result_obj.legs}
        all_wps = analyzer.all_wps

        # nav_wps-індекс -> Waypoint-об'єкт, що ВИДАЛЯЄТЬСЯ через злиття
        removed_wp_objects = {
            lr.removed_waypoint_index: nav_wps[lr.removed_waypoint_index]
            for lr in result_obj.legs if lr.merged_with_next
        }
        removed_wp_ids = {id(obj) for obj in removed_wp_objects.values()}

        def _next_nav_point(from_idx: int):
            """Найближча НАВІГАЦІЙНА точка (Waypoint), що йде ПІСЛЯ
            all_wps[from_idx] у послідовності -- для перевірки, чи
            команда стоїть БЕЗПОСЕРЕДНЬО перед видаленою точкою."""
            for j in range(from_idx, len(all_wps)):
                if all_wps[j].is_nav_point:
                    return all_wps[j]
            return None

        new_wps: list[Waypoint] = []
        nav_wps_position = 0  # позиція В nav_wps ДЛЯ ЦІЄЇ точки (рахує ВСІ, включно з видаленими)
        last_real_nav_index = -1  # nav_wps-індекс ОСТАННЬОЇ РЕАЛЬНО збереженої точки (не рахує видалені)
        pending_relocatable: list = []  # НЕ-нав команди, що стояли ПОРУЧ із видаленою точкою (до чи після)
        just_passed_removed = False  # True одразу після пропущеної видаленої точки -- команди
        # звідси й до НАСТУПНОЇ реальної нав-точки теж вважаються "поруч" і йдуть у relocatable

        for idx, wp in enumerate(all_wps):
            if wp.is_nav_point:
                if id(wp) in removed_wp_ids:
                    # ЦЯ точка зникає з маршруту -- НЕ додаємо в new_wps.
                    # pending_relocatable (команди, що йшли перед нею)
                    # розберемо нижче, коли дійдемо до точок об'єднаного
                    # обходу для цього злиття. Команди, що йдуть ПІСЛЯ
                    # (до наступної реальної нав-точки) -- теж relocatable,
                    # позначаємо прапорцем.
                    just_passed_removed = True
                    nav_wps_position += 1
                    continue

                just_passed_removed = False
                if last_real_nav_index >= 0:
                    # КЛЮЧОВЕ виправлення: leg_idx -- це індекс ОСТАННЬОЇ
                    # реально збереженої точки (last_real_nav_index), НЕ
                    # простий лічильник пройдених нав-точок. Якщо між
                    # нею й ЦІЄЮ точкою була видалена (злиття) -- це
                    # ОБ'ЄДНАНЕ ребро, а не звичайне "наступне".
                    leg_idx = last_real_nav_index
                    leg_result = legs_by_index.get(leg_idx)
                    if leg_result and leg_result.inserted_waypoints:
                        wp1 = nav_wps[leg_idx]
                        end_idx = (
                            leg_result.removed_waypoint_index + 1
                            if leg_result.merged_with_next else leg_idx + 1
                        )
                        wp2 = nav_wps[end_idx]
                        detour_wps = self._compute_detour_altitude(
                            wp1, wp2, leg_result.inserted_waypoints,
                        )

                        if leg_result.merged_with_next and pending_relocatable:
                            old_removed = removed_wp_objects[leg_result.removed_waypoint_index]
                            nearest_i = min(
                                range(len(detour_wps)),
                                key=lambda i: haversine_m(
                                    old_removed.lat, old_removed.lon,
                                    detour_wps[i].lat, detour_wps[i].lon,
                                ),
                            )
                            new_wps.extend(detour_wps[:nearest_i + 1])
                            new_wps.extend(pending_relocatable)
                            new_wps.extend(detour_wps[nearest_i + 1:])
                            summary["relocated_commands"].append({
                                "commands": [w.command for w in pending_relocatable],
                                "old_waypoint_index": leg_result.removed_waypoint_index,
                            })
                            pending_relocatable = []
                        else:
                            new_wps.extend(detour_wps)
                last_real_nav_index = nav_wps_position
                nav_wps_position += 1
                new_wps.append(wp)
            else:
                # НЕ-навігаційна команда. Relocatable, якщо стоїть
                # БЕЗПОСЕРЕДНЬО ПЕРЕД видаленою точкою (наступна
                # нав-точка попереду -- видалена) АБО ОДРАЗУ ПІСЛЯ неї
                # (just_passed_removed) -- в обох випадках вона
                # "прив'язана" саме до видаленої точки, не до жодної
                # реальної, що лишається в маршруті.
                next_nav = _next_nav_point(idx + 1)
                is_before_removed = next_nav is not None and id(next_nav) in removed_wp_ids
                if is_before_removed or just_passed_removed:
                    pending_relocatable.append(wp)
                else:
                    new_wps.append(wp)

        # --- Коригування DO_JUMP (177) ---
        # param1 у DO_JUMP -- це ЦІЛЬОВИЙ НОМЕР (послідовний файловий
        # індекс) вейпоінта для переходу. write_waypoints() ЗАВЖДИ
        # перенумеровує послідовно від 0 при збереженні -- якщо десь
        # раніше вставлені точки обходу (навіть без жодного злиття),
        # усе, що йде ПІСЛЯ місця вставки, зсувається в номерах. БЕЗ
        # цього коригування DO_JUMP мовчки почав би вказувати на
        # ІНШИЙ, неправильний вейпоінт після збереження -- реальна
        # навігаційна помилка, не просто косметика нумерації.
        old_index_to_wp = {i: w for i, w in enumerate(all_wps)}
        wp_id_to_new_index = {id(w): i for i, w in enumerate(new_wps)}
        for i, wp in enumerate(new_wps):
            if wp.command != 177:  # DO_JUMP
                continue
            old_target_index = int(wp.param1)
            target_wp = old_index_to_wp.get(old_target_index)
            if target_wp is None:
                continue  # некоректний індекс уже в оригіналі -- не наша справа це виправляти
            new_target_index = wp_id_to_new_index.get(id(target_wp))
            if new_target_index is None:
                # ціль сама зникла (видалена через злиття) -- перенаправляємо
                # на геометрично НАЙБЛИЖЧУ точку серед тих, що лишились
                if target_wp.has_position:
                    candidates = [j for j, w in enumerate(new_wps) if w.has_position]
                    if candidates:
                        new_target_index = min(
                            candidates,
                            key=lambda j: haversine_m(
                                target_wp.lat, target_wp.lon, new_wps[j].lat, new_wps[j].lon,
                            ),
                        )
                if new_target_index is None:
                    continue  # немає на що коректно перенаправити -- лишаємо як було, не гірше
                summary["jump_redirected"].append({
                    "old_target_index": old_target_index, "new_target_index": new_target_index,
                })
            else:
                summary["jump_renumbered"] += 1
            new_wps[i] = dataclasses.replace(wp, param1=float(new_target_index))

        return new_wps, summary



    def _save_optimized_mission(self):
        """Кнопка "Зберегти оптимізовану місію" -- пише ОКРЕМИЙ новий
        файл .waypoints, оригінальна завантажена місія лишається
        недоторканою (self.analyzer НЕ змінюється)."""
        if self._route_optimization_result is None:
            return

        try:
            new_wps, summary = self._build_optimized_waypoints()
        except ValueError as e:
            messagebox.showerror(i18n.t("msg_weather_title"), str(e))
            return
        except Exception as e:
            messagebox.showerror(i18n.t("msg_weather_title"), str(e))
            return

        path = filedialog.asksaveasfilename(
            title=i18n.t("dlg_save_optimized_mission_title"),
            defaultextension=".waypoints",
            filetypes=[("Waypoints", "*.waypoints")],
        )
        if not path:
            return

        try:
            write_waypoints(path, new_wps)
        except Exception as e:
            messagebox.showerror(i18n.t("msg_weather_title"), i18n.t("msg_save_failed_fmt", error=e))
            return

        messagebox.showinfo(
            i18n.t("msg_weather_title"),
            i18n.t("msg_save_success_fmt", n=len(new_wps), path=path),
        )

        # ЯВНИЙ підсумок "непомітних" змін -- перенесені команди й
        # скориговані DO_JUMP -- дописується в ТОЙ САМИЙ звіт, де вже є
        # таблиця "Було/Стало", щоб оператор бачив це одразу, а не
        # дізнавався постфактум із поведінки місії в польоті.
        if summary["relocated_commands"] or summary["jump_redirected"] or summary["jump_renumbered"]:
            lines = ["", i18n.t("opt_save_changes_header")]
            for item in summary["relocated_commands"]:
                cmds = ", ".join(str(c) for c in item["commands"])
                lines.append(i18n.t(
                    "opt_relocated_commands_line_fmt",
                    commands=cmds, waypoint=item["old_waypoint_index"],
                ))
            for item in summary["jump_redirected"]:
                lines.append(i18n.t(
                    "opt_jump_redirected_line_fmt",
                    old=item["old_target_index"], new=item["new_target_index"],
                ))
            if summary["jump_renumbered"]:
                lines.append(i18n.t("opt_jump_renumbered_line_fmt", n=summary["jump_renumbered"]))

            self.optimize_report_text.configure(state="normal")
            self.optimize_report_text.insert("end", "\n" + "\n".join(lines))
            theme.make_text_readonly(self.optimize_report_text)

    def _apply_profile_to_optimization_fields(self, profile):
        """Заповнює крейсерську швидкість/витрату/крен зі СПЕЦИФІЧНОГО
        профілю (не обов'язково поточного) -- спільна логіка для
        первинного заповнення при відкритті сторінки й для ручного
        вибору з випадаючого списку. Якщо профіль не "plane" (поки що
        єдиний тип із заповненими польотними полями) -- нічого не
        робить, поля лишаються як були."""
        if profile is None or profile.drone_type != "plane":
            return
        if profile.airspeed_cruise_ms:
            self.opt_cruise_speed_var.set(f"{profile.airspeed_cruise_ms * 3.6:.0f}")
        if profile.cruise_consumption_lph:
            self.opt_cruise_consumption_var.set(f"{profile.cruise_consumption_lph:.1f}")
        if profile.roll_limit_deg:
            self.opt_roll_limit_var.set(f"{profile.roll_limit_deg:.0f}")


    def _prefill_optimization_from_profile(self):
        """Підтягує крейсерську швидкість/витрату/крен із ПОТОЧНОГО
        профілю літака (Конфігурація), якщо такий існує -- лише при
        першому відкритті сторінки. Ємність бака СВІДОМО не чіпається
        -- вона поза профілем (може відрізнятись від вильоту до вильоту
        навіть для одного борту, задається окремо щоразу)."""
        try:
            store = aircraft_profiles.load_profiles(aircraft_profiles.default_profiles_path())
        except Exception:
            return
        self._apply_profile_to_optimization_fields(store.get_current())


    def _refresh_optimize_profile_list(self, event=None):
        """Перечитує список профілів з диску -- ПЕРЕД кожним відкриттям
        випадаючого списку (не тільки один раз при побудові сторінки),
        бо профіль міг з'явитись у "Конфігурації" вже після того, як
        "Аналіз" збудувався в цьому сеансі роботи програми."""
        try:
            store = aircraft_profiles.load_profiles(aircraft_profiles.default_profiles_path())
        except Exception:
            store = aircraft_profiles.AircraftProfileStore()
        names = [p.name for p in store.profiles]
        self.opt_profile_box.configure(values=names)
        self._opt_profile_store = store
        current = store.get_current()
        if current and not self.opt_profile_var.get():
            self.opt_profile_var.set(current.name)


    def _on_optimize_profile_selected(self, event=None):
        """Обрано профіль у випадаючому списку "Оптимізації" -- заповнює
        поля НИЖЧЕ значеннями з НЬОГО (не обов'язково поточного профілю
        з "Конфігурації"). НЕ змінює сам "поточний" профіль -- вибір тут
        суто локальна зручність для цього розрахунку, без побічного
        ефекту на решту програми."""
        store = getattr(self, "_opt_profile_store", None)
        if store is None:
            return
        name = self.opt_profile_var.get()
        profile = store.get_by_name(name)
        self._apply_profile_to_optimization_fields(profile)


    def _reset_optimization_result_cache(self):
        """Скидає кешований результат ОБОХ оптимізацій (координати +
        висота) -- та сама причина, що й _reset_analysis_result_caches
        в analysis_page.py (нова місія не має показувати результат
        ПОПЕРЕДНЬОЇ). Скидання висоти теж тут -- вона залежить від
        результату координатної оптимізації, тому нова місія завжди
        обнуляє обидва разом."""
        self._route_optimization_result = None
        self._altitude_points = None
        self._altitude_nav_wps = None
        self._altitude_dist_before_km = self._altitude_alt_before = []
        self._altitude_dist_after_km = self._altitude_alt_after = []
        self._altitude_terrain_dist_km = self._altitude_terrain_alt = []
        self._altitude_crossing_km = None
        self._altitude_angle_adapter = None
        if hasattr(self, "altitude_chart_canvas"):
            self._redraw_altitude_chart()
        if hasattr(self, "altitude_angle_chart_canvas"):
            self._redraw_altitude_angle_chart()
        if hasattr(self, "optimize_report_text"):
            self.optimize_report_text.configure(state="normal")
            self.optimize_report_text.delete("1.0", "end")
            theme.make_text_readonly(self.optimize_report_text)
        if hasattr(self, "optimize_status_var"):
            self.optimize_status_var.set("")
        if hasattr(self, "save_optimized_btn"):
            self.save_optimized_btn.configure(state="disabled")
        if hasattr(self, "altitude_report_text"):
            self.altitude_report_text.configure(state="normal")
            self.altitude_report_text.delete("1.0", "end")
            theme.make_text_readonly(self.altitude_report_text)
        if hasattr(self, "altitude_status_var"):
            self.altitude_status_var.set("")
        if hasattr(self, "save_altitude_btn"):
            self.save_altitude_btn.configure(state="disabled")

    def _get_climb_sink_rates(self):
        """(climb_rate_max_ms, sink_rate_min_ms) з ПОТОЧНОГО профілю
        літака -- None, якщо профілю немає, він не типу "Літак", чи
        значення не задані (не вигадуємо запасні числа мовчки)."""
        try:
            store = aircraft_profiles.load_profiles(aircraft_profiles.default_profiles_path())
        except Exception:
            return None
        profile = store.get_current()
        if profile is None or profile.drone_type != "plane":
            return None
        if not profile.climb_rate_max_ms or not profile.sink_rate_min_ms:
            return None
        return profile.climb_rate_max_ms, profile.sink_rate_min_ms


    def _get_country_border_polygon(self, iso_a3: str = "RUS"):
        """Полігон ДЕРЖАВНОГО кордону (Natural Earth, статичні дані,
        не змінюються щодня як лінія фронту) -- джерело "чиясь
        територія" для оптимізації висоти. Кешується ОДИН РАЗ (не
        щодня) у тій самій теці кешу тайлів."""
        try:
            cache_dir = self.tilecache_var.get().strip() or "map_cache"
        except Exception:
            cache_dir = "."
        return country_borders.fetch_country_polygon(cache_dir, iso_a3)


    def _run_altitude_optimization(self):
        """Кнопка "Оптимізувати висоту" -- ПРОСТА логіка (узгоджена з
        тим, що вже робить _compute_detour_altitude для нових точок
        обходу під час координатної оптимізації): для КОЖНОЇ точки
        маршруту -- рельєф у ній + цільова відносна висота тієї зони,
        де вона лежить; для ребра, де відбувається перетин кордону --
        висота переходу на обидві його кінцеві точки. Жодного
        розрахунку маневру чи темпу набору/зниження -- свідомо просто.

        ПІСЛЯ цього -- перевірка кліренсу рельєфу УЗДОВЖ кожного
        відрізка (крок 50м), у КІЛЬКА циклів (виправлення одного
        відрізка -- підняття спільної кінцевої точки -- може вплинути
        на СУСІДНІЙ відрізок, що використовує ту саму точку, тому один
        прохід не гарантує стабільності). Для кожного порушення:
        якщо найгірший кліренс ближче до ОДНОГО з кінців ребра --
        піднімаємо ЙОГО ціль, без нової точки; якщо ближче до
        середини -- вставляємо нову точку саме там."""
        for attr in ("_altitude_step_before_cache", "_altitude_step_terrain_cache"):
            if hasattr(self, attr):
                delattr(self, attr)  # скидаємо кеш "Було"/"Рельєф" покрокового режиму -- це НОВИЙ запуск

        if self._route_optimization_result is None:
            messagebox.showwarning(i18n.t("msg_weather_title"), i18n.t("msg_run_coordinate_optimization_first"))
            return

        route_type_store = route_types.load_route_types(route_types.default_route_types_path())
        selected_type_name = self.alt_route_type_var.get()
        route_type = (
            route_type_store.get_by_name(selected_type_name) if selected_type_name
            else route_type_store.get_current()
        )
        if route_type is None:
            messagebox.showwarning(i18n.t("msg_weather_title"), i18n.t("msg_no_route_type"))
            return

        if self.analyzer is None or self.analyzer.terrain is None:
            messagebox.showwarning(i18n.t("msg_weather_title"), i18n.t("msg_no_terrain_for_save"))
            return

        # ЗАЗДАЛЕГІДЬ, на ГОЛОВНОМУ потоці -- УСЕ, що читає Tkinter-
        # змінні (route_type ВЖЕ прочитано вище). Решта -- мережевий
        # запит кордону, побудова waypoints, увесь розрахунок списку
        # точок -- переноситься у ФОНОВИЙ потік. ВИЯВЛЕНО НА ПРАКТИЦІ,
        # ЙМОВІРНО НАЙСЕРЙОЗНІША причина повної відмови інтерфейсу:
        # мережевий запит _get_country_border_polygon РАНІШЕ виконувався
        # СИНХРОННО тут, ще ДО старту фонового потоку -- якщо мережа
        # повільна чи недоступна, ВЕСЬ інтерфейс завмирав ще до того,
        # як хоч ОДНЕ з попередніх виправлень потоків встигало
        # спрацювати (кешування, кнопка "Скасувати" -- усе це стосується
        # лише ПІСЛЯ старту потоку, а тут блокування було РАНІШЕ).
        try:
            angle_limit = float(self.angle_max_var.get())
        except (ValueError, AttributeError):
            angle_limit = 2.0
        try:
            min_clearance_m = float(self.alt_min_var.get().replace(",", "."))
        except (ValueError, AttributeError):
            min_clearance_m = 50.0

        self.optimize_altitude_btn.configure(state="disabled")
        self.altitude_status_var.set(i18n.t("status_altitude_optimization_running"))

        step_mode = self.alt_step_mode_var.get()
        if step_mode:
            self._altitude_step_event = threading.Event()
            self.alt_step_next_btn.configure(state="normal")
        self._altitude_cancel_event = threading.Event()
        self.alt_cancel_btn.configure(state="normal")

        last_ui_update = [0.0]

        def on_iteration(snapshot, iter_num):
            now = time.time()
            if step_mode or now - last_ui_update[0] > 0.15:
                last_ui_update[0] = now
                self.after(0, lambda: self._update_altitude_step_display(snapshot, iter_num))
            if step_mode:
                self._altitude_step_event.wait()
                self._altitude_step_event.clear()

        def worker():
            try:
                full_wps, _summary = self._build_optimized_waypoints()
            except Exception as e:
                self.after(0, lambda: self._on_altitude_worker_error(str(e)))
                return
            nav_wps = [wp for wp in full_wps if wp.is_nav_point]
            if len(nav_wps) < 2:
                self.after(0, lambda: self._on_altitude_setup_warning("msg_no_terrain_for_save"))
                return

            polygons = self._get_country_border_polygon("RUS")
            if not polygons:
                self.after(0, lambda: self._on_altitude_setup_warning("msg_no_occupied_data"))
                return

            all_ring_points = [pt for poly in polygons for ring in poly for pt in ring]
            if all_ring_points:
                poly_lons = [p[0] for p in all_ring_points]
                poly_lats = [p[1] for p in all_ring_points]
                self._altitude_debug_polygon_bbox = (min(poly_lats), max(poly_lats), min(poly_lons), max(poly_lons))
            else:
                self._altitude_debug_polygon_bbox = None
            self._altitude_debug_n_polygons = len(polygons)
            route_lats = [wp.lat for wp in nav_wps]
            route_lons = [wp.lon for wp in nav_wps]
            self._altitude_debug_route_bbox = (min(route_lats), max(route_lats), min(route_lons), max(route_lons))

            terrain = self.analyzer.terrain

            def _terrain_at(lat, lon):
                try:
                    return terrain.get_elevation(lat, lon)
                except SRTMError:
                    return None

            original_altitudes_m = []
            for wp in nav_wps:
                abs_alt = self.analyzer._absolute_alt(wp)
                th = _terrain_at(wp.lat, wp.lon)
                original_altitudes_m.append(abs_alt - th if th is not None else 0.0)

            in_occupied = [
                altitude_optimizer.point_in_any_polygon(wp.lat, wp.lon, polygons) for wp in nav_wps
            ]
            self._altitude_debug_n_inside = sum(in_occupied)

            maneuver = altitude_optimizer.compute_border_crossing_maneuver(
                nav_wps, polygons,
                route_type.altitude_controlled_m, route_type.altitude_occupied_m,
                route_type.altitude_border_crossing_m, route_type.border_maneuver_angle_max_deg,
            )

            cum_dist = [0.0]
            for k in range(len(nav_wps) - 1):
                cum_dist.append(cum_dist[-1] + haversine_m(
                    nav_wps[k].lat, nav_wps[k].lon, nav_wps[k + 1].lat, nav_wps[k + 1].lon,
                ))

            def _route_pos_of(leg_idx, lat, lon):
                a, b = nav_wps[leg_idx], nav_wps[leg_idx + 1]
                seg = haversine_m(a.lat, a.lon, b.lat, b.lon)
                frac = haversine_m(a.lat, a.lon, lat, lon) / seg if seg > 0 else 0.0
                return cum_dist[leg_idx] + seg * frac

            maneuver_span = None
            maneuver_new_points = []
            self._altitude_crossing_km_value = None
            if maneuver is not None:
                if not maneuver["achievable"]:
                    self.after(0, lambda: messagebox.showwarning(
                        i18n.t("msg_weather_title"), i18n.t("msg_border_maneuver_unachievable"),
                    ))
                else:
                    ms, cr, me = maneuver["maneuver_start"], maneuver["crossing"], maneuver["maneuver_end"]
                    pos_start = _route_pos_of(ms["leg_index"], ms["lat"], ms["lon"])
                    pos_cross = _route_pos_of(cr["leg_index"], cr["lat"], cr["lon"])
                    pos_end = _route_pos_of(me["leg_index"], me["lat"], me["lon"])
                    maneuver_span = (pos_start, pos_end)
                    maneuver_new_points = [
                        (pos_start, ms["lat"], ms["lon"], ms["target_agl"]),
                        (pos_cross, cr["lat"], cr["lon"], cr["target_agl"]),
                        (pos_end, me["lat"], me["lon"], me["target_agl"]),
                    ]
                    self._altitude_crossing_km_value = pos_cross / 1000.0

            crossing_found = maneuver is not None

            detour_coords = set()
            if self._route_optimization_result is not None:
                for leg in self._route_optimization_result.legs:
                    for lat, lon in leg.inserted_waypoints:
                        detour_coords.add((round(lat, 9), round(lon, 9)))

            points = []
            for k, wp in enumerate(nav_wps):
                pos_k = cum_dist[k]
                if maneuver_span is not None and maneuver_span[0] < pos_k < maneuver_span[1]:
                    continue
                target_agl = route_type.altitude_occupied_m if in_occupied[k] else route_type.altitude_controlled_m
                is_detour = (round(wp.lat, 9), round(wp.lon, 9)) in detour_coords
                points.append([wp.lat, wp.lon, target_agl, False, is_detour, pos_k])
            for pos, lat, lon, target_agl in maneuver_new_points:
                points.append([lat, lon, target_agl, True, False, pos])
            points.sort(key=lambda p: p[5])
            for p in points:
                del p[5]

            original_points_for_algo = [[wp.lat, wp.lon, original_altitudes_m[i]] for i, wp in enumerate(nav_wps)]

            try:
                n_new, n_raised, clearance_log = self._fix_terrain_clearance_cycles(
                    points, min_clearance_m, route_type.max_waypoints,
                    route_type, angle_limit, polygons,
                    original_points=original_points_for_algo, on_iteration=on_iteration,
                    cancel_event=self._altitude_cancel_event,
                )
            except Exception as e:
                # КРИТИЧНО: без цього будь-який виняток у фоновому
                # потоці ТИХО зникає (стандартна поведінка Tkinter --
                # просто друк у stderr, якого НЕМАЄ у зібраному .exe
                # без консолі) -- виглядає точно як "нічого не
                # відбувається", хоча насправді стався збій. ВИЯВЛЕНО
                # НА ПРАКТИЦІ як ймовірна причина реальних скарг
                # користувача. Тепер помилка гарантовано показується.
                import traceback
                tb_text = traceback.format_exc()
                self.after(0, lambda: self._on_altitude_worker_error(tb_text))
                return
            self.after(0, lambda: self._finish_altitude_optimization(
                points, nav_wps, original_altitudes_m, crossing_found, n_new, n_raised, clearance_log,
            ))

        threading.Thread(target=worker, daemon=True).start()


    def _on_altitude_setup_warning(self, i18n_key):
        """Попередження про НЕВДАЛУ ПІДГОТОВКУ (немає точок, немає
        полігону кордону тощо) -- сталось УСЕРЕДИНІ фонового потоку
        (мережевий запит кордону ТЕПЕР там), тому показ через
        messagebox МАЄ йти через self.after(0, ...), інакше -- та сама
        помилка звернення до Tkinter з фонового потоку."""
        self.optimize_altitude_btn.configure(state="normal")
        self.alt_step_next_btn.configure(state="disabled")
        self.alt_cancel_btn.configure(state="disabled")
        self.altitude_status_var.set("")
        messagebox.showwarning(i18n.t("msg_weather_title"), i18n.t(i18n_key))


    def _on_altitude_worker_error(self, tb_text):
        self.optimize_altitude_btn.configure(state="normal")
        self.alt_step_next_btn.configure(state="disabled")
        self.alt_cancel_btn.configure(state="disabled")
        self.altitude_status_var.set(i18n.t("status_altitude_optimization_error"))
        self.altitude_report_text.configure(state="normal")
        self.altitude_report_text.delete("1.0", "end")
        self.altitude_report_text.insert("1.0", f"{i18n.t('status_altitude_optimization_error')}\n\n{tb_text}")
        theme.make_text_readonly(self.altitude_report_text)
        messagebox.showerror(i18n.t("msg_weather_title"), f"{i18n.t('status_altitude_optimization_error')}\n\n{tb_text[-500:]}")


    def _update_altitude_step_display(self, snapshot, iter_num):
        """Оновлює статус-рядок і графік ПОТОЧНИМ станом (points на
        цій ітерації) -- викликається з головного потоку через
        self.after(0, ...), поки фоновий потік чекає на пробіл.

        Малює ВСІ ТРИ лінії -- "Було", "Рельєф", "Стало" -- не тільки
        "Стало". "Було"/"Рельєф" НЕ змінюються між ітераціями, тому
        рахуються ОДИН РАЗ (кешуються) і надалі просто перевикористовуються."""
        cl = snapshot["min_clearance"]
        cl_str = f"{cl:.1f}м" if cl is not None else "—"
        # текст залежить від режиму -- у ЗВИЧАЙНОМУ (не покроковому)
        # режимі нічого НЕ блокується, тому текст "натисни пробіл"
        # ВВОДИВ В ОМАНУ (ВИЯВЛЕНО НА ПРАКТИЦІ: користувач бачив цей
        # текст і резонно чекав, що щось треба натиснути, хоча
        # насправді нічого не блокувалось).
        if self.alt_step_mode_var.get():
            self.altitude_status_var.set(i18n.t("status_altitude_step_fmt", n=iter_num, clearance=cl_str))
        else:
            self.altitude_status_var.set(i18n.t("status_altitude_progress_fmt", n=iter_num, clearance=cl_str))

        # персистентний кеш рельєфу -- ініціалізується тут (раніше за
        # обидва блоки нижче, що його використовують), переживає між
        # ВИКЛИКАМИ цієї функції. Див. докладне пояснення нижче, біля
        # побудови лінії "Стало" -- та сама причина заморожування UI.
        if not hasattr(self, "_altitude_step_terrain_persistent_cache"):
            self._altitude_step_terrain_persistent_cache = {}
        _persist_cache = self._altitude_step_terrain_persistent_cache

        def _cached_terrain_at(lat, lon, terrain_obj):
            key = (round(lat, 7), round(lon, 7))
            if key in _persist_cache:
                return _persist_cache[key]
            try:
                val = terrain_obj.get_elevation(lat, lon)
            except SRTMError:
                val = 0.0
            _persist_cache[key] = val
            return val

        # ДЕТАЛЬНА діагностика найгіршого ребра -- у текстовий звіт
        # (велика видима область, зручно для скріншота). ВИЯВЛЕНО НА
        # ПРАКТИЦІ: раніше тут ЗАНОВО сканувались УСІ ребра з рельєфом,
        # БЕЗ кешу, НАПРЯМУ на головному потоці -- для реальної великої
        # місії з реальним диском/мережею SRTM це саме й ЗАМОРОЖУВАЛО
        # ВЕСЬ інтерфейс (пробіл/скасувати теж переставали реагувати,
        # бо весь UI-потік був зайнятий). Тепер найгірше ребро ВЖЕ
        # готове (порахував фоновий потік, з кешем) -- тут лише
        # ДВІ точки (не всі ребра), для показу деталей.
        try:
            terrain_for_diag = self.analyzer.terrain
            pts = snapshot["points"]
            worst_i = snapshot.get("worst_edge_index")
            worst_frac = snapshot.get("worst_edge_frac")
            worst_c = snapshot["min_clearance"]

            lines = [f"=== Ітерація {iter_num} -- ДІАГНОСТИКА найгіршого ребра ==="]
            lines.append(f"Точок зараз: {len(pts)}")
            lines.append(f"Мінімальний кліренс: {cl_str}")
            if worst_i is not None and worst_i + 1 < len(pts):
                lat_a, lon_a, agl_a, is_new_a, *_ = pts[worst_i]
                lat_b, lon_b, agl_b, is_new_b, *_ = pts[worst_i + 1]
                ta = _cached_terrain_at(lat_a, lon_a, terrain_for_diag)
                tb = _cached_terrain_at(lat_b, lon_b, terrain_for_diag)
                d = haversine_m(lat_a, lon_a, lat_b, lon_b)
                angle = math.degrees(math.atan2((tb + agl_b) - (ta + agl_a), d)) if d > 0.01 else 0.0
                lines.append(f"\nНайгірше ребро: індекс {worst_i} (частка вздовж={worst_frac:.2f})")
                lines.append(f"  Точка A: lat={lat_a:.7f}, lon={lon_a:.7f}, AGL={agl_a:.1f}м, рельєф={ta:.1f}м, нова={is_new_a}")
                lines.append(f"  Точка B: lat={lat_b:.7f}, lon={lon_b:.7f}, AGL={agl_b:.1f}м, рельєф={tb:.1f}м, нова={is_new_b}")
                lines.append(f"  Довжина ребра: {d:.1f}м, кут: {angle:+.2f}°")
                lines.append(f"  Найгірший кліренс на ребрі: {worst_c:.1f}м (у частці {worst_frac:.2f} вздовж)")

            # ОКРЕМИЙ блок про найгірший КУТ (не кліренс) -- ЗА ПРЯМОЮ
            # ВКАЗІВКОЮ користувача: реальне "застрягання" на графіку
            # виявилось кутовим порушенням (шпильки до +8°), яке
            # діагностика найгіршого КЛІРЕНСУ просто не бачила -- це
            # ДВІ РІЗНІ речі, обидві потрібні для повної картини.
            angle_excess = snapshot.get("worst_angle_excess")
            angle_actual = snapshot.get("worst_angle_actual")
            angle_i = snapshot.get("worst_angle_index")
            if angle_i is not None and angle_i + 1 < len(pts):
                lat_a, lon_a, agl_a, is_new_a, *_ = pts[angle_i]
                lat_b, lon_b, agl_b, is_new_b, *_ = pts[angle_i + 1]
                d2 = haversine_m(lat_a, lon_a, lat_b, lon_b)
                status = "ПОРУШЕНО" if angle_excess is not None and angle_excess > 0 else "у межах ліміту"
                lines.append(f"\nНайгірший КУТ: ребро {angle_i}, {angle_actual:+.2f}° ({status}, перевищення={angle_excess:+.2f}°)")
                lines.append(f"  Точка A: lat={lat_a:.7f}, lon={lon_a:.7f}, AGL={agl_a:.1f}м, нова={is_new_a}")
                lines.append(f"  Точка B: lat={lat_b:.7f}, lon={lon_b:.7f}, AGL={agl_b:.1f}м, нова={is_new_b}")
                lines.append(f"  Довжина ребра: {d2:.1f}м")
            self.altitude_report_text.configure(state="normal")
            self.altitude_report_text.delete("1.0", "end")
            self.altitude_report_text.insert("1.0", "\n".join(lines))
            theme.make_text_readonly(self.altitude_report_text)
        except Exception:
            pass  # діагностика НЕ має зривати сам розрахунок

        try:
            terrain = self.analyzer.terrain

            # ПЕРСИСТЕНТНИЙ кеш (переживає між ВИКЛИКАМИ цієї функції,
            # не тільки в межах одного) -- ВИЯВЛЕНО НА ПРАКТИЦІ: лінія
            # "Стало" оновлюється на КОЖНІЙ ітерації для ВСІХ точок
            # (сотні), але між сусідніми ітераціями рухається зазвичай
            # ЛИШЕ одна-дві точки -- решта лишається на тому самому
            # місці й самому терені. Без цього кешу кожне оновлення UI
            # на реальному (повільному, диск/мережа) SRTM повторно
            # запитувало рельєф для ВСІХ сотень точок -- саме це, разом
            # з подібною проблемою в діагностиці найгіршого ребра,
            # заморожувало ввесь інтерфейс на реальній великій місії.
            if not hasattr(self, "_altitude_step_terrain_persistent_cache"):
                self._altitude_step_terrain_persistent_cache = {}
            _persist_cache = self._altitude_step_terrain_persistent_cache

            def _terrain_at(lat, lon):
                key = (round(lat, 7), round(lon, 7))
                if key in _persist_cache:
                    return _persist_cache[key]
                try:
                    val = terrain.get_elevation(lat, lon)
                except SRTMError:
                    val = 0.0
                _persist_cache[key] = val
                return val

            if not hasattr(self, "_altitude_step_before_cache"):
                original_points = snapshot.get("original_points") or []
                dist_before, abs_before = [], []
                cum = 0.0
                prev = None
                for lat, lon, agl in original_points:
                    if prev is not None:
                        cum += haversine_m(prev[0], prev[1], lat, lon)
                    prev = (lat, lon)
                    dist_before.append(cum / 1000.0)
                    abs_before.append(_terrain_at(lat, lon) + agl)

                terrain_dist_km, terrain_alt = [], []
                step_m = 50.0
                cum2 = 0.0
                for i in range(len(original_points) - 1):
                    lat_a, lon_a, _ = original_points[i]
                    lat_b, lon_b, _ = original_points[i + 1]
                    seg_dist = haversine_m(lat_a, lon_a, lat_b, lon_b)
                    n_steps = max(1, int(seg_dist // step_m))
                    for s in range(n_steps + (1 if i == len(original_points) - 2 else 0)):
                        frac = s / n_steps
                        lat, lon = lat_a + (lat_b - lat_a) * frac, lon_a + (lon_b - lon_a) * frac
                        terrain_dist_km.append((cum2 + seg_dist * frac) / 1000.0)
                        terrain_alt.append(_terrain_at(lat, lon))
                    cum2 += seg_dist

                self._altitude_step_before_cache = (dist_before, abs_before)
                self._altitude_step_terrain_cache = (terrain_dist_km, terrain_alt)

            self._altitude_dist_before_km, self._altitude_alt_before = self._altitude_step_before_cache
            self._altitude_terrain_dist_km, self._altitude_terrain_alt = self._altitude_step_terrain_cache

            # живий графік -- малюємо ПОТОЧНІ (ще не фінальні) точки як "Стало"
            dist_after_km, abs_after = [], []
            cum = 0.0
            prev = None
            for lat, lon, agl, _is_new, *_ in snapshot["points"]:
                if prev is not None:
                    cum += haversine_m(prev[0], prev[1], lat, lon)
                prev = (lat, lon)
                dist_after_km.append(cum / 1000.0)
                abs_after.append(_terrain_at(lat, lon) + agl)
            self._altitude_dist_after_km = dist_after_km
            self._altitude_alt_after = abs_after

            # позиція НАЙГІРШОГО кліренсу НА ЦЕЙ момент -- ЗА ПРЯМОЮ
            # ВКАЗІВКОЮ користувача: показувати в реальному часі, не
            # тільки в кінці. worst_edge_index/frac ВЖЕ пораховані у
            # знімку (з кешем, у фоновому потоці) -- тут лише
            # інтерполяція між двома вже готовими відстанями.
            bi = snapshot.get("worst_edge_index")
            bf = snapshot.get("worst_edge_frac")
            if bi is not None and bi + 1 < len(dist_after_km):
                self._altitude_bottleneck_km = dist_after_km[bi] + (dist_after_km[bi + 1] - dist_after_km[bi]) * bf
            else:
                self._altitude_bottleneck_km = None

            self._redraw_altitude_chart()

            self._altitude_angle_adapter = self._compute_edge_angle_adapter(snapshot["points"])
            self._redraw_altitude_angle_chart()
        except Exception:
            pass  # живе оновлення графіка НЕ має зривати сам розрахунок


    def _on_altitude_step_next(self):
        """Кнопка \"Далі\" чи клавіша пробіл -- звільняє фоновий потік
        для наступної ітерації."""
        event = getattr(self, "_altitude_step_event", None)
        if event is not None:
            event.set()


    def _on_altitude_cancel(self):
        """Кнопка \"Скасувати\" -- сигналізує фоновому потоку зупинитись
        на найближчій перевірці (початок наступної ітерації), НЕ через
        Диспетчер задач. Якщо покроковий режим чекає на пробіл --
        звільняємо і його теж, інакше потік завис би на очікуванні
        назавжди, ніколи не дійшовши до перевірки скасування."""
        self.alt_cancel_btn.configure(state="disabled")
        cancel_event = getattr(self, "_altitude_cancel_event", None)
        if cancel_event is not None:
            cancel_event.set()
        step_event = getattr(self, "_altitude_step_event", None)
        if step_event is not None:
            step_event.set()


    def _finish_altitude_optimization(self, points, nav_wps, original_altitudes_m, crossing_found, n_new, n_raised, clearance_log):
        """Завершення оптимізації висоти -- звіт і графік. Викликається
        і після звичайного (синхронного), і після покрокового
        (фоновий потік + пробіл) виконання -- та сама логіка."""
        self.optimize_altitude_btn.configure(state="normal")
        self.alt_step_next_btn.configure(state="disabled")
        self.alt_cancel_btn.configure(state="disabled")

        self._altitude_points = points
        self._altitude_clearance_log = clearance_log  # повні знімки (кліренс, точки, оригінал) на кожній ітерації -- для діагностики
        self._altitude_original_altitudes_m = original_altitudes_m
        self._altitude_nav_wps = nav_wps
        self._altitude_crossing_found = crossing_found
        self._altitude_n_raised = n_raised
        self._altitude_n_new = n_new

        (self._altitude_dist_before_km, self._altitude_alt_before,
         self._altitude_dist_after_km, self._altitude_alt_after,
         self._altitude_crossing_km,
         self._altitude_terrain_dist_km, self._altitude_terrain_alt) = self._compute_altitude_profiles_simple(
            nav_wps, points, original_altitudes_m, self._altitude_crossing_km_value,
        )

        # позиція НАЙГІРШОГО кліренсу у ФІНАЛЬНОМУ стані -- той самий
        # маркер, що й у живому оновленні, тепер на завершеному профілі.
        last_snap = clearance_log[-1] if clearance_log else None
        bi = last_snap.get("worst_edge_index") if last_snap else None
        bf = last_snap.get("worst_edge_frac") if last_snap else None
        if bi is not None and bi + 1 < len(self._altitude_dist_after_km):
            self._altitude_bottleneck_km = (
                self._altitude_dist_after_km[bi]
                + (self._altitude_dist_after_km[bi + 1] - self._altitude_dist_after_km[bi]) * bf
            )
        else:
            self._altitude_bottleneck_km = None

        self._redraw_altitude_chart()

        self._altitude_angle_adapter = self._compute_edge_angle_adapter(points)
        self._redraw_altitude_angle_chart()

        self.altitude_status_var.set(i18n.t("status_altitude_optimization_done"))
        self.save_altitude_btn.configure(state="normal")

        lines = [i18n.t("altitude_report_header"), "-" * 60, ""]
        rlat_min, rlat_max, rlon_min, rlon_max = self._altitude_debug_route_bbox
        lines.append(i18n.t(
            "altitude_debug_route_bbox_fmt",
            lat_min=rlat_min, lat_max=rlat_max, lon_min=rlon_min, lon_max=rlon_max,
        ))
        lines.append(i18n.t("altitude_debug_n_polygons_fmt", n=self._altitude_debug_n_polygons))
        if self._altitude_debug_polygon_bbox:
            plat_min, plat_max, plon_min, plon_max = self._altitude_debug_polygon_bbox
            lines.append(i18n.t(
                "altitude_debug_polygon_bbox_fmt",
                lat_min=plat_min, lat_max=plat_max, lon_min=plon_min, lon_max=plon_max,
            ))
        lines.append(i18n.t("altitude_debug_n_inside_fmt", n=self._altitude_debug_n_inside, total=len(nav_wps)))
        lines.append("")
        lines.append(i18n.t(
            "altitude_report_simple_summary_fmt",
            crossing="так" if crossing_found else "ні", raised=n_raised, new=n_new,
        ))
        excess_before = getattr(self, "_altitude_phase3_excess_before", None)
        excess_after = getattr(self, "_altitude_phase3_excess_after", None)
        if excess_before is not None and excess_after is not None:
            lines.append(i18n.t(
                "altitude_phase3_gain_fmt",
                before=f"{excess_before:.1f}", after=f"{excess_after:.1f}",
                gain=f"{excess_before - excess_after:.1f}",
            ))
        lines.append("")
        lines.append(i18n.t("altitude_clearance_log_header"))
        for idx, snap in enumerate(clearance_log):
            cl = snap["min_clearance"]
            cl_str = f"{cl:.1f}м" if cl is not None else "—"
            lines.append(f"  {idx + 1}: {cl_str} ({len(snap['points'])} точок)")

        self.altitude_report_text.configure(state="normal")
        self.altitude_report_text.delete("1.0", "end")
        self.altitude_report_text.insert("1.0", "\n".join(lines))
        theme.make_text_readonly(self.altitude_report_text)


    def _fix_terrain_clearance_cycles(self, points, min_clearance_m, max_waypoints, route_type, angle_limit, polygons, original_points=None, on_iteration=None, cancel_event=None):
        """АЛГОРИТМ (за прямою вказівкою користувача, замінює попередні
        зламані спроби):

        Бюджет точок = max_waypoints - 5% резерву - вже наявні точки.
        КОЖНА вставка зменшує бюджет на 1 -- цикл ГАРАНТОВАНО
        завершується (бюджет скінченний і монотонно спадає), на
        відміну від попередніх версій.

        Одна ІТЕРАЦІЯ:
          1. Прохід ІНЕРЦІЙНОСТІ: перше ребро з кутом нахилу > 2° --
             вставляємо ОДНУ точку (з боку нижчої точки, плавний підйом
             рівно на 2°), зменшуємо бюджет, ПОЧИНАЄМО ітерацію заново.
          2. Якщо інерційність стабільна -- прохід РЕЛЬЄФУ: ранжуємо
             ВСІ ребра за мінімальним кліренсом (найгірші перші), для
             найгіршого (якщо є порушення) вставляємо ОДНУ точку САМЕ в
             місці найгіршого кліренсу цього ребра, з ЗВИЧАЙНОЮ (не
             спеціальною) AGL-ціллю тієї зони -- де реально лежить ця
             нова точка. Зменшуємо бюджет, ПОЧИНАЄМО ітерацію заново.
          3. Якщо жодних змін -- стабільно, завершуємо.

        ПЕРЕД кожною ітерацією -- фіксуємо мінімальний кліренс усього
        маршруту (для звіту).

        ПІСЛЯ вичерпання бюджету, якщо кліренс УСЕ ЩЕ недостатній --
        ЗАПАСНИЙ крок: піднімаємо висоту НАЯВНИХ точок (не додаючи
        нових, бюджету більше немає) настільки, щоб покрити залишкові
        порушення.

        Повертає (к-ть_нових_точок, к-ть_піднятих_точок, clearance_log).

        route_type, angle_limit, polygons -- ЗАЗДАЛЕГІДЬ прочитані на
        ГОЛОВНОМУ потоці (НЕ всередині цієї функції!). ВИЯВЛЕНО НА
        ПРАКТИЦІ, реальний баг: ця функція тепер завжди виконується у
        ФОНОВОМУ потоці (не тільки в покроковому режимі) -- звернення
        до Tkinter-змінної (.get()) чи мережевого запиту з фонового
        потоку кидає RuntimeError, що ТИХО гине всередині потоку
        (демон-потік просто зникає), і _finish_altitude_optimization
        ніколи не викликається -- виглядає як "зависання", хоча
        насправді -- невиловлений виняток у фоновому потоці."""
        terrain = self.analyzer.terrain
        climb_angle_limit = angle_limit
        descent_angle_limit = angle_limit

        # КЕШ рельєфу в межах ОДНОГО запуску -- ті самі координати
        # (переважно НАЯВНІ, не зсунуті точки) запитуються повторно на
        # КОЖНІЙ ітерації для перевірки кута/кліренсу; з реальним SRTM
        # (диск, не пам'ять) це могло давати відчутну повільність на
        # сотнях ітерацій. Округлення до ~1см -- набагато точніше за
        # реальну роздільність SRTM (30м), тому не впливає на
        # коректність, лише прибирає повторні дискові звернення.
        _terrain_cache: dict[tuple[float, float], float | None] = {}

        def _terrain_at(lat, lon):
            key = (round(lat, 7), round(lon, 7))
            if key in _terrain_cache:
                return _terrain_cache[key]
            try:
                val = terrain.get_elevation(lat, lon)
            except SRTMError:
                val = None
            _terrain_cache[key] = val
            return val

        def _zone_agl(lat, lon):
            is_occ = altitude_optimizer.point_in_any_polygon(lat, lon, polygons)
            return route_type.altitude_occupied_m if is_occ else route_type.altitude_controlled_m

        def _min_clearance_overall():
            worst = None
            for i in range(len(points) - 1):
                lat_a, lon_a, agl_a, *_ = points[i]
                lat_b, lon_b, agl_b, *_ = points[i + 1]
                ta, tb = _terrain_at(lat_a, lon_a), _terrain_at(lat_b, lon_b)
                if ta is None or tb is None:
                    continue
                abs_a, abs_b = ta + agl_a, tb + agl_b
                seg_dist = haversine_m(lat_a, lon_a, lat_b, lon_b)
                if seg_dist < 1.0:
                    continue
                n_steps = max(1, int(seg_dist // 25.0))
                for s in range(n_steps + 1):
                    frac = s / n_steps
                    lat, lon = lat_a + (lat_b - lat_a) * frac, lon_a + (lon_b - lon_a) * frac
                    mission_alt = abs_a + (abs_b - abs_a) * frac
                    ground = _terrain_at(lat, lon)
                    if ground is None:
                        continue
                    clearance = mission_alt - ground
                    if worst is None or clearance < worst:
                        worst = clearance
            return worst

        def _worst_edge_info():
            """(worst_clearance, worst_i, worst_frac) -- те саме, що
            _min_clearance_overall(), АЛЕ ще й запам'ятовує ЯКЕ саме
            ребро. ВИЯВЛЕНО НА ПРАКТИЦІ: раніше діагностика найгіршого
            ребра рахувалась ОКРЕМО, БЕЗ кешу _terrain_at, НАПРЯМУ на
            головному потоці (через self.after) -- для реальної
            великої місії з реальним диском/мережею SRTM це саме й
            заморожувало ВЕСЬ інтерфейс (не просто фоновий розрахунок
            повільний -- сам UI переставав відповідати). Тепер рахується
            ТУТ, у фоновому потоці, з кешем, і просто передається у
            знімок готовим значенням."""
            worst, worst_i, worst_frac = None, None, None
            for i in range(len(points) - 1):
                lat_a, lon_a, agl_a, *_ = points[i]
                lat_b, lon_b, agl_b, *_ = points[i + 1]
                ta, tb = _terrain_at(lat_a, lon_a), _terrain_at(lat_b, lon_b)
                if ta is None or tb is None:
                    continue
                abs_a, abs_b = ta + agl_a, tb + agl_b
                seg_dist = haversine_m(lat_a, lon_a, lat_b, lon_b)
                if seg_dist < 1.0:
                    continue
                n_steps = max(1, int(seg_dist // 25.0))
                for s in range(n_steps + 1):
                    frac = s / n_steps
                    lat, lon = lat_a + (lat_b - lat_a) * frac, lon_a + (lon_b - lon_a) * frac
                    mission_alt = abs_a + (abs_b - abs_a) * frac
                    ground = _terrain_at(lat, lon)
                    if ground is None:
                        continue
                    clearance = mission_alt - ground
                    if worst is None or clearance < worst:
                        worst, worst_i, worst_frac = clearance, i, frac
            return worst, worst_i, worst_frac

        def _average_excess_clearance(target_agl):
            """Середній НАДЛИШОК кліренсу (наскільки летимо ВИЩЕ за
            цільову AGL зони, у середньому по всіх семплах маршруту) --
            ЗА ПРЯМОЮ ВКАЗІВКОЮ користувача: метрика, щоб оцінити
            РЕАЛЬНИЙ виграш від Фази 3 (прилягання до рельєфу) числом,
            а не лише на око за графіком. target_agl -- орієнтовна
            ціль (напр. controlled AGL з типу маршруту) для порівняння,
            не точна для кожної точки окремо (зони можуть відрізнятись),
            але дає ЗАГАЛЬНЕ уявлення про масштаб надлишку."""
            total, count = 0.0, 0
            for i in range(len(points) - 1):
                lat_a, lon_a, agl_a, *_ = points[i]
                lat_b, lon_b, agl_b, *_ = points[i + 1]
                ta, tb = _terrain_at(lat_a, lon_a), _terrain_at(lat_b, lon_b)
                if ta is None or tb is None:
                    continue
                abs_a, abs_b = ta + agl_a, tb + agl_b
                seg_dist = haversine_m(lat_a, lon_a, lat_b, lon_b)
                if seg_dist < 1.0:
                    continue
                n_steps = max(1, int(seg_dist // 25.0))
                for s in range(n_steps + 1):
                    frac = s / n_steps
                    lat, lon = lat_a + (lat_b - lat_a) * frac, lon_a + (lon_b - lon_a) * frac
                    mission_alt = abs_a + (abs_b - abs_a) * frac
                    ground = _terrain_at(lat, lon)
                    if ground is None:
                        continue
                    clearance = mission_alt - ground
                    total += clearance - target_agl
                    count += 1
            return total / count if count > 0 else None

        def _worst_angle_info():
            """(worst_excess_deg, worst_actual_deg, worst_i) -- ЗА
            ПРЯМОЮ ВКАЗІВКОЮ користувача: діагностика раніше показувала
            лише найгірший КЛІРЕНС, а реальне "застрягання" на графіку
            виявилось КУТОВИМ порушенням (тонкі шпильки до +8° на
            реальному графіку користувача), яке ця діагностика взагалі
            не відстежувала. excess -- наскільки кут ПЕРЕВИЩУЄ ВЛАСНИЙ
            ліміт (може бути ВІД'ЄМНИМ, якщо всюди в нормі)."""
            worst_excess, worst_actual, worst_i = None, None, None
            for i in range(len(points) - 1):
                lat_a, lon_a, agl_a, *_ = points[i]
                lat_b, lon_b, agl_b, *_ = points[i + 1]
                ta, tb = _terrain_at(lat_a, lon_a), _terrain_at(lat_b, lon_b)
                if ta is None or tb is None:
                    continue
                abs_a, abs_b = ta + agl_a, tb + agl_b
                seg_dist = haversine_m(lat_a, lon_a, lat_b, lon_b)
                if seg_dist < 1.0:
                    continue
                climbing = abs_b > abs_a
                limit_deg = climb_angle_limit if climbing else descent_angle_limit
                angle_deg = math.degrees(math.atan2(abs(abs_b - abs_a), seg_dist))
                excess = angle_deg - limit_deg
                if worst_excess is None or excess > worst_excess:
                    worst_excess, worst_actual, worst_i = excess, angle_deg, i
            return worst_excess, worst_actual, worst_i

        reserve = max(1, int(max_waypoints * 0.05))
        budget = max_waypoints - reserve - len(points)

        n_new, n_raised = 0, 0
        # ЛІЧИЛЬНИК спроб виправлення КОНКРЕТНОЇ точки (за прямою
        # вказівкою користувача: гарантоване обмеження, без спроб
        # довести математику до ідеалу). Ключ -- id() самого списку
        # [lat,lon,agl,...] цієї точки -- лишається СТАЛИМ, поки точку
        # не видалено, навіть якщо її lat/lon змінюються. Якщо ОДНУ й
        # ТУ САМУ точку намагаємось зсунути забагато разів (реальний
        # рельєф може робити позицію нестабільною -- коливання, а не
        # збіжність) -- ПРИЙМАЄМО поточний стан як залишкове порушення
        # і БІЛЬШЕ НЕ чіпаємо її в цьому запуску, а не намагаємось
        # виправити ідеально.
        _angle_fix_attempts = {}
        _MAX_ATTEMPTS_PER_POINT = 3
        clearance_log = []

        def _snapshot():
            """Повний знімок на цей момент: мінімальний кліренс,
            найгірше ребро (готове, з кешем -- НЕ рахується вдруге в
            UI), копія поточних (оптимізованих ДОСІ) точок, і -- якщо
            original_points задано -- незмінний профіль ДО оптимізації
            поруч, для порівняння на кожному кроці, не тільки в кінці."""
            worst_c, worst_i, worst_frac = _worst_edge_info()
            worst_angle_excess, worst_angle_actual, worst_angle_i = _worst_angle_info()
            return {
                "min_clearance": worst_c,
                "worst_edge_index": worst_i,
                "worst_edge_frac": worst_frac,
                "worst_angle_excess": worst_angle_excess,
                "worst_angle_actual": worst_angle_actual,
                "worst_angle_index": worst_angle_i,
                "points": [list(p) for p in points],  # копія, а не посилання -- points ще змінюватиметься
                "original_points": original_points,
            }
        MAX_ITER_SAFETY = max_waypoints * 3  # запобіжник -- бюджет і так обмежує, це лише подвійна гарантія

        # --- ФАЗА 0: усунення ВСІХ важких (від'ємних -- рельєф ВИЩЕ за
        # траєкторію) порушень НАПЕРЕД, до звичайного циклу. ВИЯВЛЕНО НА
        # ПРАКТИЦІ: якщо дати звичайному циклу (інерційність -> рельєф)
        # натрапити на важкий пік, вставлена точка створює РІЗКИЙ
        # перепад висоти із сусідами, і прохід інерційності починає
        # дробити підхід/відхід МАЛЕНЬКИМИ кроками по 2°, зʼїдаючи
        # левову частку бюджету на згладжування ОДНОГО піку -- інші,
        # окремі важкі порушення лишались би невиправленими аж до
        # запасного кроку (грубе підняття всього маршруту в кінці).
        # Тому: спершу СПЕЦІАЛЬНО зарезервований прохід, що усуває
        # КОЖНЕ від'ємне порушення напряму вставкою точки, БЕЗ жодної
        # перевірки інерційності -- лише після цього інерційність і
        # звичайне ранжування рельєфу отримують ЩО ЛИШИЛОСЬ від бюджету.
        clearance_log.append(_snapshot())
        if on_iteration is not None:
            on_iteration(clearance_log[-1], len(clearance_log))
        for _severe_iter in range(MAX_ITER_SAFETY):
            if cancel_event is not None and cancel_event.is_set():
                break  # користувач натиснув "Скасувати" -- зупиняємось на найближчій перевірці, не через Диспетчер задач
            if budget <= 0:
                break
            worst_severe, worst_i, worst_frac = None, None, None
            for i in range(len(points) - 1):
                lat_a, lon_a, agl_a, *_ = points[i]
                lat_b, lon_b, agl_b, *_ = points[i + 1]
                ta, tb = _terrain_at(lat_a, lon_a), _terrain_at(lat_b, lon_b)
                if ta is None or tb is None:
                    continue
                abs_a, abs_b = ta + agl_a, tb + agl_b
                seg_dist = haversine_m(lat_a, lon_a, lat_b, lon_b)
                if seg_dist < 1.0:
                    continue
                n_steps = max(1, int(seg_dist // 25.0))
                for s in range(n_steps + 1):
                    frac = s / n_steps
                    lat, lon = lat_a + (lat_b - lat_a) * frac, lon_a + (lon_b - lon_a) * frac
                    mission_alt = abs_a + (abs_b - abs_a) * frac
                    ground = _terrain_at(lat, lon)
                    if ground is None:
                        continue
                    clearance = mission_alt - ground
                    if clearance < 0 and (worst_severe is None or clearance < worst_severe):
                        worst_severe, worst_i, worst_frac = clearance, i, frac

            if worst_severe is None:
                break  # жодного від'ємного порушення не лишилось -- фаза 0 завершена

            if worst_frac < 0.02 or worst_frac > 0.98:
                # той самий принцип, що й у Фазі 2 -- ВИЯВЛЕНО НА
                # ПРАКТИЦІ: коли найгірша точка збігається з наявним
                # кінцем ребра, вставка "виправлення" там створює
                # ТОЧНИЙ ДУБЛІКАТ (нульова відстань), нічого не
                # покращуючи -- реальний нескінченний цикл, сотні
                # точок в одній позиції. Піднімаємо AGL кінця замість.
                target_idx = worst_i if worst_frac < 0.02 else worst_i + 1
                points[target_idx][2] += abs(worst_severe) + 1.0
                clearance_log.append(_snapshot())
                if on_iteration is not None:
                    on_iteration(clearance_log[-1], len(clearance_log))
                continue
            lat_a, lon_a, *_ = points[worst_i]
            lat_b, lon_b, *_ = points[worst_i + 1]
            fix_lat = lat_a + (lat_b - lat_a) * worst_frac
            fix_lon = lon_a + (lon_b - lon_a) * worst_frac
            points.insert(worst_i + 1, [fix_lat, fix_lon, _zone_agl(fix_lat, fix_lon), True, False])
            budget -= 1
            n_new += 1
            clearance_log.append(_snapshot())
            if on_iteration is not None:
                on_iteration(clearance_log[-1], len(clearance_log))

        # --- ФАЗА 1: ПОВНИЙ контроль кутів -- ЗА ПРЯМОЮ ВКАЗІВКОЮ
        # користувача: усі порушення кута виправляються ПОВНІСТЮ, до
        # нуля, ПЕРШ ніж починається ітеративне додавання точок для
        # рельєфу. Не одна за раз всередині спільного циклу -- окрема,
        # завершена фаза. Позиція (lat/lon) НАЯВНОЇ точки зсувається --
        # точка старту набору ВЛІВО (до попередньої), точка виходу на
        # горизонт при зниженні ВПРАВО (до наступної) -- доки кут не
        # стане трохи нижче ліміту з профілю літака (climb_angle_limit/
        # descent_angle_limit). Жодних нових точок, бюджет не витрачається.
        #
        # Винесено в ОКРЕМУ функцію (за прямою вказівкою користувача):
        # дроблення довгого ребра (Фаза 3, нижче) може створити НОВЕ
        # порушення кута в щойно вставленій точці (якщо рельєф різко
        # змінюється саме там) -- Фазу 1 треба вміти запускати ЗНОВУ
        # після Фази 3, не тільки один раз на самому початку.
        def _try_altitude_squeeze_fix(idx):
            """ЗАГАЛЬНИЙ алгоритм: точка idx затиснута між двома
            сусідами (idx-1, idx+1). Знаходить діапазон висоти САМОЇ
            точки idx, що одночасно задовольняє ліміт кута на ОБОХ
            сусідніх ребрах, і застосовує, якщо такий діапазон існує.
            Не чіпає жодну позицію (lat/lon) -- тільки AGL, тому не
            може створити конфлікт із сусіднім ребром (на відміну від
            зсуву позиції, який змінює й ДОВЖИНУ сусіднього ребра).
            Повертає True, якщо застосовано."""
            if idx <= 0 or idx >= len(points) - 1:
                return False  # немає одного з двох сусідів -- нема що "затискати"
            lat_prev, lon_prev, agl_prev, *_ = points[idx - 1]
            lat_cur, lon_cur, agl_cur, *_ = points[idx]
            lat_next, lon_next, agl_next, *_ = points[idx + 1]
            t_prev = _terrain_at(lat_prev, lon_prev)
            t_cur = _terrain_at(lat_cur, lon_cur)
            t_next = _terrain_at(lat_next, lon_next)
            if t_prev is None or t_cur is None or t_next is None:
                return False
            abs_prev = t_prev + agl_prev
            abs_next = t_next + agl_next
            d1 = haversine_m(lat_prev, lon_prev, lat_cur, lon_cur)
            d2 = haversine_m(lat_cur, lon_cur, lat_next, lon_next)
            if d1 < 1.0 or d2 < 1.0:
                return False

            tan_climb = math.tan(math.radians(climb_angle_limit * 0.99))
            tan_descent = math.tan(math.radians(descent_angle_limit * 0.99))
            # діапазон abs_cur, що задовольняє ребро (prev, cur): чи
            # набір (abs_cur > abs_prev), чи зниження -- ліміт РІЗНИЙ
            # для набору й зниження, тому діапазон АСИМЕТРИЧНИЙ.
            lo1 = abs_prev - d1 * tan_descent  # якщо cur НИЖЧЕ prev (зниження ДО cur)
            hi1 = abs_prev + d1 * tan_climb    # якщо cur ВИЩЕ prev (набір ДО cur)
            lo2 = abs_next - d2 * tan_climb    # якщо cur НИЖЧЕ next (набір ВІД cur ДО next)
            hi2 = abs_next + d2 * tan_descent  # якщо cur ВИЩЕ next (зниження ВІД cur)

            lo = max(lo1, lo2)
            hi = min(hi1, hi2)
            if lo > hi:
                return False  # жодне значення висоти цієї точки не задовольнить ОБИДВА ребра одночасно

            cur_abs = t_cur + agl_cur
            target_abs = min(max(cur_abs, lo), hi)  # найближче до поточного значення в межах допустимого діапазону
            points[idx][2] = target_abs - t_cur
            return True

        def _run_phase1_angles():
            nonlocal n_raised
            for _angle_iter in range(MAX_ITER_SAFETY):
                if cancel_event is not None and cancel_event.is_set():
                    return
                fixed_any_angle = False
                for i in range(len(points) - 1):
                    lat_a, lon_a, agl_a, *_ = points[i]
                    lat_b, lon_b, agl_b, *_ = points[i + 1]
                    ta, tb = _terrain_at(lat_a, lon_a), _terrain_at(lat_b, lon_b)
                    if ta is None or tb is None:
                        continue
                    abs_a, abs_b = ta + agl_a, tb + agl_b
                    seg_dist = haversine_m(lat_a, lon_a, lat_b, lon_b)
                    if seg_dist < 1.0:
                        continue
                    climbing = abs_b > abs_a
                    limit_deg = climb_angle_limit if climbing else descent_angle_limit
                    angle_deg = math.degrees(math.atan2(abs(abs_b - abs_a), seg_dist))
                    if angle_deg <= limit_deg + 0.01:
                        continue

                    tan_limit = math.tan(math.radians(limit_deg * 0.99))
                    needed_dist = abs(abs_b - abs_a) / tan_limit if tan_limit > 0 else seg_dist

                    # ГАРАНТОВАНЕ обмеження спроб на ТУ САМУ точку --
                    # перевіряється ОДНАКОВО для climbing і descending,
                    # ще ДО будь-якої спроби зсуву/злиття нижче.
                    point_id = id(points[i] if climbing else points[i + 1])
                    attempts = _angle_fix_attempts.get(point_id, 0)
                    if attempts >= _MAX_ATTEMPTS_PER_POINT:
                        continue  # забагато спроб на цю точку -- приймаємо як залишкове, більше не чіпаємо
                    _angle_fix_attempts[point_id] = attempts + 1

                    # ЗАГАЛЬНИЙ алгоритм (ЗА ПРЯМОЮ ВКАЗІВКОЮ
                    # користувача, щоб працював УСЮДИ, де таке
                    # трапляється, не тільки в одному знайденому
                    # випадку): якщо точку "затиснуто" між двома
                    # конкуруючими вимогами кута (виправлення ОДНІЄЇ
                    # сторони зсувом позиції неминуче псує ІНШУ, бо
                    # сусідні ребра дуже короткі відносно потрібного
                    # перепаду) -- пробуємо СПЕРШУ підняти/опустити
                    # ВИСОТУ САМОЇ цієї точки (не позицію) так, щоб
                    # ОБИДВА сусідні ребра одночасно вклались у ліміт.
                    # ПІДТВЕРДЖЕНО ЧИСЕЛЬНО на реальному прикладі:
                    # зсув позиції для одного ребра штовхав сусіднє за
                    # ліміт з ІНШОГО боку -- висота тут не має цієї
                    # проблеми, бо не змінює жодної відстані.
                    squeeze_idx = i if climbing else i + 1
                    if _try_altitude_squeeze_fix(squeeze_idx):
                        fixed_any_angle = True
                        clearance_log.append(_snapshot())
                        if on_iteration is not None:
                            on_iteration(clearance_log[-1], len(clearance_log))
                        break

                    if climbing:
                        if i == 0:
                            continue
                        needed = needed_dist - seg_dist
                        if needed <= 0:
                            continue
                        # ЯКЩО одного найближчого сусіда не вистачає --
                        # ЗЛИВАЄМО його (видаляємо), пробуємо знову з
                        # НАСТУПНИМ (тепер довшим) сусідом -- ЗА ПРЯМОЮ
                        # ВКАЗІВКОЮ користувача, замість спроби
                        # "накопичувати" room (та спроба сама викликала
                        # нескінченну осциляцію -- невідповідність
                        # системи відліку, ВИЯВЛЕНО НА ПРАКТИЦІ). Злиття
                        # -- простіше й безпечніше: щоразу РЕАЛЬНО
                        # видаляє точку (бюджет ЗВІЛЬНЯЄТЬСЯ, не
                        # витрачається), тому не може зациклитись --
                        # список точок ЛИШЕ коротшає, ніколи не росте.
                        merge_i = i
                        fixed_here = False
                        deletions_done = 0
                        while True:
                            if merge_i == 0:
                                break
                            prev_lat, prev_lon, *_ = points[merge_i - 1]
                            cur_lat, cur_lon = points[merge_i][0], points[merge_i][1]
                            room = haversine_m(prev_lat, prev_lon, cur_lat, cur_lon)
                            if needed < room * 0.9:
                                # ІТЕРАТИВНЕ уточнення позиції з
                                # урахуванням РЕАЛЬНОГО рельєфу --
                                # ВИЯВЛЕНО НА ПРАКТИЦІ, справжня причина
                                # "зависання"/коливання на реальній
                                # місцевості: проста формула рахує
                                # позицію, ПРИПУСКАЮЧИ рельєф на новому
                                # місці ТАКИМ САМИМ, як у старій позиції
                                # точки i. Рельєф РЕАЛЬНО відрізняється
                                # -- фактичний кут ПІСЛЯ зсуву НЕ
                                # дорівнює цілі, і наступна ітерація
                                # знову "виправляє", кидаючи точку
                                # туди-сюди БЕЗКІНЕЧНО (підтверджено
                                # прямим тестом на нерівному рельєфі).
                                # Тепер -- до 5 уточнень ТУТ-ТАКИ:
                                # рахуємо РЕАЛЬНИЙ рельєф у кандидаті,
                                # перераховуємо, наскільки ще бракує
                                # відстані, посуваємось ще -- збігається
                                # за 2-3 кроки для звичайного рельєфу.
                                frac = needed / room if room > 0 else 0.0
                                new_lat = cur_lat + (prev_lat - cur_lat) * frac
                                new_lon = cur_lon + (prev_lon - cur_lon) * frac
                                for _refine in range(5):
                                    new_terrain = _terrain_at(new_lat, new_lon)
                                    if new_terrain is None:
                                        break
                                    new_abs_a = new_terrain + agl_a
                                    dist_to_b = haversine_m(new_lat, new_lon, lat_b, lon_b)
                                    achieved_angle = math.degrees(math.atan2(abs(abs_b - new_abs_a), dist_to_b)) if dist_to_b > 0.01 else 0.0
                                    if achieved_angle <= limit_deg:
                                        break  # ця позиція вже й так задовольняє ліміт -- досить
                                    needed_dist_new = abs(abs_b - new_abs_a) / tan_limit
                                    additional_needed = needed_dist_new - dist_to_b
                                    if additional_needed <= 0:
                                        break
                                    remaining_room = haversine_m(prev_lat, prev_lon, new_lat, new_lon)
                                    if remaining_room <= 1.0:
                                        break  # нема куди більше посуватись -- приймаємо як є (залишкове)
                                    extra_frac = min(1.0, additional_needed / remaining_room)
                                    new_lat = new_lat + (prev_lat - new_lat) * extra_frac
                                    new_lon = new_lon + (prev_lon - new_lon) * extra_frac
                                # ЗА ПРЯМОЮ ВКАЗІВКОЮ користувача: НЕ
                                # створюємо крихітне ребро як побічний
                                # ефект уточнення (найдено на практиці
                                # на реальній місії -- 29м ребро
                                # посеред двох ребер по 10-40км). Якщо
                                # уточнена позиція опинилась ДУЖЕ
                                # близько до сусіда, до якого рухались
                                # -- зливаємось із ним ПРЯМО ТУТ, а не
                                # лишаємо крихітний залишок на потім.
                                final_gap = haversine_m(new_lat, new_lon, prev_lat, prev_lon)
                                if final_gap < 50.0:
                                    del points[merge_i]
                                else:
                                    points[merge_i][0] = new_lat
                                    points[merge_i][1] = new_lon
                                fixed_here = True
                                break
                            if merge_i - 1 == 0:
                                break  # сусід -- сам початок маршруту, далі зливати нікуди
                            del points[merge_i - 1]
                            n_raised += 1  # рахуємо злиття як "втручання", для звіту
                            merge_i -= 1
                            deletions_done += 1
                        if not fixed_here:
                            # НАВІТЬ якщо зрештою не вдалось -- проміжні
                            # видалення (якщо були) ВЖЕ змінили points.
                            # ВИЯВЛЕНО НА ПРАКТИЦІ (двічі -- перше
                            # виправлення саме через merge_i завжди
                            # хибно вважало "нічого не змінилось", бо
                            # merge_i ЗМІНЮЄТЬСЯ при видаленні -- тому
                            # тут ОКРЕМИЙ лічильник deletions_done, не
                            # порівняння merge_i): "continue" зі СТАРОЮ
                            # межею range() (порахованою до видалень) --
                            # IndexError. Будь-яка зміна списку МАЄ
                            # примусово перезапускати скан, не "continue".
                            if deletions_done > 0:
                                fixed_any_angle = True
                                clearance_log.append(_snapshot())
                                if on_iteration is not None:
                                    on_iteration(clearance_log[-1], len(clearance_log))
                                break
                            continue
                    else:
                        if i + 2 >= len(points):
                            continue
                        needed = needed_dist - seg_dist
                        if needed <= 0:
                            continue
                        # той самий принцип, симетрично -- зливаємо
                        # НАСТУПНОГО сусіда (i+2), якщо його room не
                        # вистачає, пробуємо з ЩЕ наступним
                        merge_i = i + 1  # індекс ТОЧКИ, яку рухаємо (не ребра)
                        fixed_here = False
                        deletions_done = 0
                        while True:
                            if merge_i + 1 >= len(points):
                                break
                            next_lat, next_lon, *_ = points[merge_i + 1]
                            cur_lat, cur_lon = points[merge_i][0], points[merge_i][1]
                            room = haversine_m(cur_lat, cur_lon, next_lat, next_lon)
                            if needed < room * 0.9:
                                # той самий принцип ітеративного
                                # уточнення й мінімального проміжку, що
                                # й у climbing-гілці вище -- симетрично.
                                frac = needed / room if room > 0 else 0.0
                                new_lat = cur_lat + (next_lat - cur_lat) * frac
                                new_lon = cur_lon + (next_lon - cur_lon) * frac
                                for _refine in range(5):
                                    new_terrain = _terrain_at(new_lat, new_lon)
                                    if new_terrain is None:
                                        break
                                    new_abs_b = new_terrain + agl_b
                                    dist_to_a = haversine_m(lat_a, lon_a, new_lat, new_lon)
                                    achieved_angle = math.degrees(math.atan2(abs(new_abs_b - abs_a), dist_to_a)) if dist_to_a > 0.01 else 0.0
                                    if achieved_angle <= limit_deg:
                                        break
                                    needed_dist_new = abs(new_abs_b - abs_a) / tan_limit
                                    additional_needed = needed_dist_new - dist_to_a
                                    if additional_needed <= 0:
                                        break
                                    remaining_room = haversine_m(new_lat, new_lon, next_lat, next_lon)
                                    if remaining_room <= 1.0:
                                        break
                                    extra_frac = min(1.0, additional_needed / remaining_room)
                                    new_lat = new_lat + (next_lat - new_lat) * extra_frac
                                    new_lon = new_lon + (next_lon - new_lon) * extra_frac
                                final_gap = haversine_m(new_lat, new_lon, next_lat, next_lon)
                                if final_gap < 50.0:
                                    del points[merge_i]
                                else:
                                    points[merge_i][0] = new_lat
                                    points[merge_i][1] = new_lon
                                fixed_here = True
                                break
                            if merge_i + 1 == len(points) - 1:
                                break  # сусід -- сам кінець маршруту, далі зливати нікуди
                            del points[merge_i + 1]
                            n_raised += 1
                            deletions_done += 1
                        if not fixed_here:
                            # ОКРЕМИЙ лічильник -- merge_i у ЦІЙ гілці
                            # НІКОЛИ не змінюється (видаляється СУСІД,
                            # не сама точка), тому порівняння merge_i
                            # ЗАВЖДИ хибно показувало б "нічого не
                            # змінилось" -- РЕАЛЬНА причина того самого
                            # IndexError, що й у climbing-гілці вище.
                            if deletions_done > 0:
                                fixed_any_angle = True
                                clearance_log.append(_snapshot())
                                if on_iteration is not None:
                                    on_iteration(clearance_log[-1], len(clearance_log))
                                break
                            continue

                    fixed_any_angle = True
                    clearance_log.append(_snapshot())
                    if on_iteration is not None:
                        on_iteration(clearance_log[-1], len(clearance_log))
                    break  # перебудовуємо скан з початку -- зсув міг вплинути на сусіднє ребро

                if not fixed_any_angle:
                    return  # ПОВНА фаза завершена -- жодного порушення кута не лишилось (чи нема куди більше зсувати)

        _run_phase1_angles()

        def _max_safe_raise(idx):
            """Максимальне підняття AGL точки idx, що НЕ порушить
            кут ліміту на жодному з двох сусідніх ребер -- ЗА ПРЯМОЮ
            ВКАЗІВКОЮ користувача: винесено ПЕРЕД зовнішнім циклом
            (не всередині), щоб бути доступною одразу у Фазі 2 теж --
            "скрізь, де видно вузьке горлечко на графіку, можна
            підняти висоту, виходячи з кута 2°", не тільки для ребер
            обходу (is_detour)."""
            lat_cur, lon_cur, agl_cur, *_ = points[idx]
            t_cur = _terrain_at(lat_cur, lon_cur)
            if t_cur is None:
                return 0.0
            abs_cur = t_cur + agl_cur
            max_raise = float("inf")
            if idx > 0:
                lat_p, lon_p, agl_p, *_ = points[idx - 1]
                t_p = _terrain_at(lat_p, lon_p)
                if t_p is not None:
                    abs_p = t_p + agl_p
                    d = haversine_m(lat_p, lon_p, lat_cur, lon_cur)
                    if d > 1.0:
                        tan_lim = math.tan(math.radians(climb_angle_limit * 0.99))
                        max_raise = min(max_raise, (abs_p + d * tan_lim) - abs_cur)
            if idx < len(points) - 1:
                lat_n, lon_n, agl_n, *_ = points[idx + 1]
                t_n = _terrain_at(lat_n, lon_n)
                if t_n is not None:
                    abs_n = t_n + agl_n
                    d = haversine_m(lat_cur, lon_cur, lat_n, lon_n)
                    if d > 1.0:
                        tan_lim = math.tan(math.radians(descent_angle_limit * 0.99))
                        max_raise = min(max_raise, (abs_n + d * tan_lim) - abs_cur)
            return max(0.0, max_raise)

        phase3_excess_before = [None]  # контейнер (не просте значення) -- щоб змінювати зсередини вкладеного циклу

        # --- ФАЗА 2 + ФАЗА 3, у ЗОВНІШНЬОМУ циклі, що ПОВЕРТАЄТЬСЯ до
        # Фази 1 щоразу, коли одна з них щось змінює (ЗА ПРЯМОЮ
        # ВКАЗІВКОЮ користувача) -- дроблення довгого ребра (Фаза 3)
        # може створити НОВЕ порушення кута в новій точці, якщо рельєф
        # там різко змінюється -- Фаза 1 має повторно перевірити.
        for _outer_iter in range(MAX_ITER_SAFETY):
            if cancel_event is not None and cancel_event.is_set():
                break
            clearance_log.append(_snapshot())
            if on_iteration is not None:
                on_iteration(clearance_log[-1], len(clearance_log))

            if budget <= 0:
                break

            # --- ФАЗА 2 (ЗА ПРЯМОЮ ВКАЗІВКОЮ користувача): ВСІ ребра,
            # що зараз нижче порогу, виправляються за ОДИН прохід --
            # не тільки найгірше. ВИЯВЛЕНО НА ПРАКТИЦІ: коли рельєф
            # має багато ОКРЕМИХ провалів зі схожою глибиною,
            # виправлення по одному за ітерацію означає, що
            # мінімальний кліренс НЕ змінюється, доки не виправлені
            # майже всі -- 934 ітерації поспіль з незмінним числом на
            # реальному стрес-тесті, хоча прогрес реально йшов. Той
            # самий принцип, що вже застосований у Фазі 0.
            edge_clearances = []
            for i in range(len(points) - 1):
                lat_a, lon_a, agl_a, _is_new_a, is_detour_a = points[i]
                lat_b, lon_b, agl_b, _is_new_b, is_detour_b = points[i + 1]
                ta, tb = _terrain_at(lat_a, lon_a), _terrain_at(lat_b, lon_b)
                if ta is None or tb is None:
                    continue
                abs_a, abs_b = ta + agl_a, tb + agl_b
                seg_dist = haversine_m(lat_a, lon_a, lat_b, lon_b)
                if seg_dist < 1.0:
                    continue
                n_steps = max(1, int(seg_dist // 25.0))
                worst_c, worst_frac = None, None
                for s in range(n_steps + 1):
                    frac = s / n_steps
                    lat, lon = lat_a + (lat_b - lat_a) * frac, lon_a + (lon_b - lon_a) * frac
                    mission_alt = abs_a + (abs_b - abs_a) * frac
                    ground = _terrain_at(lat, lon)
                    if ground is None:
                        continue
                    clearance = mission_alt - ground
                    if worst_c is None or clearance < worst_c:
                        worst_c, worst_frac = clearance, frac
                if worst_c is not None and worst_c < min_clearance_m:
                    edge_clearances.append((worst_c, i, worst_frac))

            edge_clearances.sort(key=lambda t: t[0])  # найгірші перші -- визначає ПРІОРИТЕТ, якщо бюджету не вистачить на всі
            to_fix = edge_clearances[:budget]
            to_fix.sort(key=lambda t: t[1], reverse=True)  # тепер за ІНДЕКСОМ, у зворотному порядку -- вставка з кінця НЕ зсуває індекси ще не оброблених

            fixed_terrain = False
            for worst_c_val, i, worst_frac in to_fix:
                # ЯКЩО найгірша точка ЗБІГАЄТЬСЯ з наявним кінцем ребра
                # (frac дуже близько до 0 чи 1) -- ВИЯВЛЕНО НА ПРАКТИЦІ,
                # реальний нескінченний цикл: вставка "виправлення" ТАМ
                # створює ТОЧНИЙ ДУБЛІКАТ наявної точки (нульова
                # відстань), нічого не покращуючи -- те саме порушення
                # знаходилось знову й знову, десятки разів поспіль,
                # засмічуючи маршрут сотнями точок в ОДНІЙ позиції.
                # Замість вставки -- ПІДНІМАЄМО AGL самого кінця ребра.
                if worst_frac < 0.02 or worst_frac > 0.98:
                    target_idx = i if worst_frac < 0.02 else i + 1
                    needed_agl_raise = (min_clearance_m - worst_c_val) + 1.0
                    points[target_idx][2] += needed_agl_raise
                    n_raised += 1
                    fixed_terrain = True
                    continue
                # ЗА ПРЯМОЮ ВКАЗІВКОЮ користувача: "скрізь, де видно
                # вузьке горлечко, можна підняти висоту, виходячи з
                # кута 2°" -- ПЕРШ ніж вставляти нову точку, пробуємо
                # підняти ОБИДВА кінці ребра в межах безпечного кута.
                # Якщо ОБОХ разом вистачає для потрібного кліренсу --
                # НЕ вставляємо нову точку взагалі, просто піднімаємо
                # наявні. Раніше цей принцип застосовувався ЛИШЕ до
                # ребер обходу (is_detour) -- тепер скрізь.
                safe_a = _max_safe_raise(i)
                needed_raise = (min_clearance_m - worst_c_val) + 1.0
                take_a = min(safe_a, needed_raise)
                if take_a > 0.01:
                    points[i][2] += take_a
                remaining_needed = needed_raise - take_a
                # ПЕРЕРАХОВУЄМО safe_b ПІСЛЯ застосування підняття i --
                # ВИЯВЛЕНО НА ПРАКТИЦІ, реальна помилка (28.22°): якщо
                # рахувати ОБИДВА "безпечні" підняття НАПЕРЕД (до
                # зміни точок), а потім застосовувати послідовно,
                # фактичний результат для другої точки НЕ відповідає
                # розрахунку -- сусід i вже змінився, а розрахунок
                # цього не враховував.
                safe_b = _max_safe_raise(i + 1) if remaining_needed > 0.01 else 0.0
                if remaining_needed > 0.01 and safe_b >= remaining_needed - 0.01:
                    points[i + 1][2] += remaining_needed
                    n_raised += 2
                    fixed_terrain = True
                    continue
                elif take_a > 0.01:
                    # відкочуємо -- часткове підняття БЕЗ повного
                    # покриття потреби НЕ вважається "виправленням",
                    # інакше могло б лишити чужий, непередбачений стан
                    points[i][2] -= take_a
                lat_a, lon_a, *_ = points[i]
                lat_b, lon_b, *_ = points[i + 1]
                fix_lat = lat_a + (lat_b - lat_a) * worst_frac
                fix_lon = lon_a + (lon_b - lon_a) * worst_frac
                points.insert(i + 1, [fix_lat, fix_lon, _zone_agl(fix_lat, fix_lon), True, False])
                budget -= 1
                n_new += 1
                fixed_terrain = True

            if fixed_terrain:
                _run_phase1_angles()
                continue

            # знімок СЕРЕДНЬОГО надлишку кліренсу ОДРАЗУ ПЕРЕД першим
            # запуском Фази 3 -- ЗА ПРЯМОЮ ВКАЗІВКОЮ користувача, щоб
            # потім показати РЕАЛЬНИЙ числовий виграш від прилягання
            # до рельєфу у звіті, а не лише "на око" за графіком.
            if phase3_excess_before[0] is None:
                phase3_excess_before[0] = _average_excess_clearance(route_type.altitude_controlled_m)

            # --- ФАЗА 3 (НОВА, за прямою вказівкою користувача):
            # Фаза 2 нічого не знайшла (кліренс усюди в нормі) -- поки
            # є бюджет, ділимо НАЙДОВШЕ ребро навпіл. Нова точка
            # отримує ПРАВИЛЬНУ висоту (рельєф САМЕ в цій точці + ціль
            # зони) -- НЕ інтерполяцію між далекими кінцями. З кожним
            # діленням профіль природно щільніше прилягає до рельєфу
            # (довгі ребра з лінійною інтерполяцією абсолютної висоти
            # між віддаленими точками "перелітають" над западинами
            # рельєфу, не знижуючись до цільової AGL там, де рельєф
            # провалюється -- ВИЯВЛЕНО НА ПРАКТИЦІ, реальний приклад:
            # 69.6км між двома точками).
            if budget <= 0:
                break

            # ЗА ПРЯМОЮ ВКАЗІВКОЮ користувача (скоротити кількість
            # ітерацій): ділимо ВСІ довгі ребра за один прохід, а не
            # тільки найдовше -- той самий принцип, що вже застосований
            # у Фазах 0 і 2 (там де давно виявлено: виправлення по
            # одному за ітерацію означає сотні зайвих проходів).
            long_edges = []
            for i in range(len(points) - 1):
                lat_a, lon_a, *_ = points[i]
                lat_b, lon_b, *_ = points[i + 1]
                d = haversine_m(lat_a, lon_a, lat_b, lon_b)
                if d > 100.0:  # немає сенсу ділити вже й так короткі ребра
                    long_edges.append((d, i))

            long_edges.sort(key=lambda t: t[0], reverse=True)  # найдовші перші -- пріоритет, якщо бюджету не вистачить на всі
            to_split = long_edges[:budget]
            to_split.sort(key=lambda t: t[1], reverse=True)  # тепер за ІНДЕКСОМ, у зворотному порядку -- вставка з кінця НЕ зсуває індекси ще не оброблених

            fixed_phase3 = False
            for _, i in to_split:
                lat_a, lon_a, agl_a, *_ = points[i]
                lat_b, lon_b, agl_b, *_ = points[i + 1]
                mid_lat = (lat_a + lat_b) / 2.0
                mid_lon = (lon_a + lon_b) / 2.0
                # ІНТЕРПОЛЯЦІЯ АБСОЛЮТНОЇ висоти між кінцями ребра, НЕ
                # проста ціль зони -- ВИЯВЛЕНО НА ПРАКТИЦІ, реальна
                # регресія: якщо ребро -- частина плавного маневру
                # (напр. переходу кордону, 120->224.8 AGL), вставка
                # НОВОЇ точки з "ціллю зони" (тут просто 120) замість
                # інтерпольованого значення (~172) створює РІЗКИЙ,
                # ЩЕ ГІРШИЙ розрив посередині плавного нахилу -- Фаза 3
                # тоді нескінченно підсилює власну помилку далі
                # (кут подвоювався з кожним поділом). Інтерполяція
                # АБСОЛЮТНОЇ висоти зберігає плавність нахилу, а
                # перерахунок через рельєф САМЕ в новій точці все одно
                # дає користь "прилягання" до рельєфу (початкова мета
                # Фази 3), просто без руйнування вже наявної рампи.
                ta, tb = _terrain_at(lat_a, lon_a), _terrain_at(lat_b, lon_b)
                tm = _terrain_at(mid_lat, mid_lon)
                if ta is not None and tb is not None and tm is not None:
                    mid_abs = ((ta + agl_a) + (tb + agl_b)) / 2.0
                    mid_agl = mid_abs - tm
                else:
                    mid_agl = _zone_agl(mid_lat, mid_lon)
                points.insert(i + 1, [mid_lat, mid_lon, mid_agl, True, False])
                budget -= 1
                n_new += 1
                fixed_phase3 = True

            if fixed_phase3:
                _run_phase1_angles()
                continue

            break  # стабільно -- ані рельєф, ані дроблення нічого більше не потребують

        # --- ЗАПАСНИЙ крок (ЗА ПРЯМОЮ ВКАЗІВКОЮ користувача: ПІДНІМАТИ
        # ВЕСЬ маршрут -- НЕДОПУСТИМО, надто грубо й непередбачувано).
        # Якщо після всього циклу (Фази 0-3, підняття, злиття) десь усе
        # ще лишається залишкове порушення кліренсу -- піднімаємо ЛИШЕ
        # ту КОНКРЕТНУ пару точок на цьому ребрі, а не весь маршрут.
        # ВИЯВЛЕНО НА ПРАКТИЦІ: без виклику Фази 1 ПІСЛЯ підняття (на
        # відміну від УСІХ інших виправлень у цьому файлі) різкий,
        # нічим не обмежений стрибок висоти лишався неконтрольованим --
        # "вертикальні лінії" на графіку (фізично неможливий кут).
        # Тепер -- у циклі, з тим самим лічильником спроб на точку, що
        # й скрізь, і Фаза 1 запускається ПІСЛЯ кожного підняття, щоб
        # згладити результат так само, як і решта виправлень.
        # ОДИН прохід, БЕЗ циклу -- ВИЯВЛЕНО НА ПРАКТИЦІ: повторний
        # виклик у циклі сам створював гіршу картину (72.97° замість
        # прийнятного залишку), бо кожен наступний прохід знаходив
        # "нове найгірше" місце вже ПІСЛЯ того, як попередній прохід
        # змістив геометрію -- каскад, що погіршував, а не покращував.
        # Тут -- фінальна, крайня міра: підняти рівно один раз, дати
        # Фазі 1 одну спробу згладити, і ПРИЙНЯТИ результат яким би він
        # не був -- без спроб "довести до ідеалу" рекурсивно.
        worst_c, worst_i, worst_frac = _worst_edge_info()
        if worst_c is not None and worst_c < min_clearance_m:
            deficit = (min_clearance_m - worst_c) + 1.0
            points[worst_i][2] += deficit
            points[worst_i + 1][2] += deficit
            n_raised += 2
            clearance_log.append(_snapshot())
            if on_iteration is not None:
                on_iteration(clearance_log[-1], len(clearance_log))
            _run_phase1_angles()

        # РЕАЛЬНИЙ числовий виграш від Фази 3 (прилягання до рельєфу)
        # -- ЗА ПРЯМОЮ ВКАЗІВКОЮ користувача, для показу у звіті.
        if phase3_excess_before[0] is not None:
            self._altitude_phase3_excess_before = phase3_excess_before[0]
            self._altitude_phase3_excess_after = _average_excess_clearance(route_type.altitude_controlled_m)
        else:
            self._altitude_phase3_excess_before = None
            self._altitude_phase3_excess_after = None

        return n_new, n_raised, clearance_log



    def _compute_altitude_profiles_simple(self, nav_wps, points, original_altitudes_m, crossing_km_value):
        """(відстань_км, висота) для графіка "Було/Стало" -- версія під
        СПРОЩЕНУ логіку (points -- вже готовий робочий список
        [lat, lon, target_agl, is_new] з _run_altitude_optimization,
        не окремий об'єкт результату). Усі висоти -- АБСОЛЮТНІ (AMSL),
        як і скрізь на графіках проєкту. crossing_km_value -- позиція
        (у км від початку маршруту) точки перетину кордону, ВЖЕ
        порахована окремим блоком-маневром (None, якщо перетину немає
        чи маневр геометрично неможливий)."""
        terrain = self.analyzer.terrain

        def _terrain_at(lat, lon):
            try:
                return terrain.get_elevation(lat, lon)
            except SRTMError:
                return None

        dist_before = [0.0]
        for i in range(len(nav_wps) - 1):
            dist_before.append(dist_before[-1] + haversine_m(
                nav_wps[i].lat, nav_wps[i].lon, nav_wps[i + 1].lat, nav_wps[i + 1].lon,
            ))
        dist_before_km = [d / 1000.0 for d in dist_before]
        abs_before = []
        for i, wp in enumerate(nav_wps):
            th = _terrain_at(wp.lat, wp.lon)
            abs_before.append((th or 0.0) + original_altitudes_m[i])

        step_m = 50.0
        terrain_dist_km, terrain_alt = [], []
        for i in range(len(nav_wps) - 1):
            a, b = nav_wps[i], nav_wps[i + 1]
            seg_dist = haversine_m(a.lat, a.lon, b.lat, b.lon)
            n_steps = max(1, int(seg_dist // step_m))
            for s in range(n_steps + (1 if i == len(nav_wps) - 2 else 0)):
                frac = s / n_steps
                lat, lon = a.lat + (b.lat - a.lat) * frac, a.lon + (b.lon - a.lon) * frac
                terrain_dist_km.append((dist_before[i] + seg_dist * frac) / 1000.0)
                terrain_alt.append(_terrain_at(lat, lon))

        # КУМУЛЯТИВНА відстань УЗДОВЖ ПОТОЧНОГО списку points (у
        # порядку самого маршруту), а НЕ "прив'язка до найближчого
        # оригінального вейпоінта" -- ВИЯВЛЕНО НА ПРАКТИЦІ: останній
        # підхід (був тут раніше) давав РІЗКІ стрибки графіка при
        # завершенні, бо не збігався з методом живого оновлення
        # (_update_altitude_step_display використовує САМЕ кумулятивну
        # відстань) -- точка Фази 3 посередині довгого ребра могла
        # "прилипнути" до будь-якого з двох далеких кінців залежно від
        # того, який трохи ближчий, замість своєї реальної позиції
        # вздовж маршруту.
        dist_after_km, abs_after = [], []
        cum = 0.0
        prev = None
        for lat, lon, agl, _is_new, *_ in points:
            if prev is not None:
                cum += haversine_m(prev[0], prev[1], lat, lon)
            prev = (lat, lon)
            th = _terrain_at(lat, lon)
            dist_after_km.append(cum / 1000.0)
            abs_after.append((th or 0.0) + agl)

        crossing_km = crossing_km_value

        return dist_before_km, abs_before, dist_after_km, abs_after, crossing_km, terrain_dist_km, terrain_alt


    def _redraw_altitude_chart(self):
        if not hasattr(self, "altitude_chart_canvas"):
            return
        draw_altitude_before_after(
            self.altitude_chart_canvas,
            getattr(self, "_altitude_dist_before_km", []) or [],
            getattr(self, "_altitude_alt_before", []) or [],
            getattr(self, "_altitude_dist_after_km", []) or [],
            getattr(self, "_altitude_alt_after", []) or [],
            crossing_km=getattr(self, "_altitude_crossing_km", None),
            terrain_dist_km=getattr(self, "_altitude_terrain_dist_km", []) or [],
            terrain_alt=getattr(self, "_altitude_terrain_alt", []) or [],
            dark=self._is_dark_theme(),
            bottleneck_km=getattr(self, "_altitude_bottleneck_km", None),
        )


    def _compute_edge_angle_adapter(self, points):
        """Легкий адаптер -- ЖОДНОЇ власної логіки малювання, лише
        видає ту САМУ структуру даних, що analyzer.
        flight_path_angle_profile() (dist_start/dist_end/angle/from_seq/
        to_seq), щоб напряму перевикористати angle_view.draw_angle_
        profile() -- ТОЧНО той самий графік, що вже є в "Аналіз ->
        Маршрут" (за прямою вказівкою користувача: не винаходити
        власне, повторити наявне)."""
        terrain = self.analyzer.terrain

        # той самий ПЕРСИСТЕНТНИЙ кеш, що й _update_altitude_step_
        # display -- ВИЯВЛЕНО НА ПРАКТИЦІ (остання, третя причина
        # заморожування UI): ця функція викликається на КОЖНІЙ
        # ітерації для графіка кута, і БЕЗ кешу повторно запитувала
        # рельєф для ВСІХ точок щоразу, навіть коли більшість із них
        # не змінювались між ітераціями.
        if not hasattr(self, "_altitude_step_terrain_persistent_cache"):
            self._altitude_step_terrain_persistent_cache = {}
        _persist_cache = self._altitude_step_terrain_persistent_cache

        def _terrain_at(lat, lon):
            key = (round(lat, 7), round(lon, 7))
            if key in _persist_cache:
                return _persist_cache[key]
            try:
                val = terrain.get_elevation(lat, lon)
            except SRTMError:
                val = 0.0
            _persist_cache[key] = val
            return val

        try:
            angle_max = float(self.angle_max_var.get())
        except (ValueError, AttributeError):
            angle_max = 2.0

        segments = []
        cum = 0.0
        for i in range(len(points) - 1):
            lat_a, lon_a, agl_a, *_ = points[i]
            lat_b, lon_b, agl_b, *_ = points[i + 1]
            dist = haversine_m(lat_a, lon_a, lat_b, lon_b)
            abs_a = _terrain_at(lat_a, lon_a) + agl_a
            abs_b = _terrain_at(lat_b, lon_b) + agl_b
            angle = None
            if dist < 1e-6:
                angle = 90.0 if abs_b > abs_a else (-90.0 if abs_b < abs_a else 0.0)
            else:
                angle = math.degrees(math.atan2(abs_b - abs_a, dist))
            segments.append({
                "dist_start": cum, "dist_end": cum + dist, "angle": angle,
                "from_idx": i, "to_idx": i + 1, "from_seq": i + 1, "to_seq": i + 2,
            })
            cum += dist

        class _Adapter:
            pass
        adapter = _Adapter()
        adapter.angle_max = angle_max
        adapter.flight_path_angle_profile = lambda: segments
        return adapter


    def _redraw_altitude_angle_chart(self):
        if not hasattr(self, "altitude_angle_chart_canvas"):
            return
        adapter = getattr(self, "_altitude_angle_adapter", None)
        draw_angle_profile(self.altitude_angle_chart_canvas, adapter, dark=self._is_dark_theme())


    def _save_altitude_optimized_mission(self):
        """Записує РОБОЧИЙ список points (lat, lon, target_agl, is_new)
        як реальні Waypoint -- рельєф САМЕ в цій точці + ціль AGL,
        конвертовано під той самий frame, що й решта місії."""
        points = getattr(self, "_altitude_points", None)
        nav_wps = getattr(self, "_altitude_nav_wps", None)
        if points is None or nav_wps is None:
            return

        terrain = self.analyzer.terrain
        home_amsl = self.analyzer.home_amsl or 0.0
        target_frame = nav_wps[0].frame if nav_wps else 3

        final_wps = []
        for lat, lon, agl, _is_new, *_ in points:
            try:
                terrain_h = terrain.get_elevation(lat, lon)
            except SRTMError:
                terrain_h = 0.0
            abs_alt = terrain_h + agl
            if target_frame in (0, 2):
                alt_for_frame = abs_alt
            elif target_frame == 10:
                alt_for_frame = agl
            else:
                alt_for_frame = abs_alt - home_amsl
            final_wps.append(Waypoint(
                index=0, current=0, frame=target_frame, command=16,
                param1=0.0, param2=0.0, param3=0.0, param4=0.0,
                lat=lat, lon=lon, alt=alt_for_frame, autocontinue=1,
            ))

        path = filedialog.asksaveasfilename(
            title=i18n.t("dlg_save_optimized_mission_title"),
            defaultextension=".waypoints",
            filetypes=[("Waypoints", "*.waypoints")],
        )
        if not path:
            return

        try:
            write_waypoints(path, final_wps)
        except Exception as e:
            messagebox.showerror(i18n.t("msg_weather_title"), i18n.t("msg_save_failed_fmt", error=e))
            return

        messagebox.showinfo(
            i18n.t("msg_weather_title"),
            i18n.t("msg_save_success_fmt", n=len(final_wps), path=path),
        )

    def _refresh_altitude_route_type_list(self, event=None):
        """Перечитує список типів маршруту з диску -- ПЕРЕД кожним
        відкриттям випадаючого списку (не тільки один раз при
        побудові сторінки), той самий принцип, що вже є для профілю
        літака на вкладці "Координати"."""
        try:
            store = route_types.load_route_types(route_types.default_route_types_path())
        except Exception:
            store = route_types.RouteTypeStore()
        names = [rt.name for rt in store.route_types]
        self.alt_route_type_box.configure(values=names)
        current = store.get_current()
        if current and not self.alt_route_type_var.get():
            self.alt_route_type_var.set(current.name)
