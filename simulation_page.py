"""
simulation_page.py -- вкладка "Симуляция": полёт XX-1 на собственном
ArduPlane SITL по уже загруженной миссии. Физику считает SITL; страница
подключается к нему по MAVLink, показывает HUD, носовую камеру и карту
миссии и позволяет управлять бортом (ARM, режимы, клавиатура, джойстик).

HUD -- копия HUD из X-Plane поверх видео носовой камеры (hud_view.py); настройки -- на странице «Конфігурація».

Страница собрана из частей (примесей): sim_sitl, sim_control, sim_joystick, sim_camera, sim_maps, sim_impact, sim_hud, sim_scene, sim_flight,
sim_config_page, sim_commands. Здесь остаётся построение страницы, старт и остановка симуляции и главный такт.
"""
from __future__ import annotations

import atexit
import os
import sys
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")      # pygame при первом импорте печатает приветствие «pygame-ce 2.x (SDL ...)»: оно в консоли не нужно (joystick_input ниже подтягивает pygame)
import tkinter as tk
from tkinter import ttk, messagebox
import i18n
from geo import haversine_m
from terrain_impact import TerrainImpactDetector
from joystick_input import JoystickManager, load_joystick_mapping, default_joystick_mapping_path
# Имена прежнего simulation_page (константы и функции камеры, SITL, карт и т.д.) остаются доступны отсюда: см. __all__.
from sim_common import (
    FPV_PALETTES,
    LOCAL_HOSTS,
    connection_is_local_sitl,
)
from sim_sitl import (
    SitlMixin,
    SITL_TCP_PORTS,
    tcp_port_busy,
    parse_netstat_owners,
    _run_text,
    find_port_owners,
    tasklist_image,
    sitl_busy_ports,
    sitl_failure_reason,
)
from sim_control import (
    ControlMixin,
    FORCE_DISARM_MAGIC,
)
from sim_joystick import (
    JoystickMixin,
)
from sim_camera import (
    CameraMixin,
    NOSE_CAM_ZOOM_BASE,
    NOSE_CAM_ZOOM_MAX,
    SIM_CAMERA_PROVIDER,
    NOSE_CAM_MAX_TILES,
    CAMERA_FOV_DEG,
    CAMERA_MIN_HALF_KM,
    CAMERA_MAX_HALF_KM,
    FETCH_MARGIN_MULT,
    FPV_MAX_RANGE_M,
    FPV_MAX_ZOOM,
    FPV_LOAD_TIMEOUT_S,
    FPV_ERROR_GRACE_S,
    camera_half_km_for_altitude,
    compose_and_rotate_pil,
    pil_to_photo,
)
from sim_maps import (
    MapsMixin,
    SIM_MISSION_MAP_PROVIDER,
    MAX_PAN_FRACTION,
    TRAIL_LENGTH_PX,
)
from sim_impact import (
    ImpactMixin,
    OBSTACLE_LABELS,
    TERRAIN_LAND_ZONE_M,
)
from sim_hud import (
    HudMixin,
)
from sim_scene import (
    SceneMixin,
)
from sim_flight import (
    FlightMixin,
)
from sim_config_page import (
    ConfigPageMixin,
)
from flyby_panel import FlybyBar
from sim_launch import LaunchMixin
from sim_restart import RestartMixin
from sim_limits import LimitsMixin
from sim_seek import SeekMixin
from sim_commands import (
    CommandsMixin,
)

from sim_fullscreen import FullscreenMixin

__all__ = [
    "SimulationPageMixin",
    "CAMERA_FOV_DEG",
    "CAMERA_MAX_HALF_KM",
    "CAMERA_MIN_HALF_KM",
    "FETCH_MARGIN_MULT",
    "FORCE_DISARM_MAGIC",
    "FPV_ERROR_GRACE_S",
    "FPV_LOAD_TIMEOUT_S",
    "FPV_MAX_RANGE_M",
    "FPV_MAX_ZOOM",
    "FPV_PALETTES",
    "LOCAL_HOSTS",
    "MAX_PAN_FRACTION",
    "NOSE_CAM_MAX_TILES",
    "NOSE_CAM_ZOOM_BASE",
    "NOSE_CAM_ZOOM_MAX",
    "OBSTACLE_LABELS",
    "SIM_CAMERA_PROVIDER",
    "SIM_MISSION_MAP_PROVIDER",
    "SITL_TCP_PORTS",
    "TERRAIN_LAND_ZONE_M",
    "TRAIL_LENGTH_PX",
    "_run_text",
    "camera_half_km_for_altitude",
    "compose_and_rotate_pil",
    "connection_is_local_sitl",
    "find_port_owners",
    "parse_netstat_owners",
    "pil_to_photo",
    "sitl_busy_ports",
    "sitl_failure_reason",
    "tasklist_image",
    "tcp_port_busy",
]

SIM_STEP_MS = 100

HEADING_REFRESH_THRESHOLD_DEG = 15.0

CENTER_REFRESH_FRACTION = 0.35

FPV_PALETTE_LABELS = ("opt_sim_pal_white", "opt_sim_pal_black", "opt_sim_pal_iron", "opt_sim_pal_color")

FPV_DEFAULT_PALETTE = "white_hot"

KB_THROTTLE_RATE = 0.5  # уровень газа с клавиатуры (MAVLink): доля полного хода в секунду



class SimulationPageMixin(SitlMixin, ControlMixin, JoystickMixin, CameraMixin, MapsMixin, ImpactMixin, HudMixin, SceneMixin, FlightMixin, ConfigPageMixin, CommandsMixin, SeekMixin, LimitsMixin, LaunchMixin, RestartMixin, FullscreenMixin):
    """Вкладка «Симуляция»: собирается из примесей по предметным областям."""

    def _build_simulation_page(self, content, pad):
        page = ttk.Frame(content)
        page.grid(row=0, column=0, sticky="nsew")
        self.pages["simulation"] = page

        top = ttk.Frame(page)
        top.pack(fill="x", **pad)
        self.sim_start_btn = ttk.Button(top, command=self._on_sim_start_stop)
        self._reg_i18n(self.sim_start_btn, "text", "btn_sim_start")
        self.sim_start_btn.pack(side="left")

        # --- SITL launcher (ArduPlane.elf) ---
        # Пути по умолчанию -- под конкретную сборку. При
        # необходимости заменить здесь или вынести в настройки.
        self._sitl_proc = None
        self._sitl_log_file = None
        atexit.register(self._sitl_kill_at_exit)     # закрыли программу -- SITL не остаётся держать порты
        # Пути к SITL -- ОТНОСИТЕЛЬНО папки программы, а не абсолютные.
        # Работает на любом компе, где лежит проект (или .exe).
        _base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
                else os.path.dirname(os.path.abspath(__file__))
        self._sitl_cwd = os.path.join(_base, "SITL")
        self._sim_settings_path = os.path.join(_base, "settings.json")
        self._sitl_elf_path = os.path.join(self._sitl_cwd, "ArduPlane.elf")
        self._sitl_port = 14550
        self.sim_sitl_btn = ttk.Button(top, text="SITL: START", width=14,
                                        command=self._on_sitl_toggle)
        self.sim_sitl_btn.pack(side="left", padx=(6, 0))

        # --- кнопки управления бортом (MAVLink-режим) ---
        # Появляются всегда, активируются только когда запущена
        # симуляция с источником MAVLINK.
        self.sim_arm_btn = ttk.Button(top, text="ARM", width=8,
                                       command=self._on_sim_arm_click,
                                       state="disabled")
        self.sim_arm_btn.pack(side="left", padx=(6, 2))

        self._sim_mode_buttons = {}
        for _mk in ("MANUAL", "FBWA", "AUTO"):
            _b = ttk.Button(top, text=_mk, width=8,
                            command=lambda mk=_mk: self._on_sim_mode_button(mk),
                            state="disabled")
            _b.pack(side="left", padx=2)
            self._sim_mode_buttons[_mk] = _b

        # --- вид камеры: вперёд (FPV, как видит оператор) или вниз (надир, прежний вид) ---
        self._reg_i18n(ttk.Label(top), "text", "lbl_sim_camera_view").pack(side="left", padx=(16, 4))
        self._sim_view_key = "nadir" if self._load_pref("sim_camera_view", "fpv") == "nadir" else "fpv"
        self.sim_view_cb = ttk.Combobox(top, state="readonly", width=16,
                                        values=[i18n.t("opt_sim_view_fpv"), i18n.t("opt_sim_view_nadir")])
        self.sim_view_cb.current(1 if self._sim_view_key == "nadir" else 0)
        self.sim_view_cb.pack(side="left")
        self.sim_view_cb.bind("<<ComboboxSelected>>", self._on_sim_view_selected)

        # --- палитра теплового вида (камера тепловизионная); меняется на лету, «цветная» -- прежний вид снимка ---
        self._reg_i18n(ttk.Label(top), "text", "lbl_sim_thermal_palette").pack(side="left", padx=(16, 4))
        saved_palette = self._load_pref("sim_thermal_palette", FPV_DEFAULT_PALETTE)
        self._sim_palette_key = saved_palette if saved_palette in FPV_PALETTES else FPV_DEFAULT_PALETTE
        self.sim_palette_cb = ttk.Combobox(top, state="readonly", width=18, values=[i18n.t(k) for k in FPV_PALETTE_LABELS])
        self.sim_palette_cb.current(FPV_PALETTES.index(self._sim_palette_key))
        self.sim_palette_cb.pack(side="left")
        self.sim_palette_cb.bind("<<ComboboxSelected>>", self._on_sim_palette_selected)
        self._sim_fpv_quality_init()                          # «Плавність» видео живёт в «Конфіг» (вкладка HUD); здесь только загрузка выбора
        callbacks = getattr(self, "_retranslate_callbacks", None)
        if isinstance(callbacks, list):
            callbacks.append(self._sim_fpv_quality_retranslate)

        # --- рельеф (подсветка склонов и настоящая перспектива холмов по SRTM); без SRTM остаётся плоская земля ---
        saved_relief = self._load_pref("sim_fpv_relief", True)
        self._sim_relief_on = saved_relief if isinstance(saved_relief, bool) else True
        self.sim_relief_var = tk.BooleanVar(value=self._sim_relief_on)
        self.sim_relief_cb = ttk.Checkbutton(top, variable=self.sim_relief_var, command=self._on_sim_relief_toggled)
        self._reg_i18n(self.sim_relief_cb, "text", "chk_sim_relief")
        self.sim_relief_cb.pack(side="left", padx=(16, 0))
        self._sim_limits_build_top(top)                       # кнопка «Звук» красных кодов лимитов (sim_limits.py): одна кнопка, по умолчанию звук выключен

        self.sim_status_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.sim_status_var).pack(side="left", padx=(16, 0))

        joy_row = ttk.Frame(page)
        joy_row.pack(fill="x", **pad)
        self._reg_i18n(ttk.Label(joy_row), "text", "lbl_joystick").pack(side="left", padx=(0, 4))
        self.sim_joystick_var = tk.StringVar(value="")
        self.sim_joystick_combo = ttk.Combobox(joy_row, textvariable=self.sim_joystick_var, state="readonly", width=28)
        self.sim_joystick_combo.pack(side="left")
        self.sim_joystick_refresh_btn = ttk.Button(joy_row, command=self._on_sim_joystick_refresh)
        self._reg_i18n(self.sim_joystick_refresh_btn, "text", "btn_joystick_refresh")
        self.sim_joystick_refresh_btn.pack(side="left", padx=(6, 0))
        self.sim_joystick_connect_btn = ttk.Button(joy_row, command=self._on_sim_joystick_connect)
        self._reg_i18n(self.sim_joystick_connect_btn, "text", "btn_joystick_connect")
        self.sim_joystick_connect_btn.pack(side="left", padx=(6, 0))
        self.sim_joystick_calibrate_btn = ttk.Button(joy_row, command=self._on_sim_joystick_calibrate, state="disabled")
        self._reg_i18n(self.sim_joystick_calibrate_btn, "text", "btn_joystick_calibrate")
        self.sim_joystick_calibrate_btn.pack(side="left", padx=(6, 0))

        self.sim_joystick_status_var = tk.StringVar(value="")
        ttk.Label(joy_row, textvariable=self.sim_joystick_status_var).pack(side="left", padx=(12, 0))

        # sim_mode_var -- хранит текущий выбранный режим. Управление
        # -- через верхние кнопки MANUAL/FBWA/AUTO (с подсветкой по
        # реальному custom_mode из HEARTBEAT). Нижний ряд радиокнопок
        # удалён как визуальный дубль.
        self.sim_mode_var = tk.StringVar(value="AUTO")

        # Что реально считает полёт (собственный SITL, модель XX-1, рельеф, солнце, препятствия) на странице НЕ показывается: строка «Фізика: …» пишется в sim_commands.log
        # («[фізика] …») при каждом изменении и при старте симуляции.
        self._update_sim_physics_info()
        self.after(1000, self._refresh_sim_physics_info_loop)

        # Облёт 3D-объектов: видео MP4 создаётся само после полёта, здесь же просмотр (полоска внизу страницы; настройки -- на «Конфігурація»)
        self._fs_build(page, pad)                             # полоса пиктограмм (звук, полный экран): самая нижняя на странице; sim_fullscreen.py
        self._flyby_bar = FlybyBar(page, self, auto=self._load_pref("flyby_auto", True) is not False, on_auto=lambda v: self._save_pref("flyby_auto", bool(v)))
        self._flyby_bar.pack(side="bottom", fill="x", **pad)
        callbacks = getattr(self, "_retranslate_callbacks", None)
        if isinstance(callbacks, list):
            callbacks.append(self._flyby_bar.retranslate)

        body = ttk.Frame(page)
        body.pack(fill="both", expand=True, **pad)

        # Слева HUD больше нет: все показатели выводятся поверх видео (hud_view.py, настройки -- на «Конфігурація»).

        map_frame = ttk.Frame(body)
        map_frame.pack(side="left", fill="both", expand=True)
        map_frame.rowconfigure(0, weight=1)
        map_frame.columnconfigure(0, weight=60)
        map_frame.columnconfigure(1, weight=40)

        # Слева -- носовая камера (супутник), справа -- карта миссии.
        self.sim_map_canvas = tk.Canvas(map_frame, bg="#1e1e1e", highlightthickness=0)
        self.sim_map_canvas.grid(row=0, column=0, sticky="nsew")

        # справа: карта миссии, под ней полоска перемотки по вейпоинтам (sim_seek.py)
        right_frame = ttk.Frame(map_frame, width=378, height=120)         # размер задан: ширина полоски перемотки не должна отбирать место у камеры слева
        right_frame.grid_propagate(False)
        right_frame.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        right_frame.rowconfigure(0, weight=1)
        right_frame.columnconfigure(0, weight=1)
        self.sim_mission_canvas = tk.Canvas(right_frame, bg="#1e1e1e",
                                             highlightthickness=1,
                                             highlightbackground="#333")
        self.sim_mission_canvas.grid(row=0, column=0, sticky="nsew")

        self._sim_state = None
        self._sim_fpv_layers = None           # слои земли вида вперёд (создаются при старте)
        self._sim_fpv_worker = None           # рабочий поток кадров
        self._sim_thermal = None              # тепловизор: матрица и АРУ (создаётся при старте вида вперёд)
        self._sim_sun = None                  # Солнце на момент вылета: азимут, высота, источник (время или условное)
        self._sim_obstacles = None            # загрузчик слоя препятствий (ветряки, мачты, опоры, ЛЭП) для района миссии
        self._sim_prev_air = None             # положение борта на прошлом такте: проверка идёт по ОТРЕЗКУ пути
        self._sim_prev_scene_air = None       # то же для проверки столкновения с 3D-объектами
        self._sim_heights = None              # менеджер сетки высот SRTM (создаётся при старте, если рельеф включён и есть SRTM)
        self._sim_fpv_started = False
        self._sim_fpv_t0 = 0.0
        self._sim_fpv_latest = None
        self._sim_fpv_show_scheduled = False
        self._sim_fpv_photo = None
        self._sim_fpv_item = None
        self._sim_fpv_errors = 0
        self._sim_prep_busy = False          # идёт подготовка к старту (подключение, заливка миссии)
        self._sim_impact = None               # детектор столкновения с рельефом (создаётся при старте)
        self._sim_terrain_requested = set()   # тайлы SRTM, которые уже запрошены в фоне
        self._sim_after_id = None
        self._sim_map_ready = False
        self._sim_map_transform = None
        self._sim_map_center = None
        self._sim_map_radius_m = 0.0
        self._sim_map_altitude = 0.0
        self._sim_map_heading = 0.0
        self._sim_map_fetch_pending = False
        self._sim_map_pending_request = None
        self._sim_marker_id = None
        self._sim_trail_id = None
        self._sim_paused = False

        # Поля карты миссии (правый канвас):
        self._sim_mission_transform = None   # (zoom, origin_gx, origin_gy)
        self._sim_mission_map_images = []    # защита PhotoImage от GC
        self._sim_mission_render_generation = 0
        self._sim_mission_tile_cache = None
        self._sim_mission_last_size = None    # размер холста, под который нарисована карта миссии
        self._sim_mission_resize_job = None   # отложенная перерисовка при изменении размера окна
        self._sim_flown_path = []            # список (lat, lon) пройденного пути
        self._sim_seek_build(right_frame)
        self.sim_map_canvas.bind("<Button-1>", self._on_sim_map_click)
        # Правая карта рисуется один раз (при Start) под размер холста в тот момент;
        # без этого обработчика при растягивании окна ПОСЛЕ Start она не перерисовывалась.
        self.sim_mission_canvas.bind("<Configure>", self._on_sim_mission_canvas_configure)
        self._sim_map_images = []

        self._sim_keys = set()
        self.bind_all("<KeyPress>", self._sim_keyboard_press)
        self.bind_all("<KeyRelease>", self._sim_keyboard_release)

        self._sim_joystick = JoystickManager()
        self._sim_joystick_mapping = load_joystick_mapping(default_joystick_mapping_path())
        self._on_sim_joystick_refresh()

        self._sim_hud_init()
        self.after(0, self._sim_hud_attach_settings)    # когда весь интерфейс уже собран
        self._scene_init()
        self.after(0, self._sim_scene_attach_settings)
        self.after(0, self._sim_aircraft_attach_settings)
        self.after(0, self._sim_flyby_attach_settings)
        self.after(40, self._sim_config_tabs_attach)


    _sim_phys_last = None

    def _sim_physics_text(self) -> str:
        """Строка "Фізика: ..." -- что реально считает полёт (физика, модель, соединение, рельеф, солнце, препятствия)."""
        name = self._SITL_MODEL_NAME
        if not self._sitl_running():
            text = f"Фізика: SITL, модель {name} (SITL не запущено — кнопка SITL: START)"
        elif getattr(self, "_flight_conn", None) is None:
            text = f"Фізика: SITL, модель {name} (підключення — автоматично при Start)"
        else:
            ok, why = connection_is_local_sitl(self._flight_conn, self._sitl_port)
            text = (f"Фізика: SITL, модель {name}" if ok
                    else f"Фізика: SITL, модель {name} — з'єднання НЕ з локальним SITL ({why})")
        return text + self._sim_terrain_note() + self._sim_sun_note() + self._sim_obstacle_note()

    def _update_sim_physics_info(self):
        """Что считает полёт -- в журнал симуляции (sim_commands.log, «[фізика] …»), только когда оно изменилось: на странице этой строки больше нет."""
        try:
            text = self._sim_physics_text()
            if text != self._sim_phys_last:
                self._sim_phys_last = text
                if hasattr(self, "_sim_cmd_log"):
                    self._sim_cmd_log("[фізика] " + text)
        except Exception:
            pass

    def _refresh_sim_physics_info_loop(self):
        """Раз в секунду обновляет подпись: соединение и SITL меняются вне
        этой вкладки, событий оттуда нет."""
        try:
            self._update_sim_physics_info()
            self.after(1000, self._refresh_sim_physics_info_loop)
        except Exception:
            pass


    def _on_sim_start_stop(self):
        """Start: миссия берётся из памяти программы, подключение к локальному SITL и заливка миссии делаются
        здесь же (без возврата на вкладку «Місія»). На вкладку «Місія» отправляем только если миссии в памяти нет."""
        if self._sim_state is not None:
            self._sim_stop()
            return
        if self._sim_prep_busy:                    # подготовка уже идёт (двойной щелчок)
            return

        self._sim_clear_crash_marks()

        if not self._sim_mission_in_memory():
            if messagebox.askyesno(i18n.t("msg_sim_title"), i18n.t("msg_sim_need_mission_body")):
                self._show_page("mission")
            return

        ok, why = self._check_sitl_ready()
        if not ok:
            messagebox.showwarning(i18n.t("msg_sim_title"), why)
            return

        dirty = self._sitl_dirty_reason() if self.SITL_RESTART_WHEN_DIRTY else None
        if dirty:                                  # прошлый полёт кончился аварией или остановкой не на земле: борт в SITL не на старте -- перезапускаем SITL (sim_restart.py)
            self._sim_restart_sitl_then_start(dirty)
            return

        if getattr(self, "_flight_conn", None) is None:
            self._sim_prepare_connect()
        else:
            self._sim_prepare_after_connect()

    def _sim_begin(self):
        """Всё готово (миссия в памяти, соединение с локальным SITL, миссия залита): запускаем симуляцию."""
        self._sim_prep_busy = False
        problem = self._sim_conn_problem()            # пока шла подготовка, SITL или соединение могли измениться
        if problem is not None:
            self._sim_prepare_fail(problem)
            return
        nav_wps = self.analyzer.nav_wps
        first = nav_wps[0]
        self._sim_state = {
            "lat": first.lat, "lon": first.lon, "alt": 0.0, "heading": 0.0,
            "target_idx": 1, "nav_wps": nav_wps,
            "mode": self.sim_mode_var.get(), "armed": True,
            "source": "MAVLINK",
        }
        self._sim_last_real_custom_mode = None  # первый HEARTBEAT снова синхронизирует режим
        self._sim_kb_throttle_level = 0.0
        self._sim_sw_arm_last = None
        self._sim_sw_mode_last = None
        self._sim_seen_ack_seq = 0
        self._sim_seen_txt_seq = 0
        self._scene_wait_ready()                       # 3D-модели читаются в фоне с запуска программы: к полёту они должны быть готовы
        self._sim_commands_begin()
        self._sim_seek_begin()
        self._sim_limits_begin()
        self._sim_launch_begin()                       # готовность борта к ARM, хронология пуска в журнале (sim_launch.py)
        self._sim_blackbox_reset()
        self._sim_impact = TerrainImpactDetector()
        self._sim_terrain_requested = set()
        self._sim_preload_terrain()
        self._sim_start_obstacles(nav_wps)
        self._sim_prev_scene_air = None          # новая симуляция: путь для проверки 3D-объектов начинается заново
        self._sim_ground_under_last = None       # и «последняя известная земля под бортом» -- тоже
        try:
            if hasattr(self, "_sim_cmd_log"):                # что считает полёт: в журнал каждого запуска (на странице строки с этим больше нет)
                self._sim_cmd_log("[фізика] " + self._sim_physics_text())
        except Exception:
            pass
        try:
            if hasattr(self, "_sim_cmd_log"):                # про звук столкновения: что лежит в sounds/ (нет файла -- сказано заранее, а не только при ударе)
                import alert_sound
                state, text, _info = alert_sound.describe()
                self._sim_cmd_log("[звук] зіткнення: " + {"ok": f"файл {text}", "missing": f"немає файлу {text} (або .mp3): звуку зіткнення не буде", "bad": f"файл не підходить: {text}"}[state])
        except Exception:
            pass
        self._start_sim_mavlink_telemetry()
        self._sim_cam_start()
        self._flight_start()
        self.sim_start_btn.configure(text=i18n.t("btn_sim_stop"), state="disabled")
        self.sim_status_var.set(i18n.t("status_sim_loading_map"))
        self._sim_map_ready = False
        self._sim_map_center = None
        self._sim_map_fetch_pending = False
        self._sim_map_pending_request = None
        self._sim_flown_path = []
        self._sim_mission_transform = None
        self._sim_render_mission_map()
        self._sim_view_lock(True)
        if self._sim_view_key == "fpv":
            self._sim_fpv_begin(first)
        else:
            self._sim_load_nose_cam(first.lat, first.lon, 100.0, 0.0, start_after=True)


    def _sim_launch_pending_request_if_any(self):
        """Після завершення будь-якого запиту -- перевіряє, чи не
        накопичився відкладений, і якщо так, запускає його."""
        req = self._sim_map_pending_request
        if req is None:
            return
        self._sim_map_pending_request = None
        lat, lon, alt, hdg, start_after = req
        self._sim_load_nose_cam(lat, lon, alt, hdg, start_after=start_after)



    def _sim_stop(self, status_key: str = "status_sim_stopped"):
        try:
            self._sim_stop_check_parked()          # остановка заармленным или в воздухе -- борту в SITL нужен перезапуск перед новым стартом (sim_restart.py)
        except Exception:
            pass
        try:
            self._sim_seek_shutdown()              # перемотка отменяется, ускорение SITL возвращается, пока соединение ещё есть
        except Exception:
            pass
        try:
            self._sim_limits_end()                 # звук кодов лимитов обрывается, коды с HUD убираются
        except Exception:
            pass
        try:
            self._sim_launch_end()                 # отложенный ARM отменяется
        except Exception:
            pass
        self._flight_stop()
        self._sim_cam_stop()
        if self._sim_after_id is not None:
            self.after_cancel(self._sim_after_id)
            self._sim_after_id = None
        if self._sim_state is not None and self._sim_state.get("source") == "MAVLINK":
            conn = self._sim_conn()
            if conn is not None:
                self._stop_sim_mavlink_telemetry()
                try:
                    conn.mav.rc_channels_override_send(conn.target_system, conn.target_component, 0, 0, 0, 0, 0, 0, 0, 0)
                except Exception:
                    pass
            else:
                # соединение уже не ведёт на локальный SITL (или SITL остановлен): на него ничего не
                # отправляем, только останавливаем свой поток приёма
                tm_state = getattr(self, "_sim_mavlink_state", None)
                if isinstance(tm_state, dict):
                    tm_state["active"] = False
        try:
            self._sim_tlog_end()                   # tlog закрывается после последних отправок на борт (sim_commands.py)
        except Exception:
            pass
        self._sim_state = None
        self._sim_paused = False
        self._sim_keys.clear()
        self._sim_fpv_stop()
        self._sim_stop_obstacles()
        self._sim_view_lock(False)
        self._sim_map_pending_request = None
        # state="normal": пока ждали первый слой вида вперёд, кнопка была отключена, и после ошибки загрузки она
        # осталась бы недоступной
        self.sim_start_btn.configure(text=i18n.t("btn_sim_start"), state="normal")
        self.sim_status_var.set(i18n.t(status_key))

    def _sim_tick_mavlink(self, s):
        mstate = getattr(self, "_sim_mavlink_state", {})
        s["lat"] = mstate.get("lat", s["lat"])
        s["lon"] = mstate.get("lon", s["lon"])
        s["alt"] = mstate.get("alt", s["alt"])
        s["heading"] = mstate.get("heading", s["heading"])
        roll_rad = mstate.get("roll", 0.0)
        pitch_rad = mstate.get("pitch", 0.0)
        groundspeed = mstate.get("groundspeed", 0.0)
        climb = mstate.get("climb", 0.0)
        self._flight_record(s, roll_rad, pitch_rad, groundspeed)

        conn = self._sim_conn()
        # Текущая точка -- ИЗ БОРТА (MISSION_CURRENT). Раньше target_idx
        # ставился в 1 на старте и двигался только встроенной кинематикой,
        # поэтому HUD и карта показывали "WP 1", хотя борт по своей миссии
        # уже летел к седьмой.
        seq = mstate.get("mission_current")
        if seq is not None:
            idx = self._sim_nav_index_for_seq(s["nav_wps"], seq)
            if idx is not None:
                s["target_idx"] = idx
        self._sim_commands_tick(mstate, seq, conn)
        self._sim_seek_tick(mstate, s, conn)
        self._sim_sync_mode_from_vehicle(s, mstate)
        self._sim_handle_switches()
        mode_now = s.get("mode")
        self._sim_hud_update(s, mstate, conn, roll_rad, pitch_rad, groundspeed, climb, mode_now)
        self._sim_limits_tick(s, mstate, conn)               # красные коды лимитов (TER, SPD, BNK, PIT) и их звук: sim_limits.py
        self._sim_launch_tick(s, mstate, conn)               # ожидание готовности перед ARM, ответ на ARM, «заармлен, но не взлетел»: sim_launch.py

        roll_stick = pitch_stick = throttle_stick = yaw_stick = 0.0
        if mode_now in ("MANUAL", "FBWA"):
            if self._sim_joystick.is_connected:
                self._sim_joystick.poll()
                mapping = self._sim_joystick_mapping
                roll_stick = self._sim_joystick.mapped_value(mapping.roll_axis, mapping.roll_invert)
                pitch_stick = self._sim_joystick.mapped_value(mapping.pitch_axis, mapping.pitch_invert)
                throttle_stick = self._sim_joystick.mapped_value(mapping.throttle_axis, mapping.throttle_invert)
                yaw_stick = self._sim_joystick.mapped_value(mapping.yaw_axis, mapping.yaw_invert)
            else:
                roll_stick, pitch_stick, kb_throttle, yaw_stick = self._sim_keyboard_sticks()
                # Газ самолёта с клавиатуры -- УДЕРЖИВАЕМЫЙ уровень (W
                # повышает, S понижает), а не пружинный стик: у стика
                # "нет клавиш" = 0 = 1500 мкс = полгаза, и борт отказывает
                # в arm ("Throttle (RC3) is not neutral"). Нейтраль газа
                # самолёта -- минимум. Пока борт не заармлен, уровень
                # принудительно 0, чтобы arm не блокировался.
                level = getattr(self, "_sim_kb_throttle_level", 0.0)
                if mstate.get("armed") is True:
                    level += kb_throttle * KB_THROTTLE_RATE * (SIM_STEP_MS / 1000.0)
                else:
                    level = 0.0
                level = max(0.0, min(1.0, level))
                self._sim_kb_throttle_level = level
                throttle_stick = level * 2.0 - 1.0   # -1..+1 -> PWM 1000..2000

            if conn is not None:
                def _to_pwm(v):
                    return int(1500 + max(-1.0, min(1.0, v)) * 500)
                # ARM і MODE з тумблерів пульта -- напряму в
                # RC-override, щоб Куб їх бачив, як реальні RC-канали.
                # raw_axis (не mapped_value) -- тумблер не стик,
                # deadzone не потрібен. Канали CH5/CH7 -- типові для
                # ArduPilot (FLTMODE_CH=5, RC7_OPTION=41). Якщо в тебе
                # інакше -- заміни числа нижче.
                mapping = self._sim_joystick_mapping
                # Каналы 5-8 не перебиваем (0): тумблеры Arm/Mode работают
                # командами, см. _sim_handle_switches.
                arm_pwm = 0
                mode_pwm = 0
                try:
                    conn.mav.rc_channels_override_send(
                        conn.target_system, conn.target_component,
                        _to_pwm(roll_stick), _to_pwm(pitch_stick), _to_pwm(throttle_stick), _to_pwm(yaw_stick),
                        mode_pwm, 0, arm_pwm, 0,   # CH5=MODE, CH7=ARM
                    )
                except Exception:
                    pass
        elif conn is not None and mode_now == "AUTO":
            try:
                conn.mav.rc_channels_override_send(conn.target_system, conn.target_component, 0, 0, 0, 0, 0, 0, 0, 0)
            except Exception:
                pass

        # Обновление кнопок ARM/MODE по реальному состоянию борта
        self._update_sim_control_buttons(mstate)
        self._show_mavlink_feedback(mstate)

        # Накапливаем путь борта, обновляем overlay карты миссии
        self._sim_flown_path.append((s["lat"], s["lon"]))
        if len(self._sim_flown_path) > 2000:
            self._sim_flown_path = self._sim_flown_path[-2000:]
        self._redraw_mission_overview()

        if self._sim_view_key == "fpv":
            if not self._sim_cam_active():               # у камеры свой таймер; такт HUD подменяет его, только если он не подаёт кадры
                self._sim_fpv_tick(s, roll_rad, pitch_rad)
        else:
            self._redraw_sim_map()
            if self._sim_map_center is not None:
                dist_from_center = haversine_m(s["lat"], s["lon"], self._sim_map_center[0], self._sim_map_center[1])
                alt_changed = abs(s["alt"] - self._sim_map_altitude) > self._sim_map_altitude * 0.2 + 5.0
                heading_diff = abs((s["heading"] - self._sim_map_heading + 180.0) % 360.0 - 180.0)
                if (dist_from_center > self._sim_map_radius_m * CENTER_REFRESH_FRACTION
                        or alt_changed
                        or heading_diff > HEADING_REFRESH_THRESHOLD_DEG):
                    self._sim_load_nose_cam(s["lat"], s["lon"], s["alt"], s["heading"], start_after=False)

        self._sim_hud_render()

        hit = (self._sim_check_terrain_impact(s, mstate) or self._sim_check_obstacle_impact(s, mstate)
               or self._sim_check_scene_impact(s, mstate))
        if hit is not None:
            self._sim_on_terrain_impact(hit)
            return

        self._sim_after_id = self.after(SIM_STEP_MS, self._sim_tick)


    def _sim_tick(self):
        s = self._sim_state
        if s is None:
            return

        if self._sim_paused:
            self._sim_after_id = self.after(SIM_STEP_MS, self._sim_tick)
            return

        ok, _why = self._sim_conn_check()
        if not ok:                       # соединение сменили или SITL остановлен: команды не посылаем
            self._sim_stop(status_key="status_sim_conn_lost")
            return
        self._sim_tick_mavlink(s)
