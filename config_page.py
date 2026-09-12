"""
config_page.py — сторінка "Конфігурація": параметри аналізу (мін.
висота, мін. кут повороту, крейсерська швидкість), SRTM/тайл-кеш,
провайдер карти, посилання на картографічні/метеосервіси.

ConfigPageMixin підмішується до класу App (app.py).
"""

from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from online_tiles import PROVIDERS
import i18n
import aircraft_profiles
import route_types
import theme


class ConfigPageMixin:
    """Сторінка "Конфігурація"."""

    def _build_config_page(self, content, pad):
        page_config = ttk.Frame(content)
        page_config.grid(row=0, column=0, sticky="nsew")
        self.pages["config"] = page_config

        # Прокрутка на всю сторінку -- той самий Canvas+Scrollbar
        # паттерн, що вже перевірений в analysis_page.py (make_scroll_tab).
        # БЕЗ цього довгий вміст (після додавання розділу "Профілі
        # літака" сторінка стала суттєво довшою за типову висоту вікна)
        # просто виходить за межі видимого вікна БЕЗ жодного способу
        # прокрутити до нього -- саме тому кнопка "Заповнити профіль"
        # була фізично недосяжна, хоча код і працював коректно.
        outer = tk.Canvas(page_config, highlightthickness=0, bg=self.palette["bg"])
        sc = theme.slider_colors(self._is_dark_theme())
        vbar = tk.Scrollbar(
            page_config, orient="vertical", command=outer.yview,
            bg=sc["bg"], troughcolor=sc["trough"], activebackground=sc["active"],
            highlightthickness=0, bd=0,
        )
        outer.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        outer.pack(side="left", fill="both", expand=True)

        page_config_inner = ttk.Frame(outer)
        inner_id = outer.create_window((0, 0), window=page_config_inner, anchor="nw")

        def _on_inner_configure(_e=None):
            outer.configure(scrollregion=outer.bbox("all"))

        def _on_outer_configure(event):
            if event.width > 20:
                outer.itemconfig(inner_id, width=event.width)

        page_config_inner.bind("<Configure>", _on_inner_configure)
        outer.bind("<Configure>", _on_outer_configure)

        def _on_wheel(event):
            outer.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _bind_wheel(_e=None):
            page_config.bind_all("<MouseWheel>", _on_wheel)

        def _unbind_wheel(_e=None):
            page_config.unbind_all("<MouseWheel>")

        page_config.bind("<Enter>", _bind_wheel)
        page_config.bind("<Leave>", _unbind_wheel)

        # УСІ розділи нижче тепер пакуються в page_config_inner (не
        # page_config напряму) -- інакше вони не потраплять всередину
        # прокручуваної області.
        page_config = page_config_inner

        opts = ttk.LabelFrame(page_config)
        opts.pack(fill="x", **pad)
        self._reg_i18n(opts, "text", "label_params")

        self._reg_i18n(ttk.Label(opts), "text", "label_alt_min").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(opts, textvariable=self.alt_min_var, width=8).grid(row=0, column=1, sticky="w")

        self._reg_i18n(ttk.Label(opts), "text", "label_turn_min").grid(row=0, column=2, sticky="w", padx=(16, 6))
        ttk.Entry(opts, textvariable=self.turn_min_var, width=8).grid(row=0, column=3, sticky="w")

        self._reg_i18n(ttk.Label(opts), "text", "label_angle_max").grid(row=0, column=4, sticky="w", padx=(16, 6))
        ttk.Entry(opts, textvariable=self.angle_max_var, width=8).grid(row=0, column=5, sticky="w")

        self._reg_i18n(
            ttk.Checkbutton(opts, variable=self.use_srtm_var), "text", "check_srtm"
        ).grid(row=1, column=0, sticky="w", padx=6, pady=(2, 4))
        ttk.Entry(opts, textvariable=self.srtm_var).grid(row=1, column=1, columnspan=2, sticky="we", padx=4)
        self._reg_i18n(
            ttk.Button(opts, command=self.browse_srtm), "text", "btn_browse"
        ).grid(row=1, column=3, sticky="w", padx=6)

        self._reg_i18n(ttk.Label(opts), "text", "label_map_cache").grid(row=2, column=0, sticky="w", padx=6, pady=(2, 4))
        ttk.Entry(opts, textvariable=self.tilecache_var).grid(row=2, column=1, columnspan=2, sticky="we", padx=4)
        self._reg_i18n(
            ttk.Button(opts, command=self.browse_tilecache), "text", "btn_browse"
        ).grid(row=2, column=3, sticky="w", padx=6)

        opts.columnconfigure(1, weight=1)
        opts.columnconfigure(2, weight=1)

        # Тема ВСЬОГО додатку (темна/світла) -- перемикається одразу, без
        # перезапуску програми (app.py: apply_app_theme()).
        theme_frame = ttk.LabelFrame(page_config)
        theme_frame.pack(fill="x", **pad)
        self._reg_i18n(theme_frame, "text", "label_app_theme")
        self._reg_i18n(
            ttk.Radiobutton(
                theme_frame, variable=self.app_theme_var, value="dark",
                command=self._on_app_theme_changed,
            ),
            "text", "radio_theme_dark",
        ).pack(side="left", padx=6, pady=6)
        self._reg_i18n(
            ttk.Radiobutton(
                theme_frame, variable=self.app_theme_var, value="light",
                command=self._on_app_theme_changed,
            ),
            "text", "radio_theme_light",
        ).pack(side="left", padx=(0, 6), pady=6)

        map_opts = ttk.LabelFrame(page_config)
        map_opts.pack(fill="x", **pad)
        self._reg_i18n(map_opts, "text", "label_map_settings")

        self._reg_i18n(ttk.Label(map_opts), "text", "label_map_provider").grid(row=0, column=0, sticky="w", padx=6, pady=4)

        # Комбобокс показує ПЕРЕКЛАДЕНІ назви провайдерів, а не самі ключі
        # -- при зміні мови потрібно перебудувати і values, і поточне
        # значення (self.provider_key лишається тим самим, міняється лише
        # те, як він підписаний). Звичайний self._reg_i18n тут не підходить
        # (там лише один рядок тексту, тут -- цілий список), тому окремий
        # retranslate-callback.
        self.provider_var = tk.StringVar()
        provider_box = ttk.Combobox(
            map_opts, textvariable=self.provider_var, state="readonly", width=28,
        )
        provider_box.grid(row=0, column=1, sticky="w", padx=4)
        provider_box.bind("<<ComboboxSelected>>", self._on_provider_selected)
        self._provider_names = {}   # display_name -> key (для поточної мови)

        def _retranslate_provider_box():
            self._provider_names = {}
            display_names = []
            current_display = None
            for key, info in PROVIDERS.items():
                display = i18n.t(f"provider_{key}")
                self._provider_names[display] = key
                display_names.append(display)
                if key == self.provider_key:
                    current_display = display
            provider_box.configure(values=display_names)
            self.provider_var.set(current_display or (display_names[0] if display_names else ""))

        _retranslate_provider_box()
        self._retranslate_callbacks.append(_retranslate_provider_box)

        self._reg_i18n(
            ttk.Checkbutton(map_opts, variable=self.show_occupied_var), "text", "check_occupied",
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=6, pady=(2, 4))

        # Ліміт тайлів на один рендер карти -- вплив на максимально
        # доступний зум (весь маршрут будується ОДНІЄЮ мозаїкою, не як
        # панорамована вьюпорт-карта в Mission Planner, тому високий зум
        # на широкому маршруті реально вимагає багато тайлів одразу;
        # занизький ліміт -- карта мовчки відкочує зум назад).
        self._reg_i18n(ttk.Label(map_opts), "text", "label_max_tiles").grid(
            row=2, column=0, sticky="w", padx=6, pady=(2, 4),
        )
        ttk.Entry(map_opts, textvariable=self.max_tiles_var, width=8).grid(
            row=2, column=1, sticky="w", padx=4,
        )
        self._reg_i18n(ttk.Label(map_opts), "text", "hint_max_tiles").grid(
            row=3, column=0, columnspan=3, sticky="w", padx=6, pady=(0, 4),
        )

        # === Картографічні та метеосервіси ===
        svc_frame = ttk.LabelFrame(page_config)
        svc_frame.pack(fill="x", **pad)
        self._reg_i18n(svc_frame, "text", "box_map_weather_services")

        service_keys = ["label_occupied_layer", "label_windy_service", "label_openmeteo_service", "label_gwa_service"]
        service_vars = [self.url_occupied_var, self.url_windy_var, self.url_forecast_var, self.url_gwa_var]
        for row_i, (key, var) in enumerate(zip(service_keys, service_vars)):
            self._reg_i18n(ttk.Label(svc_frame), "text", key).grid(row=row_i, column=0, sticky="w", padx=6, pady=3)
            ttk.Entry(svc_frame, textvariable=var).grid(
                row=row_i, column=1, sticky="we", padx=(4, 6), pady=3
            )
            ttk.Button(
                svc_frame, text="↗",
                command=lambda u=var: self._open_url(u.get()),
                width=3,
            ).grid(row=row_i, column=2, padx=(0, 6), pady=3)

        svc_frame.columnconfigure(1, weight=1)

        # === Профілі літака ===
        # Постійні льотні характеристики (не міняються від місії до
        # місії) -- заповнюються один раз тут, далі "Аналіз" (Оптимізація:
        # паливо потребує швидкість, перевірка радіуса повороту потребує
        # швидкість+крен) бере їх з ПОТОЧНОГО профілю замість того, щоб
        # питати вручну щоразу. tank_capacity_l СВІДОМО відсутній тут --
        # задається окремо в самій Оптимізації (може відрізнятись від
        # вильоту до вильоту навіть для одного літака).
        self._aircraft_profile_store = aircraft_profiles.load_profiles(
            aircraft_profiles.default_profiles_path()
        )

        profiles_frame = ttk.LabelFrame(page_config)
        profiles_frame.pack(fill="x", **pad)
        self._reg_i18n(profiles_frame, "text", "box_aircraft_profiles")

        # --- рядок вибору профілю + керування ---
        self.profile_select_var = tk.StringVar()
        profile_select_box = ttk.Combobox(
            profiles_frame, textvariable=self.profile_select_var, state="readonly", width=24,
        )
        profile_select_box.grid(row=0, column=0, columnspan=2, sticky="w", padx=6, pady=(6, 4))
        profile_select_box.bind("<<ComboboxSelected>>", self._on_profile_selected)
        self._profile_select_box = profile_select_box

        self._reg_i18n(
            ttk.Button(profiles_frame, command=self._new_profile_form), "text", "btn_new_profile",
        ).grid(row=0, column=2, padx=(6, 0), pady=(6, 4))
        self._reg_i18n(
            ttk.Button(profiles_frame, command=self._delete_profile), "text", "btn_delete_profile",
        ).grid(row=0, column=3, padx=(6, 6), pady=(6, 4))

        # --- поля форми ---
        self._reg_i18n(ttk.Label(profiles_frame), "text", "lbl_profile_name").grid(
            row=1, column=0, sticky="w", padx=6, pady=3,
        )
        self.profile_name_var = tk.StringVar()
        ttk.Entry(profiles_frame, textvariable=self.profile_name_var, width=24).grid(
            row=1, column=1, columnspan=3, sticky="we", padx=(4, 6), pady=3,
        )

        self._reg_i18n(ttk.Label(profiles_frame), "text", "lbl_drone_type").grid(
            row=2, column=0, sticky="w", padx=6, pady=3,
        )
        self.drone_type_var = tk.StringVar()
        drone_type_box = ttk.Combobox(profiles_frame, textvariable=self.drone_type_var, state="readonly", width=22)
        drone_type_box.grid(row=2, column=1, sticky="w", padx=4, pady=3)
        self._drone_type_names = {}   # display_name -> key (для поточної мови)
        self._drone_type_box = drone_type_box

        self._reg_i18n(ttk.Label(profiles_frame), "text", "lbl_engine_type").grid(
            row=2, column=2, sticky="w", padx=(16, 6), pady=3,
        )
        self.engine_type_var = tk.StringVar()
        engine_type_box = ttk.Combobox(profiles_frame, textvariable=self.engine_type_var, state="readonly", width=16)
        engine_type_box.grid(row=2, column=3, sticky="w", padx=4, pady=3)
        self._engine_type_names = {}
        self._engine_type_box = engine_type_box

        def _retranslate_type_boxes():
            self._drone_type_names = {}
            display_names = []
            current_display = None
            current_key = self._current_form_drone_type if hasattr(self, "_current_form_drone_type") else "plane"
            for key in aircraft_profiles.DRONE_TYPES:
                display = i18n.t(f"drone_type_{key}")
                self._drone_type_names[display] = key
                display_names.append(display)
                if key == current_key:
                    current_display = display
            drone_type_box.configure(values=display_names)
            self.drone_type_var.set(current_display or (display_names[0] if display_names else ""))

            self._engine_type_names = {}
            display_names2 = []
            current_display2 = None
            current_key2 = self._current_form_engine_type if hasattr(self, "_current_form_engine_type") else "ice"
            for key in aircraft_profiles.ENGINE_TYPES:
                display = i18n.t(f"engine_type_{key}")
                self._engine_type_names[display] = key
                display_names2.append(display)
                if key == current_key2:
                    current_display2 = display
            engine_type_box.configure(values=display_names2)
            self.engine_type_var.set(current_display2 or (display_names2[0] if display_names2 else ""))

        self._retranslate_type_boxes = _retranslate_type_boxes
        _retranslate_type_boxes()
        self._retranslate_callbacks.append(_retranslate_type_boxes)

        # --- Динамічна область полів "повного профілю" -- НАБІР полів
        # залежить від обраного типу ЛА (aircraft_profiles.
        # PROFILE_FIELDS_BY_TYPE). У літака -- швидкості/крен/тангаж,
        # у майбутнього коптера буде ЗОВСІМ інший набір -- архітектура
        # закладається зараз, реально заповнений лише "plane". Контейнер
        # перебудовується (_rebuild_profile_fields) при кожній зміні
        # drone_type_var, не тільки один раз при створенні сторінки.
        self._profile_field_vars = {}   # field_name -> tk.StringVar (лише АКТУАЛЬНІ для поточного типу)
        profile_fields_container = ttk.Frame(profiles_frame)
        profile_fields_container.grid(row=3, column=0, columnspan=4, sticky="we", padx=0, pady=0)
        self._profile_fields_container = profile_fields_container

        def _on_drone_type_selected(event=None):
            # синхронізуємо _current_form_drone_type З ТИМ, що користувач
            # щойно обрав -- інакше наступна зміна мови (_retranslate_
            # type_boxes читає САМЕ _current_form_drone_type) відкотила б
            # вибір комбобокса назад до попереднього значення
            self._current_form_drone_type = self._drone_type_names.get(
                self.drone_type_var.get(), "plane",
            )
            self._rebuild_profile_fields()

        drone_type_box.bind("<<ComboboxSelected>>", _on_drone_type_selected)

        # --- кнопки дій + індикатор поточного профілю ---
        self._reg_i18n(
            ttk.Button(profiles_frame, command=self._fill_profile), "text", "btn_save_profile",
        ).grid(row=4, column=0, padx=6, pady=(8, 6), sticky="w")
        self._reg_i18n(
            ttk.Button(profiles_frame, command=self._set_current_profile), "text", "btn_set_current_profile",
        ).grid(row=4, column=1, columnspan=2, padx=6, pady=(8, 6), sticky="w")

        self.current_profile_label_var = tk.StringVar()
        current_profile_row = ttk.Frame(profiles_frame)
        current_profile_row.grid(row=5, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 6))
        self._reg_i18n(ttk.Label(current_profile_row), "text", "lbl_current_profile").pack(side="left")
        ttk.Label(current_profile_row, textvariable=self.current_profile_label_var, foreground="#4CAF50").pack(
            side="left", padx=(4, 0),
        )

        self._refresh_profile_combobox()
        self._update_current_profile_label()

        # === Тип маршруту ===
        # Висотні вимоги залежно від того, ЧИЯ територія фактично (за
        # лінією розмежування, не державним кордоном) під конкретною
        # точкою маршруту. Це ЛИШЕ вихідні дані -- сам алгоритм
        # використання (визначення "де я" й плавний перехід висоти) --
        # окремий, ще не реалізований крок в "Аналіз -> Оптимізація".
        self._route_type_store = route_types.load_route_types(
            route_types.default_route_types_path()
        )

        rt_frame = ttk.LabelFrame(page_config)
        rt_frame.pack(fill="x", **pad)
        self._reg_i18n(rt_frame, "text", "box_route_types")

        rt_select_row = ttk.Frame(rt_frame)
        rt_select_row.pack(fill="x", pady=(6, 4))
        self.route_type_select_var = tk.StringVar()
        self.route_type_select_box = ttk.Combobox(
            rt_select_row, textvariable=self.route_type_select_var, state="readonly", width=24,
        )
        self.route_type_select_box.pack(side="left", padx=6)
        self.route_type_select_box.bind("<<ComboboxSelected>>", self._on_route_type_selected)
        self._reg_i18n(
            ttk.Button(rt_select_row, command=self._new_route_type_form), "text", "btn_new_route_type",
        ).pack(side="left", padx=(6, 0))
        self._reg_i18n(
            ttk.Button(rt_select_row, command=self._delete_route_type), "text", "btn_delete_route_type",
        ).pack(side="left", padx=(6, 6))

        rt_form = ttk.Frame(rt_frame)
        rt_form.pack(fill="x", padx=6, pady=(0, 6))

        self._reg_i18n(ttk.Label(rt_form), "text", "lbl_route_type_name").grid(
            row=0, column=0, sticky="w", padx=(0, 4), pady=3,
        )
        self.route_type_name_var = tk.StringVar()
        ttk.Entry(rt_form, textvariable=self.route_type_name_var, width=24).grid(
            row=0, column=1, columnspan=3, sticky="we", padx=(0, 6), pady=3,
        )

        self._reg_i18n(ttk.Label(rt_form), "text", "lbl_alt_controlled").grid(
            row=1, column=0, sticky="w", padx=(0, 4), pady=3,
        )
        self.alt_controlled_var = tk.StringVar()
        ttk.Entry(rt_form, textvariable=self.alt_controlled_var, width=8).grid(
            row=1, column=1, sticky="w", padx=(0, 4), pady=3,
        )
        self._reg_i18n(ttk.Label(rt_form), "text", "lbl_ms_unit_m").grid(row=1, column=2, sticky="w")

        self._reg_i18n(ttk.Label(rt_form), "text", "lbl_alt_occupied").grid(
            row=2, column=0, sticky="w", padx=(0, 4), pady=3,
        )
        self.alt_occupied_var = tk.StringVar()
        ttk.Entry(rt_form, textvariable=self.alt_occupied_var, width=8).grid(
            row=2, column=1, sticky="w", padx=(0, 4), pady=3,
        )
        self._reg_i18n(ttk.Label(rt_form), "text", "lbl_ms_unit_m").grid(row=2, column=2, sticky="w")

        self._reg_i18n(ttk.Label(rt_form), "text", "lbl_alt_border_crossing").grid(
            row=3, column=0, sticky="w", padx=(0, 4), pady=3,
        )
        self.alt_border_crossing_var = tk.StringVar()
        ttk.Entry(rt_form, textvariable=self.alt_border_crossing_var, width=8).grid(
            row=3, column=1, sticky="w", padx=(0, 4), pady=3,
        )
        self._reg_i18n(ttk.Label(rt_form), "text", "lbl_ms_unit_m").grid(row=3, column=2, sticky="w")

        self._reg_i18n(ttk.Label(rt_form), "text", "lbl_max_waypoints").grid(
            row=4, column=0, sticky="w", padx=(0, 4), pady=3,
        )
        self.max_waypoints_var = tk.StringVar()
        ttk.Entry(rt_form, textvariable=self.max_waypoints_var, width=8).grid(
            row=4, column=1, sticky="w", padx=(0, 4), pady=3,
        )

        self._reg_i18n(ttk.Label(rt_form), "text", "lbl_border_maneuver_angle").grid(
            row=5, column=0, sticky="w", padx=(0, 4), pady=3,
        )
        self.border_maneuver_angle_var = tk.StringVar()
        ttk.Entry(rt_form, textvariable=self.border_maneuver_angle_var, width=8).grid(
            row=5, column=1, sticky="w", padx=(0, 4), pady=3,
        )
        self._reg_i18n(ttk.Label(rt_form), "text", "lbl_deg").grid(row=5, column=2, sticky="w")

        # --- Радіус обльоту НП, залежно від населення (ЗА ПРЯМОЮ
        # ВКАЗІВКОЮ користувача: список пар, не одне число -- більший
        # НП потребує ширшого обходу, ніж мале село). Treeview для
        # показу наявних пар + окремі поля для додавання нової.
        self._reg_i18n(
            ttk.Label(rt_frame), "text", "lbl_population_radii",
        ).pack(anchor="w", padx=6, pady=(6, 2))

        radii_frame = ttk.Frame(rt_frame)
        radii_frame.pack(fill="x", padx=6, pady=(0, 4))
        self.population_radii_tree = ttk.Treeview(
            radii_frame, columns=("population", "radius"), show="headings", height=4,
        )
        self.population_radii_tree.heading("population", text=i18n.t("lbl_col_max_population"))
        self.population_radii_tree.heading("radius", text=i18n.t("lbl_col_radius_km"))
        self._retranslate_callbacks.append(
            lambda: (
                self.population_radii_tree.heading("population", text=i18n.t("lbl_col_max_population")),
                self.population_radii_tree.heading("radius", text=i18n.t("lbl_col_radius_km")),
            )
        )
        self.population_radii_tree.column("population", width=140, anchor="e")
        self.population_radii_tree.column("radius", width=100, anchor="e")
        self.population_radii_tree.pack(side="left", fill="x", expand=True)

        radii_add_row = ttk.Frame(rt_frame)
        radii_add_row.pack(fill="x", padx=6, pady=(0, 6))
        self._reg_i18n(ttk.Label(radii_add_row), "text", "lbl_col_max_population").pack(side="left", padx=(0, 4))
        self.new_radius_population_var = tk.StringVar()
        ttk.Entry(radii_add_row, textvariable=self.new_radius_population_var, width=10).pack(side="left", padx=(0, 8))
        self._reg_i18n(ttk.Label(radii_add_row), "text", "lbl_col_radius_km").pack(side="left", padx=(0, 4))
        self.new_radius_km_var = tk.StringVar()
        ttk.Entry(radii_add_row, textvariable=self.new_radius_km_var, width=8).pack(side="left", padx=(0, 8))
        self._reg_i18n(
            ttk.Button(radii_add_row, command=self._add_population_radius_row), "text", "btn_add_radius_row",
        ).pack(side="left", padx=(0, 6))
        self._reg_i18n(
            ttk.Button(radii_add_row, command=self._remove_population_radius_row), "text", "btn_remove_radius_row",
        ).pack(side="left")

        rt_btn_row = ttk.Frame(rt_frame)
        rt_btn_row.pack(fill="x", padx=6, pady=(0, 6))
        self._reg_i18n(
            ttk.Button(rt_btn_row, command=self._save_route_type), "text", "btn_save_profile",
        ).pack(side="left")
        self._reg_i18n(
            ttk.Button(rt_btn_row, command=self._set_current_route_type), "text", "btn_set_current_profile",
        ).pack(side="left", padx=(6, 0))

        self.current_route_type_label_var = tk.StringVar()
        rt_current_row = ttk.Frame(rt_frame)
        rt_current_row.pack(fill="x", padx=6, pady=(0, 6))
        self._reg_i18n(ttk.Label(rt_current_row), "text", "lbl_current_route_type").pack(side="left")
        ttk.Label(rt_current_row, textvariable=self.current_route_type_label_var, foreground="#4CAF50").pack(
            side="left", padx=(4, 0),
        )

        self._refresh_route_type_combobox()

    def _on_app_theme_changed(self):
        self.apply_app_theme()
        self._save_settings()


    def _on_provider_selected(self, event=None):
        self.provider_key = self._provider_names.get(self.provider_var.get(), self.provider_key)
        self._save_settings()


    def browse_srtm(self):
        path = filedialog.askdirectory(title=i18n.t("dlg_choose_srtm_title"))
        if path:
            self.srtm_var.set(path)
            self._save_settings()


    def browse_tilecache(self):
        path = filedialog.askdirectory(title=i18n.t("dlg_choose_mapcache_title"))
        if path:
            self.tilecache_var.set(path)
            self._save_settings()

    # ------------------------------------------------------------ анализ --



    # ------------------------------------------------------- профілі --

    def _refresh_profile_combobox(self):
        names = [p.name for p in self._aircraft_profile_store.profiles]
        self._profile_select_box.configure(values=names)
        current = self._aircraft_profile_store.get_current()
        if current:
            self.profile_select_var.set(current.name)
            self._load_profile_into_form(current)
        elif names:
            self.profile_select_var.set(names[0])
            self._load_profile_into_form(self._aircraft_profile_store.get_by_name(names[0]))
        else:
            self.profile_select_var.set("")
            self._new_profile_form()


    def _rebuild_profile_fields(self):
        """Перебудовує self._profile_fields_container під ПОТОЧНИЙ обраний
        тип ЛА (self.drone_type_var). Викликається і при зміні типу в
        комбобоксі, і при завантаженні профілю у форму -- НАБІР полів
        визначається типом, не є статичним. Якщо для типу немає полів
        (aircraft_profiles.PROFILE_FIELDS_BY_TYPE[type] порожній) --
        показує явне повідомлення, а не порожнє місце (щоб було
        зрозуміло, що це "ще не реалізовано", а не "нічого немає")."""
        for child in self._profile_fields_container.winfo_children():
            child.destroy()
        self._profile_field_vars = {}

        drone_type = self._drone_type_names.get(
            self.drone_type_var.get(),
            getattr(self, "_current_form_drone_type", "plane"),
        )
        field_names = aircraft_profiles.PROFILE_FIELDS_BY_TYPE.get(drone_type, [])

        if not field_names:
            self._reg_i18n(
                ttk.Label(self._profile_fields_container, foreground="#888"),
                "text", "msg_type_fields_not_implemented",
            ).grid(row=0, column=0, columnspan=4, sticky="w", padx=6, pady=6)
            return

        for i, field_name in enumerate(field_names):
            label_key, unit_key = aircraft_profiles.PROFILE_FIELD_META[field_name]
            row, col_pair = divmod(i, 2)
            col = col_pair * 3
            self._reg_i18n(ttk.Label(self._profile_fields_container), "text", label_key).grid(
                row=row, column=col, sticky="w", padx=6, pady=3,
            )
            # порожній дефолт (НЕ "0") -- з "0" користувач мусив би
            # спершу стерти нуль перед вводом власного числа, інакше
            # отримує "047" замість "47" (реальна скарга з практики)
            var = tk.StringVar(value="")
            ttk.Entry(self._profile_fields_container, textvariable=var, width=8).grid(
                row=row, column=col + 1, sticky="w", padx=4, pady=3,
            )
            self._reg_i18n(ttk.Label(self._profile_fields_container, foreground="#888"), "text", unit_key).grid(
                row=row, column=col + 2, sticky="w", padx=(2, 16), pady=3,
            )
            self._profile_field_vars[field_name] = var


    def _load_profile_into_form(self, profile):
        self.profile_name_var.set(profile.name)
        self._current_form_drone_type = profile.drone_type
        self._current_form_engine_type = profile.engine_type
        self._retranslate_type_boxes()
        self._rebuild_profile_fields()
        for field_name, var in self._profile_field_vars.items():
            var.set(str(getattr(profile, field_name, 0.0)))


    def _on_profile_selected(self, event=None):
        name = self.profile_select_var.get()
        profile = self._aircraft_profile_store.get_by_name(name)
        if profile:
            self._load_profile_into_form(profile)


    def _new_profile_form(self):
        self.profile_name_var.set("")
        self._current_form_drone_type = "plane"
        self._current_form_engine_type = "ice"
        self._retranslate_type_boxes()
        self._rebuild_profile_fields()
        self.profile_select_var.set("")


    def _read_profile_form(self):
        """Зчитує поточні значення форми як AircraftProfile. Піднімає
        ValueError з чітким повідомленням при некоректному вводі.
        Поля, ЯКИХ немає для поточного типу (self._profile_field_vars),
        лишаються дефолтними значеннями AircraftProfile (0.0) -- це
        коректно: наприклад, для "flying_wing" (поки без полів) профіль
        все одно можна створити (тільки ім'я+тип), просто без льотних
        характеристик, доки для цього типу не реалізовано власний набір."""
        name = self.profile_name_var.get().strip()
        if not name:
            raise ValueError(i18n.t("msg_profile_name_required"))
        drone_type = self._drone_type_names.get(self.drone_type_var.get(), "plane")
        engine_type = self._engine_type_names.get(self.engine_type_var.get(), "ice")

        def _f(field_name):
            var = self._profile_field_vars.get(field_name)
            if var is None:
                return 0.0
            try:
                return float(var.get().replace(",", "."))
            except ValueError:
                return 0.0

        # ДИНАМІЧНО, а не жорстко перелічений список полів -- інакше
        # ЩОРАЗУ, коли в PROFILE_FIELDS_BY_TYPE додається нове поле
        # (як щойно сталось із трьома TECS-полями), ця функція мовчки
        # ігнорує його, і збереження просто НЕ записує нові значення,
        # хоча вони видимі й редаговані у формі -- реальний баг,
        # знайдений на цьому самому кроці.
        field_values = {name: _f(name) for name in self._profile_field_vars}
        return aircraft_profiles.AircraftProfile(
            name=name, drone_type=drone_type, engine_type=engine_type,
            **field_values,
        )


    def _fill_profile(self):
        """Кнопка "Заповнити профіль" -- зберігає (створює новий чи
        ОНОВЛЮЄ існуючий з тим самим ім'ям -- upsert) профіль з поточних
        значень форми."""
        try:
            profile = self._read_profile_form()
        except ValueError as e:
            messagebox.showwarning(i18n.t("msg_weather_title"), str(e))
            return
        self._aircraft_profile_store.upsert(profile)
        self._save_aircraft_profiles()
        self._refresh_profile_combobox()
        self.profile_select_var.set(profile.name)


    def _set_current_profile(self):
        """Кнопка "Встановити поточним" -- позначає обраний у списку
        профіль як ПОТОЧНИЙ (не постійний "типовий", можна перемкнути
        будь-коли на інший)."""
        name = self.profile_select_var.get()
        if not name:
            return
        self._aircraft_profile_store.current_name = name
        self._save_aircraft_profiles()
        self._update_current_profile_label()


    def _delete_profile(self):
        name = self.profile_select_var.get()
        if not name:
            return
        if not messagebox.askyesno(
            i18n.t("msg_weather_title"), i18n.t("msg_confirm_delete_profile_fmt", name=name),
        ):
            return
        self._aircraft_profile_store.remove(name)
        self._save_aircraft_profiles()
        self._refresh_profile_combobox()
        self._update_current_profile_label()


    def _update_current_profile_label(self):
        current = self._aircraft_profile_store.get_current()
        self.current_profile_label_var.set(current.name if current else "-")


    def _save_aircraft_profiles(self):
        aircraft_profiles.save_profiles(
            aircraft_profiles.default_profiles_path(), self._aircraft_profile_store,
        )

    # -------------------------------------------------------- тип маршруту --

    def _refresh_route_type_combobox(self):
        names = [rt.name for rt in self._route_type_store.route_types]
        self.route_type_select_box.configure(values=names)
        current = self._route_type_store.get_current()
        if current:
            self.route_type_select_var.set(current.name)
            self._load_route_type_into_form(current)
        elif names:
            self.route_type_select_var.set(names[0])
            self._load_route_type_into_form(self._route_type_store.get_by_name(names[0]))
        else:
            self.route_type_select_var.set("")
            self._new_route_type_form()
        self._update_current_route_type_label()


    def _add_population_radius_row(self):
        try:
            pop = float(self.new_radius_population_var.get().replace(",", "."))
            radius = float(self.new_radius_km_var.get().replace(",", "."))
        except ValueError:
            messagebox.showwarning(i18n.t("msg_weather_title"), i18n.t("msg_invalid_number"))
            return
        if pop <= 0 or radius <= 0:
            messagebox.showwarning(i18n.t("msg_weather_title"), i18n.t("msg_invalid_number"))
            return
        self.population_radii_tree.insert("", "end", values=(pop, radius))
        self.new_radius_population_var.set("")
        self.new_radius_km_var.set("")


    def _remove_population_radius_row(self):
        for item in self.population_radii_tree.selection():
            self.population_radii_tree.delete(item)


    def _load_route_type_into_form(self, rt):
        self.route_type_name_var.set(rt.name)
        self.alt_controlled_var.set(str(rt.altitude_controlled_m))
        self.alt_occupied_var.set(str(rt.altitude_occupied_m))
        self.alt_border_crossing_var.set(str(rt.altitude_border_crossing_m))
        self.max_waypoints_var.set(str(rt.max_waypoints))
        self.border_maneuver_angle_var.set(str(rt.border_maneuver_angle_max_deg))
        self.population_radii_tree.delete(*self.population_radii_tree.get_children())
        for pop, radius in sorted(rt.population_radii_km, key=lambda t: t[0]):
            self.population_radii_tree.insert("", "end", values=(pop, radius))


    def _on_route_type_selected(self, event=None):
        name = self.route_type_select_var.get()
        rt = self._route_type_store.get_by_name(name)
        if rt:
            self._load_route_type_into_form(rt)


    def _new_route_type_form(self):
        self.route_type_name_var.set("")
        for var in (self.alt_controlled_var, self.alt_occupied_var, self.alt_border_crossing_var):
            var.set("")
        self.max_waypoints_var.set("255")
        self.border_maneuver_angle_var.set("2.0")
        self.route_type_select_var.set("")
        self.population_radii_tree.delete(*self.population_radii_tree.get_children())
        self.population_radii_tree.insert("", "end", values=(1.0, 1.0))  # той самий дефолт, що й у RouteType.population_radii_km


    def _save_route_type(self):
        name = self.route_type_name_var.get().strip()
        if not name:
            messagebox.showwarning(i18n.t("msg_weather_title"), i18n.t("msg_profile_name_required"))
            return

        def _f(var):
            try:
                return float(var.get().replace(",", "."))
            except ValueError:
                return 0.0

        try:
            max_wp = int(self.max_waypoints_var.get())
        except ValueError:
            max_wp = 255

        population_radii_km = []
        for item in self.population_radii_tree.get_children():
            pop_val, radius_val = self.population_radii_tree.item(item, "values")
            try:
                population_radii_km.append((float(pop_val), float(radius_val)))
            except ValueError:
                continue  # рядок пошкоджено -- пропускаємо, не валимо все збереження
        if not population_radii_km:
            population_radii_km = [(1.0, 1.0)]  # той самий дефолт, що й у RouteType -- порожній список ніколи не зберігаємо

        rt = route_types.RouteType(
            name=name,
            altitude_controlled_m=_f(self.alt_controlled_var),
            altitude_occupied_m=_f(self.alt_occupied_var),
            altitude_border_crossing_m=_f(self.alt_border_crossing_var),
            max_waypoints=max_wp,
            border_maneuver_angle_max_deg=_f(self.border_maneuver_angle_var) or 2.0,
            population_radii_km=population_radii_km,
        )
        self._route_type_store.upsert(rt)
        self._save_route_types_to_disk()
        self._refresh_route_type_combobox()
        self.route_type_select_var.set(rt.name)


    def _set_current_route_type(self):
        name = self.route_type_select_var.get()
        if not name:
            return
        self._route_type_store.current_name = name
        self._save_route_types_to_disk()
        self._update_current_route_type_label()


    def _delete_route_type(self):
        name = self.route_type_select_var.get()
        if not name:
            return
        if not messagebox.askyesno(
            i18n.t("msg_weather_title"), i18n.t("msg_confirm_delete_profile_fmt", name=name),
        ):
            return
        self._route_type_store.remove(name)
        self._save_route_types_to_disk()
        self._refresh_route_type_combobox()


    def _update_current_route_type_label(self):
        current = self._route_type_store.get_current()
        self.current_route_type_label_var.set(current.name if current else "-")


    def _save_route_types_to_disk(self):
        route_types.save_route_types(route_types.default_route_types_path(), self._route_type_store)
