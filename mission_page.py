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

from waypoints import parse_waypoints, command_name, Waypoint
from geo import haversine_m, bearing_deg, TILE_SIZE, lonlat_to_pixel, pixel_to_lonlat
from srtm import SRTMTerrain, SRTMError
from online_tiles import OnlineTileCache
from analyzer import MissionAnalyzer
from meta import AUTHOR
import map_view
from map_view import (
    compute_tile_bounds, compute_viewport_tile_bounds, fetch_tiles,
    render_tiles_fit, MapTooLargeError,
    begin_viewport_render, draw_single_tile, _draw_polygon_overlay,
)
from occupied_layer import fetch_occupied_geojson, extract_polygons
import theme
import i18n

# MAV_FRAME -> спрощена назва (як у Mission Planner): Absolute / Relative / Terrain
_MAV_FRAME_TERRAIN = {13, 14}  # GLOBAL_TERRAIN_ALT, GLOBAL_TERRAIN_ALT_INT

# Дві палітри для сторінки "Місія" (перемикаються в Конфігурації) --
# ВСІ кольори, які раніше були зашиті прямо в коді (чорний фон, білі/сірі
# лінії тощо), тепер беруться звідси. Ключі однакові в обох словниках.
MISSION_THEME_DARK = {
    "bg": "black",
    "fg": "white",
    "muted_fg": "#aaaaaa",
    "table_bg": "black", "table_fg": "#e8e8e8",
    "table_sel_bg": "#3a5f8a", "table_sel_fg": "#ffffff",
    "table_head_bg": "#1a1a1a", "table_head_fg": "#e8e8e8", "table_head_active": "#333333",
    "map_placeholder_bg": theme.CHART_COLORS_DARK["grid"],
    "slider_bg": theme.SLIDER_COLORS_DARK["bg"],
    "slider_trough": theme.SLIDER_COLORS_DARK["trough"],
    "slider_active": theme.SLIDER_COLORS_DARK["active"],
    "alt_bg": "black",
    "alt_grid": theme.CHART_COLORS_DARK["grid"], "alt_axis_label": theme.CHART_COLORS_DARK["text"],
    "alt_axis_line": "#888888",
    "alt_terrain_line": theme.CHART_COLORS_DARK["line_terrain"],
    "alt_flight_line": theme.CHART_COLORS_DARK["line_primary"],
    "alt_marker_text": "#ffffff",
    "alt_marker_outline": "white",
    "alt_no_terrain": "#ff6666", "alt_unit_label": "#888888",
}
MISSION_THEME_LIGHT = {
    "bg": "#f0f0f0",
    "fg": "black",
    "muted_fg": "#555555",
    "table_bg": "white", "table_fg": "black",
    "table_sel_bg": "#3399ff", "table_sel_fg": "white",
    "table_head_bg": "#e0e0e0", "table_head_fg": "black", "table_head_active": "#cfcfcf",
    "map_placeholder_bg": "#dddddd",
    "slider_bg": theme.SLIDER_COLORS_LIGHT["bg"],
    "slider_trough": theme.SLIDER_COLORS_LIGHT["trough"],
    "slider_active": theme.SLIDER_COLORS_LIGHT["active"],
    "alt_bg": "white",
    "alt_grid": theme.CHART_COLORS_LIGHT["grid"], "alt_axis_label": theme.CHART_COLORS_LIGHT["text"],
    "alt_axis_line": "#888888",
    "alt_terrain_line": theme.CHART_COLORS_LIGHT["line_terrain"],
    "alt_flight_line": theme.CHART_COLORS_LIGHT["line_primary"],
    "alt_marker_text": "#000000",
    "alt_marker_outline": "black",
    "alt_no_terrain": "#cc0000", "alt_unit_label": "#666666",
}


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

    def _mission_colors(self) -> dict:
        theme_var = getattr(self, "app_theme_var", None)
        key = theme_var.get() if theme_var is not None else "dark"
        return MISSION_THEME_LIGHT if key == "light" else MISSION_THEME_DARK


    def _apply_mission_table_style(self):
        """Стиль ttk.Treeview для таблиці місії -- усі три шари одразу:
        звичайний стан, стан "виділено" (.map, інакше виділений рядок
        лишиться зі старим кольором) і заголовки колонок (Treeview.Heading
        -- інший шар стилю, не той самий, що й самі рядки). Викликається
        і при першій побудові сторінки, і при перемиканні теми в
        Конфігурації."""
        c = self._mission_colors()
        style = ttk.Style()
        style.configure(
            "MissionBlack.Treeview",
            background=c["table_bg"], fieldbackground=c["table_bg"], foreground=c["table_fg"],
        )
        style.map(
            "MissionBlack.Treeview",
            background=[("selected", c["table_sel_bg"])],
            foreground=[("selected", c["table_sel_fg"])],
        )
        style.configure(
            "MissionBlack.Treeview.Heading",
            background=c["table_head_bg"], foreground=c["table_head_fg"], relief="flat",
        )
        style.map(
            "MissionBlack.Treeview.Heading",
            background=[("active", c["table_head_active"])],
        )


    def _apply_mission_theme(self):
        """Перефарбовує ВЖЕ ПОБУДОВАНУ сторінку "Місія" під поточну тему
        (app_theme_var) -- викликається з Конфігурації при перемиканні
        світла/темна. Без перебудови сторінки заново: кожен колірний
        віджет або лежить у self._mission_bg_widgets (прості фони), або
        має власне ім'я (таблиця, слайдер, скролбар, канваси)."""
        if not hasattr(self, "mission_content"):
            return  # сторінку ще не побудовано (наприклад, виклик до _build_ui)
        c = self._mission_colors()

        for w in self._mission_bg_widgets:
            if w.winfo_exists():
                w.configure(bg=c["bg"])

        self._apply_mission_table_style()

        if hasattr(self, "mission_outer") and self.mission_outer.winfo_exists():
            self.mission_outer.configure(bg=c["bg"])
        if hasattr(self, "mission_vbar") and self.mission_vbar.winfo_exists():
            self.mission_vbar.configure(
                bg=c["slider_bg"], troughcolor=c["slider_trough"], activebackground=c["slider_active"],
            )
        if hasattr(self, "_table_vbar") and self._table_vbar.winfo_exists():
            self._table_vbar.configure(
                bg=c["slider_bg"], troughcolor=c["slider_trough"], activebackground=c["slider_active"],
            )
        if hasattr(self, "_occupied_status_label") and self._occupied_status_label.winfo_exists():
            self._occupied_status_label.configure(bg=c["bg"], fg=c["muted_fg"])
        if hasattr(self, "map_canvas") and self.map_canvas.winfo_exists():
            # тайли/маршрут перемальовуються поверх -- цей колір видно
            # лише як заглушку "тайл не завантажено" (map_view.render_*)
            self.map_canvas.configure(bg=c["map_placeholder_bg"])
        if hasattr(self, "map_zoom_label") and self.map_zoom_label.winfo_exists():
            self.map_zoom_label.configure(bg=c["bg"], fg=c["fg"])
        if hasattr(self, "_alt_zoom_label") and self._alt_zoom_label.winfo_exists():
            self._alt_zoom_label.configure(bg=c["bg"], fg=c["fg"])
        if hasattr(self, "map_zoom_slider") and self.map_zoom_slider.winfo_exists():
            self.map_zoom_slider.configure(
                bg=c["slider_bg"], fg=c["fg"], troughcolor=c["slider_trough"], activebackground=c["slider_active"],
            )
        if hasattr(self, "_alt_profile_frame") and self._alt_profile_frame.winfo_exists():
            self._alt_profile_frame.configure(bg=c["bg"], fg=c["fg"])
        if hasattr(self, "alt_profile_canvas") and self.alt_profile_canvas.winfo_exists():
            self.alt_profile_canvas.configure(bg=c["alt_bg"])
            self._redraw_altitude_profile()

        # карту (тайли + маршрут) теж варто перемалювати -- вона сама по
        # собі не залежить від теми, але дешево з кешу, а не з мережі
        if self._last_map_render is not None:
            self._redraw_last_map_render()


    def _build_mission_page(self, content, pad):
        c = self._mission_colors()
        # список пар (widget, "bg") для простих tk.Frame/tk.Label/tk.LabelFrame,
        # де ЄДИНЕ, що змінюється між темами -- фон (bg=c["bg"]). Заповнюється
        # по ходу побудови, читається в _apply_mission_theme() при перемиканні
        # теми в Конфігурації -- без цього списку довелось би перебудовувати
        # всю сторінку заново на кожне перемикання.
        self._mission_bg_widgets = []

        # === страница "Місія" ===
        # ЗВИЧАЙНИЙ tk.Frame -- сторінка ціла має бути чорною, включно з
        # рядком кнопок зверху (Завантажити/Зберегти/Редагувати), який
        # раніше лишався ЗА МЕЖАМИ mission_body (сусідній елемент, а не
        # дочірній) і тому не потрапив під попередню перефарбовку взагалі.
        page_mission = tk.Frame(content, bg=c["bg"])
        page_mission.grid(row=0, column=0, sticky="nsew")
        self.pages["mission"] = page_mission
        self._mission_bg_widgets.append(page_mission)

        btns = tk.Frame(page_mission, bg=c["bg"])
        btns.pack(fill="x", **pad)
        self._mission_bg_widgets.append(btns)
        load_btn, save_btn = self._make_toggle_action_buttons(
            btns, [(i18n.t("btn_load"), self.load_mission), (i18n.t("btn_save"), self.save_csv)]
        )
        self._reg_i18n(load_btn, "text", "btn_load")
        self._reg_i18n(save_btn, "text", "btn_save")
        load_btn.pack(side="left")
        save_btn.pack(side="left", padx=6)

        self._edit_mode = False
        self.edit_btn = tk.Button(
            btns, text=i18n.t("btn_edit_mission"), command=self._toggle_edit_mode,
            font=("Segoe UI", 9, "bold"), bd=2, relief="groove", cursor="hand2",
            padx=16, pady=6, bg="#DEE3E8",
        )
        self._reg_i18n(self.edit_btn, "text", "btn_edit_mission")
        # спочатку схована -- нема сенсу редагувати, поки місію не
        # завантажено/зчитано (див. _show_edit_button у _finish_load)
        self._retranslate_callbacks.append(self._retranslate_edit_button)

        # кнопки Info/Read/Write/Files SD для ArduPilot -- видимі тільки
        # коли підключено. Окрема підгрупа, притиснута до ПРАВОГО краю
        # рядка (side="right"), щоб не змішуватись з Завантажити/Зберегти
        # зліва -- порядок усередині групи зберігається зліва направо:
        # Info, Зчитати, Записати, Файли SD.
        ardu_btns_frame = tk.Frame(btns, bg=c["bg"])
        ardu_btns_frame.pack(side="right")
        self._mission_bg_widgets.append(ardu_btns_frame)
        self._ardu_info_btn, self._ardu_read_btn, self._ardu_write_btn, self._ardu_files_btn, self._ardu_commands_btn, self._ardu_params_btn = \
            self._make_toggle_action_buttons(
                ardu_btns_frame, [
                    (i18n.t("btn_info"), self._show_flight_info),
                    (i18n.t("btn_read"), self._load_mission_from_mavlink),
                    (i18n.t("btn_write"), self._save_mission_to_mavlink),
                    (i18n.t("btn_sd_files"), self._show_sd_files),
                    (i18n.t("btn_scripted_commands"), self._show_scripted_commands_scan),
                    (i18n.t("btn_params"), self._show_key_params),
                ]
            )
        self._reg_i18n(self._ardu_info_btn, "text", "btn_info")
        self._reg_i18n(self._ardu_read_btn, "text", "btn_read")
        self._reg_i18n(self._ardu_write_btn, "text", "btn_write")
        self._reg_i18n(self._ardu_files_btn, "text", "btn_sd_files")
        self._reg_i18n(self._ardu_commands_btn, "text", "btn_scripted_commands")
        self._reg_i18n(self._ardu_params_btn, "text", "btn_params")
        # спочатку сховані -- покажемо при підключенні
        self._ardu_btns_visible = False

        # тело страницы -- либо чёрный плейсхолдер с лого (пока ничего не
        # загружено), либо таблица+карта (после успешной загрузки миссии).
        # Пустая таблица/карта до загрузки не несут смысла, поэтому вместо
        # них показываем то же самое, что и на сплэш-экране при старте.
        # ЗВИЧАЙНИЙ tk.Frame (не ttk!) -- той самий принцип, що й нижче
        # для всіх дочірніх контейнерів: на Windows нативна ttk-тема часто
        # ігнорує background, заданий через ttk.Style/опції, і контейнер
        # однаково малюється кольором системної теми. tk.Frame поза
        # ttk-рушієм узагалі, bg= завжди чесно спрацьовує.
        mission_body = tk.Frame(page_mission, bg=c["bg"])
        mission_body.pack(fill="both", expand=True)
        mission_body.rowconfigure(0, weight=1)
        mission_body.columnconfigure(0, weight=1)
        self._mission_bg_widgets.append(mission_body)

        self.mission_placeholder = tk.Frame(mission_body, bg=c["bg"])
        self.mission_placeholder.grid(row=0, column=0, sticky="nsew")
        self._mission_bg_widgets.append(self.mission_placeholder)
        logo_path = self._find_asset(("icon.png", "logo.png"))
        if logo_path:
            self._mission_placeholder_logo = self._load_logo_thumbnail(logo_path, target_h=170)
            if self._mission_placeholder_logo is not None:
                logo_label = tk.Label(self.mission_placeholder, image=self._mission_placeholder_logo, bg=c["bg"])
                logo_label.place(relx=0.5, rely=0.5, anchor="center")
                self._mission_bg_widgets.append(logo_label)

                from meta import VERSION
                version_label = tk.Label(
                    self.mission_placeholder,
                    text=f"Version: {VERSION}", fg="#4CAF50", bg=c["bg"],
                    font=("Segoe UI", 10, "bold"),
                )
                version_label.place(relx=0.5, rely=0.5, anchor="n", y=90)
                self._mission_bg_widgets.append(version_label)

                author_label = tk.Label(
                    self.mission_placeholder,
                    text=f"by {AUTHOR}", fg="#888888", bg=c["bg"],
                    font=("Segoe UI", 9),
                )
                author_label.place(relx=0.5, rely=0.5, anchor="n", y=114)
                self._mission_bg_widgets.append(author_label)

        self.mission_content = tk.Frame(mission_body, bg=c["bg"])
        self.mission_content.grid(row=0, column=0, sticky="nsew")
        self._mission_bg_widgets.append(self.mission_content)

        # Один зовнішній вертикальний скрол на всю сторінку (той самий
        # прийом, що й make_scroll_tab на "Аналіз") -- замість того, щоб
        # таблиця/карта/профіль висот боролись за одну й ту саму видиму
        # висоту вікна й ставали занадто вузькими для роботи в редакторі.
        # Кожен блок нижче отримує щедру фіксовану висоту, а прокрутка
        # сторінки показує решту.
        self.mission_outer = tk.Canvas(self.mission_content, highlightthickness=0, bg=c["bg"])
        # tk.Scrollbar (НЕ ttk.Scrollbar!) -- та сама причина, що й для
        # tk.Scale вище: ttk-скролбар на Windows часто ігнорує кольори,
        # задані через ttk.Style, під нативною темою. tk.Scrollbar поза
        # ttk-рушієм узагалі, bg=/troughcolor= завжди чесно спрацьовують.
        # Темний повзунок на світлому жолобі -- добре видно, за що тягнути.
        self.mission_vbar = tk.Scrollbar(
            self.mission_content, orient="vertical", command=self.mission_outer.yview,
            bg=c["slider_bg"], troughcolor=c["slider_trough"], activebackground=c["slider_active"],
            highlightthickness=0, bd=0,
        )
        self.mission_outer.configure(yscrollcommand=self.mission_vbar.set)
        self.mission_vbar.pack(side="right", fill="y")
        self.mission_outer.pack(side="left", fill="both", expand=True)

        # Контейнери -- ЗВИЧАЙНІ tk.Frame (bg=), не ttk.Frame зі стилем:
        # на Windows нативна тема ttk (vista/xpnative) часто просто
        # ІГНОРУЄ background, заданий через ttk.Style().configure() --
        # віджет однаково малюється системною темою ОС. tk.Frame поза
        # ttk-рушієм узагалі, bg= завжди чесно спрацьовує незалежно від
        # активної теми.
        #
        # Таблиця (Treeview) -- єдиний ttk-віджет тут (без нього втратимо
        # заголовки колонок і клас Treeview узагалі не має tk-аналога).
        # На відміну від Frame/Label, Treeview зазвичай ЧЕСНО реагує на
        # background/fieldbackground навіть під нативною Windows-темою --
        # тому лишаємо через ttk.Style, але налаштовуємо ВСІ шари одразу:
        # звичайний стан (.configure), стан "виділено" (.map -- інакше
        # виділений рядок лишиться зі старим кольором), і заголовки колонок
        # окремо (Treeview.Heading -- це інший шар стилю, не той самий,
        # що й самі рядки).
        self._apply_mission_table_style()

        mission_inner = tk.Frame(self.mission_outer, bg=c["bg"])
        self._mission_bg_widgets.append(mission_inner)
        mission_inner_id = self.mission_outer.create_window((0, 0), window=mission_inner, anchor="nw")

        def _on_mission_inner_configure(_e=None):
            self.mission_outer.configure(scrollregion=self.mission_outer.bbox("all"))

        def _on_mission_outer_configure(event):
            if event.width > 20:
                self.mission_outer.itemconfig(mission_inner_id, width=event.width)

        mission_inner.bind("<Configure>", _on_mission_inner_configure)
        self.mission_outer.bind("<Configure>", _on_mission_outer_configure)

        def _on_mission_wheel(event):
            self.mission_outer.yview_scroll(int(-1 * (event.delta / 120)), "units")

        # <MouseWheel> не спливає від дочірніх canvas-віджетів (карта,
        # профіль висот) до батьківського контейнера -- тому глобальний
        # перехоплювач, що вмикається/вимикається за курсором, як і на
        # "Аналіз".
        def _bind_mission_wheel(_e=None):
            self.mission_content.bind_all("<MouseWheel>", _on_mission_wheel)

        def _unbind_mission_wheel(_e=None):
            self.mission_content.unbind_all("<MouseWheel>")

        self.mission_content.bind("<Enter>", _bind_mission_wheel)
        self.mission_content.bind("<Leave>", _unbind_mission_wheel)

        table_frame = tk.Frame(mission_inner, bg=c["bg"])
        table_frame.pack(fill="x", **pad)
        self._mission_bg_widgets.append(table_frame)
        table_columns = ("idx", "cmd", "p1", "p2", "p3", "p4", "lat", "lon", "alt", "frame", "dist", "az")
        self.mission_table = ttk.Treeview(
            table_frame, columns=table_columns, show="headings", height=7, style="MissionBlack.Treeview",
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
        # Власний вертикальний скролбар ПОВЕРНУТО: тепер таблиця обмежена
        # висотою "екран мінус шапка" (self._max_table_rows_for_viewport,
        # рахується в _apply_viewport_heights) -- якщо точок більше, ніж
        # влазить, прокручується САМА таблиця, а не вся сторінка.
        # tk.Scrollbar (НЕ ttk.Scrollbar!) -- та сама причина, що й для
        # mission_vbar/map_zoom_slider нижче: на Windows нативна ttk-тема
        # часто ігнорує кольори, задані через ttk.Style. Кольори -- з
        # ЄДИНОГО джерела (theme.slider_colors) -- той самий вигляд, що
        # й в усіх інших повзунків/смуг прокрутки програми.
        self._table_vbar = tk.Scrollbar(
            table_frame, orient="vertical", command=self.mission_table.yview,
            bg=c["slider_bg"], troughcolor=c["slider_trough"], activebackground=c["slider_active"],
            highlightthickness=0, bd=0,
        )
        self.mission_table.configure(yscrollcommand=self._table_vbar.set)
        self.mission_table.pack(side="left", fill="x", expand=True)
        self._table_vbar.pack(side="left", fill="y")

        map_ctrl2 = tk.Frame(mission_inner, bg=c["bg"])
        map_ctrl2.pack(fill="x", padx=6)
        self._mission_bg_widgets.append(map_ctrl2)
        self._occupied_status_label = tk.Label(
            map_ctrl2, textvariable=self.occupied_status_var, bg=c["bg"], fg=c["muted_fg"],
        )
        self._occupied_status_label.pack(side="left")

        # Карта -- висота обмежена "екран мінус шапка" (self._viewport_h,
        # рахується в _apply_viewport_heights від РЕАЛЬНОГО розміру вікна,
        # а не довільним числом), а не всією висотою мозаїки тайлів --
        # інакше на дуже витягнутому маршруті карта вимагала б гортати
        # через неї всю сторінку, щоб дістатись профілю висот нижче.
        self._MAP_FRAME_MIN_H = 300
        self._MAP_FRAME_MAX_H = 700  # перезаписується _apply_viewport_heights під реальний розмір вікна
        self.map_canvas_frame = tk.Frame(mission_inner, height=self._MAP_FRAME_MIN_H, bg=c["bg"])
        self.map_canvas_frame.pack(fill="x", **pad)
        self.map_canvas_frame.pack_propagate(False)
        self._mission_bg_widgets.append(self.map_canvas_frame)

        self.map_canvas = tk.Canvas(self.map_canvas_frame, bg=c["map_placeholder_bg"], highlightthickness=0, bd=0)
        self.map_canvas.pack(side="left", fill="both", expand=True)

        # Кнопка-піктограма "скинути вигляд" у верхньому лівому куті
        # карти (накладена поверх, .place() -- не займає власного місця
        # в layout): з будь-якого зуму/панорамування повертає до
        # початкового вигляду -- увесь маршрут вміщується в канвас, без
        # прокрутки. Домальована вручну (без icons.py в цьому проєкті)
        # -- квадратні дужки по кутах, як типова піктограма "вмістити
        # все"/"fit to screen" у картографічних застосунках.
        self._reset_view_btn = tk.Canvas(
            self.map_canvas_frame, width=30, height=30, highlightthickness=0, bd=0,
            bg="#ffffff", cursor="hand2",
        )
        self._reset_view_btn.place(x=8, y=8)
        self._draw_reset_view_icon()
        self._reset_view_btn.bind("<Button-1>", lambda _e: self._reset_map_view())

        # Вертикальний повзунок зуму вздовж правого краю карти (як у
        # Mission Planner) -- зверху максимальне наближення, знизу
        # максимальне віддалення. Рендер (мережевий запит тайлів) триґериться
        # лише по відпусканню кнопки миші, а не на кожен проміжний крок
        # перетягування повзунка -- інакше під час руху повзунка полетить
        # шквал запитів.
        zoom_slider_frame = tk.Frame(self.map_canvas_frame, width=34, bg=c["bg"])
        zoom_slider_frame.pack(side="right", fill="y")
        zoom_slider_frame.pack_propagate(False)
        self._mission_bg_widgets.append(zoom_slider_frame)
        self.map_zoom_label = tk.Label(
            zoom_slider_frame, textvariable=self.zoom_var, anchor="center",
            bg=c["bg"], fg=c["fg"],
        )
        self.map_zoom_label.pack(pady=(4, 0))
        # tk.Scale (НЕ ttk.Scale!) -- та сама причина, що й для решти
        # контейнерів на цій сторінці: ttk-віджети на Windows часто
        # ігнорують кольори, задані через ttk.Style/опції, під нативною
        # темою (vista/xpnative). tk.Scale поза ttk-рушієм узагалі, bg=/
        # troughcolor=/fg= завжди чесно спрацьовують незалежно від теми.
        self.map_zoom_slider = tk.Scale(
            zoom_slider_frame, from_=24, to=0, orient="vertical", variable=self.zoom_var,
            bg=c["slider_bg"], fg=c["fg"], troughcolor=c["slider_trough"], activebackground=c["slider_active"],
            highlightthickness=0, bd=0, showvalue=False,
        )
        self.map_zoom_slider.pack(fill="y", expand=True, pady=(2, 8))
        self.map_zoom_slider.bind("<ButtonRelease-1>", lambda _e: self.render_map())

        # Панорамування -- ЄДИНІ обробники для обох режимів (замість
        # старого bind_pan): в оглядовому режимі (весь маршрут однією
        # мозаїкою) -- звичайне scan_mark/scan_dragto по великому
        # scrollregion; у "віконному" (Mission Planner-подібному) --
        # canvas.move("all", ...) наживо під час руху миші, і повне
        # перезавантаження тайлів навколо нового центру по відпусканню
        # (бо scrollregion там дорівнює розміру самого канваса -- скролити
        # там просто нічого, потрібна саме заміна центру + новий фетч).
        # mission_editor.py (режим редагування) викликає ЦІ САМІ методи
        # для "не влучили в маркер -- отже панорамування", щоб поведінка
        # була однаковою в обох режимах.
        self.map_canvas.bind("<ButtonPress-1>", self._on_map_pan_press)
        self.map_canvas.bind("<B1-Motion>", self._on_map_pan_motion)
        self.map_canvas.bind("<ButtonRelease-1>", self._on_map_pan_release)
        # Пряме масштабування колесом миші НАД КАРТОЮ (як у Mission
        # Planner -- без Ctrl). return "break" у _on_map_wheel зупиняє
        # спливання події ДО глобального перехоплювача прокрутки сторінки
        # (_on_mission_wheel вище) -- інакше одна й та сама подія колеса
        # одночасно й зумила б карту, і прокручувала сторінку.
        self.map_canvas.bind("<MouseWheel>", self._on_map_wheel)
        self.map_canvas.bind("<Button-4>", self._on_map_wheel)       # Linux — колесо вверх
        self.map_canvas.bind("<Button-5>", self._on_map_wheel)       # Linux — колесо вниз

        # Підказка при наведенні на точку маршруту -- номер/lat/lon/висота
        self.map_canvas.bind("<Motion>", self._on_map_hover)
        self.map_canvas.bind("<Leave>", lambda _e: self._hide_map_tooltip())

        # если ширина канваса меняется (пользователь потянул окно) --
        # перерисовываем карту точно под новую ширину, а не оставляем
        # старый масштаб
        self._last_map_render = None
        self._initial_map_render = None  # незмінний знімок першого (auto_zoom) рендеру -- див. _on_tiles_ready
        self._map_resize_after_id = None
        self._zoom_wheel_after_id = None
        self._map_render_generation = 0
        self.map_canvas.bind("<Configure>", self._on_map_canvas_resize)

        # "Вікно в карту" (як у Mission Planner) -- коли зум перевищує
        # той, що потрібен для показу ВСЬОГО маршруту (self._fit_zoom),
        # довантажуємо лише область НАВКОЛО self._map_center_lat/lon
        # замість усього маршруту одразу -- завдяки цьому зум завжди
        # доступний аж до 19, незалежно від протяжності місії. Доки зум
        # <= _fit_zoom -- працює старий режим "весь маршрут однією
        # мозаїкою" (він і так дешевий на низькому зумі).
        self._fit_zoom = None
        self._map_center_lat = None
        self._map_center_lon = None
        self._map_viewport_mode = False
        self._pan_start = None  # {"x","y","center_lat","center_lon"} під час активного панорамування

        # Профіль висот -- також обмежений висотою "екран мінус шапка".
        # В режимі редагування маркери точок тягнуться мишею по вертикалі
        # (тільки висота -- X/дистанція не змінюється перетягуванням тут).
        self._alt_profile_frame = tk.LabelFrame(
            mission_inner, text=i18n.t("box_altitude_profile"), bg=c["bg"], fg=c["fg"],
        )
        self._alt_profile_frame.pack(fill="x", padx=6, pady=(4, 6))
        self._reg_i18n(self._alt_profile_frame, "text", "box_altitude_profile")

        self.alt_profile_canvas = tk.Canvas(self._alt_profile_frame, bg=c["alt_bg"], height=260, highlightthickness=0)
        self.alt_profile_canvas.pack(fill="x")
        self._alt_profile_geom = None
        self._alt_drag = None
        self._alt_zoom_range = None  # (dist_start, dist_end) у метрах уздовж маршруту -- None -- показуємо весь маршрут
        self._alt_zoom_drag = None   # {"start_x": ...} під час виділення прямокутника лівою кнопкою (лише коли клік НЕ на маркері -- інакше це перетягування точки, _on_alt_press_edit)
        self.alt_profile_canvas.bind("<Configure>", lambda _e: self._redraw_altitude_profile())

        # Кнопка-піктограма "скинути вигляд" (той самий принцип, що й на
        # карті, _draw_reset_view_icon/_reset_map_view) -- з будь-якого
        # рівня зуму графіка повертає до початкового вигляду (весь
        # маршрут). Накладена поверх канваса графіка (.place()), у
        # ЛІВОМУ верхньому куті -- та сама позиція, що й у аналогічної
        # кнопки на карті.
        self._alt_reset_view_btn = tk.Canvas(
            self._alt_profile_frame, width=30, height=30, highlightthickness=0, bd=0,
            bg="#ffffff", cursor="hand2",
        )
        self._alt_reset_view_btn.place(in_=self.alt_profile_canvas, x=8, y=8)
        self._draw_alt_reset_view_icon()
        self._alt_reset_view_btn.bind("<Button-1>", lambda _e: self._reset_alt_zoom())

        # Числовий індикатор поточного зуму -- у ПРАВОМУ верхньому куті
        # графіка (та сама позиція, де на карті стоїть map_zoom_label
        # над повзунком). Значення рахується й оновлюється в
        # _redraw_altitude_profile() (формула -- див. коментар там).
        self._alt_zoom_label = tk.Label(
            self._alt_profile_frame, text="1", anchor="center",
            bg=c["bg"], fg=c["fg"], font=("Arial", 9, "bold"),
        )
        self._alt_zoom_label.place(in_=self.alt_profile_canvas, relx=1.0, x=-8, y=8, anchor="ne")

        # Перераховуємо всі три висоти під реальний розмір вікна одразу
        # після побудови сторінки і при кожній зміні розміру вікна
        # (дебаунс -- той самий прийом, що й для ресайзу карти).
        self._viewport_resize_after_id = None
        self.bind("<Configure>", self._on_root_configure_for_viewport)
        self.mission_content.bind("<Configure>", self._on_root_configure_for_viewport)
        self.after(200, self._apply_viewport_heights)

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

        # висота таблиці обмежена висотою видимої області вікна (див.
        # _apply_viewport_heights) -- якщо точок більше, ніж влазить,
        # прокручується САМА таблиця (власний table_vbar), не вся сторінка
        visible_rows = len(self.mission_table.get_children())
        max_rows = getattr(self, "_max_table_rows_for_viewport", None) or visible_rows
        self.mission_table.configure(height=max(3, min(visible_rows, max_rows)))


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
        self.edit_btn.pack(side="left", padx=6)

        # НЕ рахуємо analyzer.analyze() тут -- це важка перевірка (SRTM-
        # запити на кожні 50м кожного відрізка маршруту), потрібна лише
        # для сторінки "Аналіз". Раніше рахувалась одразу на "Місія" (де
        # взагалі не потрібна) -- звідси й видима затримка перед тим, як
        # карта хоч починала завантажуватись (вона чекала завершення
        # цього рахунку, хоча логічно з ним ніяк не пов'язана), і
        # "критичні відмітки" в статус-рядку одразу після завантаження
        # файлу, коли користувач ще навіть не відкривав "Аналіз".
        # Тепер: analyze() рахується лінива, ОДИН раз, при реальному
        # відкритті "Аналіз" (_ensure_analysis_built), а карта на "Місія"
        # стартує одразу й незалежно від нього.
        self._analysis_built = False
        self.status_var.set(i18n.t("status_loaded_fmt", n=len(self.analyzer.nav_wps)))
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
            defaultextension=".waypoints",
            filetypes=[
                (i18n.t("filetype_waypoints"), "*.waypoints"),
                ("CSV", "*.csv"),
            ],
        )
        if not path:
            return
        if path.lower().endswith(".csv"):
            self.analyzer.export_csv(path)
        else:
            self._export_waypoints(path)
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


    def _on_root_configure_for_viewport(self, event):
        # реагуємо на зміну розміру КОРЕНЕВОГО вікна (event.widget -- сам
        # self) АБО на зміну позиції/розміру self.mission_content -- друге
        # потрібно, бо шапка може вирости/зменшитись (наприклад, з'являються
        # кнопки Info/Read/Write/Files після підключення ArduPilot) БЕЗ
        # зміни розміру самого вікна -- тоді <Configure> на self взагалі
        # не спрацює, а top_offset (висота шапки) застаріє, і карта може
        # вилізти за межі видимої області.
        if event.widget is not self and event.widget is not self.mission_content:
            return
        if self._viewport_resize_after_id is not None:
            self.after_cancel(self._viewport_resize_after_id)
        self._viewport_resize_after_id = self.after(150, self._apply_viewport_heights)


    def _apply_viewport_heights(self):
        """Обмежує висоту карти/таблиці/профілю висот висотою РЕАЛЬНО
        видимої області вікна за мінусом шапки (заголовок+навігація+ряд
        кнопок) -- кожен блок отримує "по екрану", а не бореться за місце
        з рештою чи розтягується на весь маршрут. Що не влізло -- показує
        зовнішня прокрутка сторінки (mission_outer) чи власний скролбар
        таблиці. Рахується від РЕАЛЬНОГО розміру вікна (не довільне число),
        тому коректно підлаштовується під будь-який монітор й при зміні
        розміру вікна користувачем."""
        self._viewport_resize_after_id = None
        if not hasattr(self, "mission_content") or not self.mission_content.winfo_exists():
            return
        self.update_idletasks()
        top_offset = self.mission_content.winfo_rooty() - self.winfo_rooty()
        raw_h = self.winfo_height() - top_offset - 20

        # Захист від "просідання" геометрії: якщо вікно щойно програмно
        # змінило розмір (self.geometry(...)) або щойно з'явилось,
        # winfo_height() інколи повертає ЩЕ НЕ ОСІЛИЙ (проміжний, менший)
        # розмір навіть після update_idletasks() -- це чекає на вікно, не
        # на Tk. Якщо порахований viewport РАПТОВО й СИЛЬНО менший за вже
        # встановлений (self._viewport_h) -- це підозріло, ймовірно
        # проміжний стан, а не реальне зменшення вікна користувачем.
        # Замість того, щоб одразу застосувати (і зіпсувати щойно
        # правильно відрендерену карту), плануємо ще одну перевірку
        # трохи пізніше -- і застосовуємо, лише якщо значення
        # підтвердиться повторно.
        prev_h = getattr(self, "_viewport_h", None)
        suspicious = prev_h is not None and raw_h < prev_h * 0.5 and raw_h < 400
        if suspicious and not getattr(self, "_viewport_height_recheck_pending", False):
            self._viewport_height_recheck_pending = True
            self.after(250, self._apply_viewport_heights)
            return
        self._viewport_height_recheck_pending = False

        viewport_h = max(300, raw_h)
        self._viewport_h = viewport_h
        self._MAP_FRAME_MAX_H = viewport_h
        # Раніше тут був окремий шлях "запам'ятати РЕАЛЬНУ висоту щойно
        # відмальованої мозаїки" (_last_map_draw_h/_apply_map_frame_height)
        # -- потрібний, коли render_tiles_fit міг дати картинку ІНШОЇ
        # висоти, ніж запитана. Тепер карта завжди малюється (render_
        # viewport) РІВНО під заданий розмір канваса -- висота фрейму
        # завжди просто дорівнює висоті вьюпорту, без винятків.
        self.map_canvas_frame.configure(height=viewport_h)

        # ~22px -- стандартна висота рядка ttk.Treeview за замовчуванням
        self._max_table_rows_for_viewport = max(3, viewport_h // 22)
        actual_rows = len(self.mission_table.get_children())
        if actual_rows:
            self.mission_table.configure(height=min(actual_rows, self._max_table_rows_for_viewport))

        self.alt_profile_canvas.configure(height=viewport_h)

        # Ті самі "екран на елемент" блоки на "Аналіз → Маршрут" (карта,
        # графік висоти, графік кута) -- синхронізуємо з тим самим
        # self._viewport_h, що й карта/профіль висот на "Місія", щоб
        # при resize вікна вони теж підлаштовувались. hasattr -- сторінка
        # "Аналіз" могла ще не бути побудована (лінива побудова).
        if hasattr(self, "_traj_map_box") and self._traj_map_box.winfo_exists():
            self._traj_map_box.configure(height=viewport_h)
        if hasattr(self, "plot_canvas") and self.plot_canvas.winfo_exists():
            self.plot_canvas.configure(height=viewport_h)
        if hasattr(self, "angle_canvas") and self.angle_canvas.winfo_exists():
            self.angle_canvas.configure(height=viewport_h)
        if hasattr(self, "_populated_map_box") and self._populated_map_box.winfo_exists():
            self._populated_map_box.configure(height=viewport_h)
        if hasattr(self, "_optimize_map_box") and self._optimize_map_box.winfo_exists():
            self._optimize_map_box.configure(height=viewport_h)
        if hasattr(self, "_optimize_report_box") and self._optimize_report_box.winfo_exists():
            self._optimize_report_box.configure(height=viewport_h)
        if hasattr(self, "_populated_report_box") and self._populated_report_box.winfo_exists():
            self._populated_report_box.configure(height=viewport_h)


    def _on_map_hover(self, event):
        """Підказка при наведенні на маркер точки маршруту -- номер,
        широта, довгота, висота. Читає той самий тег wp_marker_<index>,
        що й перетягування в редакторі (mission_editor.py), але сама
        працює завжди, незалежно від режиму редагування -- це просто
        інформаційна підказка."""
        cx = self.map_canvas.canvasx(event.x)
        cy = self.map_canvas.canvasy(event.y)
        item = self.map_canvas.find_closest(cx, cy)
        tags = self.map_canvas.gettags(item) if item else ()
        wp_index = None
        for t in tags:
            if t.startswith("wp_marker_"):
                wp_index = int(t.rsplit("_", 1)[-1])
                break

        if wp_index is None or self.analyzer is None:
            self._hide_map_tooltip()
            return
        wp = next((w for w in self.analyzer.all_wps if w.index == wp_index), None)
        if wp is None:
            self._hide_map_tooltip()
            return

        text = f"#{wp.index}\n{i18n.t('table_col_lat')}: {wp.lat:.6f}\n{i18n.t('table_col_lon')}: {wp.lon:.6f}\n{i18n.t('table_col_alt')}: {wp.alt:g}"
        self._show_map_tooltip(event.x_root, event.y_root, text)


    def _show_map_tooltip(self, x_root: int, y_root: int, text: str):
        tip = getattr(self, "_map_tooltip", None)
        if tip is None or not tip.winfo_exists():
            tip = tk.Toplevel(self)
            tip.overrideredirect(True)
            tip.attributes("-topmost", True)
            label = tk.Label(
                tip, text=text, background="#ffffe0", relief="solid", borderwidth=1,
                font=("Consolas", 9), justify="left", padx=6, pady=3,
            )
            label.pack()
            self._map_tooltip = tip
            self._map_tooltip_label = label
        else:
            self._map_tooltip_label.configure(text=text)
        tip.geometry(f"+{x_root + 14}+{y_root + 12}")
        tip.deiconify()


    def _hide_map_tooltip(self):
        tip = getattr(self, "_map_tooltip", None)
        if tip is not None and tip.winfo_exists():
            tip.withdraw()





    def _redraw_last_map_render(self):
        """Canvas міг змінити розмір (вікно користувач потягнув) --
        обидва режими ("overview"/"viewport") тепер малюються ОДНАКОВО
        (render_viewport, кожен тайл окремо, 1:1) -- просто перераховуємо
        діапазон тайлів під НОВИЙ розмір канваса навколо ТОГО САМОГО
        геоцентру через звичайний render_map(). Якщо видима область
        побільшала, можуть знадобитись тайли поза тим, що вже
        завантажено -- тому НЕ обмежуємось старим tx_min..ty_max.

        Якщо це ще "overview" (користувач не панорамував/не зумив
        вручну) -- викликаємо саме render_map(auto_zoom=True), а не
        просто render_map(): інакше оптимальний зум підгонки не
        перерахувався б заново під НОВИЙ розмір канваса (він залежить
        від розміру канваса -- при resize вікна може змінитись)."""
        self._map_resize_after_id = None
        if self._last_map_render is None:
            return
        is_overview = self._last_map_render.get("mode") == "overview"
        self.render_map(auto_zoom=is_overview)


    def _draw_reset_view_icon(self):
        """Малює піктограму "вмістити все" -- 4 кутові дужки, спрямовані
        всередину (типова піктограма fit-to-screen/reset view).

        Свідомо НЕ залежить від теми програми (на відміну від решти
        сторінки): ця кнопка лежить ПОВЕРХ реальних супутникових/OSM
        тайлів карти, а не поверх фону теми -- колір тайлів довільний
        (може бути темний ліс чи світле поле), тому непрозорий білий
        бокс + темна іконка завжди читабельні, незалежно і від теми
        застосунку, і від того, що саме зараз під кнопкою на карті
        (той самий підхід, що й у плаваючих елементах керування
        картою в більшості картографічних застосунків)."""
        canvas = self._reset_view_btn
        canvas.delete("all")
        canvas.configure(bg="#ffffff")
        color = "#333333"
        L, m = 8, 4  # довжина плеча дужки, відступ від краю
        w = int(canvas.cget("width"))
        h = int(canvas.cget("height"))
        # верхній лівий
        canvas.create_line(m, m, m + L, m, fill=color, width=2)
        canvas.create_line(m, m, m, m + L, fill=color, width=2)
        # верхній правий
        canvas.create_line(w - m, m, w - m - L, m, fill=color, width=2)
        canvas.create_line(w - m, m, w - m, m + L, fill=color, width=2)
        # нижній лівий
        canvas.create_line(m, h - m, m + L, h - m, fill=color, width=2)
        canvas.create_line(m, h - m, m, h - m - L, fill=color, width=2)
        # нижній правий
        canvas.create_line(w - m, h - m, w - m - L, h - m, fill=color, width=2)
        canvas.create_line(w - m, h - m, w - m, h - m - L, fill=color, width=2)


    def _reset_map_view(self):
        """Клік по піктограмі у верхньому лівому куті карти -- повертає
        до початкового вигляду (весь маршрут вміщується в канвас, без
        прокрутки) з БУДЬ-ЯКОГО поточного зуму/панорамування. Той самий
        розрахунок, що й одразу після завантаження місії (auto_zoom=True
        заново підбирає зум під актуальний розмір канваса й скидає
        _fit_zoom/_map_center_lat/lon)."""
        if self.analyzer is None:
            return
        self.render_map(auto_zoom=True)


    def _draw_alt_reset_view_icon(self):
        """Та сама піктограма "вмістити все" (4 кутові дужки), що й на
        карті -- ІДЕНТИЧНА за кольором (_draw_reset_view_icon: завжди
        білий фон #ffffff + темна іконка #333333, БЕЗ прив'язки до
        теми) -- щоб обидві кнопки скидання вигляду (карта й графік)
        виглядали однаково."""
        canvas = self._alt_reset_view_btn
        canvas.delete("all")
        canvas.configure(bg="#ffffff")
        color = "#333333"
        L, m = 8, 4
        w = int(canvas.cget("width"))
        h = int(canvas.cget("height"))
        canvas.create_line(m, m, m + L, m, fill=color, width=2)
        canvas.create_line(m, m, m, m + L, fill=color, width=2)
        canvas.create_line(w - m, m, w - m - L, m, fill=color, width=2)
        canvas.create_line(w - m, m, w - m, m + L, fill=color, width=2)
        canvas.create_line(m, h - m, m + L, h - m, fill=color, width=2)
        canvas.create_line(m, h - m, m, h - m - L, fill=color, width=2)
        canvas.create_line(w - m, h - m, w - m - L, h - m, fill=color, width=2)
        canvas.create_line(w - m, h - m, w - m, h - m - L, fill=color, width=2)


    def _reset_alt_zoom(self):
        """Клік по піктограмі у верхньому лівому куті графіка висот --
        повертає до початкового вигляду (весь маршрут), з БУДЬ-ЯКОГО
        поточного (в т.ч. послідовного, кілька разів поспіль) зуму."""
        self._alt_zoom_range = None
        self._redraw_altitude_profile()


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

        new_zoom = max(0, min(24, self.zoom_var.get() + direction))
        if new_zoom == self.zoom_var.get():
            return "break"
        self.zoom_var.set(new_zoom)
        # дебаунс: швидка прокрутка колеса (кожен тик -- нова асинхронна
        # довантажка тайлів) інакше запускає купу паралельних запитів, що
        # навіть із генераційним захистом (див. render_map/_on_tiles_ready)
        # виглядає ривками -- чекаємо коротку паузу після ОСТАННЬОГО тика.
        # Число на self.map_zoom_label оновлюється миттєво (textvariable=
        # self.zoom_var) незалежно від цієї паузи -- відчуття "миттєвої
        # реакції" лишається, тільки сама картинка карти підтягується
        # трохи пізніше.
        if self._zoom_wheel_after_id is not None:
            self.after_cancel(self._zoom_wheel_after_id)
        self._zoom_wheel_after_id = self.after(120, self.render_map)
        # зупиняємо подію ТУТ -- інакше вона спливе далі до глобального
        # перехоплювача прокрутки сторінки (_on_mission_wheel), і те саме
        # колесо одночасно й зумило б карту, і гортало сторінку
        return "break"


    def _on_map_pan_press(self, event):
        """Початок панорамування -- запам'ятовуємо стартову точку й (для
        "віконного" режиму) поточний геоцентр, щоб на відпусканні
        порахувати, куди саме він переїхав."""
        self._pan_start = {
            "x": event.x, "y": event.y,
            "center_lat": self._map_center_lat, "center_lon": self._map_center_lon,
        }
        self._pan_last_dx = self._pan_last_dy = 0
        if not self._map_viewport_mode:
            # оглядовий режим -- уся мозаїка вже завантажена одним шматком,
            # звичайне scan-панорамування по великому scrollregion
            self.map_canvas.scan_mark(event.x, event.y)


    def _on_map_pan_motion(self, event):
        if self._pan_start is None:
            return
        if self._map_viewport_mode:
            # "Віконний" режим: scrollregion дорівнює розміру самого
            # канваса (render_viewport) -- scan_dragto тут просто нічого
            # не прокрутить (нема куди). Натомість рухаємо ВСІ об'єкти
            # канваса напряму, наживо, без перезавантаження тайлів --
            # справжній перерахунок і фетч нових тайлів станеться щойно
            # по відпусканню (_on_map_pan_release).
            dx = event.x - self._pan_start["x"]
            dy = event.y - self._pan_start["y"]
            self.map_canvas.move("all", dx - self._pan_last_dx, dy - self._pan_last_dy)
            self._pan_last_dx, self._pan_last_dy = dx, dy
        else:
            self.map_canvas.scan_dragto(event.x, event.y, gain=1)


    def _on_map_pan_release(self, event):
        if self._pan_start is None:
            return
        start = self._pan_start
        self._pan_start = None

        if not self._map_viewport_mode:
            # оглядовий режим -- нічого довантажувати не треба, тайли й
            # так вже всі завантажені, canvas сам запам'ятав нову позицію
            # прокрутки
            return

        dx = event.x - start["x"]
        dy = event.y - start["y"]
        self._pan_last_dx = self._pan_last_dy = 0

        if dx == 0 and dy == 0:
            return
        if self._last_map_render is None or start["center_lat"] is None:
            return

        r = self._last_map_render
        zoom = r["zoom"]
        # зсув курсора в ЕКРАННИХ пікселях -- це той самий зсув у
        # глобальних пікселях цього zoom (масштаб завжди 1:1 у
        # "віконному" режимі, на відміну від оглядового), тому просто
        # віднімаємо його від поточного геоцентру
        cx, cy = lonlat_to_pixel(start["center_lat"], start["center_lon"], zoom)
        new_center_gx, new_center_gy = cx - dx, cy - dy
        new_lat, new_lon = pixel_to_lonlat(new_center_gx, new_center_gy, zoom)
        self._map_center_lat = new_lat
        self._map_center_lon = new_lon

        # Якщо нова видима область (після панорамування) ПОВНІСТЮ
        # вкладається в уже завантажений запас тайлів (compute_viewport_
        # tile_bounds фетчить із запасом buffer_factor -- завжди більше,
        # ніж просто видима область) -- НІЧОГО не перезавантажуємо. Тайли
        # вже наживо пересунуті canvas.move() під час руху миші й
        # виглядають коректно. Раніше тут ЗАВЖДИ викликався render_map()
        # -- навіть на панорамування на 5px -- що на кожне відпускання
        # кнопки миші робило canvas.delete("all") і перемальовку заново,
        # і давало помітний "спалах" порожнього канваса між ними.
        canvas_w = max(self.map_canvas.winfo_width(), 1)
        canvas_h = max(self.map_canvas.winfo_height(), 1)
        view_left = new_center_gx - canvas_w / 2
        view_right = new_center_gx + canvas_w / 2
        view_top = new_center_gy - canvas_h / 2
        view_bottom = new_center_gy + canvas_h / 2

        buf_left = r["tx_min"] * TILE_SIZE
        buf_right = (r["tx_max"] + 1) * TILE_SIZE
        buf_top = r["ty_min"] * TILE_SIZE
        buf_bottom = (r["ty_max"] + 1) * TILE_SIZE

        still_covered = (
            view_left >= buf_left and view_right <= buf_right
            and view_top >= buf_top and view_bottom <= buf_bottom
        )
        if still_covered:
            # оновлюємо збережений origin, щоб drag точок/підказка при
            # наведенні (mission_editor._current_map_geom) далі рахували
            # координати від АКТУАЛЬНОЇ позиції мозаїки на екрані
            r["origin_gx"] = int(view_left)
            r["origin_gy"] = int(view_top)
            return

        self.render_map()



    def _find_native_fit_zoom(self, canvas_width: int, canvas_height: int, max_zoom: int = 24, min_zoom: int = 0):
        """
        Підбір зуму у стилі GMap.NET (SetZoomToFitRect/GetMaxZoomToFitRect
        -- той самий метод, що використовує сам Mission Planner): шукаємо
        МАКСИМАЛЬНИЙ зум, на якому тісна геообласть маршруту (з невеликим
        відступом, БЕЗ жодного аспект-розширення чи майбутнього
        масштабування) вже ПОМІЩАЄТЬСЯ ЦІЛКОМ у (canvas_width,
        canvas_height) -- одразу "як є", без подальшого resize.

        ВАЖЛИВО: рахуємо розмір маршруту ТОЧНОЮ проекцією координат
        (lonlat_to_pixel), а НЕ через compute_tile_bounds -- та
        округлює діапазон до ЦІЛИХ тайлів (256px блоки), що на
        низькому зумі, де сам тайл -- відчутна частка всього маршруту,
        могло РОЗДУВАТИ оцінку вдвічі й більше (напр. реальні 374px
        оберталися на 768px лише через невдале вирівнювання по межах
        тайлів) -- і через це підбір був значно консервативнішим, ніж
        реально потрібно (обирав зум на рівень нижче, хоча наступний
        вищий уже прекрасно вміщувався). compute_tile_bounds лишається
        придатною для СКАЧУВАННЯ тайлів (там округлення до цілих
        тайлів обов'язкове), але не для ЦІЄЇ перевірки "чи влазить".

        Це свідома зміна архітектури: раніше карта на "Місія" збирала
        ВСЮ мозаїку тайлів в одну велику PIL-картинку й масштабувала її
        одним PIL.resize() під точний розмір канваса (~1 секунда на
        відмальовку, вимірювано). Mission Planner (через бібліотеку
        GMap.NET) цього кроку не робить ВЗАГАЛІ -- лише підбирає зум із
        дискретних рівнів і малює кожен тайл окремо, 1:1, без склейки.

        Повертає None, якщо у маршруту взагалі немає точок.
        """
        nav_wps = self.analyzer.nav_wps if self.analyzer is not None else None
        if not nav_wps:
            return None
        lats = [wp.lat for wp in nav_wps]
        lons = [wp.lon for wp in nav_wps]
        # той самий відступ (5%), що й раніше в compute_tile_bounds --
        # лишається доречним і тут, лише БЕЗ округлення до тайлів після
        pad_lat = max((max(lats) - min(lats)) * 0.05, 0.002)
        pad_lon = max((max(lons) - min(lons)) * 0.05, 0.002)
        lat_min, lat_max = min(lats) - pad_lat, max(lats) + pad_lat
        lon_min, lon_max = min(lons) - pad_lon, max(lons) + pad_lon

        best_zoom = None
        fallback_zoom = None
        fallback_diff = None
        for z in range(max_zoom, min_zoom - 1, -1):
            x1, y1 = lonlat_to_pixel(lat_max, lon_min, z)  # north-west
            x2, y2 = lonlat_to_pixel(lat_min, lon_max, z)  # south-east
            native_w = abs(x2 - x1)
            native_h = abs(y2 - y1)

            diff = max(native_w - canvas_width, native_h - canvas_height, 0)
            if fallback_diff is None or diff < fallback_diff:
                fallback_diff = diff
                fallback_zoom = z

            if native_w <= canvas_width and native_h <= canvas_height:
                best_zoom = z
                break  # перебираємо ЗВЕРХУ ВНИЗ -- перший, що влазить, і є максимальним

        if best_zoom is not None:
            return best_zoom
        # жоден зум не влазить ІДЕАЛЬНО (патологічно витягнутий маршрут) --
        # беремо той, що ближче за все підходить, як розумний запасний варіант
        if fallback_zoom is not None:
            return fallback_zoom
        return min_zoom


    def _find_best_fit_zoom(self, canvas_width: int, canvas_height: int | None = None, max_zoom: int = 24, min_zoom: int = 0):
        """
        Підбирає зум, на якому РЕЗУЛЬТУЮЧА висота (після масштабування
        вже аспект-скоригованої compute_tile_bounds(target_aspect=...)
        мозаїки рівно по ширині канваса) найближча до canvas_height.

        Чому не просто "нативна ширина близька до canvas_width" (як
        було): на низькому зумі діапазон тайлів -- лише 1-2 тайли,
        розширення під target_aspect округлюється до ЦІЛОГО числа
        тайлів і може дати помітно ГІРШЕ наближення до пропорцій
        канваса (напр. вийде рівно 2.0 замість потрібних 2.33) --
        хоча його нативна ШИРИНА при цьому випадково опиняється
        числово ближче до canvas_width, ніж у сусіднього зуму з
        кращими пропорціями. Порівняння за РЕЗУЛЬТУЮЧОЮ висотою прямо
        відображає те, що реально важливо: наскільки добре картинка
        після масштабування влізе в канвas без обрізання чи недобору.

        ПРИМІТКА: цей метод БІЛЬШЕ НЕ використовується для початкового
        показу карти на "Місія" (див. _find_native_fit_zoom вище) --
        лишений як є на випадок, якщо десь іще знадобиться підхід
        "розтягнути мозаїку під канвас".

        Повертає None, якщо у маршруту взагалі немає точок.
        """
        target_aspect = (canvas_width / canvas_height) if canvas_height else None
        best_zoom = None
        best_diff = None
        for z in range(max_zoom, min_zoom - 1, -1):
            try:
                tx_min, tx_max, ty_min, ty_max, total = compute_tile_bounds(
                    self.analyzer, z, max_tiles=self.max_tiles_var.get(), target_aspect=target_aspect,
                )
            except MapTooLargeError:
                continue
            except ValueError:
                return None
            native_w = (tx_max - tx_min + 1) * TILE_SIZE
            native_h = (ty_max - ty_min + 1) * TILE_SIZE
            if canvas_height is not None and native_w > 0:
                final_h = native_h * (canvas_width / native_w)
                diff = abs(final_h - canvas_height)
            else:
                diff = abs(native_w - canvas_width)
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_zoom = z
        return best_zoom if best_zoom is not None else min_zoom


    def render_map(self, auto_zoom: bool = False):
        if self.analyzer is None:
            messagebox.showwarning(i18n.t("msg_no_data_title"), i18n.t("msg_no_data_body"))
            return

        # Єдиний механізм відмальовки для ВСІХ випадків -- як у Mission
        # Planner (через бібліотеку GMap.NET): кожен тайл малюється
        # окремо, 1:1, БЕЗ склейки в одну мозаїку й БЕЗ подальшого
        # PIL.resize() (це й було головним гальмом -- ~1 секунда на
        # відмальовку, вимірювано [timing]-логами раніше). "Показати
        # весь маршрут" (auto_zoom) відрізняється від "користувач
        # покрутив зум/попанорамував" лише тим, ЯКИЙ центр і зум
        # використовується для розрахунку -- сам виклик
        # compute_viewport_tile_bounds/render_viewport той самий.
        self.map_canvas.update_idletasks()
        map_w = self.map_canvas.winfo_width()
        canvas_w = map_w if map_w > 10 else max(self.mission_content.winfo_width() - 34, 100)
        # ВАЖЛИВО: примусово (синхронно) перераховуємо висоту вьюпорту
        # ПЕРЕД тим, як міряти map_canvas.winfo_height() -- інакше, якщо
        # це перший рендер одразу після завантаження місії, дебаунс-
        # таймер _apply_viewport_heights (запускається на <Configure>,
        # з паузою) міг ще не встигнути спрацювати жодного разу, і
        # map_canvas_frame лишався б на ПОЧАТКОВІЙ, значно меншій
        # висоті (self._MAP_FRAME_MIN_H, виставленій при побудові
        # сторінки) -- підбір зуму (_find_native_fit_zoom нижче) тоді
        # орієнтувався б на занижену висоту й обирав зум КОНСЕРВАТИВНІШЕ,
        # ніж насправді вміщується (напр. zoom=6 замість 7, коли 7 вже
        # чудово поміщається в реальний, повністю "осілий" розмір вікна).
        self._apply_viewport_heights()
        self.map_canvas.update_idletasks()
        canvas_h = self.map_canvas.winfo_height()
        if canvas_h <= 10:
            canvas_h = getattr(self, "_viewport_h", None) or getattr(self, "_MAP_FRAME_MAX_H", None) or 500

        is_initial_view = auto_zoom or self._map_center_lat is None
        if is_initial_view:
            # Нова місія (чи перший рендер) -- підбираємо зум і центр
            # заново під сам маршрут: геоцентр усіх точок, і зум --
            # МАКСИМАЛЬНИЙ, на якому тісна геообласть маршруту вже
            # вміщується в канвас "як є" (_find_native_fit_zoom, той
            # самий підхід, що й GetMaxZoomToFitRect у GMap.NET).
            # Запам'ятовуємо як self._fit_zoom -- поріг, нижче/на якому
            # центр наступних рендерів -- знову геоцентр маршруту
            # (не місце, де користувач востаннє панoramував).
            zoom = self._find_native_fit_zoom(canvas_w, canvas_h)
            if zoom is None:
                messagebox.showwarning(i18n.t("msg_no_points_title"), i18n.t("msg_no_points_body"))
                return
            self.zoom_var.set(zoom)
            self._fit_zoom = zoom
            self._initial_auto_zoom = zoom
            nav_wps = self.analyzer.nav_wps
            if nav_wps:
                # ВАЖЛИВО: геометричний ЦЕНТР РАМКИ маршруту
                # ((min+max)/2), а НЕ середнє арифметичне координат
                # точок (центроїд)! Якщо точки розподілені нерівномірно
                # (густо на одному кінці маршруту, рідко на іншому --
                # звичайна ситуація для реальних місій), центроїд
                # зміщується у бік, де точок більше, і центрування
                # viewport на ньому лишає різний запас по краях: забагато
                # з одного боку, і потенційно ОБРІЗАЄ маршрут з іншого,
                # навіть якщо _find_native_fit_zoom коректно порахував,
                # що тісна рамка "влазить" у розмір канваса -- та
                # перевірка симетрична відносно ЦЕНТРУ РАМКИ, а не
                # центроїда точок.
                lats = [wp.lat for wp in nav_wps]
                lons = [wp.lon for wp in nav_wps]
                self._map_center_lat = (min(lats) + max(lats)) / 2
                self._map_center_lon = (min(lons) + max(lons)) / 2
        else:
            zoom = int(self.zoom_var.get())

        viewport_center = (self._map_center_lat, self._map_center_lon)
        tx_min, tx_max, ty_min, ty_max = compute_viewport_tile_bounds(
            self._map_center_lat, self._map_center_lon, zoom, canvas_w, canvas_h,
        )
        total = (tx_max - tx_min + 1) * (ty_max - ty_min + 1)

        self._map_viewport_mode = True

        # Лічильник поколінь рендеру -- захист від "гонки": швидка
        # прокрутка колеса (кожен тик викликає render_map()) запускає
        # КІЛЬКА паралельних асинхронних завантажень; без цієї перевірки
        # старіший (повільніший) запит міг би завершитись ПІЗНІШЕ за
        # новіший і на мить показати НЕПРАВИЛЬНИЙ (застарілий) зум --
        # видиме "миготіння"/ривок. _on_tiles_ready звіряє generation і
        # просто ігнорує застарілий результат, якщо вже стартував новіший.
        self._map_render_generation = getattr(self, "_map_render_generation", 0) + 1
        my_generation = self._map_render_generation

        disk_cache = self.tilecache_var.get().strip() or None
        self.tile_cache = OnlineTileCache(provider=self.provider_key, disk_cache_dir=disk_cache)
        self._save_settings()

        self._cancel_event = threading.Event()
        self._map_loading = True
        self.map_status_var.set(i18n.t("status_loading_tiles_fmt", done=0, total=total))

        # Прогресивна відмальовка: почати ОДРАЗУ (плейсхолдери + маршрут,
        # синхронно, у головному потоці -- це дешева операція), а не
        # чекати завершення завантаження ВСІХ тайлів. Кожен тайл потім
        # домальовується ОКРЕМО по готовності (self._on_tile_ready).
        # placeholder_bg -- ТОЙ САМИЙ колір, що й фон канваса
        # (c["map_placeholder_bg"]), під поточну тему -- інакше в темній
        # темі при кожному новому рендері мигав би контрастний
        # СВІТЛО-СІРИЙ прямокутник на місці кожного тайла, поки він
        # завантажується.
        c = self._mission_colors()
        screen_origin_gx, screen_origin_gy = begin_viewport_render(
            self.map_canvas, self.analyzer, zoom, viewport_center[0], viewport_center[1],
            tx_min, tx_max, ty_min, ty_max, self._map_images,
            placeholder_bg=c["map_placeholder_bg"], placeholder_outline=c["map_placeholder_bg"],
        )
        self._map_found = 0
        self._map_undecodable = 0

        def progress_cb(done, tot):
            if my_generation != self._map_render_generation:
                return
            self.after(0, lambda: self.map_status_var.set(i18n.t("status_loading_tiles_fmt", done=done, total=tot)))

        def on_tile_ready(tx, ty, data):
            # виконується у ФОНОВОМУ потоці (as_completed у fetch_tiles) --
            # саму відмальовку (Tkinter, тільки з головного потоку!)
            # передаємо через self.after(0, ...), з перевіркою покоління
            # ПЕРЕД РЕАЛЬНИМ малюванням -- швидка прокрутка колеса зуму
            # запускає кілька рендерів поспіль, і застарілий (вже
            # неактуальний) тайл не повинен домалюватись поверх нового.
            self.after(0, lambda: self._on_tile_ready(tx, ty, data, my_generation, screen_origin_gx, screen_origin_gy))

        def worker():
            tiles, cancelled = fetch_tiles(
                self.tile_cache, tx_min, tx_max, ty_min, ty_max, zoom,
                progress_cb=progress_cb, cancel_event=self._cancel_event,
                tile_ready_cb=on_tile_ready,
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
                    occupied_polygons, occupied_date, viewport_center,
                    generation=my_generation, is_initial_view=is_initial_view,
                    screen_origin_gx=screen_origin_gx, screen_origin_gy=screen_origin_gy,
                ),
            )

        threading.Thread(target=worker, daemon=True).start()


    def _on_tile_ready(self, tx, ty, data, generation, screen_origin_gx, screen_origin_gy):
        """Домальовує ОДИН тайл на карті "Місія" одразу по готовності --
        частина прогресивної відмальовки (render_map -> worker() ->
        on_tile_ready). Викликається ОКРЕМО на кожен тайл, з головного
        потоку (через self.after(0, ...))."""
        if generation != self._map_render_generation:
            return  # застарілий рендер (стартував новіший, поки цей тайл ще довантажувався) -- відкидаємо мовчки
        result = draw_single_tile(self.map_canvas, self._map_images, tx, ty, data, screen_origin_gx, screen_origin_gy)
        if result == "found":
            self._map_found += 1
        elif result == "undecodable":
            self._map_undecodable += 1


    def _on_tiles_ready(
        self, tiles, zoom, tx_min, tx_max, ty_min, ty_max, cancelled,
        occupied_polygons=None, occupied_date=None, viewport_center=None,
        generation=None, is_initial_view=False,
        screen_origin_gx=None, screen_origin_gy=None,
    ):
        # застарілий результат (стартував новіший рендер, поки цей ще
        # довантажувався) -- відкидаємо мовчки, інакше повільніший запит
        # міг би домалювати НЕПРАВИЛЬНИЙ зум ПІСЛЯ новішого й дати помітний
        # ривок/миготіння при швидкій прокрутці колеса
        if generation is not None and generation != self._map_render_generation:
            return

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

        # УСІ тайли вже домальовані ПРОГРЕСИВНО, по одному, ще до цього
        # моменту (_on_tile_ready -- викликався на кожен тайл окремо по
        # готовності). Тут НЕ перемальовуємо все заново -- лише
        # домальовуємо полігони окупованих територій (вони готові лише
        # тепер, після fetch_occupied_geojson, окремо від самих тайлів)
        # і піднімаємо маршрут над ними.
        if occupied_polygons:
            _draw_polygon_overlay(self.map_canvas, occupied_polygons, zoom, screen_origin_gx, screen_origin_gy)
            self.map_canvas.tag_raise("route_layer")

        found = self._map_found
        undecodable = self._map_undecodable
        total = (tx_max - tx_min + 1) * (ty_max - ty_min + 1)
        missing = total - found - undecodable
        origin_gx, origin_gy = int(screen_origin_gx), int(screen_origin_gy)

        self._last_map_render = {
            "mode": "overview" if is_initial_view else "viewport",
            "tiles": tiles, "zoom": zoom,
            "tx_min": tx_min, "tx_max": tx_max, "ty_min": ty_min, "ty_max": ty_max,
            "occupied_polygons": occupied_polygons,
            "center_lat": viewport_center[0], "center_lon": viewport_center[1],
            "origin_gx": origin_gx, "origin_gy": origin_gy,
        }
        if is_initial_view:
            # ОКРЕМИЙ, НЕЗМІННИЙ знімок саме початкового (auto_zoom)
            # рендеру -- на відміну від _last_map_render (оновлюється
            # на КОЖЕН рендер, включно з ручним зумом/панорамуванням
            # користувача пізніше) цей ЗАВЖДИ лишається тим самим
            # виглядом "весь маршрут на екран", яким був одразу після
            # завантаження місії. "Аналіз → Маршрут" бере карту саме
            # звідси (_load_trajectory_map в analysis_page.py) -- щоб
            # показувати завжди огляд усього маршруту, незалежно від
            # того, як користувач потім покрутив зум на "Місія".
            self._initial_map_render = dict(self._last_map_render)

        status = i18n.t("status_rendered_fmt", found=found, total=total, missing=missing)
        if undecodable:
            status += i18n.t("status_undecodable_suffix_fmt", n=undecodable)
        pending_notice = getattr(self, "_pending_zoom_cap_notice", None)
        if pending_notice:
            status = f"{pending_notice}  {status}"
            self._pending_zoom_cap_notice = None
        self.map_status_var.set(status)

        if undecodable and not self._pil_warning_shown:
            self._pil_warning_shown = True
            messagebox.showinfo(i18n.t("msg_need_pillow_title"), i18n.t("msg_need_pillow_body", n=undecodable))

        self._redraw_altitude_profile()


