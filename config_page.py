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

        self._reg_i18n(ttk.Label(opts), "text", "label_cruise_speed").grid(row=0, column=4, sticky="w", padx=(16, 6))
        ttk.Entry(opts, textvariable=self.cruise_speed_var, width=6).grid(row=0, column=5, sticky="w")

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

        return aircraft_profiles.AircraftProfile(
            name=name, drone_type=drone_type, engine_type=engine_type,
            cruise_consumption_lph=_f("cruise_consumption_lph"),
            airspeed_min_ms=_f("airspeed_min_ms"),
            airspeed_cruise_ms=_f("airspeed_cruise_ms"),
            airspeed_max_ms=_f("airspeed_max_ms"),
            roll_limit_deg=_f("roll_limit_deg"),
            pitch_limit_max_deg=_f("pitch_limit_max_deg"),
            pitch_limit_min_deg=_f("pitch_limit_min_deg"),
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
