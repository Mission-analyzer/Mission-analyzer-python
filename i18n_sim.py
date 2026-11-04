"""
i18n_sim.py — переводы вкладки "Симуляція": кнопки старт/стоп, режимы
полёта, джойстик и его калибровка, статусы симуляции, камера, запуск
(миссия из памяти, подключение к своему SITL, заливка миссии), защита
соединения и столкновение с рельефом SRTM.

Собирается в i18n.py вместе с остальными тематическими словарями.
"""

TR_SIM = {
    # --- верхняя панель ---
    "btn_sim_start": {"uk": "Старт симуляції", "en": "Start simulation"},
    "btn_sim_stop": {"uk": "Зупинити", "en": "Stop"},
    "lbl_flight_mode": {"uk": "Режим польоту:", "en": "Flight mode:"},

    # --- статусы симуляции ---
    "status_sim_running": {"uk": "Симуляція триває...", "en": "Simulation running..."},
    "status_sim_loading_map": {"uk": "Завантаження карти...", "en": "Loading map..."},
    "status_sim_stopped": {"uk": "Симуляцію зупинено", "en": "Simulation stopped"},
    "status_sim_paused": {"uk": "Симуляцію призупинено (клік -- продовжити)", "en": "Simulation paused (click to resume)"},

    # --- джойстик ---
    "lbl_joystick": {"uk": "Джойстик:", "en": "Joystick:"},
    "btn_joystick_refresh": {"uk": "Оновити список", "en": "Refresh list"},
    "btn_joystick_connect": {"uk": "Підключити", "en": "Connect"},
    "btn_joystick_calibrate": {"uk": "Калібрувати", "en": "Calibrate"},
    "msg_pygame_not_installed": {
        "uk": "pygame не встановлено -- керування джойстиком недоступне.",
        "en": "pygame is not installed -- joystick control unavailable.",
    },
    "status_joystick_found_fmt": {"uk": "Знайдено пристроїв: {n}", "en": "Devices found: {n}"},
    "msg_no_joystick_selected": {
        "uk": "Оберіть джойстик зі списку перед підключенням.",
        "en": "Select a joystick from the list before connecting.",
    },
    "status_joystick_connected_fmt": {"uk": "Підключено: {name}", "en": "Connected: {name}"},
    "status_joystick_profile_own": {"uk": "(калібрування цього пульта)", "en": "(this remote's calibration)"},
    "status_joystick_profile_common": {"uk": "(загальне калібрування -- відкалібруйте цей пульт, буде своє)",
                                       "en": "(shared calibration -- calibrate this remote to give it its own)"},
    "status_joystick_profile_none": {"uk": "(не відкалібрований -- натисніть «Калібрування»)",
                                     "en": "(not calibrated -- press «Calibration»)"},
    "status_joystick_connect_failed": {"uk": "Не вдалось підключити джойстик", "en": "Failed to connect joystick"},

    # --- калибровка ---
    "msg_joystick_not_connected": {
        "uk": "Спершу підключіть джойстик.",
        "en": "Connect a joystick first.",
    },
    "msg_calibrate_title": {"uk": "Калібрування джойстика", "en": "Joystick calibration"},
    "lbl_calibrate_instructions": {
        "uk": "Рухайте стики -- дивіться, яка вісь реагує, і оберіть для неї канал нижче.",
        "en": "Move the sticks -- see which axis reacts, and pick a channel for it below.",
    },
    "lbl_axis": {"uk": "Вісь", "en": "Axis"},
    "lbl_invert": {"uk": "Інверсія", "en": "Invert"},
    "msg_joystick_no_axes": {
        "uk": "У джойстика немає жодної осі.",
        "en": "The joystick has no axes.",
    },
    "msg_calibrate_done": {
        "uk": "Калібрування завершено й збережено.",
        "en": "Calibration complete and saved.",
    },
    "msg_calibrate_missing_channels_fmt": {
        "uk": "Не призначено канали: {channels}.\nВсе одно завершити калібрування?",
        "en": "Channels not assigned: {channels}.\nFinish the calibration anyway?",
    },

    # --- MAVLink-источник и переключение режимов ---
    "msg_mode_not_supported_fmt": {
        "uk": "Борт не підтримує режим {mode}",
        "en": "Vehicle does not support mode {mode}",
    },
    "msg_mode_switch_failed_fmt": {
        "uk": "Не вдалось перемкнути режим: {error}",
        "en": "Failed to switch mode: {error}",
    },

    # --- запуск/проверки ---
    "msg_cam_insufficient_coverage": {
        "uk": "Замало площі карти для поточного вікна/висоти -- лишаю попередній кадр камери.",
        "en": "Not enough map coverage for the current window/altitude -- keeping previous camera frame.",
    },

    # --- вид камеры ---
    "lbl_sim_camera_view": {"uk": "Вид камери:", "en": "Camera view:"},
    "opt_sim_view_fpv": {"uk": "Вперед (FPV)", "en": "Forward (FPV)"},
    "opt_sim_view_nadir": {"uk": "Вниз", "en": "Down"},
    "chk_sim_relief": {"uk": "Рельєф", "en": "Relief"},
    "chk_sim_gpu_window": {"uk": "HUD у окремому вікні", "en": "HUD in a separate window"},
    "status_gl_ready_fmt": {"uk": "Камера: відеокарта ({renderer})", "en": "Camera: GPU ({renderer})"},
    "status_gl_failed_fmt": {"uk": "Камера: процесор (відеокарта недоступна: {error})",
                             "en": "Camera: CPU (GPU unavailable: {error})"},
    "lbl_sim_thermal_palette": {"uk": "Палітра:", "en": "Palette:"},
    "opt_sim_pal_white": {"uk": "Біле гаряче", "en": "White hot"},
    "opt_sim_pal_black": {"uk": "Чорне гаряче", "en": "Black hot"},
    "opt_sim_pal_iron": {"uk": "Ironbow", "en": "Ironbow"},
    "opt_sim_pal_color": {"uk": "Кольорова (знімок)", "en": "Colour (imagery)"},

    # --- запуск: миссия из памяти, подключение к своему SITL, заливка миссии ---
    "msg_sim_title": {"uk": "Симуляція", "en": "Simulation"},
    "msg_sim_need_mission_body": {
        "uk": "У пам'яті немає завантаженої місії.\nПерейти на вкладку «Місія», щоб її завантажити?",
        "en": "No mission is loaded in memory.\nGo to the Mission tab to load one?",
    },
    "status_sim_connecting": {"uk": "Підключення до SITL…", "en": "Connecting to SITL…"},
    "status_sim_uploading_mission_fmt": {
        "uk": "Заливаю місію в SITL ({n} точок)…",
        "en": "Uploading the mission to SITL ({n} items)…",
    },
    "msg_sim_connect_failed_fmt": {
        "uk": "Не вдалося підключитися до SITL ({port}):\n{error}",
        "en": "Could not connect to SITL ({port}):\n{error}",
    },
    "msg_sim_upload_failed_fmt": {
        "uk": "Не вдалося залити місію в SITL:\n{error}",
        "en": "Could not upload the mission to SITL:\n{error}",
    },

    # --- захист з'єднання: симуляція працює лише з власним SITL ---
    "msg_sim_conn_not_local": {
        "uk": ("Симуляція працює лише з власним SITL цієї програми.\nПричина: {reason}.\n"
               "Відключіться на вкладці «Місія» і натисніть Start ще раз: програма підключиться до SITL сама "
               "(udp:127.0.0.1:{port})."),
        "en": ("The simulation works only with this application's own SITL.\nReason: {reason}.\n"
               "Disconnect on the Mission tab and press Start again: the program will connect to the SITL itself "
               "(udp:127.0.0.1:{port})."),
    },
    "status_sim_conn_lost": {
        "uk": "Симуляцію зупинено: з'єднання не з локальним SITL або SITL зупинено",
        "en": "Simulation stopped: the connection is not to the local SITL, or SITL has stopped",
    },

    # --- зіткнення з рельєфом SRTM ---
    "status_sim_obstacle_impact": {
        "uk": "Зіткнення з перешкодою: {what} (верх {top:.0f} м AMSL), борт на {alt:.0f} м{approx}",
        "en": "Obstacle collision: {what} (top {top:.0f} m AMSL), aircraft at {alt:.0f} m{approx}",
    },
    "obs_approx_height": {"uk": ", висота умовна", "en": ", assumed height"},
    "obs_turbine": {"uk": "вітрогенератор (башта)", "en": "wind turbine (tower)"},
    "obs_turbine_rotor": {"uk": "ротор вітрогенератора", "en": "wind turbine rotor"},
    "obs_mast": {"uk": "щогла", "en": "mast"},
    "obs_tower": {"uk": "вежа", "en": "tower"},
    "obs_chimney": {"uk": "димар", "en": "chimney"},
    "obs_crane": {"uk": "кран", "en": "crane"},
    "obs_pylon": {"uk": "опора ЛЕП", "en": "power pylon"},
    "obs_power_line": {"uk": "провід ЛЕП", "en": "power line wire"},
    "status_sim_ground_impact": {
        "uk": "Зіткнення із землею: борт на висоті {alt:.0f} м (AMSL) досяг рівня землі SITL",
        "en": "Ground collision: the aircraft at {alt:.0f} m (AMSL) reached the SITL ground level",
    },
    "info_render_gpu": {"uk": "Кадр: відеокарта", "en": "Frame: GPU"},
    "info_render_cpu": {"uk": "Кадр: процесор (ЛЕП і ліс у кадрі не малюються)", "en": "Frame: CPU (power lines and forest are not drawn)"},
    "info_render_wait": {"uk": "Кадр: перевірка відеокарти…", "en": "Frame: checking GPU…"},
    "info_obst_fmt": {"uk": "ЛЕП: {lines} ліній, опор і споруд {points} (OSM {done}/{total})",
                      "en": "Power lines: {lines}, towers and structures {points} (OSM {done}/{total})"},
    "info_obst_frame_fmt": {"uk": "у кадрі ({km:.0f} км): {points} споруд, {spans} прольотів",
                            "en": "in frame ({km:.0f} km): {points} structures, {spans} spans"},
    "info_line_near_fmt": {"uk": "найближча ЛЕП {km:.1f} км", "en": "nearest power line {km:.1f} km"},
    "info_line_none_fmt": {"uk": "ЛЕП ближче {km:.0f} км немає", "en": "no power line within {km:.0f} km"},
    "info_forest_old_file": {"uk": "Ліс: у папці програми старий sim_impact.py — замініть файл", "en": "Forest: sim_impact.py in the program folder is outdated — replace it"},
    "info_forest_failed_fmt": {"uk": "Ліс: не запустився — {err}", "en": "Forest: failed to start — {err}"},
    "info_forest_off": {"uk": "Ліс: завантажиться при старті симуляції", "en": "Forest: loads when the simulation starts"},
    "info_forest_here_fmt": {"uk": "під бортом: ЛІС, крони {m:.0f} м", "en": "below: FOREST, canopy {m:.0f} m"},
    "info_forest_not_here": {"uk": "під бортом: не ліс", "en": "below: no forest"},
    "info_forest_err_fmt": {"uk": "помилки: {n} — {err}", "en": "errors: {n} — {err}"},
    "info_forest_done": {"uk": "усе завантажено", "en": "all loaded"},
    "info_forest_done_fmt": {"uk": "усе завантажено: {folder}", "en": "all loaded: {folder}"},
    "info_forest_fmt": {"uk": "Ліс (ETH): тайлів {have}/{total}", "en": "Forest (ETH): tiles {have}/{total}"},
    "info_forest_dl_fmt": {"uk": "завантаження {tile}: {pct}", "en": "downloading {tile}: {pct}"},
    "info_forest_tile_wait_fmt": {"uk": "під бортом: тайла {tile} ще немає", "en": "below: tile {tile} not available yet"},
    "info_forest_manual_fmt": {"uk": "скачайте вручну {name} у {folder}", "en": "download {name} manually into {folder}"},
    "info_osm_failed_fmt": {"uk": "помилки OSM: {n} (див. журнал)", "en": "OSM errors: {n} (see log)"},
    "info_bld_fmt": {"uk": "Будівлі: квадратів {done}/{total}, будівель {n}", "en": "Buildings: tiles {done}/{total}, buildings {n}"},
    "info_bld_frame_fmt": {"uk": ", у кадрі ({km:.0f} км) {n}", "en": ", in frame ({km:.0f} km) {n}"},
    "info_bld_part_fmt": {"uk": "квадрат {cell}: {part}", "en": "tile {cell}: {part}"},
    "info_bld_err_fmt": {"uk": ", помилки {n}: {err}", "en": ", errors {n}: {err}"},
    "status_sim_building_impact": {
        "uk": "Зіткнення з будівлею: борт {alt:.0f} м, дах ~{ground:.0f} м (AMSL; висота будівлі {h:.0f} м{assumed})",
        "en": "Building impact: aircraft {alt:.0f} m, roof ~{ground:.0f} m (AMSL; building height {h:.0f} m{assumed})",
    },
    "txt_height_assumed": {"uk": ", умовна", "en": ", assumed"},
    "status_sim_forest_impact": {
        "uk": "Зіткнення з лісом: борт {alt:.0f} м, верхівки дерев ~{ground:.0f} м (AMSL; крони {canopy:.0f} м за картою ETH 2020)",
        "en": "Forest impact: aircraft {alt:.0f} m, treetops ~{ground:.0f} m (AMSL; canopy {canopy:.0f} m from the ETH 2020 map)",
    },
    "status_sim_terrain_impact": {
        "uk": "Зіткнення з рельєфом: борт {alt:.0f} м, рельєф {ground:.0f} м (AMSL)",
        "en": "Terrain impact: aircraft {alt:.0f} m, terrain {ground:.0f} m (AMSL)",
    },
}
