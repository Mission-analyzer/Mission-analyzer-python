"""
analysis_page.py — усе, що стосується сторінки "Аналіз" в App:
вкладки (Зліт/Маршрут/Посадка), метео, PDF-звіт, графіки (висота, кут,
глісада), карта маршруту зверху.

AnalysisPageMixin підмішується до класу App (app.py).
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import dataclasses
import math
import urllib.request
import urllib.parse
import datetime
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from analyzer import MissionAnalyzer
from online_tiles import OnlineTileCache
from geo import haversine_m, bearing_deg, lonlat_to_pixel
from elevation_view import draw_elevation_profile, draw_takeoff_profile
from angle_view import draw_angle_profile
from landing_view import draw_landing_approach
from mission_page import MISSION_THEME_DARK, MISSION_THEME_LIGHT
import theme
from map_view import fetch_tiles, bind_pan, MapTooLargeError, render_viewport, draw_single_tile
import populated_areas
import route_types
import restricted_zones
import route_optimizer
import aircraft_profiles
from waypoints import Waypoint, write_waypoints
from srtm import SRTMError
from overview_map import compute_area_tile_bounds, render_area_map, begin_area_render
import i18n


class AnalysisPageMixin:
    """Сторінка "Аналіз": вкладки, метео, звіти, графіки, PDF."""

    def _build_analysis_page(self, content, pad):
        page_analysis = ttk.Frame(content)
        page_analysis.grid(row=0, column=0, sticky="nsew")
        self.pages["analysis"] = page_analysis

        # === страница "Аналіз" ===
        page_analysis = ttk.Frame(content)
        page_analysis.grid(row=0, column=0, sticky="nsew")
        self.pages["analysis"] = page_analysis

        # рядок дати/часу планованого польоту -- вгорі, над вкладками
        flight_row = ttk.Frame(page_analysis)
        flight_row.pack(fill="x", **pad)

        self._reg_i18n(ttk.Label(flight_row), "text", "label_flight_date").pack(side="left")
        _date_dark = self.palette.get("dark", False)
        self._date_btn = tk.Button(
            flight_row,
            textvariable=self.flight_date_var,
            font=("Segoe UI", 9, "bold"),
            bg=("#3a3a3a" if _date_dark else "#DEE3E8"), fg=self.palette["text"],
            bd=2, relief="groove", cursor="hand2", padx=8, pady=3,
            highlightthickness=1,
            highlightbackground=self.palette["border"],
            command=self._pick_date,
        )
        self._date_btn.pack(side="left", padx=(4, 16))
        if not self.flight_date_var.get():
            import datetime
            self.flight_date_var.set(datetime.date.today().strftime("%Y-%m-%d"))

        self._reg_i18n(ttk.Label(flight_row), "text", "label_departure_time").pack(side="left")
        hours = [f"{h:02d}:00" for h in range(24)]
        hour_combo = ttk.Combobox(
            flight_row, textvariable=self.flight_time_var,
            values=hours, width=6, state="readonly",
        )
        hour_combo.pack(side="left", padx=(4, 16))

        self._reg_i18n(ttk.Label(flight_row), "text", "label_arrival_time").pack(side="left")
        self.arrival_time_var = tk.StringVar(value="—")
        ttk.Label(flight_row, textvariable=self.arrival_time_var,
                  font=("Segoe UI", 9, "bold")).pack(side="left", padx=(4, 20))

        weather_btn = ttk.Button(
            flight_row,
            command=self._fetch_meteo,
        )
        weather_btn.pack(side="left")
        self._reg_i18n(weather_btn, "text", "btn_get_weather")

        # плейсхолдер поки не натиснута "Отримати метео" -- вкладки з
        # порожніми/сірими картами й текстом виглядають зламаними, тому
        # ховаємо їх до появи реальних даних
        self.analysis_placeholder = ttk.Frame(page_analysis)
        self.analysis_placeholder.pack(fill="both", expand=True, **pad)
        self._reg_i18n(
            ttk.Label(self.analysis_placeholder, font=("Segoe UI", 11), foreground="#888"),
            "text", "hint_press_get_weather",
        ).pack(expand=True)

        self.notebook = ttk.Notebook(page_analysis)
        # не пакуємо одразу -- з'явиться після _fetch_meteo (див. _show_analysis_tabs)

        self._meteo_canvases = []          # [0]=Зліт(старт), [1]=Глісада(посадка)
        self._meteo_map_images = [[], []]  # тримаємо refs до PhotoImage
        self._meteo_render_params = [None, None]  # кеш параметрів останнього рендеру -- для перемальовки при <Configure>
        self._meteo_render_generation = [0, 0]  # захист від гонки при прогресивній відмальовці -- окремо на кожну з двох карт
        self._last_meteo_raw = None        # кеш сирих даних API -- для ретрансляції тексту погоди без мережі
        self._glide_issues_text = ""       # текст проблем глісади зі звіту (наповнюється при завантаженні місії)
        self._land_weather_text = ""       # текст погоди для посадки (наповнюється по кнопці "Отримати метео")
        self._analysis_outer_canvases = []  # усі "outer" канваси вкладок (make_scroll_tab) -- для перефарбовки при зміні теми
        self._analysis_report_texts = []  # усі tk.Text звітів (make_plain_text) -- те саме
        self._analysis_vbars = []  # усі tk.Scrollbar вкладок (make_scroll_tab) -- те саме

        def make_scroll_tab(tab_title_key: str):
            """Один вертикальний скрол на всю вкладку -- контент іде
            суцільним стовпцем зверху вниз, ніяких вкладених панелей.
            Повертає (tab_frame, inner_frame). tab_title_key -- КЛЮЧ
            i18n (не готовий текст) -- сама реєструє ретрансляцію
            заголовка вкладки Notebook.tab() (інший API, ніж
            .configure(text=...), тому self._reg_i18n сюди не годиться)."""
            tab = ttk.Frame(self.notebook)
            self.notebook.add(tab, text=i18n.t(tab_title_key))
            self._retranslate_callbacks.append(
                lambda _t=tab, _k=tab_title_key: self.notebook.tab(_t, text=i18n.t(_k))
            )

            outer = tk.Canvas(tab, highlightthickness=0, bg=self.palette["bg"])
            self._analysis_outer_canvases.append(outer)
            # tk.Scrollbar (НЕ ttk.Scrollbar!) -- та сама причина, що й
            # для всіх інших повзунків/смуг прокрутки програми: на
            # Windows нативна ttk-тема часто ігнорує кольори, задані
            # через ttk.Style. Кольори -- з ЄДИНОГО джерела
            # (theme.slider_colors) -- той самий вигляд, що й на "Місія".
            sc = theme.slider_colors(self._is_dark_theme())
            vbar = tk.Scrollbar(
                tab, orient="vertical", command=outer.yview,
                bg=sc["bg"], troughcolor=sc["trough"], activebackground=sc["active"],
                highlightthickness=0, bd=0,
            )
            self._analysis_vbars.append(vbar)
            outer.configure(yscrollcommand=vbar.set)
            vbar.pack(side="right", fill="y")
            outer.pack(side="left", fill="both", expand=True)

            inner = ttk.Frame(outer)
            inner_id = outer.create_window((0, 0), window=inner, anchor="nw")

            def _on_inner_configure(_e=None):
                outer.configure(scrollregion=outer.bbox("all"))

            def _on_outer_configure(event):
                if event.width > 20:
                    outer.itemconfig(inner_id, width=event.width)

            inner.bind("<Configure>", _on_inner_configure)
            outer.bind("<Configure>", _on_outer_configure)

            def _on_wheel(event):
                outer.yview_scroll(int(-1 * (event.delta / 120)), "units")

            # <MouseWheel> НЕ спливає від дочірніх віджетів (текст, канваси
            # карт/графіків) до батьківського контейнера -- тому просте
            # outer.bind()/inner.bind() ловить колесо лише над порожнім
            # місцем. Замість цього тримаємо глобальний перехоплювач, який
            # вмикається/вимикається залежно від того, чи курсор всередині
            # цієї вкладки -- так колесо працює над будь-яким її вмістом.
            def _bind_wheel(_e=None):
                tab.bind_all("<MouseWheel>", _on_wheel)

            def _unbind_wheel(_e=None):
                tab.unbind_all("<MouseWheel>")

            tab.bind("<Enter>", _bind_wheel)
            tab.bind("<Leave>", _unbind_wheel)

            return tab, inner

        def add_map_block(parent, map_title_key: str, height: int = 460):
            """Карта -- КВАДРАТНА, вписана у ВИСОТУ видимої області вкладки
            (viewport -- self._analysis_outer_canvases[-1], а не вся
            прокручувана висота). На широкому екрані по боках лишаються
            смуги (не розтягується на всю ширину) -- натомість уся площа
            4х4км видна одразу, без порожніх країв і без зайвого зуму."""
            viewport = self._analysis_outer_canvases[-1]

            outer_frame = tk.Frame(parent, bg=self._map_placeholder_bg())
            outer_frame.pack(fill="x", pady=(0, 8))

            map_box = ttk.LabelFrame(outer_frame, height=height, width=height)
            self._reg_i18n(map_box, "text", map_title_key)
            map_box.pack(anchor="center")
            map_box.pack_propagate(False)

            def _keep_square(event=None, _box=map_box, _outer=outer_frame, _viewport=viewport):
                # ширину й висоту читаємо НАПРЯМУ з відповідних віджетів,
                # а не з event.width -- обробник викликається і з
                # outer_frame, і з viewport (їхні <Configure> дають РІЗНІ
                # event.width для одного й того самого стану), тому єдине
                # надійне джерело -- winfo_width()/winfo_height() у момент
                # виклику, незалежно від того, яка саме подія спрацювала
                w = _outer.winfo_width()
                if w <= 20:
                    return
                avail_h = self._viewport_available_height(_viewport)
                side = min(w, avail_h)
                _box.configure(width=side, height=side)

            outer_frame.bind("<Configure>", _keep_square)
            viewport.bind("<Configure>", _keep_square, add="+")

            canvas = tk.Canvas(map_box, bg=self._map_placeholder_bg(), highlightthickness=0)
            canvas.pack(fill="both", expand=True)
            bind_pan(canvas)
            idx = len(self._meteo_canvases)
            self._meteo_canvases.append(canvas)

            def _on_canvas_configure(event, _idx=idx):
                params = self._meteo_render_params[_idx]
                if params is None:
                    return
                render_area_map(canvas, *params, arrow_length=self._wind_arrow_length_px())

            canvas.bind("<Configure>", _on_canvas_configure)
            return canvas

        def make_plain_text(parent, height: int):
            """Звичайний tk.Text БЕЗ власної смуги прокрутки -- на вкладці
            має бути лише один спільний вертикальний повзунок (від
            make_scroll_tab), а не по одному на кожен текстовий блок."""
            c = MISSION_THEME_DARK if self._is_dark_theme() else MISSION_THEME_LIGHT
            widget = tk.Text(
                parent, wrap="word", font=("Consolas", 9),
                height=height, width=1, relief="solid", borderwidth=1,
                bg=c["table_bg"], fg=c["table_fg"], insertbackground=c["table_fg"],
            )
            theme.make_text_readonly(widget)
            self._analysis_report_texts.append(widget)
            return widget

        # --- «Зліт» = текст (погода), карта, профіль висоти зльоту ---
        takeoff_tab, takeoff_inner = make_scroll_tab("tab_takeoff")
        self.takeoff_weather_text = make_plain_text(takeoff_inner, height=1)
        self.takeoff_weather_text.pack(fill="x", pady=(0, 8))
        add_map_block(takeoff_inner, "box_takeoff_area")

        takeoff_profile_box = ttk.LabelFrame(takeoff_inner)
        self._reg_i18n(takeoff_profile_box, "text", "box_takeoff_profile")
        takeoff_profile_box.pack(fill="x", pady=(0, 8))
        self.takeoff_profile_canvas = tk.Canvas(takeoff_profile_box, bg=self._graph_canvas_bg())
        self.takeoff_profile_canvas.pack(fill="x")
        self.takeoff_profile_canvas.bind("<Configure>", lambda e: self._redraw_takeoff_profile())

        # висота графіка -- від видимої області вкладки (viewport), та сама
        # узгоджена логіка, що й для квадратної карти; ширина лишається
        # fill="x" (на всю ширину вкладки, як на "Місія") -- НЕ квадрат
        _takeoff_viewport = self._analysis_outer_canvases[-1]
        def _fit_takeoff_profile_height(event=None, _v=_takeoff_viewport):
            self.takeoff_profile_canvas.configure(height=self._viewport_available_height(_v))
        _takeoff_viewport.bind("<Configure>", _fit_takeoff_profile_height, add="+")
        _fit_takeoff_profile_height()

        # --- «Траєкторія» = текст (звіти висоти+кута), карта маршруту, профілі (висота+кут) ---
        trajectory_tab, trajectory_inner = make_scroll_tab("tab_route")

        traj_text_box = ttk.LabelFrame(trajectory_inner)
        self._reg_i18n(traj_text_box, "text", "tab_report")
        traj_text_box.pack(fill="x", pady=(0, 8))
        self._reg_i18n(ttk.Label(traj_text_box), "text", "tab_elevation").pack(anchor="w", padx=4)
        self.elev_report_text = make_plain_text(traj_text_box, height=1)
        self.elev_report_text.pack(fill="x", padx=4, pady=(0, 4))
        self._reg_i18n(ttk.Label(traj_text_box), "text", "tab_angle").pack(anchor="w", padx=4)
        self.angle_report_text = make_plain_text(traj_text_box, height=1)
        self.angle_report_text.pack(fill="x", padx=4, pady=(0, 4))

        # карта всього маршруту -- окремий, read-only модуль overview_map.py
        # (без зуму й без можливості редагування -- на відміну від «Місія»,
        # де планується редактор місії; спільна лише "чиста" математика
        # тайлів (compute_tile_bounds/fetch_tiles), сама відмальовка -- ні)
        #
        # Висота -- self._viewport_h (та сама змінна, що й для карти на
        # "Місія", рахується в _apply_viewport_heights під РЕАЛЬНИЙ
        # розмір вікна) -- "екран на карту", а не мала фіксована коробка.
        # Значення тут -- лише початковий запасний варіант (перше
        # відкриття "Аналіз" до першого resize), далі підтримується
        # синхронно з "Місія" в тій самій _apply_viewport_heights.
        initial_viewport_h = getattr(self, "_viewport_h", None) or 700
        traj_map_box = ttk.LabelFrame(trajectory_inner, height=initial_viewport_h)
        self._reg_i18n(traj_map_box, "text", "box_route_top_view")
        traj_map_box.pack(fill="x", pady=(0, 8))
        traj_map_box.pack_propagate(False)
        self._traj_map_box = traj_map_box

        # НЕ квадрат (на відміну від add_map_block вище/нижче -- ті
        # показують фіксовану площу 4x4 км навколо однієї точки, тому
        # квадрат для них і є правильною формою). Тут -- огляд усього
        # маршруту: карта береться ГОТОВОЮ з "Місія" (self.
        # _initial_map_render, той самий зум і центр, що й початковий
        # автопідбір при завантаженні -- див. _load_trajectory_map
        # нижче) -- жодного власного підбору зуму чи мережевого запиту
        # тут немає. Без власного скролбара -- як і решта карт на
        # "Аналіз" (add_map_block вище/нижче), прокрутка тільки одна,
        # зовнішня, для всієї вкладки.
        self.trajectory_map_canvas = tk.Canvas(traj_map_box, bg=self._map_placeholder_bg(), highlightthickness=0, bd=0)
        self.trajectory_map_canvas.pack(fill="both", expand=True)
        bind_pan(self.trajectory_map_canvas)
        self._trajectory_map_params = None  # кеш (tiles, zoom, bounds, center) -- для перемальовки без повторного фетчу
        self._traj_map_resize_after_id = None

        def _on_traj_map_configure(event):
            # canvas змінив розмір (вікно потягнули) -- render_viewport
            # (усередині _load_trajectory_map) сам заново міряє розмір
            # канваса й перемальовує вже ГОТОВІ (з _initial_map_render)
            # тайли під нього -- ніякого мережевого запиту чи повторного
            # підбору зуму тут більше немає, це дешева локальна
            # операція. Дебаунс лишається як проста, необтяжлива
            # обережність -- під час активного розтягування вікна
            # Configure сиплеться десятками подій на секунду.
            if self._traj_map_resize_after_id is not None:
                self.after_cancel(self._traj_map_resize_after_id)
            self._traj_map_resize_after_id = self.after(150, self._load_trajectory_map)

        self._trajectory_map_images = []
        self.trajectory_map_canvas.bind("<Configure>", _on_traj_map_configure)

        # Вітер уздовж маршруту -- ОКРЕМОЇ кнопки більше немає (раніше
        # тестування показало, що це зайва сутність): дані запитуються
        # разом з рештою метео за кнопкою "Отримати метео" вгорі
        # сторінки (_fetch_meteo), карта тут просто оновлюється, коли
        # результат готовий.
        self._route_wind_points = []  # [(lat, lon, arrival_dt, wind_dir, wind_spd, err), ...] -- для перемальовки без мережі


        elev_box = ttk.LabelFrame(trajectory_inner)
        self._reg_i18n(elev_box, "text", "tab_elevation")
        elev_box.pack(fill="x", pady=(0, 8))
        # "екран на кожен графік" -- та сама self._viewport_h, що й
        # карта вище й карта на "Місія" (замість фіксованих 320px)
        self.plot_canvas = tk.Canvas(elev_box, bg=self._graph_canvas_bg(), height=initial_viewport_h)
        self.plot_canvas.pack(fill="x")
        self.plot_canvas.bind("<Configure>", lambda e: self._redraw_plot())

        angle_box = ttk.LabelFrame(trajectory_inner)
        self._reg_i18n(angle_box, "text", "tab_angle")
        angle_box.pack(fill="x", pady=(0, 8))
        self.angle_canvas = tk.Canvas(angle_box, bg=self._graph_canvas_bg(), height=initial_viewport_h)
        self.angle_canvas.pack(fill="x")
        self.angle_canvas.bind("<Configure>", lambda e: self._redraw_angle_plot())

        # --- «Глісада» = звіт+погода, потім карта, потім графік глісади ---
        landing_tab, landing_inner = make_scroll_tab("tab_landing_phase")
        self.glide_report_text = make_plain_text(landing_inner, height=1)
        self.glide_report_text.pack(fill="x", pady=(0, 8))
        add_map_block(landing_inner, "box_landing_area")
        landing_chart_box = ttk.LabelFrame(landing_inner)
        self._reg_i18n(landing_chart_box, "text", "box_glide_chart")
        landing_chart_box.pack(fill="x", pady=(0, 8))
        self.landing_canvas = tk.Canvas(landing_chart_box, bg=self._graph_canvas_bg())
        self.landing_canvas.pack(fill="x")
        self.landing_canvas.bind("<Configure>", lambda e: self._redraw_landing_plot())

        # висота графіка -- від видимої області вкладки (viewport), та сама
        # узгоджена логіка, що й для карти й профілю зльоту; ширина -- fill="x"
        _landing_viewport = self._analysis_outer_canvases[-1]
        def _fit_landing_chart_height(event=None, _v=_landing_viewport):
            self.landing_canvas.configure(height=self._viewport_available_height(_v))
        _landing_viewport.bind("<Configure>", _fit_landing_chart_height, add="+")
        _fit_landing_chart_height()

        # --- «Обліт НП» = карта маршруту + позначки населених пунктів
        # (Overpass API, точки place=*), кольорові за відстанню до
        # найближчого відрізка маршруту, плюс текстовий звіт порушень ---
        populated_tab, populated_inner = make_scroll_tab("tab_populated_areas")

        populated_controls = ttk.Frame(populated_inner)
        populated_controls.pack(fill="x", pady=(0, 8))
        self._reg_i18n(ttk.Label(populated_controls), "text", "lbl_exclude_landing_legs").pack(side="left", padx=(0, 4))
        self.settlements_exclude_legs_var = tk.StringVar(value="2")
        ttk.Entry(populated_controls, textvariable=self.settlements_exclude_legs_var, width=4).pack(
            side="left", padx=(0, 12),
        )
        self.check_settlements_btn = ttk.Button(populated_controls, command=self._check_populated_areas)
        self._reg_i18n(self.check_settlements_btn, "text", "btn_check_settlements")
        self.check_settlements_btn.pack(side="left")

        self.settlements_status_var = tk.StringVar(value="")
        ttk.Label(populated_inner, textvariable=self.settlements_status_var, foreground="#888").pack(
            anchor="w", pady=(0, 4)
        )

        populated_map_box = ttk.LabelFrame(populated_inner, height=initial_viewport_h)
        self._reg_i18n(populated_map_box, "text", "box_populated_areas_map")
        populated_map_box.pack(fill="x", pady=(0, 8))
        populated_map_box.pack_propagate(False)
        self._populated_map_box = populated_map_box

        # той самий принцип, що й "Траєкторія" (trajectory_map_canvas) --
        # карта береться ГОТОВОЮ з _initial_map_render, жодного власного
        # підбору зуму; ЄДИНА відмінність -- зверху домальовуються
        # позначки населених пунктів (self._settlements_cache)
        self.populated_map_canvas = tk.Canvas(populated_map_box, bg=self._map_placeholder_bg(), highlightthickness=0, bd=0)
        self.populated_map_canvas.pack(fill="both", expand=True)
        bind_pan(self.populated_map_canvas)
        self._populated_map_images = []
        self._settlements_cache = None       # list[dict] з fetch_settlements() -- None, доки не натиснута кнопка
        self._settlements_violations = None  # list[dict] з check_route_settlement_distances()
        self._populated_map_resize_after_id = None

        def _on_populated_map_configure(event):
            if self._populated_map_resize_after_id is not None:
                self.after_cancel(self._populated_map_resize_after_id)
            self._populated_map_resize_after_id = self.after(150, self._load_populated_areas_map)

        self.populated_map_canvas.bind("<Configure>", _on_populated_map_configure)

        populated_report_box = ttk.LabelFrame(populated_inner, height=initial_viewport_h)
        self._reg_i18n(populated_report_box, "text", "box_settlement_violations")
        populated_report_box.pack(fill="x", pady=(0, 8))
        populated_report_box.pack_propagate(False)
        self._populated_report_box = populated_report_box

        # ТОЧНА пікселева висота через pack_propagate(False) -- той самий
        # метод, що вже перевірений і працює для карти й для таблиці
        # "Оптимізації" (керується централізовано через _apply_viewport_
        # heights у mission_page.py, разом з рештою карт/таблиць).
        populated_report_inner = tk.Frame(populated_report_box)
        populated_report_inner.pack(fill="both", expand=True, padx=4, pady=4)
        self.settlements_report_text = make_plain_text(populated_report_inner, height=1)
        populated_report_scroll = ttk.Scrollbar(
            populated_report_inner, orient="vertical", command=self.settlements_report_text.yview,
        )
        self.settlements_report_text.configure(yscrollcommand=populated_report_scroll.set)
        self.settlements_report_text.pack(side="left", fill="both", expand=True)
        populated_report_scroll.pack(side="right", fill="y")

        # Текст звіту (аналіз висоти/кута/глісади) і заголовки на самих
        # графіках приходять через i18n.t() з analyzer.py -- це не карта,
        # а звичайний текст і легка локальна перемальовка (без мережі),
        # тому оновлюємо їх при зміні мови. Якщо місію ще не завантажено
        # -- нема чого оновлювати.
        def _retranslate_analysis_content():
            if self.analyzer is None:
                return
            self._distribute_report_text(self._captured_report())
            self._redraw_plot()
            self._redraw_takeoff_profile()
            self._redraw_angle_plot()
            self._redraw_landing_plot()

        self._retranslate_callbacks.append(_retranslate_analysis_content)

        # Текст погоди (Зліт/Посадка) форматується з уже отриманих даних
        # API (self._last_meteo_raw, кешується в _meteo_worker_impl) -- БЕЗ
        # повторного запиту в мережу. Компас-карти 4×4 км теж перемальовуємо
        # локально з self._meteo_render_params (тайли вже завантажені) --
        # там теж є текст, залежний від мови ("Боковий вітер").
        def _retranslate_weather():
            if self._last_meteo_raw:
                texts = [
                    self._format_weather_text(wp, label_key, az, date_str, hour, data, err)
                    for wp, label_key, az, date_str, hour, data, err in self._last_meteo_raw
                ]
                self._set_meteo_texts(*texts)
            for idx, params in enumerate(self._meteo_render_params):
                if params is None:
                    continue
                (lat, lon, zoom, tiles, image_refs,
                 tx_min, tx_max, ty_min, ty_max,
                 flight_az, wind_dir, wind_spd, wind_time) = params
                canvas = self._meteo_canvases[idx]
                render_area_map(
                    canvas, lat, lon, zoom, tiles, image_refs,
                    tx_min, tx_max, ty_min, ty_max,
                    flight_az=flight_az, wind_dir=wind_dir, wind_spd=wind_spd,
                    wind_time=wind_time, arrow_length=self._wind_arrow_length_px(),
                )

        self._retranslate_callbacks.append(_retranslate_weather)


    def _build_analysis_save_button(self, parent: ttk.Frame):
        """Кнопка «Зберегти» на сторінці «Аналіз» -- зберігає весь звіт
        аналізу (Зліт/Траєкторія/Глісада) в PDF."""
        colors = self.palette
        dark = colors.get("dark", False)
        idle_bg = "#3a3a3a" if dark else "#DEE3E8"
        idle_fg = colors["text"]
        idle_active_bg = "#4a4a4a" if dark else "#C9CFD6"
        border = colors["border"]

        self.analysis_save_btn = tk.Button(
            parent, text=i18n.t("btn_save_pdf"),
            bg=idle_bg, fg=idle_fg, activebackground=idle_active_bg, activeforeground=idle_fg,
            font=("Segoe UI", 9, "bold"), bd=2, relief="groove", cursor="hand2",
            padx=16, pady=6,
            highlightthickness=1, highlightbackground=border, highlightcolor=border,
            command=self._save_analysis_pdf,
        )
        self.analysis_save_btn.pack(side="left")
        self._reg_i18n(self.analysis_save_btn, "text", "btn_save_pdf")


    def _save_analysis_pdf(self):
        if self.analyzer is None:
            messagebox.showwarning(i18n.t("msg_no_data_title"), i18n.t("msg_no_data_body"))
            return

        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.units import mm
            from reportlab.lib.utils import ImageReader
            from reportlab.pdfgen import canvas as pdfcanvas
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
        except ImportError:
            messagebox.showerror(
                i18n.t("msg_reportlab_missing_title"),
                i18n.t("msg_reportlab_missing_body"),
            )
            return

        path = filedialog.asksaveasfilename(
            title=i18n.t("dlg_save_report_title"),
            defaultextension=".pdf",
            filetypes=[("PDF", "*.pdf")],
        )
        if not path:
            return

        images = self._capture_analysis_images_for_pdf()

        try:
            self._render_analysis_pdf(path, pdfcanvas, A4, mm, pdfmetrics, TTFont, ImageReader, images)
        except Exception as e:
            messagebox.showerror("PDF", i18n.t("msg_pdf_save_failed_body", error=e))
            return

        messagebox.showinfo("PDF", i18n.t("msg_saved_body", path=path))


    @staticmethod
    def _grab_widget_image(widget):
        """Знімок поточного вигляду віджета (карти/графіка) для вставки в
        PDF -- через PIL.ImageGrab (потребує, щоб віджет реально був на
        екрані, тобто його вкладка мала бути активною на момент виклику)."""
        try:
            from PIL import ImageGrab
        except ImportError:
            return None
        try:
            widget.update_idletasks()
            x = widget.winfo_rootx()
            y = widget.winfo_rooty()
            w = widget.winfo_width()
            h = widget.winfo_height()
            if w <= 1 or h <= 1:
                return None
            return ImageGrab.grab(bbox=(x, y, x + w, y + h))
        except Exception:
            return None


    def _force_light_graphs_for_pdf(self) -> bool:
        """Тимчасово перемикає 4 канваси графіків (профіль зльоту/висоти/
        кута/глісади) на СВІТЛУ палітру перед знімком у PDF -- звіти
        іноді друкують, тёмний фон на папері марно жере чорнило й
        виглядає погано. Мапи (_meteo_canvases/trajectory_map_canvas) не
        чіпаємо -- їх фон і так не залежить від теми (показують реальні
        тайли карти, не колір інтерфейсу). Повертає True, якщо реально
        щось перемикала (тема БУЛА темна) -- щоб знати, чи треба
        повертати назад."""
        if not self._is_dark_theme():
            return False
        for attr in ("plot_canvas", "angle_canvas", "landing_canvas", "takeoff_profile_canvas"):
            canvas = getattr(self, attr, None)
            if canvas is not None and canvas.winfo_exists():
                canvas.configure(bg="white")
        draw_elevation_profile(self.plot_canvas, self.analyzer, dark=False)
        draw_takeoff_profile(self.takeoff_profile_canvas, self.analyzer, n_wps=3, dark=False)
        draw_angle_profile(self.angle_canvas, self.analyzer, dark=False)
        draw_landing_approach(self.landing_canvas, self.analyzer, dark=False)
        self.update()
        return True


    def _restore_graphs_theme_after_pdf(self, was_forced: bool):
        """Повертає графіки назад під поточну (темну) тему після знімку
        для PDF -- інакше екран лишився б "застряглим" на світлих
        графіках посеред темного інтерфейсу."""
        if was_forced:
            self._apply_analysis_theme()


    def _capture_analysis_images_for_pdf(self) -> dict:
        """Обгортка над _capture_analysis_images(): якщо зараз темна тема,
        тимчасово перемикає ЛИШЕ графіки на світлу палітру перед знімком
        (PDF-звіти іноді друкують -- темний фон на папері непрактичний),
        і повертає їх назад одразу після, незалежно від того, вдався
        знімок чи ні (finally)."""
        was_forced = self._force_light_graphs_for_pdf()
        try:
            return self._capture_analysis_images()
        finally:
            self._restore_graphs_theme_after_pdf(was_forced)


    def _capture_analysis_images(self) -> dict:
        """Проходить по всіх трьох вкладках «Аналіз», роблячи знімки карт
        і графіків -- ImageGrab бачить лише те, що реально на екрані,
        тому доводиться по черзі перемикати вкладки. Повертає вихідну
        вкладку/видимість плейсхолдера як були."""
        images: dict = {}
        was_visible = self.notebook.winfo_ismapped() if hasattr(self, "notebook") else False
        prev_tab = None
        if was_visible:
            try:
                prev_tab = self.notebook.index(self.notebook.select())
            except tk.TclError:
                prev_tab = None

        self._show_analysis_tabs()

        try:
            self.notebook.select(0)  # Зліт
            self.update()
            if self._meteo_canvases:
                images["takeoff_map"] = self._grab_widget_image(self._meteo_canvases[0])
            images["takeoff_profile"] = self._grab_widget_image(self.takeoff_profile_canvas)

            self.notebook.select(1)  # Маршрут
            self.update()
            images["route_map"] = self._grab_widget_image(self.trajectory_map_canvas)
            images["elevation"] = self._grab_widget_image(self.plot_canvas)
            images["angle"] = self._grab_widget_image(self.angle_canvas)

            self.notebook.select(2)  # Посадка
            self.update()
            if len(self._meteo_canvases) > 1:
                images["landing_map"] = self._grab_widget_image(self._meteo_canvases[1])
            images["landing_profile"] = self._grab_widget_image(self.landing_canvas)
        finally:
            if was_visible and prev_tab is not None:
                self.notebook.select(prev_tab)
            else:
                self._hide_analysis_tabs()

        return images


    def _render_analysis_pdf(self, path, pdfcanvas, A4, mm, pdfmetrics, TTFont, ImageReader, images: dict):
        """Формує PDF зі звітом: Зліт (погода+карта+профіль), Маршрут
        (звіти+карта маршруту+графіки висоти/кута), Посадка
        (проблеми+погода+карта+графік глісади). Карти/графіки вставляються
        як знімки екрана (PIL.ImageGrab), зроблені перед викликом цього
        методу -- сам pdfcanvas не має доступу до вікна програми."""
        # шрифт з підтримкою кирилиці, якщо є в системі; інакше -- вбудований
        font_name = "Helvetica"
        for candidate in ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/calibri.ttf"):
            if os.path.exists(candidate):
                try:
                    pdfmetrics.registerFont(TTFont("BodyFont", candidate))
                    font_name = "BodyFont"
                    break
                except Exception:
                    pass

        c = pdfcanvas.Canvas(path, pagesize=A4)
        page_w, page_h = A4
        margin = 18 * mm
        y = page_h - margin
        line_h = 4.6 * mm

        def new_page():
            nonlocal y
            c.showPage()
            c.setFont(font_name, 9)
            y = page_h - margin

        def write_title(text, size=14):
            nonlocal y
            c.setFont(font_name, size)
            c.drawString(margin, y, text)
            y -= size * 0.6 * mm + line_h

        def write_heading(text):
            nonlocal y
            if y < margin + 20 * mm:
                new_page()
            c.setFont(font_name, 11)
            c.drawString(margin, y, text)
            y -= line_h * 1.3

        def write_body(text: str):
            nonlocal y
            c.setFont(font_name, 9)
            for raw_line in text.split("\n"):
                # проста обгортка по ширині сторінки
                max_chars = 100
                line = raw_line if raw_line else " "
                while len(line) > max_chars:
                    cut = line.rfind(" ", 0, max_chars)
                    cut = cut if cut > 0 else max_chars
                    if y < margin:
                        new_page()
                    c.drawString(margin, y, line[:cut])
                    y -= line_h
                    line = line[cut:].lstrip()
                if y < margin:
                    new_page()
                c.drawString(margin, y, line)
                y -= line_h

        def write_image(pil_img, max_w_mm=170, max_h_mm=110):
            nonlocal y
            if pil_img is None:
                return
            iw, ih = pil_img.size
            if iw <= 1 or ih <= 1:
                return
            max_w = max_w_mm * mm
            max_h = max_h_mm * mm
            scale = min(max_w / iw, max_h / ih, 1.0)
            draw_w = iw * scale
            draw_h = ih * scale
            if y - draw_h < margin:
                new_page()
            c.drawImage(
                ImageReader(pil_img), margin, y - draw_h,
                width=draw_w, height=draw_h,
                preserveAspectRatio=True, mask="auto",
            )
            y -= draw_h + line_h

        c.setFont(font_name, 9)
        write_title(i18n.t("pdf_title"))
        write_body(i18n.t("pdf_mission_file_fmt", file=self.file_var.get() or "—"))
        write_body(i18n.t(
            "pdf_flight_info_fmt",
            date=self.flight_date_var.get() or "—",
            time=self.flight_time_var.get() or "—",
            arrival=self.arrival_time_var.get() if hasattr(self, "arrival_time_var") else "—",
        ))
        y -= line_h

        write_heading(i18n.t("pdf_heading_takeoff_weather"))
        write_body(self._get_text(self.takeoff_weather_text) or i18n.t("msg_no_data_press_weather"))
        write_image(images.get("takeoff_map"))
        write_image(images.get("takeoff_profile"))
        y -= line_h

        write_heading(i18n.t("pdf_heading_route_map"))
        write_image(images.get("route_map"))

        write_heading(i18n.t("pdf_heading_route_elevation"))
        write_body(self._get_text(self.elev_report_text) or i18n.t("pdf_no_remarks"))
        write_image(images.get("elevation"))
        y -= line_h

        write_heading(i18n.t("pdf_heading_route_angle"))
        write_body(self._get_text(self.angle_report_text) or i18n.t("pdf_no_remarks"))
        write_image(images.get("angle"))
        y -= line_h

        write_heading(i18n.t("pdf_heading_landing"))
        write_body(self._get_text(self.glide_report_text) or i18n.t("pdf_no_remarks"))
        write_image(images.get("landing_map"))
        write_image(images.get("landing_profile"))

        c.save()


    @staticmethod
    def _get_text(widget) -> str:
        return widget.get("1.0", "end").rstrip()


    def _pick_date(self):
        """Модальний календар для вибору дати польоту."""
        import datetime
        dlg = tk.Toplevel(self)
        dlg.title(i18n.t("dlg_pick_date_title"))
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()
        dlg.configure(bg=self.palette["bg"])

        # поточна вибрана дата
        try:
            cur = datetime.date.fromisoformat(self.flight_date_var.get())
        except ValueError:
            cur = datetime.date.today()

        state = {"year": cur.year, "month": cur.month}

        header = ttk.Frame(dlg)
        header.pack(fill="x", padx=8, pady=(8, 0))
        month_lbl = ttk.Label(header, font=("Segoe UI", 10, "bold"), width=16, anchor="center")
        month_lbl.pack(side="left", expand=True)

        cal_frame = ttk.Frame(dlg)
        cal_frame.pack(padx=8, pady=4)

        day_btns: list[tk.Button] = []
        selected_cell: dict = {"btn": None}

        DAY_NAMES = i18n.t("calendar_day_names").split(",")
        for col, dn in enumerate(DAY_NAMES):
            ttk.Label(cal_frame, text=dn, width=4, anchor="center",
                      font=("Segoe UI", 8, "bold")).grid(row=0, column=col, padx=1, pady=2)

        def render(year, month):
            for b in day_btns:
                b.destroy()
            day_btns.clear()

            import calendar
            month_lbl.config(text=f"{calendar.month_name[month]} {year}")
            first_wd, n_days = calendar.monthrange(year, month)
            today = datetime.date.today()

            cell = 0
            for day in range(1, n_days + 1):
                wd = (first_wd + day - 1) % 7
                row = cell // 7 + 1
                col = wd
                d = datetime.date(year, month, day)
                is_sel = (d == cur)
                is_past = (d < today)

                bg = self.palette["blue"] if is_sel else (
                    ("#3a3a3a" if self.palette.get("dark", False) else "#E8ECEF") if is_past
                    else ("#3a3a3a" if self.palette.get("dark", False) else "#DEE3E8")
                )
                fg = self.palette["text_light"] if is_sel else (
                    "#AAAAAA" if is_past else self.palette["text"]
                )

                def pick(date=d):
                    nonlocal cur
                    cur = date
                    self.flight_date_var.set(date.strftime("%Y-%m-%d"))
                    self._save_settings()
                    self._compute_arrival_time()
                    dlg.destroy()

                btn = tk.Button(
                    cal_frame, text=str(day), width=4,
                    bg=bg, fg=fg, relief="flat", bd=0,
                    font=("Segoe UI", 9),
                    activebackground=self.palette["blue"],
                    activeforeground=self.palette["text_light"],
                    cursor="hand2",
                    command=pick,
                    state="disabled" if is_past else "normal",
                )
                btn.grid(row=row, column=col, padx=1, pady=1)
                day_btns.append(btn)
                if wd == 6:
                    cell += 1
                cell += 1

        def prev_month():
            m, y = state["month"] - 1, state["year"]
            if m < 1:
                m, y = 12, y - 1
            state["month"], state["year"] = m, y
            render(y, m)

        def next_month():
            m, y = state["month"] + 1, state["year"]
            if m > 12:
                m, y = 1, y + 1
            state["month"], state["year"] = m, y
            render(y, m)

        ttk.Button(header, text="◀", width=3, command=prev_month).pack(side="left")
        ttk.Button(header, text="▶", width=3, command=next_month).pack(side="right")

        render(state["year"], state["month"])

        # по центру окна
        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3
        dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.wait_window(dlg)


    def _get_cruise_speed_ms(self) -> float | None:
        """Крейсерська швидкість (м/с) з ПОТОЧНОГО профілю літака --
        ЄДИНЕ джерело цього значення в програмі (окреме поле в
        "Конфігурація" видалено -- воно лише дублювало те саме число,
        яке легко забути тримати синхронним). Повертає None, якщо
        профілю немає, він не типу "Літак", чи швидкість не задана --
        виклик МАЄ явно обробити цей випадок, не вигадувати запасне
        значення."""
        try:
            store = aircraft_profiles.load_profiles(aircraft_profiles.default_profiles_path())
        except Exception:
            return None
        profile = store.get_current()
        if profile is None or profile.drone_type != "plane" or not profile.airspeed_cruise_ms:
            return None
        return profile.airspeed_cruise_ms


    def _compute_arrival_time(self):
        """Обчислює час прибуття = час вильоту + час польоту (відстань/крейсерська швидкість)."""
        if not hasattr(self, "arrival_time_var"):
            return
        if self.analyzer is None:
            self.arrival_time_var.set("—")
            return
        speed = self._get_cruise_speed_ms()
        if speed is None:
            self.arrival_time_var.set(i18n.t("hint_no_profile_speed"))
            return

        total_dist = sum(
            haversine_m(
                self.analyzer.nav_wps[i].lat, self.analyzer.nav_wps[i].lon,
                self.analyzer.nav_wps[i + 1].lat, self.analyzer.nav_wps[i + 1].lon,
            )
            for i in range(len(self.analyzer.nav_wps) - 1)
        )
        flight_seconds = total_dist / speed

        import datetime
        date_str = self.flight_date_var.get().strip()
        time_str = self.flight_time_var.get().strip()
        try:
            departure = datetime.datetime.fromisoformat(f"{date_str}T{time_str}:00")
        except ValueError:
            self.arrival_time_var.set("—")
            return

        arrival = departure + datetime.timedelta(seconds=flight_seconds)
        mins = int(flight_seconds // 60)
        self.arrival_time_var.set(
            i18n.t("label_minutes_suffix_fmt", time=arrival.strftime("%H:%M"), mins=mins)
        )


    def _hide_analysis_tabs(self):
        """Повертає плейсхолдер замість вкладок -- викликається при
        завантаженні нової місії, доки для неї ще не отримано погоду."""
        if hasattr(self, "notebook"):
            self.notebook.pack_forget()
        if hasattr(self, "analysis_placeholder"):
            pad = {"padx": 6, "pady": 4}
            self.analysis_placeholder.pack(fill="both", expand=True, **pad)


    def _show_analysis_tabs(self):
        """Ховає плейсхолдер і показує вкладки «Аналіз» -- викликається,
        коли користувач натискає «Отримати метео» (до того порожні/сірі
        вкладки виглядали б зламаними)."""
        self._ensure_analysis_built()
        if hasattr(self, "analysis_placeholder"):
            self.analysis_placeholder.pack_forget()
        pad = {"padx": 6, "pady": 4}
        self.notebook.pack(fill="both", expand=True, **pad)


    def _fetch_meteo(self):
        """Запит метеоданих з Open-Meteo для координат старту та посадки."""
        if self.analyzer is None:
            messagebox.showwarning(i18n.t("msg_weather_title"), i18n.t("msg_load_mission_first_body"))
            return

        date_str = self.flight_date_var.get().strip()
        time_str = self.flight_time_var.get().strip()
        if not date_str:
            messagebox.showwarning(i18n.t("msg_weather_title"), i18n.t("msg_set_flight_date_body"))
            return
        if not time_str:
            messagebox.showwarning(i18n.t("msg_weather_title"), i18n.t("msg_set_departure_time_body"))
            return

        # точка старту -- перша nav-точка, точка посадки -- остання
        wps = self.analyzer.nav_wps
        if not wps:
            messagebox.showwarning(i18n.t("msg_weather_title"), i18n.t("msg_no_route_points_body"))
            return

        start_wp = wps[0]
        land_wp  = wps[-1]

        self._show_analysis_tabs()
        self._set_meteo_texts(i18n.t("status_loading_weather"), i18n.t("status_loading_weather"))
        self.notebook.select(0)  # перемикаємось на вкладку «Зліт»
        threading.Thread(
            target=self._meteo_worker,
            args=(date_str, time_str, start_wp, land_wp),
            daemon=True,
        ).start()

        # вітер уздовж усього маршруту -- ОКРЕМИЙ потік (інший набір
        # точок/годин, ніж старт+посадка вище), запускається тією ж
        # кнопкою "Отримати метео", без окремої кнопки для користувача
        route_wind_points = self._compute_wind_sample_points()
        if route_wind_points:
            threading.Thread(
                target=self._route_wind_worker, args=(route_wind_points,), daemon=True,
            ).start()


    def _meteo_worker(self, date_str, time_str, start_wp, land_wp):
        """Обгортка: ловить БУДЬ-ЯКУ помилку, щоб вона не падала в консоль, а
        показувалась користувачу в обох текстових блоках."""
        try:
            self._meteo_worker_impl(date_str, time_str, start_wp, land_wp)
        except Exception as e:
            err = i18n.t("msg_weather_fetch_error_fmt", error=e)
            self.after(0, lambda: self._set_meteo_texts(err, err))


    def _meteo_worker_impl(self, date_str, time_str, start_wp, land_wp):
        import urllib.request, json

        hour = 12
        try:
            hour = int(time_str.split(":")[0])
        except Exception:
            pass

        def fetch_point(lat, lon):
            url = (
                f"https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat:.5f}&longitude={lon:.5f}"
                f"&hourly=windspeed_10m,winddirection_10m,temperature_2m"
                f"&daily=sunrise,sunset,windspeed_10m_max,winddirection_10m_dominant"
                f",temperature_2m_max,temperature_2m_min"
                f"&timezone=auto"
                f"&start_date={date_str}&end_date={date_str}"
            )
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "MissionAnalyzer/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read()), None
            except Exception as e:
                return None, str(e)

        # азимут полёту: старт→наступна точка і передостання→посадка
        wps = self.analyzer.nav_wps
        az_start = bearing_deg(start_wp.lat, start_wp.lon,
                               wps[1].lat, wps[1].lon) if len(wps) > 1 else 0.0
        az_land  = bearing_deg(wps[-2].lat, wps[-2].lon,
                               land_wp.lat, land_wp.lon) if len(wps) > 1 else 0.0

        map_data = []       # [(wind_dir, wind_spd, flight_az, label, error), ...]
        texts = []           # текст окремо для «Зліт» і «Глісада»
        raw_cache = []       # для перемальовки тексту при зміні мови -- БЕЗ мережі

        for wp, label_key, az in [
            (start_wp, "label_start_takeoff", az_start),
            (land_wp,  "tab_landing_phase",   az_land),
        ]:
            data, err = fetch_point(wp.lat, wp.lon)
            raw_cache.append((wp, label_key, az, date_str, hour, data, err))
            texts.append(self._format_weather_text(wp, label_key, az, date_str, hour, data, err))

            wind_dir, wind_spd = None, None
            if not err:
                h = data.get("hourly", {})
                times = h.get("time", [])
                target = f"{date_str}T{hour:02d}:00"
                idx = next((i for i, t in enumerate(times) if t == target), None)
                if idx is not None:
                    wind_spd = h.get("windspeed_10m",  [None] * (idx + 1))[idx]
                    wind_dir = h.get("winddirection_10m", [None] * (idx + 1))[idx]

            wind_time_str = f"{hour:02d}:00"
            map_data.append((wind_dir, wind_spd, az, i18n.t(label_key), err, wind_time_str))

        self._last_meteo_raw = raw_cache  # кеш для ретрансляції тексту без мережі
        self.after(0, lambda: self._on_meteo_ready(texts, map_data))


    def _format_weather_text(self, wp, label_key, az, date_str, hour, data, err) -> str:
        """
        Форматує текстовий звіт погоди в точці (Зліт/Посадка) з уже
        отриманих даних API (data/err) під ПОТОЧНУ мову. Винесено окремо
        від _meteo_worker_impl, щоб при зміні мови можна було
        переформатувати текст без повторного походу в мережу -- дані
        (data/err) вже кешуються в self._last_meteo_raw.
        """
        label = i18n.t(label_key)
        lines = [f" {label}  ({wp.lat:.5f}, {wp.lon:.5f})", "=" * 44]

        if err:
            lines.append(i18n.t("weather_error_line_fmt", error=err))
            return "\n".join(lines)

        d = data.get("daily", {})
        if d:
            lines.append(i18n.t("weather_date_line_fmt", date=date_str))
            lines.append(i18n.t("weather_sunrise_line_fmt", time=(d.get("sunrise") or ["?"])[0]))
            lines.append(i18n.t("weather_sunset_line_fmt", time=(d.get("sunset") or ["?"])[0]))
            t_max = (d.get("temperature_2m_max") or [None])[0]
            t_min = (d.get("temperature_2m_min") or [None])[0]
            lines.append(i18n.t("weather_temp_minmax_line_fmt", t_min=t_min, t_max=t_max))
            ws_max = (d.get("windspeed_10m_max") or [None])[0]
            wd_dom = (d.get("winddirection_10m_dominant") or [None])[0]
            lines.append(i18n.t("weather_wind_max_line_fmt", speed=ws_max, dir=wd_dom))

        h = data.get("hourly", {})
        times = h.get("time", [])
        target = f"{date_str}T{hour:02d}:00"
        idx = next((i for i, t in enumerate(times) if t == target), None)
        if idx is not None:
            wind_spd = h.get("windspeed_10m",  [None] * (idx + 1))[idx]
            wind_dir = h.get("winddirection_10m", [None] * (idx + 1))[idx]
            tmp      = h.get("temperature_2m", [None] * (idx + 1))[idx]
            lines.append(i18n.t("weather_at_time_header_fmt", time=target))
            lines.append(i18n.t("weather_wind_speed_line_fmt", speed=wind_spd))
            lines.append(i18n.t("weather_wind_dir_line_fmt", dir=wind_dir))
            lines.append(i18n.t("weather_temp_line_fmt", temp=tmp))

            if wind_dir is not None and wind_spd is not None:
                diff = abs((wind_dir - az + 360) % 360)
                if diff > 180:
                    diff = 360 - diff
                cross = abs(90 - abs(diff - 90))
                head_on = diff < 90
                strength = i18n.t("weather_strong_word") if cross > 30 else i18n.t("weather_normal_word")
                lines.append(i18n.t("weather_crosswind_line_fmt", cross=cross, strength=strength))
                headwind_val = i18n.t("weather_headwind_yes") if head_on else i18n.t("weather_headwind_no")
                lines.append(i18n.t("weather_headwind_line_fmt", value=headwind_val))
        else:
            lines.append(i18n.t("weather_hourly_unavailable_fmt", time=target))

        return "\n".join(lines)


    def _on_meteo_ready(self, texts: list, map_data: list):
        self._set_meteo_texts(*texts)
        # запускаємо завантаження тайлів для кожної зони в окремих потоках
        for i, (canvas, item) in enumerate(zip(self._meteo_canvases, map_data)):
            wind_dir, wind_spd, flight_az, label, err, wind_time = item
            if err:
                canvas.delete("all")
                canvas.create_text(
                    canvas.winfo_width() // 2 or 150, canvas.winfo_height() // 2 or 150,
                    text=i18n.t("msg_map_unavailable_fmt", error=err), fill="#FF6666",
                    font=("Segoe UI", 9), justify="center",
                )
                continue
            wp = (self.analyzer.nav_wps[0] if i == 0 else self.analyzer.nav_wps[-1])

            # Підбір зуму (чиста математика, БЕЗ мережі) і власне
            # begin_area_render (малює в Tkinter, тому МАЄ виконуватись у
            # ГОЛОВНОМУ потоці) -- зроблено ТУТ, синхронно, ДО старту
            # фонового потоку з мережею -- та сама схема, що й на "Місія"
            # (render_map() робить синхронну частину сама, worker() --
            # лише мережу). Якщо зробити навпаки (плейсхолдери через
            # self.after(0, ...) З фонового потоку) -- виникає гонка:
            # якщо якийсь тайл вже лежить у диск-кеші й відповідає майже
            # миттєво, його колбек міг би виконатись РАНІШЕ, ніж
            # begin_area_render встигне реально відпрацювати (self.after
            # лише СТАВИТЬ колбек у чергу, не виконує одразу) -- і такий
            # тайл тихо губився б назавжди, без жодної спроби повтору
            # (саме це й трапилось на практиці -- 1 плейсхолдер із 133
            # так і не замінився).
            self._meteo_render_generation[i] += 1
            my_generation = self._meteo_render_generation[i]

            zoom = 16
            bounds = None
            # цільовий розмір -- реальний розмір canvas ЗАРАЗ (після
            # <Configure>, layout вже усталений на момент виклику).
            # Мінімум 300px -- запасний варіант, якщо canvas ще не
            # отримав реальний розмір (напр. вкладка ще не показана).
            target_px = max(canvas.winfo_width(), canvas.winfo_height(), 300)

            # спочатку пробуємо з базового zoom=16 РОСТИ вгору, поки
            # покриття 4х4км (у пікселях) не досягне розміру canvas --
            # інакше на широких моніторах тайли покривають лише центр,
            # а решта canvas лишається порожньою (немає жодного тайла
            # за межами обчисленого bounds, просто фон canvas).
            best_bounds, best_zoom = None, None
            for z in range(zoom, 20):
                try:
                    b = compute_area_tile_bounds(wp.lat, wp.lon, z, max_tiles=550)
                except MapTooLargeError:
                    break  # далі буде ще більше тайлів -- зупиняємось, лишаємо останній вдалий
                tiles_x = b[1] - b[0] + 1
                covered_px = tiles_x * 256
                best_bounds, best_zoom = b, z
                if covered_px >= target_px:
                    break

            if best_bounds is not None:
                bounds, zoom = best_bounds, best_zoom
            else:
                # запасний варіант -- як і раніше, спускаємось від 16 вниз
                for z in range(zoom, 0, -1):
                    try:
                        bounds = compute_area_tile_bounds(wp.lat, wp.lon, z)
                        zoom = z
                        break
                    except MapTooLargeError:
                        continue
            if bounds is None:
                canvas.delete("all")
                canvas.create_text(
                    canvas.winfo_width() // 2 or 150, canvas.winfo_height() // 2 or 150,
                    text=i18n.t("msg_render_error_fmt", error="MapTooLargeError"), fill="#FF6666",
                    font=("Segoe UI", 9), justify="center",
                )
                continue

            tx_min, tx_max, ty_min, ty_max, _ = bounds
            image_refs = self._meteo_map_images[i]
            sc = self._map_placeholder_bg()
            screen_origin_gx, screen_origin_gy = begin_area_render(
                canvas, wp.lat, wp.lon, zoom, tx_min, tx_max, ty_min, ty_max, image_refs,
                placeholder_bg=sc, placeholder_outline=sc,
                flight_az=flight_az, wind_dir=wind_dir, wind_spd=wind_spd,
                wind_time=wind_time, arrow_length=self._wind_arrow_length_px(),
            )

            threading.Thread(
                target=self._load_area_tiles,
                args=(
                    canvas, i, wp.lat, wp.lon, flight_az, wind_dir, wind_spd, wind_time,
                    zoom, tx_min, tx_max, ty_min, ty_max,
                    screen_origin_gx, screen_origin_gy, my_generation,
                ),
                daemon=True,
            ).start()


    def _load_area_tiles(self, canvas: tk.Canvas, idx: int,
                         lat: float, lon: float,
                         flight_az: float | None,
                         wind_dir: float | None, wind_spd: float | None, wind_time: str | None,
                         zoom: int, tx_min: int, tx_max: int, ty_min: int, ty_max: int,
                         screen_origin_gx: float, screen_origin_gy: float,
                         my_generation: int):
        """Фоновий поток -- ЛИШЕ мережа (fetch_tiles) і завершення. Підбір
        зуму й begin_area_render (плейсхолдери + компас/стрілки) уже
        виконані СИНХРОННО в головному потоці, у _on_meteo_ready, ДО
        старту цього потоку -- жодної гонки між ними тепер немає:
        screen_origin_gx/gy передаються сюди вже ГОТОВИМИ, замість того,
        щоб чекати на асинхронний self.after(0, ...) з фонового потоку.

        self._meteo_render_generation[idx] -- захист від гонки, той
        самий принцип, що й self._map_render_generation на "Місія":
        якщо цю функцію викликали повторно (напр. користувач швидко
        переключив місію) ПОКИ попередня загрузка ще йде, старий,
        вже неактуальний результат не повинен домалюватись поверх
        нового.
        """
        try:
            if self.tile_cache is None:
                # той самий лінивий конструктор, що і в render_map() -- якщо
                # користувач ще жодного разу не відкривав карту на "Місія"
                disk_cache = self.tilecache_var.get().strip() or None
                self.tile_cache = OnlineTileCache(provider=self.provider_key, disk_cache_dir=disk_cache)

            image_refs = self._meteo_map_images[idx]
            tiles: dict = {}

            def on_tile_ready(tx, ty, data):
                tiles[(tx, ty)] = data
                self.after(
                    0,
                    lambda: self._on_area_tile_ready(
                        canvas, idx, tx, ty, data, my_generation, screen_origin_gx, screen_origin_gy,
                    ),
                )

            fetch_tiles(
                self.tile_cache, tx_min, tx_max, ty_min, ty_max, zoom,
                tile_ready_cb=on_tile_ready,
            )

            def do_finish():
                if my_generation != self._meteo_render_generation[idx]:
                    return
                # зберігаємо параметри -- знадобляться для перемальовки,
                # коли канвас (можливо, прихованої зараз вкладки) реально
                # отримає свій розмір і викличе <Configure>, чи при зміні
                # мови (перемальовка вже готового, без повторної мережі)
                self._meteo_render_params[idx] = (
                    lat, lon, zoom, tiles, image_refs,
                    tx_min, tx_max, ty_min, ty_max,
                    flight_az, wind_dir, wind_spd, wind_time,
                )

            self.after(0, do_finish)

        except Exception as e:
            def show_error():
                if my_generation != self._meteo_render_generation[idx]:
                    return
                canvas.delete("all")
                canvas.create_text(
                    canvas.winfo_width() // 2 or 150, canvas.winfo_height() // 2 or 150,
                    text=i18n.t("msg_render_error_fmt", error=e),
                    fill="#FF6666", font=("Segoe UI", 9), justify="center",
                )
            self.after(0, show_error)


    def _on_area_tile_ready(self, canvas, idx, tx, ty, data, generation, screen_origin_gx, screen_origin_gy):
        """Домальовує ОДИН тайл на area-карті (Зліт/Посадка) одразу по
        готовності -- частина прогресивної відмальовки (_load_area_tiles).
        Викликається окремо на кожен тайл, з головного потоку.

        screen_origin_gx/gy передаються НАПРЯМУ (не через спільний
        мутабельний render_state, як було раніше) -- вони обчислені й
        готові ще ДО старту фонового потоку (_on_meteo_ready), тому тут
        гонки бути не може: жодного стану, який міг би "ще не встигнути"
        заповнитись."""
        if generation != self._meteo_render_generation[idx]:
            return  # застарілий рендер -- відкидаємо мовчки
        draw_single_tile(
            canvas, self._meteo_map_images[idx], tx, ty, data,
            screen_origin_gx, screen_origin_gy, raise_tag="overlay_layer",
        )


    def _set_meteo_texts(self, start_text: str, land_text: str):
        self._set_text_widget(self.takeoff_weather_text, start_text)
        self._land_weather_text = land_text
        self._refresh_glide_panel()


    def _reset_analysis_result_caches(self):
        """Скидає ВСІ кешовані результати «Обліт НП»/вітру уздовж
        маршруту -- викликається при завантаженні НОВОЇ місії
        (mission_page._finish_load). БЕЗ цього ці результати лишались
        від ПОПЕРЕДНЬОЇ місії й показувались на картах нової, поки
        користувач не натисне відповідну кнопку ще раз -- реальний баг,
        виявлений на практиці: стара карта видна одразу при заході на
        «Аналіз» нової місії.

        Скидання результату «Оптимізації» (окрема сторінка,
        optimization_page.py) -- через _reset_optimization_result_cache,
        не дублюється тут."""
        self._settlements_cache = None
        self._settlements_violations = []
        self._route_wind_points = []
        self._populated_areas_processed_legs = 0
        if hasattr(self, "settlements_report_text"):
            self.settlements_report_text.configure(state="normal")
            self.settlements_report_text.delete("1.0", "end")
            theme.make_text_readonly(self.settlements_report_text)
        if hasattr(self, "settlements_status_var"):
            self.settlements_status_var.set("")
        if hasattr(self, "_reset_optimization_result_cache"):
            self._reset_optimization_result_cache()


    def _ensure_analysis_built(self):
        """Лінива побудова важких елементів «Аналіз» (аналіз місії,
        графіки, карта маршруту) -- рахуються один раз, при першому
        реальному показі вкладок (або повторно, якщо місію відредагували
        в редакторі на "Місія" й позначили застарілою -- див.
        mission_editor._run_reanalysis), а не при кожному завантаженні
        місії чи кожній правці точки на "Місія"."""
        if getattr(self, "_analysis_built", False) or self.analyzer is None:
            return
        # analyzer.analyze() -- сама важка перевірка (SRTM-запити на
        # кожні 50м кожного відрізка маршруту) -- рахується САМЕ ТУТ,
        # а не на "Місія": це єдине місце, де результат реально
        # використовується (звіт нижче, підсвітка низького AGL на
        # графіках). self._analyzed -- захист від повторного рахунку,
        # якщо цю функцію викликали кілька разів поспіль без реальних
        # змін місії між ними.
        if not self.analyzer._analyzed:
            self.analyzer.analyze()
            self.status_var.set(
                i18n.t("status_ready_fmt", n=len(self.analyzer.nav_wps), m=len(self.analyzer.issues))
            )
            self._distribute_report_text(self._captured_report())
        self._redraw_plot()
        self._redraw_takeoff_profile()
        self._redraw_angle_plot()
        self._redraw_landing_plot()
        self._load_trajectory_map()
        self._analysis_built = True


    def _captured_report(self) -> str:
        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            self.analyzer.print_report()
        finally:
            sys.stdout = old_stdout
        return buf.getvalue()


    @staticmethod
    def _set_text_widget(widget, text: str):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        # без власного скролу текст мусить повністю влазити -- підганяємо
        # висоту під реальний вміст (в межах розумного), решту прокручує
        # єдиний спільний повзунок вкладки
        n_lines = max(text.count("\n") + 1, 1)
        widget.configure(height=min(max(n_lines, 3), 40))
        theme.make_text_readonly(widget)


    def _distribute_report_text(self, report_text: str):
        """
        Розбиває загальний звіт (self.analyzer.print_report()) на секції за
        заголовками "=== ... ===" і розкладає по відповідних вкладках:
        - висота/запас висоти  -> «Траєкторія» (графік висоти)
        - кут траєкторії       -> «Траєкторія» (кут траєкторії)
        - глісада/посадка      -> «Глісада»
        - решта (загальний підсумок) -> до блоку висоти, як загальний огляд
        """
        import re
        parts = re.split(r"(?m)^(=== .* ===)\s*$", report_text)
        # parts[0] -- текст до першого заголовка; далі йдуть пари (заголовок, тіло)

        intro = parts[0].strip()
        elevation_blocks = []
        angle_blocks = []
        glide_blocks = []

        if intro:
            # у вступному тексті (до першого "=== ... ===") може ховатися
            # абзац "Глісада заходу на посадку..." без власного заголовка --
            # витягуємо його окремо, решта інтро йде в блок висоти.
            #
            # Заголовки самого report_text приходять з analyzer.py через
            # i18n.t() -- в англійському режимі UI вони будуть англійською,
            # тому перевіряємо ключові слова ОБОМА мовами одразу, а не
            # лише українською (інакше в EN усе валилось би в один блок).
            intro_paragraphs = re.split(r"\n\s*\n", intro)
            intro_elevation_parts = []
            for para in intro_paragraphs:
                p_low = para.lower()
                if "глісад" in p_low or "glide" in p_low:
                    glide_blocks.append(para.strip())
                else:
                    intro_elevation_parts.append(para)
            if intro_elevation_parts:
                elevation_blocks.append("\n\n".join(intro_elevation_parts))

        i = 1
        while i < len(parts):
            header = parts[i]
            body = parts[i + 1] if i + 1 < len(parts) else ""
            block = (header + "\n" + body).rstrip()
            h_low = header.lower()
            if any(k in h_low for k in ("гліссад", "глісад", "посадк", "glide", "landing")):
                glide_blocks.append(block)
            elif any(k in h_low for k in ("кут", "angle")):
                angle_blocks.append(block)
            elif any(k in h_low for k in ("висот", "elevation", "height")):
                elevation_blocks.append(block)
            else:
                # невідома категорія -- краще показати десь, ніж загубити
                elevation_blocks.append(block)
            i += 2

        self._set_text_widget(self.elev_report_text, "\n\n".join(elevation_blocks) or i18n.t("pdf_no_remarks"))
        self._set_text_widget(self.angle_report_text, "\n\n".join(angle_blocks) or i18n.t("pdf_no_remarks"))
        self._glide_issues_text = "\n\n".join(glide_blocks) or i18n.t("msg_no_remarks_glide")
        self._refresh_glide_panel()


    def _refresh_glide_panel(self):
        """Об'єднує звіт по глісаді (зі звіту аналізу) з погодою посадки в один текст."""
        if not hasattr(self, "glide_report_text"):
            return
        weather = self._land_weather_text.strip()
        combined = self._glide_issues_text
        if weather:
            combined += "\n\n" + ("-" * 44) + "\n" + weather
        self._set_text_widget(self.glide_report_text, combined)


    def _is_dark_theme(self) -> bool:
        theme_var = getattr(self, "app_theme_var", None)
        return theme_var.get() == "dark" if theme_var is not None else True


    def _graph_canvas_bg(self) -> str:
        """Фон канвасів графіків (профіль висоти/кута/глісади) -- майже
        чорний у темній темі (не чистий "black", щоб не зливатись із
        текстом-міткою "0" на сітці графіка), білий у світлій."""
        return "#1a1a1a" if self._is_dark_theme() else "white"


    @staticmethod
    def _viewport_available_height(viewport, margin: int = 40, min_h: int = 200) -> int:
        """Висота видимої області вкладки (viewport -- tk.Canvas із
        make_scroll_tab) за вирахуванням запасу під сусідній контент.
        ЄДИНЕ джерело цього розрахунку -- використовується і для
        квадратної карти (add_map_block), і для графіків (профіль
        зльоту, глісада) -- узгоджена логіка розмірів по всій вкладці."""
        return max(viewport.winfo_height() - margin, min_h)


    def _map_placeholder_bg(self) -> str:
        """Фон канвасів карт (Взліт/Маршрут/Посадка) -- ТОЙ САМИЙ колір,
        що й на "Місія" (MISSION_THEME_DARK/LIGHT["map_placeholder_bg"]),
        для узгодженості. Без цього в темній темі під час завантаження
        тайлів мигав би контрастний СВІТЛО-СІРИЙ прямокутник на місці
        кожного ще не завантаженого тайла -- та сама причина, що й
        нещодавно виправлена на "Місія"."""
        c = MISSION_THEME_DARK if self._is_dark_theme() else MISSION_THEME_LIGHT
        return c["map_placeholder_bg"]


    def _apply_analysis_theme(self):
        """Перефарбовує ВЖЕ ПОБУДОВАНУ сторінку "Аналіз" під поточну тему
        -- викликається з app.py: apply_app_theme(). Canvas-фони не
        підхоплюють зміну ttk.Style автоматично (bg= задається один раз
        при створенні, це не ttk-стиль) -- перефарбовуємо явно й
        перемальовуємо графіки з новими кольорами ліній/тексту."""
        if not hasattr(self, "notebook"):
            return  # сторінку ще не побудовано
        bg = self.palette["bg"]
        for outer in self._analysis_outer_canvases:
            if outer.winfo_exists():
                outer.configure(bg=bg)

        sc = theme.slider_colors(self._is_dark_theme())
        for vbar in self._analysis_vbars:
            if vbar.winfo_exists():
                vbar.configure(bg=sc["bg"], troughcolor=sc["trough"], activebackground=sc["active"])

        c = MISSION_THEME_DARK if self._is_dark_theme() else MISSION_THEME_LIGHT
        for text_widget in self._analysis_report_texts:
            if text_widget.winfo_exists():
                text_widget.configure(bg=c["table_bg"], fg=c["table_fg"], insertbackground=c["table_fg"])

        graph_bg = self._graph_canvas_bg()
        for attr in ("plot_canvas", "angle_canvas", "landing_canvas", "takeoff_profile_canvas"):
            canvas = getattr(self, attr, None)
            if canvas is not None and canvas.winfo_exists():
                canvas.configure(bg=graph_bg)

        map_bg = self._map_placeholder_bg()
        for canvas in self._meteo_canvases:
            if canvas.winfo_exists():
                canvas.configure(bg=map_bg)
        if hasattr(self, "trajectory_map_canvas") and self.trajectory_map_canvas.winfo_exists():
            self.trajectory_map_canvas.configure(bg=map_bg)

        # кнопки "Зберегти PDF" і вибору дати -- звичайні tk.Button з
        # кольором, "заскленим" при створенні (не ttk.Style) -- без цього
        # лишились би зі старими кольорами (той самий баг "світлий текст
        # на світлому фоні", що й у _make_toggle_action_buttons).
        dark = self.palette.get("dark", False)
        idle_bg = "#3a3a3a" if dark else "#DEE3E8"
        idle_active_bg = "#4a4a4a" if dark else "#C9CFD6"
        if hasattr(self, "analysis_save_btn") and self.analysis_save_btn.winfo_exists():
            self.analysis_save_btn.configure(
                bg=idle_bg, fg=self.palette["text"],
                activebackground=idle_active_bg, activeforeground=self.palette["text"],
                highlightbackground=self.palette["border"], highlightcolor=self.palette["border"],
            )
        if hasattr(self, "_date_btn") and self._date_btn.winfo_exists():
            self._date_btn.configure(
                bg=idle_bg, fg=self.palette["text"], highlightbackground=self.palette["border"],
            )

        if self.analyzer is not None:
            self._redraw_plot()
            self._redraw_takeoff_profile()
            self._redraw_angle_plot()
            self._redraw_landing_plot()


    def _redraw_plot(self):
        draw_elevation_profile(self.plot_canvas, self.analyzer, dark=self._is_dark_theme())


    def _redraw_takeoff_profile(self):
        draw_takeoff_profile(self.takeoff_profile_canvas, self.analyzer, n_wps=3, dark=self._is_dark_theme())


    def _redraw_angle_plot(self):
        draw_angle_profile(self.angle_canvas, self.analyzer, dark=self._is_dark_theme())


    def _redraw_landing_plot(self):
        draw_landing_approach(self.landing_canvas, self.analyzer, dark=self._is_dark_theme())

    # -------------------------------------------------------------- карта --


    def _load_trajectory_map(self):
        """Показує карту всього маршруту для вкладки «Траєкторія».

        Бере ГОТОВИЙ, вже відмальований на "Місія" початковий (auto_zoom)
        рендер напряму -- self._initial_map_render (mission_page.py):
        той самий зум, той самий набір тайлів, той самий центр. Жодного
        повторного підбору зуму, жодного мережевого запиту, жодного
        окремого потоку -- усі дані вже готові в пам'яті, лишається
        тільки намалювати.

        ВАЖЛИВО: саме _initial_map_render, а НЕ _last_map_render --
        останній оновлюється на КОЖЕН рендер "Місія", включно з ручним
        зумом/панорамуванням користувача ПІЗНІШЕ. "Маршрут" завжди
        повинен показувати огляд УСЬОГО маршруту, незалежно від того,
        куди користувач потім покрутив камеру на "Місія".
        """
        if self.analyzer is None or not hasattr(self, "trajectory_map_canvas"):
            return

        snapshot = getattr(self, "_initial_map_render", None)
        if snapshot is None:
            # "Місія" ще не рендерилась жодного разу в цій сесії --
            # нема звідки брати готове. Мовчки нічого не малюємо
            # (звичайна ситуація, якщо файл місії ще не завантажували).
            return

        zoom = snapshot["zoom"]
        tiles = snapshot["tiles"]
        tx_min, tx_max = snapshot["tx_min"], snapshot["tx_max"]
        ty_min, ty_max = snapshot["ty_min"], snapshot["ty_max"]
        center_lat, center_lon = snapshot["center_lat"], snapshot["center_lon"]

        self._trajectory_map_params = (tiles, zoom, tx_min, tx_max, ty_min, ty_max, center_lat, center_lon)
        result = render_viewport(
            self.trajectory_map_canvas, self.analyzer, zoom, center_lat, center_lon,
            tx_min, tx_max, ty_min, ty_max, tiles, self._trajectory_map_images,
        )
        if self._route_wind_points:
            screen_origin_gx, screen_origin_gy = result[4], result[5]
            self._draw_route_wind_markers(zoom, screen_origin_gx, screen_origin_gy)


    def _check_populated_areas(self):
        """Кнопка "Перевірити обліт НП" -- запит Overpass API (мережа,
        фоновий потік) і розрахунок відстаней маршруту до знайдених
        населених пунктів."""
        if self.analyzer is None or not self.analyzer.nav_wps:
            messagebox.showwarning(i18n.t("msg_weather_title"), i18n.t("msg_load_mission_first_body"))
            return

        # той самий тип маршруту, що й координатна оптимізація й
        # оптимізація висоти -- ЗА ПРЯМОЮ ВКАЗІВКОЮ користувача: ОДНА
        # наскрізна логіка радіуса обльоту (залежно від населення) для
        # ВСІХ місць, де вона потрібна, замість окремого поля-порогу
        # тільки для цієї перевірки.
        route_type_store = route_types.load_route_types(route_types.default_route_types_path())
        route_type = route_type_store.get_current()
        if route_type is None:
            messagebox.showwarning(i18n.t("msg_weather_title"), i18n.t("msg_no_route_type_for_coords"))
            return

        try:
            exclude_legs = int(self.settlements_exclude_legs_var.get())
            if exclude_legs < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning(i18n.t("msg_weather_title"), i18n.t("msg_invalid_threshold_body"))
            return

        # ті самі ребра зони посадки, що вже виключає "Оптимізація" --
        # приліт до злітно-посадкової смуги біля НП часто неминучий і
        # не є чимось, що можна чи треба "виправляти" вибором іншого
        # шляху -- перевірка цих ребер лише плодить нерелевантний шум
        all_wps = self.analyzer.nav_wps
        n_optimizable = max(0, len(all_wps) - 1 - exclude_legs)
        wps = all_wps[:n_optimizable + 1] if n_optimizable > 0 else all_wps
        lats = [wp.lat for wp in wps]
        lons = [wp.lon for wp in wps]
        lat_min, lat_max = min(lats), max(lats)
        lon_min, lon_max = min(lons), max(lons)

        # захист від аномально великої області (сотні/тисячі км) --
        # звичайна польотна місія ніколи не охоплює таку відстань.
        # Причина такого може бути будь-якою (випадково завантажений не
        # той файл, забракований вейпоінт з некоректними координатами
        # тощо) -- у будь-якому разі запит до Overpass на ТАКУ площу
        # дасть тисячі точок (нечитабельна карта) і триватиме довго,
        # тому попереджаємо ДО запиту, а не після.
        span_km = haversine_m(lat_min, lon_min, lat_max, lon_max) / 1000.0
        MAX_REASONABLE_SPAN_KM = 300.0
        if span_km > MAX_REASONABLE_SPAN_KM:
            proceed = messagebox.askyesno(
                i18n.t("msg_weather_title"),
                i18n.t("msg_huge_area_warning_fmt", span=span_km),
            )
            if not proceed:
                return

        self.check_settlements_btn.configure(state="disabled")
        self.settlements_status_var.set(i18n.t("status_fetching_settlements"))

        # частковий стан для прогресивного малювання карти -- та сама
        # логіка, що вже реалізована для "Оптимізації": порушення
        # накопичуються по мірі готовності кожного ребра, карта
        # перемальовується після кожного, не чекаючи завершення
        # розрахунку по ВСЬОМУ маршруту
        self._settlements_violations = []
        self._settlements_cache = None
        self._populated_areas_processed_legs = 0
        self._load_populated_areas_map()

        threading.Thread(
            target=self._populated_areas_worker,
            args=(lat_min, lat_max, lon_min, lon_max, route_type, wps),
            daemon=True,
        ).start()


    def _populated_areas_worker(self, lat_min, lat_max, lon_min, lon_max, route_type, wps):
        """Фоновий потік: запит шару заборонних зон (restricted_zones.py --
        просторовий файловий кеш, мережа лише за дійсно нові клітинки)
        + розрахунок відстаней. НІЯКОГО звернення до Tkinter напряму --
        лише через self.after(0, ...)."""
        try:
            settlements, all_covered = restricted_zones.get_settlements_for_bbox(
                lat_min, lat_max, lon_min, lon_max,
            )
            if not all_covered:
                self.after(0, lambda: self.settlements_status_var.set(
                    i18n.t("status_zones_partial_coverage"),
                ))
            # settlements відомі одразу після запиту -- виставляємо
            # в кеш негайно, щоб карта могла почати малювати позначки ще
            # до завершення повного проходу по ребрах
            self.after(0, lambda: setattr(self, "_settlements_cache", settlements))

            def on_progress(done, total, leg_violations):
                self.after(0, lambda: self._on_populated_areas_progress(done, total, leg_violations))

            violations = populated_areas.check_route_settlement_distances(
                wps, settlements, radius_lookup=route_type.radius_for_population,
                progress_callback=on_progress,
            )
            self.after(0, lambda: self._on_populated_areas_ready(settlements, violations, None))
        except populated_areas.OverpassError as e:
            self.after(0, lambda: self._on_populated_areas_ready(None, None, str(e)))
        except Exception as e:
            self.after(0, lambda: self._on_populated_areas_ready(None, None, str(e)))


    def _on_populated_areas_progress(self, done, total, leg_violations):
        """Викликається після КОЖНОГО обробленого ребра (з головного
        потоку, через after()) -- накопичує знайдені порушення й
        перемальовує карту з урахуванням ЛИШЕ вже перевірених ребер."""
        self._settlements_violations.extend(leg_violations)
        self._populated_areas_processed_legs = done
        self.settlements_status_var.set(i18n.t("status_checking_leg_fmt", done=done, total=total))
        self._load_populated_areas_map()


    def _on_populated_areas_ready(self, settlements, violations, error):
        self.check_settlements_btn.configure(state="normal")

        if error:
            self.settlements_status_var.set(i18n.t("status_settlements_error_fmt", error=error))
            return

        self._settlements_cache = settlements
        self._settlements_violations = violations
        self._populated_areas_processed_legs = len(self.analyzer.nav_wps) - 1

        self.settlements_status_var.set(i18n.t(
            "status_settlements_found_fmt",
            total=len(settlements), violations=len(violations),
        ))

        lines = [i18n.t("box_settlement_violations"), "-" * 44, ""]
        if not violations:
            lines.append(i18n.t("msg_no_settlement_violations"))
        else:
            # вирівняна таблиця (та сама структура, що й debug_full_route.py)
            # -- моноширинний шрифт вже застосовується до
            # settlements_report_text, тому фіксовані ширини колонок
            # реально дадуть вирівняні стовпці на екрані.
            # Сортуємо за НОМЕРОМ РЕБРА (прогресивно вздовж маршруту,
            # як реально летиш), а не за відстанню (як повертає сама
            # check_route_settlement_distances -- той порядок лишається
            # незмінним для інших можливих використань функції, тут
            # просто сортуємо КОПІЮ під конкретно цей звіт).
            violations_sorted = sorted(violations, key=lambda v: v["leg_index"])

            col_num   = i18n.t("settlement_col_num")
            col_leg   = i18n.t("settlement_col_leg")
            col_pts   = i18n.t("settlement_col_points")
            col_side  = i18n.t("settlement_col_side")
            col_name  = i18n.t("settlement_col_name")
            col_place = i18n.t("settlement_col_place")
            col_pop   = i18n.t("settlement_col_population")
            col_dist  = i18n.t("settlement_col_distance")
            lines.append(
                f"{col_num:<5}{col_leg:<7}{col_pts:<9}{col_side:<6}{col_name:<28}{col_place:<10}{col_pop:<12}{col_dist}"
            )
            lines.append("-" * 101)
            for row_num, v in enumerate(violations_sorted, start=1):
                s = v["settlement"]
                pop_str = str(s["population"]) if s.get("population") else "-"
                leg_pts = f"{v['wp1_index']}-{v['wp2_index']}"
                dist_str = f"{v['distance_m']:.0f}м"

                wp1 = self.analyzer.nav_wps[v["leg_index"]]
                wp2 = self.analyzer.nav_wps[v["leg_index"] + 1]
                side = populated_areas.side_of_travel(
                    s["lat"], s["lon"], wp1.lat, wp1.lon, wp2.lat, wp2.lon,
                )
                side_str = i18n.t("side_left") if side == "L" else i18n.t("side_right")

                lines.append(
                    f"{row_num:<5}{v['leg_index']:<7}{leg_pts:<9}{side_str:<6}{s['name']:<28}"
                    f"{s['place']:<10}{pop_str:<12}{dist_str}"
                )
        self.settlements_report_text.configure(state="normal")
        self.settlements_report_text.delete("1.0", "end")
        self.settlements_report_text.insert("end", "\n".join(lines))
        n_lines = max(len(lines), 3)
        self.settlements_report_text.configure(height=min(n_lines, 40))
        theme.make_text_readonly(self.settlements_report_text)

        self._load_populated_areas_map()


    def _load_populated_areas_map(self):
        """Малює карту всього маршруту (той самий знімок _initial_map_render,
        що й "Траєкторія") і зверху -- ЛИШЕ ті населені пункти, для яких
        порушено умову безпечного обльоту (той самий список порушень,
        що й у таблиці settlements_report_text, жодних додаткових
        "майже безпечних" точок для контексту). Одне порушене село на
        кількох сусідніх ребрах -- одна точка на карті (та сама фізична
        точка), не кілька позначок в одному місці."""
        if self.analyzer is None or not hasattr(self, "populated_map_canvas"):
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
            self.populated_map_canvas, self.analyzer, zoom, center_lat, center_lon,
            tx_min, tx_max, ty_min, ty_max, tiles, self._populated_map_images,
        )
        screen_origin_gx, screen_origin_gy = result[4], result[5]

        violations = getattr(self, "_settlements_violations", None)
        if not violations:
            return

        # ОДНА точка на СЕЛО, не на порушення -- те саме село на кількох
        # сусідніх ребрах не повинно давати кілька позначок в тому
        # самому фізичному місці на карті
        seen = {}
        for v in violations:
            s = v["settlement"]
            key = (s["name"], s["lat"], s["lon"])
            if key not in seen:
                seen[key] = s

        for s in seen.values():
            gx, gy = lonlat_to_pixel(s["lat"], s["lon"], zoom)
            x, y = gx - screen_origin_gx, gy - screen_origin_gy

            r = 7 if s.get("place") in ("city", "town") else 5
            self.populated_map_canvas.create_oval(
                x - r, y - r, x + r, y + r,
                fill="#e02020", outline="white", width=1,
                tags=("settlement_marker",),
            )
            self.populated_map_canvas.create_text(
                x, y - r - 8, text=s["name"], font=("Segoe UI", 8, "bold"),
                fill="white", tags=("settlement_marker",),
            )


    def _compute_wind_sample_points(self):
        """Точки уздовж маршруту для показу вітру -- кількість залежить
        від ЗАГАЛЬНОЇ тривалості польоту (крейсерська швидкість --
        _get_cruise_speed_ms(), з ПОТОЧНОГО профілю літака):
            до 60 хв     -> старт + посадка (2 точки)
            60-180 хв    -> + середина (3 точки)
            понад 180 хв -> + 1/3 і 2/3 замість середини (4 точки)
        Кожна точка -- (lat, lon, час_прильоту_у_неї). Розрахунковий
        час рахується від фактично пройденої ВІДСТАНІ до цієї точки
        (не по індексу вейпоінта), лінійна інтерполяція вздовж ребра,
        де опиняється ця частка загальної дистанції."""
        if self.analyzer is None or not self.analyzer.nav_wps:
            return []
        nav_wps = self.analyzer.nav_wps
        if len(nav_wps) < 2:
            return []

        cum = [0.0]
        for i in range(len(nav_wps) - 1):
            d = haversine_m(nav_wps[i].lat, nav_wps[i].lon, nav_wps[i + 1].lat, nav_wps[i + 1].lon)
            cum.append(cum[-1] + d)
        total_dist = cum[-1]
        if total_dist <= 0:
            return []

        speed = self._get_cruise_speed_ms()
        if speed is None:
            return []  # немає профілю -- "Прибуття" вгорі вже явно про це повідомляє,
            # тут просто мовчки не показуємо позначки вітру (не дублюємо те саме попередження двічі)

        total_seconds = total_dist / speed
        total_minutes = total_seconds / 60.0

        if total_minutes <= 60:
            fractions = [0.0, 1.0]
        elif total_minutes <= 180:
            fractions = [0.0, 0.5, 1.0]
        else:
            fractions = [0.0, 1.0 / 3.0, 2.0 / 3.0, 1.0]

        import datetime
        date_str = self.flight_date_var.get().strip()
        time_str = self.flight_time_var.get().strip()
        try:
            departure = datetime.datetime.fromisoformat(f"{date_str}T{time_str}:00")
        except ValueError:
            return []

        points = []
        for frac in fractions:
            target_dist = frac * total_dist
            idx = 0
            while idx < len(cum) - 2 and cum[idx + 1] < target_dist:
                idx += 1
            wp1, wp2 = nav_wps[idx], nav_wps[idx + 1]
            seg_len = cum[idx + 1] - cum[idx]
            seg_frac = (target_dist - cum[idx]) / seg_len if seg_len > 1e-6 else 0.0
            seg_frac = max(0.0, min(1.0, seg_frac))
            lat = wp1.lat + (wp2.lat - wp1.lat) * seg_frac
            lon = wp1.lon + (wp2.lon - wp1.lon) * seg_frac
            arrival = departure + datetime.timedelta(seconds=frac * total_seconds)
            points.append((lat, lon, arrival))

        return points


    def _route_wind_worker(self, points):
        """Фоновий потік вітру УЗДОВЖ маршруту -- запускається разом з
        рештою метео (_fetch_meteo), НЕ окремою кнопкою (прибрано за
        результатами тестування -- зайва сутність, коли є "Отримати
        метео")."""
        import urllib.request, json

        def fetch_wind_at(lat, lon, dt):
            date_str = dt.strftime("%Y-%m-%d")
            hour = dt.hour
            url = (
                f"https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat:.5f}&longitude={lon:.5f}"
                f"&hourly=windspeed_10m,winddirection_10m"
                f"&timezone=auto&start_date={date_str}&end_date={date_str}"
            )
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "MissionAnalyzer/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
            except Exception as e:
                return None, None, str(e)

            h = data.get("hourly", {})
            times = h.get("time", [])
            target = f"{date_str}T{hour:02d}:00"
            idx = next((i for i, t in enumerate(times) if t == target), None)
            if idx is None:
                return None, None, i18n.t("msg_no_forecast_for_hour")
            spd = h.get("windspeed_10m", [None] * (idx + 1))[idx]
            direction = h.get("winddirection_10m", [None] * (idx + 1))[idx]
            return direction, spd, None

        results = []
        for lat, lon, dt in points:
            wind_dir, wind_spd, err = fetch_wind_at(lat, lon, dt)
            results.append((lat, lon, dt, wind_dir, wind_spd, err))

        self.after(0, lambda: self._on_route_wind_ready(results))


    def _on_route_wind_ready(self, results):
        self._route_wind_points = results
        self._load_trajectory_map()


    def _wind_arrow_length_px(self) -> float:
        """ОДНЕ спільне число довжини стрілки вітру для ВСІХ трьох
        місць (Зліт/Посадка/Маршрут) -- та сама формула
        (R = viewport_h//2-16, довжина = R*0.6), що вже була на Зльоті/
        Посадці, але порахована ОДИН РАЗ від self._viewport_h (спільне
        джерело висоти для всіх карт на "Аналіз"), а НЕ окремо для
        кожного canvas за його власними W/H -- інакше квадратна area-
        карта (Зліт/Посадка) і широкий прямокутник (Маршрут) дали б
        РІЗНІ числа навіть з тим самим viewport_h, бо R рахувався б від
        min(W,H) конкретного canvas, а не від спільного орієнтира."""
        viewport_h = getattr(self, "_viewport_h", None) or 700
        R = viewport_h // 2 - 16
        return R * 0.6


    def _draw_route_wind_markers(self, zoom, screen_origin_gx, screen_origin_gy):
        """Стрілки вітру на карті "Маршрут" -- ПОВНІСТЮ узгоджені зі
        Зльотом/Посадкою: та сама довжина (self._wind_arrow_length_px()),
        той самий формат підпису (час, напрямок, швидкість --
        wind_marker_label_fmt), підпис ПІСЛЯ вістря стрілки (length+20,
        як в overview_map.py), а не біля основи. Кожна позначка тут --
        у ДОВІЛЬНІЙ точці маршруту (не в центрі власної area-карти, як
        на зльоті/посадці), тому стрілка/підпис МОЖЕ реально вилізти за
        canvas -- обмежуємо координати в межах видимої області."""
        length = self._wind_arrow_length_px()
        W = self.trajectory_map_canvas.winfo_width() or 600
        H = self.trajectory_map_canvas.winfo_height() or 400
        margin = 14
        label_margin = 60  # ширше, підпис тепер 2 рядки й довший (час+напрямок+швидкість)

        for lat, lon, dt, wind_dir, wind_spd, err in self._route_wind_points:
            gx, gy = lonlat_to_pixel(lat, lon, zoom)
            x, y = gx - screen_origin_gx, gy - screen_origin_gy
            if err or wind_dir is None:
                continue

            # стрілка показує напрямок, КУДИ дме вітер (не звідки -- це
            # узгоджено з тим самим візуальним трактуванням, що вже є в
            # overview_map.py для зльоту/посадки)
            wind_to = (wind_dir + 180) % 360
            rad = math.radians(wind_to)

            ex = max(margin, min(W - margin, x + length * math.sin(rad)))
            ey = max(margin, min(H - margin, y - length * math.cos(rad)))
            self.trajectory_map_canvas.create_line(
                x, y, ex, ey, fill=theme.MAP_OVERLAY_COLORS["wind"], width=theme.MAP_ARROW_WIDTH,
                arrow="last", arrowshape=theme.MAP_ARROW_SHAPE, tags=("route_wind",),
            )
            self.trajectory_map_canvas.create_oval(
                x - 4, y - 4, x + 4, y + 4, fill=theme.MAP_OVERLAY_COLORS["wind"], outline="white", tags=("route_wind",),
            )

            # підпис ПІСЛЯ вістря (length+20 -- та сама точка відліку,
            # що й у overview_map.py), теж обмежений у межах canvas
            lx = max(label_margin, min(W - label_margin, x + (length + 20) * math.sin(rad)))
            ly = max(label_margin, min(H - label_margin, y - (length + 20) * math.cos(rad)))

            label = i18n.t(
                "wind_marker_label_fmt",
                time=dt.strftime("%H:%M"), dir=wind_dir, speed=wind_spd,
                unit=i18n.t("unit_kmh_short"),
            )
            # підкладка під текст -- ЄДИНА реалізація для ВСІХ карт
            # (theme.draw_label_backdrop), справжня альфа-прозорість
            # через PIL, не пунктирний stipple -- інакше кілька позначок
            # вітру поруч на "Маршруті" зливались у суцільну темну пляму
            text_id = self.trajectory_map_canvas.create_text(
                lx, ly, text=label, fill=theme.MAP_OVERLAY_COLORS["wind"], font=theme.map_overlay_font(),
                tags=("route_wind",),
            )
            bbox = self.trajectory_map_canvas.bbox(text_id)
            if bbox:
                theme.draw_label_backdrop(self.trajectory_map_canvas, bbox, self._trajectory_map_images, tag="route_wind")
                self.trajectory_map_canvas.tag_raise(text_id)
