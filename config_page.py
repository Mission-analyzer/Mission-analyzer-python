"""
config_page.py — сторінка "Конфігурація": параметри аналізу (мін.
висота, мін. кут повороту, крейсерська швидкість), SRTM/тайл-кеш,
провайдер карти, посилання на картографічні/метеосервіси.

ConfigPageMixin підмішується до класу App (app.py).
"""

from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, ttk

from online_tiles import PROVIDERS
import i18n


class ConfigPageMixin:
    """Сторінка "Конфігурація"."""

    def _build_config_page(self, content, pad):
        page_config = ttk.Frame(content)
        page_config.grid(row=0, column=0, sticky="nsew")
        self.pages["config"] = page_config

        # === страница "Конфігурація" ===
        page_config = ttk.Frame(content)
        page_config.grid(row=0, column=0, sticky="nsew")
        self.pages["config"] = page_config

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


