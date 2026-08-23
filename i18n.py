"""
i18n.py — локализация интерфейса: украинский и английский (без русского —
это язык только переписки с разработчиком, не язык программы).

Простое глобальное текущее состояние языка (get_lang/set_lang) — это
однопользовательское десктопное приложение с одним окном, глобального
состояния здесь достаточно и не создаёт проблем.
"""

from __future__ import annotations

LANGS = ("uk", "en")
DEFAULT_LANG = "uk"

_current_lang = DEFAULT_LANG

# --- таблица переводов -----------------------------------------------------

_TR: dict[str, dict[str, str]] = {
    # --- общие элементы окна ---
    "app_title": {"uk": "Mission Analyzer", "en": "Mission Analyzer"},
    "app_subtitle": {
        "uk": "Аналіз місій ArduPilot / Mission Planner",
        "en": "ArduPilot / Mission Planner mission analysis",
    },
    "label_mission_file": {"uk": "Файл місії:", "en": "Mission file:"},
    "btn_browse": {"uk": "Огляд...", "en": "Browse..."},
    "label_params": {"uk": "Параметри аналізу", "en": "Analysis parameters"},

    # --- навигационные кнопки (иконка + подпись) ---
    "nav_mission": {"uk": "Місія", "en": "Mission"},
    "nav_analysis": {"uk": "Аналіз", "en": "Analysis"},
    "nav_config": {"uk": "Конфігурація", "en": "Configuration"},
    "nav_help": {"uk": "Довідка", "en": "Help"},
    "tab_help": {"uk": "Довідка", "en": "Help"},
    "tab_changelog": {"uk": "Історія змін", "en": "Changelog"},
    "label_version": {"uk": "версія", "en": "version"},

    "help_text_body": {
        "uk": (
            "MISSION ANALYZER — аналіз місій ArduPilot / Mission Planner (.waypoints)\n\n"
            "СТОРІНКИ\n"
            "Місія — «Завантажити» відкриває файл місії, одразу рахує повний аналіз і будує "
            "карту; таблиця точок (як у Mission Planner) і карта — тут же. «Зберегти» — за "
            "замовчуванням у .waypoints (або CSV, якщо обрати це розширення).\n\n"
            "Аналіз — результати: текстовий звіт, графік висоти (з рельєфом SRTM), графік "
            "кута нахилу траєкторії, глісада заходу на посадку.\n\n"
            "Конфігурація — пороги критичної висоти й кута повороту, папка SRTM (рельєф), "
            "папка диск-кешу тайлів карти, тип підложки карти (OpenStreetMap/Google), шар "
            "окупованих територій.\n\n"
            "Довідка — цей текст.\n\n"
            "РЕДАГУВАННЯ МІСІЇ\n"
            "Кнопка «Редагувати» з'являється після завантаження/зчитування місії. Точку можна "
            "тягнути мишею на карті (позиція) і на профілі висот під картою (окремо висота). "
            "У таблиці — подвійний клік по комірці редагує значення. Правий клік по рядку "
            "таблиці або по точці на карті — додати точку/службову команду чи видалити цю точку.\n\n"
            "ARDUPILOT\n"
            "У шапці сторінки «Місія» — підключення по MAVLink (порт «AUTO» сам знайде "
            "контролер). Read/Write — зчитати місію з борту або записати на борт. Info — "
            "версія прошивки, датчики, стан SD-карти. Файли SD — перегляд, скачування, "
            "вивантаження, видалення файлів і графіки польотних датафлеш-логів (.BIN).\n\n"
            "ЩО ПЕРЕВІРЯЄТЬСЯ\n"
            "- критично низька висота (за файлом і за рельєфом SRTM, в точках і вздовж усієї "
            "лінії польоту між точками)\n"
            "- критично гострі кути повороту\n"
            "- кут нахилу траєкторії (набір/зниження) поза допуском\n\n"
            "НАЛАШТУВАННЯ\n"
            "Обрані папки, пороги, зум, мова тощо запам'ятовуються автоматично між запусками "
            "(файл settings.json поруч із програмою). Щоб скинути все на типові значення — "
            "просто видали цей файл.\n\n"
            "Карта якщо є інтернет, тайли качаються з OpenStreetMap/Google. Диск-кеш "
            "(необов'язковий) зберігає вже завантажені тайли, щоб не качати повторно.\n\n"
            "Мову інтерфейсу (UA/EN) можна переключити кнопками у верхньому правому куті."
        ),
        "en": (
            "MISSION ANALYZER — ArduPilot / Mission Planner (.waypoints) mission analysis\n\n"
            "PAGES\n"
            "Mission — \"Load\" opens a mission file, immediately runs the full analysis and "
            "builds the map; the waypoint table (like in Mission Planner) and the map are right "
            "here. \"Save\" defaults to .waypoints (or CSV if you pick that extension).\n\n"
            "Analysis — results: text report, elevation graph (with SRTM terrain), flight path "
            "angle graph, landing approach glide slope.\n\n"
            "Configuration — critical altitude/turn-angle thresholds, SRTM terrain tile folder, "
            "map tile disk cache folder, map basemap type (OpenStreetMap/Google), occupied-"
            "territories layer.\n\n"
            "Help — this text.\n\n"
            "EDITING A MISSION\n"
            "The \"Edit\" button appears once a mission is loaded/read. Drag a point on the map "
            "to move it (position), or on the altitude profile below the map to change just its "
            "altitude. Double-click a table cell to edit its value. Right-click a table row or a "
            "point on the map to add a waypoint/command, or delete that point.\n\n"
            "ARDUPILOT\n"
            "On the Mission page header — MAVLink connection (\"AUTO\" port finds the controller "
            "itself). Read/Write — download the mission from the aircraft or upload it. Info — "
            "firmware version, sensors, SD card status. SD Files — browse, download, upload, "
            "delete files, and plot flight dataflash logs (.BIN).\n\n"
            "WHAT IS CHECKED\n"
            "- critically low altitude (from the file and from SRTM terrain, at points and along "
            "the whole flight line between points)\n"
            "- critically sharp turn angles\n"
            "- flight path angle (climb/descent) out of tolerance\n\n"
            "SETTINGS\n"
            "Chosen folders, thresholds, zoom, language etc. are remembered automatically between "
            "runs (settings.json next to the program). Delete that file to reset everything to "
            "defaults.\n\n"
            "Map: if there's internet, tiles are downloaded from OpenStreetMap/Google. The disk "
            "cache (optional) keeps already-downloaded tiles so they aren't re-downloaded.\n\n"
            "Interface language (UA/EN) can be switched with the buttons in the top-right corner."
        ),
    },
    "label_alt_min": {"uk": "Мін. висота, м:", "en": "Min altitude, m:"},
    "label_turn_min": {"uk": "Мін. кут повороту, °:", "en": "Min turn angle, °:"},
    "check_srtm": {"uk": "Рельєф (SRTM)", "en": "Terrain (SRTM)"},
    "label_map_cache": {"uk": "Диск-кеш карти (необов'язково):", "en": "Map disk cache (optional):"},
    "btn_analyze": {"uk": "Аналізувати", "en": "Analyze"},
    "btn_save_csv": {"uk": "Зберегти CSV...", "en": "Save CSV..."},
    "btn_load": {"uk": "Завантажити", "en": "Load"},
    "btn_save": {"uk": "Зберегти", "en": "Save"},
    "label_map_settings": {"uk": "Карта", "en": "Map"},

    "table_col_idx": {"uk": "#", "en": "#"},
    "table_col_command": {"uk": "Команда", "en": "Command"},
    "table_col_p1": {"uk": "P1", "en": "P1"},
    "table_col_p2": {"uk": "P2", "en": "P2"},
    "table_col_p3": {"uk": "P3", "en": "P3"},
    "table_col_p4": {"uk": "P4", "en": "P4"},
    "table_col_lat": {"uk": "Шир.", "en": "Lat"},
    "table_col_lon": {"uk": "Довг.", "en": "Lon"},
    "table_col_alt": {"uk": "Вис.", "en": "Alt"},
    "table_col_frame": {"uk": "Фрейм", "en": "Frame"},
    "table_col_dist": {"uk": "Відст, м", "en": "Dist, m"},
    "table_col_az": {"uk": "AZ, °", "en": "AZ, °"},
    "status_loaded_fmt": {
        "uk": "Завантажено: {n} точок маршруту",
        "en": "Loaded: {n} route points",
    },

    "tab_report": {"uk": "Звіт", "en": "Report"},
    "tab_elevation": {"uk": "Графік висоти", "en": "Elevation graph"},
    "tab_angle": {"uk": "Кут траєкторії", "en": "Path angle"},
    "tab_map": {"uk": "Карта", "en": "Map"},
    "tab_landing": {"uk": "Глісада", "en": "Glide slope"},

    "title_landing_approach": {"uk": "Глісада заходу на посадку", "en": "Landing approach glide slope"},
    "landing_no_data": {
        "uk": "Недостатньо даних для профілю заходу на посадку",
        "en": "Not enough data for the landing approach profile",
    },
    "landing_leg_label": {
        "uk": "{dist:.0f} м, азимут {bearing:.0f}°, кут {angle}",
        "en": "{dist:.0f} m, bearing {bearing:.0f}°, angle {angle}",
    },
    "landing_speed_marker_label": {
        "uk": "{command}: V={speed:.1f} м/с ({speed_type})",
        "en": "{command}: V={speed:.1f} m/s ({speed_type})",
    },

    "label_map_provider": {"uk": "Карта:", "en": "Map:"},
    "label_zoom": {"uk": "Зум:", "en": "Zoom:"},
    "hint_wheel_zoom": {
        "uk": "(або крути колесо миші над картою)",
        "en": "(or scroll mouse wheel over the map)",
    },
    "btn_update_map": {"uk": "Оновити карту", "en": "Update map"},
    "btn_cancel": {"uk": "Скасувати", "en": "Cancel"},
    "check_occupied": {
        "uk": "Окуповані території / лінія зіткнення (deepstatemap.live)",
        "en": "Occupied territories / line of contact (deepstatemap.live)",
    },

    "status_default": {
        "uk": "Обери файл місії і натисни «Аналізувати»",
        "en": "Choose a mission file and click \"Analyze\"",
    },

    # --- провайдеры карт ---
    "provider_osm": {"uk": "OpenStreetMap", "en": "OpenStreetMap"},
    "provider_google_roadmap": {"uk": "Google Карти (схема)", "en": "Google Maps (roadmap)"},
    "provider_google_satellite": {"uk": "Google Супутник", "en": "Google Satellite"},
    "provider_google_hybrid": {"uk": "Google Гібрид (супутник+підписи)", "en": "Google Hybrid (satellite+labels)"},

    # --- диалоги выбора файла/папки ---
    "dlg_choose_mission_title": {"uk": "Обери файл місії", "en": "Choose mission file"},
    "filetype_waypoints": {"uk": "Waypoints", "en": "Waypoints"},
    "filetype_all": {"uk": "Всі файли", "en": "All files"},
    "dlg_choose_srtm_title": {"uk": "Папка з SRTM-тайлами (.hgt)", "en": "Folder with SRTM tiles (.hgt)"},
    "dlg_choose_mapcache_title": {
        "uk": "Папка для локального диск-кешу тайлів карти",
        "en": "Folder for local map tile disk cache",
    },
    "dlg_save_csv_title": {"uk": "Зберегти звіт як...", "en": "Save report as..."},

    # --- сообщения ---
    "msg_no_file_title": {"uk": "Немає файлу", "en": "No file"},
    "msg_no_file_body": {"uk": "Спочатку обери файл місії", "en": "Choose a mission file first"},
    "msg_file_not_found_title": {"uk": "Помилка", "en": "Error"},
    "msg_file_not_found_body": {"uk": "Файл не знайдено:\n{path}", "en": "File not found:\n{path}"},
    "msg_bad_numbers_title": {"uk": "Помилка", "en": "Error"},
    "msg_bad_numbers_body": {
        "uk": "Пороги висоти/кута мають бути числами",
        "en": "Altitude/angle thresholds must be numbers",
    },
    "msg_file_read_error_title": {"uk": "Помилка читання файлу", "en": "File read error"},
    "msg_srtm_unavailable_title": {"uk": "SRTM недоступний", "en": "SRTM unavailable"},
    "msg_srtm_unavailable_body": {
        "uk": "{err}\n\nПродовжую аналіз без урахування рельєфу.",
        "en": "{err}\n\nContinuing analysis without terrain.",
    },
    "status_ready_fmt": {
        "uk": "Готово: {n} точок маршруту, {m} критичних відміток",
        "en": "Done: {n} route points, {m} critical flags",
    },
    "status_loaded_fmt": {
        "uk": "Завантажено: {n} точок маршруту",
        "en": "Loaded: {n} route points",
    },
    "msg_no_data_title": {"uk": "Немає даних", "en": "No data"},
    "msg_no_data_body": {"uk": "Спочатку виконай аналіз", "en": "Run analysis first"},
    "msg_saved_title": {"uk": "Готово", "en": "Done"},
    "msg_saved_body": {"uk": "Звіт збережено:\n{path}", "en": "Report saved:\n{path}"},
    "msg_too_large_zoom_title": {"uk": "Занадто великий масштаб", "en": "Zoom level too large"},
    "msg_too_large_zoom_body": {
        "uk": "На цьому зумі потрібно {total} тайлів — це багато. Збільш зум (менша цифра) і повтори.",
        "en": "This zoom needs {total} tiles — that's a lot. Increase zoom (lower number) and retry.",
    },
    "msg_no_points_title": {"uk": "Немає точок", "en": "No points"},
    "msg_no_points_body": {
        "uk": "У місії немає точок з координатами",
        "en": "The mission has no points with coordinates",
    },
    "status_loading_tiles_fmt": {
        "uk": "Завантажую тайли: {done} з {total}...",
        "en": "Loading tiles: {done} of {total}...",
    },
    "status_cancelling": {"uk": "Скасовую...", "en": "Cancelling..."},
    "status_map_cancelled": {"uk": "Завантаження карти скасовано", "en": "Map loading cancelled"},
    "occupied_status_date_fmt": {"uk": "Дані окупації на {date}", "en": "Occupation data as of {date}"},
    "occupied_status_failed": {
        "uk": "Не вдалося завантажити шар окупації (немає мережі/кешу)",
        "en": "Failed to load occupation layer (no network/cache)",
    },
    "status_rendered_fmt": {
        "uk": "Відображено: {found} з {total} (не знайдено/немає мережі: {missing})",
        "en": "Rendered: {found} of {total} (not found/no network: {missing})",
    },
    "status_undecodable_suffix_fmt": {
        "uk": ", не декодовано (JPEG без Pillow): {n}",
        "en": ", undecodable (JPEG without Pillow): {n}",
    },
    "msg_need_pillow_title": {"uk": "Потрібен Pillow для JPEG-тайлів", "en": "Pillow needed for JPEG tiles"},
    "msg_need_pillow_body": {
        "uk": (
            "{n} тайлів — це JPEG, а Tkinter без Pillow їх не відкриває.\n\n"
            "Постав: python -m pip install Pillow\n\n"
            "Після встановлення просто натисни «Оновити карту» ще раз."
        ),
        "en": (
            "{n} tiles are JPEG, and Tkinter can't open them without Pillow.\n\n"
            "Install: python -m pip install Pillow\n\n"
            "After installing, just click \"Update map\" again."
        ),
    },

    # --- надписи на канвасах ---
    "title_elevation_profile": {"uk": "Профіль висоти місії", "en": "Mission elevation profile"},
    "title_angle_profile": {
        "uk": "Кут нахилу траєкторії (набір/зниження)",
        "en": "Flight path angle (climb/descent)",
    },
    "map_no_tile": {"uk": "немає тайла", "en": "no tile"},
    "map_jpeg_no_pillow": {"uk": "JPEG без Pillow", "en": "JPEG w/o Pillow"},

    # --- отчёт (analyzer.py) ---
    "report_nav_points": {"uk": "Точок маршруту: {n}", "en": "Route points: {n}"},
    "report_total_distance": {
        "uk": "Загальна дальність маршруту: {km:.2f} км",
        "en": "Total route distance: {km:.2f} km",
    },
    "report_note_land": {
        "uk": "точки посадки (#{idxs}) не перевіряються на критичну висоту — 0 м на посадці це норма",
        "en": "landing points (#{idxs}) are not checked for critical altitude — 0 m at touchdown is normal",
    },
    "report_note_no_pos": {
        "uk": (
            "точки без координат (#{idxs}, зазвичай TAKEOFF) перевіряються на висоту, "
            "але не беруть участі в дистанції/поворотах/AGL"
        ),
        "en": (
            "points without coordinates (#{idxs}, usually TAKEOFF) are checked for altitude "
            "but not used for distance/turns/AGL"
        ),
    },
    "report_no_critical": {"uk": "Критичних точок не знайдено.", "en": "No critical points found."},
    "report_landing_approach_title": {
        "uk": "Глісада заходу на посадку (дистанція / азимут / кут зниження):",
        "en": "Landing approach glide slope (distance / bearing / descent angle):",
    },
    "report_landing_leg_line": {
        "uk": "  WP {from_idx} -> WP {to_idx}: {dist:.0f} м, азимут {bearing:.0f}°, кут {angle}",
        "en": "  WP {from_idx} -> WP {to_idx}: {dist:.0f} m, bearing {bearing:.0f}°, angle {angle}",
    },
    "report_landing_speed_line": {
        "uk": "  Після WP {wp_index} спрацьовує DO_CHANGE_SPEED: швидкість обмежується {speed:.1f} м/с ({speed_type})",
        "en": "  After WP {wp_index}, DO_CHANGE_SPEED triggers: speed limited to {speed:.1f} m/s ({speed_type})",
    },
    "speed_type_0": {"uk": "повітряна", "en": "airspeed"},
    "speed_type_1": {"uk": "путьова", "en": "ground speed"},
    "speed_type_2": {"uk": "набору висоти", "en": "climb speed"},
    "speed_type_3": {"uk": "зниження", "en": "descent speed"},
    "report_count_suffix": {"uk": "{count} шт.", "en": "{count} pcs."},
    "report_wp_line": {"uk": "  WP #{idx}: {detail}", "en": "  WP #{idx}: {detail}"},

    "title_low_altitude": {
        "uk": "Критично низька висота за даними файлу, без урахування рельєфу (< {threshold:.0f} м)",
        "en": "Critically low altitude from raw file, terrain not considered (< {threshold:.0f} m)",
    },
    "title_low_agl": {
        "uk": "Критично низька висота над рельєфом, за SRTM (< {threshold:.0f} м)",
        "en": "Critically low altitude above terrain, from SRTM (< {threshold:.0f} m)",
    },
    "title_low_agl_segment": {
        "uk": "Критично низька висота НАД РЕЛЬЄФОМ між точками (лінія польоту перетинає рельєф) (< {threshold:.0f} м)",
        "en": "Critically low altitude above terrain BETWEEN points (flight line crosses terrain) (< {threshold:.0f} m)",
    },
    "title_sharp_turn": {
        "uk": "Критично гострий кут повороту (< {threshold:.0f}°)",
        "en": "Critically sharp turn angle (< {threshold:.0f}°)",
    },
    "title_steep_angle": {
        "uk": "Кут нахилу траєкторії поза допуском (|кут| > {threshold:.0f}°)",
        "en": "Flight path angle out of tolerance (|angle| > {threshold:.0f}°)",
    },
    "title_srtm_missing": {
        "uk": "Немає даних SRTM для точки (тайл не знайдено)",
        "en": "No SRTM data for point (tile not found)",
    },

    "detail_low_altitude": {
        "uk": "Висота {value:.1f} м < порогу {threshold:.1f} м",
        "en": "Altitude {value:.1f} m < threshold {threshold:.1f} m",
    },
    "detail_low_agl": {
        "uk": "Висота над рельєфом {value:.1f} м < порогу {threshold:.1f} м",
        "en": "Altitude above terrain {value:.1f} m < threshold {threshold:.1f} m",
    },
    "detail_low_agl_segment": {
        "uk": "Мінімальний запас висоти {value:.1f} м < порогу {threshold:.1f} м десь на ділянці WP {from_idx} -> WP {to_idx}",
        "en": "Minimum clearance {value:.1f} m < threshold {threshold:.1f} m somewhere on segment WP {from_idx} -> WP {to_idx}",
    },
    "detail_sharp_turn": {
        "uk": "Кут повороту {value:.2f}° < порогу {threshold:.1f}°",
        "en": "Turn angle {value:.2f}° < threshold {threshold:.1f}°",
    },
    "detail_steep_angle": {
        "uk": "Кут траєкторії {value:+.2f}° перевищує допустимі ±{threshold:.1f}° (ділянка WP {from_idx} -> WP {to_idx})",
        "en": "Path angle {value:+.2f}° exceeds allowed ±{threshold:.1f}° (segment WP {from_idx} -> WP {to_idx})",
    },
    "detail_srtm_missing": {
        "uk": "Немає даних висоти для точки ({lat:.5f}, {lon:.5f})",
        "en": "No elevation data for point ({lat:.5f}, {lon:.5f})",
    },

    # --- сторінка "Місія": підключення до ArduPilot по MAVLink ---
    "btn_connect": {"uk": "Підєднатись", "en": "Connect"},
    "btn_disconnect": {"uk": "Роз'єднати", "en": "Disconnect"},
    "status_connecting": {"uk": "Підключення...", "en": "Connecting..."},
    "msg_choose_port_body": {"uk": "Оберіть порт підключення", "en": "Choose a connection port"},
    "msg_bad_baud_body": {"uk": "Некоректна швидкість обміну", "en": "Invalid baud rate"},
    "msg_connect_failed_body_fmt": {
        "uk": "Не вдалося підключитись до {port} @ {baud}",
        "en": "Could not connect to {port} @ {baud}",
    },
    "status_connected_fmt": {"uk": "Підключено: {port} @ {baud}", "en": "Connected: {port} @ {baud}"},
    "status_downloading_mission": {
        "uk": "Завантаження місії з борту...",
        "en": "Downloading mission from the aircraft...",
    },
    "status_analyzing": {"uk": "Аналіз місії...", "en": "Analyzing mission..."},
    "status_uploading_mission": {
        "uk": "Запис місії на борт...",
        "en": "Uploading mission to the aircraft...",
    },
    "status_mission_uploaded": {"uk": "Місію записано на борт", "en": "Mission uploaded to the aircraft"},
    "msg_mission_uploaded_body": {
        "uk": "Місію успішно завантажено на борт ArduPilot",
        "en": "Mission successfully uploaded to ArduPilot",
    },
    "msg_action_failed_body": {"uk": "{action} не вдалося:\n{error}", "en": "{action} failed:\n{error}"},
    "action_download": {"uk": "Завантаження", "en": "Download"},
    "action_write": {"uk": "Запис", "en": "Write"},

    # --- сторінка "Аналіз" ---
    "label_flight_date": {"uk": "Дата польоту:", "en": "Flight date:"},
    "label_departure_time": {"uk": "Час вильоту (UTC):", "en": "Departure time (UTC):"},
    "label_arrival_time": {"uk": "Прибуття (UTC):", "en": "Arrival (UTC):"},
    "btn_get_weather": {"uk": "Отримати метео", "en": "Get weather"},
    "hint_press_get_weather": {
        "uk": "Натисніть «Отримати метео», щоб побачити аналіз місії",
        "en": 'Press "Get weather" to see the mission analysis',
    },
    "box_takeoff_profile": {"uk": "Профіль висоти — зліт", "en": "Elevation profile — takeoff"},
    "box_route_top_view": {"uk": "Маршрут — вигляд згори", "en": "Route — top view"},
    "box_glide_chart": {"uk": "Графік глісади", "en": "Glide slope chart"},
    "btn_save_pdf": {"uk": "Зберегти PDF", "en": "Save PDF"},
    "dlg_save_report_title": {"uk": "Зберегти звіт аналізу", "en": "Save analysis report"},
    "msg_pdf_save_failed_body": {
        "uk": "Не вдалося зберегти PDF:\n{error}",
        "en": "Could not save PDF:\n{error}",
    },
    "msg_weather_title": {"uk": "Метео", "en": "Weather"},
    "msg_load_mission_first_body": {"uk": "Спочатку завантажте місію", "en": "Load a mission first"},
    "msg_set_flight_date_body": {
        "uk": "Вкажіть дату польоту (наприклад: 2026-08-10)",
        "en": "Enter the flight date (e.g. 2026-08-10)",
    },
    "msg_set_departure_time_body": {
        "uk": "Оберіть час вильоту -- без нього неможливо отримати погоду і карта не відмалюється",
        "en": "Choose a departure time -- without it the weather can't be fetched and the map won't render",
    },
    "msg_no_route_points_body": {"uk": "Немає точок маршруту", "en": "No route points"},
    "tab_takeoff": {"uk": "Зліт", "en": "Takeoff"},
    "tab_route": {"uk": "Маршрут", "en": "Route"},
    "tab_landing_phase": {"uk": "Посадка", "en": "Landing"},
    "hint_bad_speed": {"uk": "швидкість?", "en": "speed?"},

    # --- сторінка "Конфігурація" ---
    "label_cruise_speed": {"uk": "Крейсерська швидкість (м/с):", "en": "Cruise speed (m/s):"},
    "box_map_weather_services": {
        "uk": "Картографічні та метеосервіси",
        "en": "Map & weather services",
    },

    # --- сторінка "Довідка": перевірка оновлень ---
    "btn_check_updates": {"uk": "Перевірити оновлення", "en": "Check for updates"},
    "msg_update_title": {"uk": "Оновлення", "en": "Update"},
    "msg_update_check_failed_body": {
        "uk": "Не вдалося перевірити оновлення:\n{error}",
        "en": "Could not check for updates:\n{error}",
    },
    "msg_latest_version_body": {
        "uk": "У вас найновіша версія ({version}).",
        "en": "You have the latest version ({version}).",
    },
    "status_checking_updates": {"uk": "Перевірка оновлень...", "en": "Checking for updates..."},
    "status_up_to_date_fmt": {
        "uk": "Встановлена версія актуальна ({version})",
        "en": "Installed version is up to date ({version})",
    },
    "status_update_available_fmt": {
        "uk": "Доступна нова версія: {tag}",
        "en": "New version available: {tag}",
    },
    "msg_update_available_title": {"uk": "Доступне оновлення", "en": "Update available"},
    "msg_update_available_body_fmt": {
        "uk": "Доступна нова версія {tag} (зараз встановлено {current}).",
        "en": "A new version {tag} is available (currently installed: {current}).",
    },
    "msg_update_whats_new": {"uk": "Що нового:", "en": "What's new:"},
    "msg_update_confirm_install": {
        "uk": "Завантажити і встановити зараз?",
        "en": "Download and install now?",
    },
    "status_downloading_update_fmt": {"uk": "Завантаження {tag}...", "en": "Downloading {tag}..."},
    "msg_update_install_failed_body": {
        "uk": "Не вдалося встановити оновлення:\n{error}",
        "en": "Could not install the update:\n{error}",
    },
    "status_update_installed": {
        "uk": "Оновлено — перезапустіть програму",
        "en": "Updated — please restart the program",
    },
    "msg_map_unavailable_fmt": {"uk": "Карта недоступна\n{error}", "en": "Map unavailable\n{error}"},
    "msg_render_error_fmt": {"uk": "Помилка відмальовки:\n{error}", "en": "Rendering error:\n{error}"},

    # --- сторінка "Конфігурація": сервіси карт/погоди ---
    "label_occupied_layer": {"uk": "Шар окупованих територій:", "en": "Occupied-territories layer:"},
    "label_windy_service": {"uk": "Windy (вітер, онлайн-карта):", "en": "Windy (wind, online map):"},
    "label_openmeteo_service": {
        "uk": "Open-Meteo (прогноз, безкоштовно):",
        "en": "Open-Meteo (forecast, free):",
    },
    "label_gwa_service": {"uk": "Global Wind Atlas (кліматика):", "en": "Global Wind Atlas (climatology):"},

    # --- погода: "Зліт"/"Посадка" на "Аналіз" (текстовий звіт) ---
    "status_loading_weather": {"uk": "Завантаження метеоданих...", "en": "Loading weather data..."},
    "msg_weather_fetch_error_fmt": {
        "uk": "Помилка отримання метео:\n{error}",
        "en": "Error fetching weather:\n{error}",
    },
    "label_start_takeoff": {"uk": "Старт (Зліт)", "en": "Start (Takeoff)"},
    "weather_error_line_fmt": {"uk": "  Помилка: {error}", "en": "  Error: {error}"},
    "weather_date_line_fmt": {"uk": "  Дата            : {date}", "en": "  Date            : {date}"},
    "weather_sunrise_line_fmt": {
        "uk": "  Схід сонця      : {time}",
        "en": "  Sunrise         : {time}",
    },
    "weather_sunset_line_fmt": {"uk": "  Захід сонця     : {time}", "en": "  Sunset          : {time}"},
    "weather_temp_minmax_line_fmt": {
        "uk": "  Темп. (min/max) : {t_min}°C / {t_max}°C",
        "en": "  Temp. (min/max) : {t_min}°C / {t_max}°C",
    },
    "weather_wind_max_line_fmt": {
        "uk": "  Вітер макс.     : {speed} км/год, напрямок {dir}°",
        "en": "  Max wind        : {speed} km/h, direction {dir}°",
    },
    "weather_at_time_header_fmt": {"uk": "  — На {time} UTC —", "en": "  — At {time} UTC —"},
    "weather_wind_speed_line_fmt": {
        "uk": "  Швидкість вітру : {speed} км/год",
        "en": "  Wind speed      : {speed} km/h",
    },
    "weather_wind_dir_line_fmt": {"uk": "  Напрямок вітру  : {dir}°", "en": "  Wind direction  : {dir}°"},
    "weather_temp_line_fmt": {"uk": "  Температура     : {temp}°C", "en": "  Temperature     : {temp}°C"},
    "weather_strong_word": {"uk": "⚠ сильний", "en": "⚠ strong"},
    "weather_normal_word": {"uk": "норма", "en": "normal"},
    "weather_crosswind_line_fmt": {
        "uk": "  Боковий вітер   : {cross:.0f}°  ({strength})",
        "en": "  Crosswind       : {cross:.0f}°  ({strength})",
    },
    "weather_headwind_yes": {"uk": "так ✓ (добре)", "en": "yes ✓ (good)"},
    "weather_headwind_no": {"uk": "ні (попутний)", "en": "no (tailwind)"},
    "weather_headwind_line_fmt": {
        "uk": "  Зустрічний вітер: {value}",
        "en": "  Headwind        : {value}",
    },
    "weather_hourly_unavailable_fmt": {
        "uk": "  Погодинні дані на {time} UTC: недоступні",
        "en": "  Hourly data for {time} UTC: unavailable",
    },

    # --- PDF-звіт: заголовки та запасні тексти ---
    "pdf_title": {"uk": "Звіт аналізу місії — Mission Analyzer", "en": "Mission analysis report — Mission Analyzer"},
    "pdf_mission_file_fmt": {"uk": "Файл місії: {file}", "en": "Mission file: {file}"},
    "pdf_flight_info_fmt": {
        "uk": "Дата польоту: {date}   Час вильоту (UTC): {time}   Прибуття: {arrival}",
        "en": "Flight date: {date}   Departure time (UTC): {time}   Arrival: {arrival}",
    },
    "pdf_heading_takeoff_weather": {"uk": "Зліт — погода в точці старту", "en": "Takeoff — weather at start point"},
    "msg_no_data_press_weather": {
        "uk": "Немає даних (натисніть «Отримати метео»).",
        "en": 'No data (press "Get weather").',
    },
    "pdf_heading_route_map": {"uk": "Маршрут — карта", "en": "Route — map"},
    "pdf_heading_route_elevation": {"uk": "Маршрут — графік висоти", "en": "Route — elevation profile"},
    "pdf_no_remarks": {"uk": "Без зауважень.", "en": "No remarks."},
    "pdf_heading_route_angle": {"uk": "Маршрут — кут траєкторії", "en": "Route — flight path angle"},
    "pdf_heading_landing": {
        "uk": "Посадка — проблеми та погода посадки",
        "en": "Landing — issues and landing weather",
    },
    "msg_no_remarks_glide": {"uk": "Без зауважень по глісаді.", "en": "No remarks on the glide slope."},
    "dlg_pick_date_title": {"uk": "Дата польоту", "en": "Flight date"},
    "calendar_day_names": {"uk": "Пн,Вт,Ср,Чт,Пт,Сб,Нд", "en": "Mo,Tu,We,Th,Fr,Sa,Su"},
    "label_minutes_suffix_fmt": {"uk": "{time} (+{mins} хв)", "en": "{time} (+{mins} min)"},
    "msg_map_load_error_fmt": {
        "uk": "Помилка завантаження карти:\n{error}",
        "en": "Map loading error:\n{error}",
    },
    "box_takeoff_area": {"uk": "Старт — 4×4 км", "en": "Start — 4×4 km"},
    "box_landing_area": {"uk": "Посадка — 4×4 км", "en": "Landing — 4×4 km"},
    "msg_reportlab_missing_title": {"uk": "PDF", "en": "PDF"},
    "msg_reportlab_missing_body": {
        "uk": "Для збереження в PDF потрібна бібліотека reportlab.\n\nВстановіть її командою:\n    pip install reportlab",
        "en": "Saving to PDF requires the reportlab library.\n\nInstall it with:\n    pip install reportlab",
    },
    "msg_update_installed_body_fmt": {
        "uk": "Оновлення встановлено.\n\nРезервна копія попередніх файлів: {backup_dir}\n\nПерезапустіть програму, щоб застосувати зміни.",
        "en": "Update installed.\n\nBackup of previous files: {backup_dir}\n\nRestart the program to apply the changes.",
    },
    "unit_kmh_short": {"uk": "км/г", "en": "km/h"},
    "weather_crosswind_map_label_fmt": {
        "uk": "Боковий вітер: {cross:.0f}°",
        "en": "Crosswind: {cross:.0f}°",
    },

    # --- кнопка "Info" на "Місія": інформація про політний контролер ---
    "btn_info": {"uk": "Info", "en": "Info"},
    "btn_read": {"uk": "Зчитати", "en": "Read"},
    "btn_write": {"uk": "Записати", "en": "Write"},
    "btn_close": {"uk": "Закрити", "en": "Close"},
    "status_fetching_info": {"uk": "Отримання інформації...", "en": "Fetching info..."},
    "dlg_flight_info_title": {
        "uk": "Інформація про політний контролер",
        "en": "Flight controller info",
    },
    "info_header_fmt": {
        "uk": "{autopilot} / {vtype}",
        "en": "{autopilot} / {vtype}",
    },
    "info_section_firmware": {"uk": "Прошивка та плата", "en": "Firmware & board"},
    "info_no_response": {"uk": "Немає відповіді від контролера", "en": "No response from the controller"},
    "info_fw_version_fmt": {"uk": "Версія прошивки: {version}", "en": "Firmware version: {version}"},
    "info_board_version_fmt": {"uk": "Версія плати: {version}", "en": "Board version: {version}"},
    "info_vendor_product_fmt": {
        "uk": "Vendor ID: {vendor}   Product ID: {product}",
        "en": "Vendor ID: {vendor}   Product ID: {product}",
    },
    "info_uid_fmt": {"uk": "UID: {uid}", "en": "UID: {uid}"},
    "info_git_hash_fmt": {"uk": "Git-хеш прошивки: {hash}", "en": "Firmware git hash: {hash}"},
    "info_section_sensors": {"uk": "Датчики", "en": "Sensors"},
    "info_no_sensors": {"uk": "Дані про датчики відсутні", "en": "No sensor data available"},
    "info_sensor_unhealthy": {"uk": "НЕСПРАВНИЙ", "en": "UNHEALTHY"},
    "info_battery_fmt": {
        "uk": "Батарея: {voltage} В, {current} А, лишилось {remaining}%",
        "en": "Battery: {voltage} V, {current} A, {remaining}% remaining",
    },
    "value_na": {"uk": "н/д", "en": "n/a"},
    "info_section_storage": {"uk": "SD-карта", "en": "SD card"},
    "info_no_sd_card": {"uk": "SD-карта не знайдена або недоступна", "en": "SD card not found or unavailable"},
    "info_sd_present_no_capacity": {
        "uk": "Карта є й читається (підтверджено списком файлів), але контролер не повідомляє обсяг/зайнято/вільно",
        "en": "Card is present and readable (confirmed by file listing), but the controller doesn't report capacity/used/free",
    },
    "info_storage_capacity_fmt": {
        "uk": "Обсяг: {total:.0f} МБ, зайнято {used:.0f} МБ, вільно {available:.0f} МБ",
        "en": "Capacity: {total:.0f} MB, used {used:.0f} MB, free {available:.0f} MB",
    },
    "info_storage_speed_fmt": {
        "uk": "Швидкість: читання {read:.1f} МБ/с, запис {write:.1f} МБ/с",
        "en": "Speed: read {read:.1f} MB/s, write {write:.1f} MB/s",
    },
    "info_section_files": {"uk": "Файли на SD-карті (корінь)", "en": "Files on SD card (root)"},
    "info_no_files": {"uk": "Порожньо", "en": "Empty"},
    "info_ftp_not_supported": {
        "uk": "Контролер не підтримує MAVLink FTP -- список файлів недоступний",
        "en": "The controller doesn't support MAVLink FTP -- file list unavailable",
    },
    "info_ftp_error_fmt": {"uk": "Не вдалося отримати список файлів: {error}", "en": "Could not get file list: {error}"},
    "info_fetch_error_fmt": {
        "uk": "Не вдалося отримати інформацію від контролера:\n{error}",
        "en": "Could not get info from the controller:\n{error}",
    },

    # --- AUTO-підключення (перебір портів/швидкостей) ---
    "status_auto_detecting": {"uk": "Автопошук порту...", "en": "Auto-detecting port..."},
    "msg_no_heartbeat_body": {
        "uk": "Порт відкрито, але heartbeat від контролера не отримано",
        "en": "Port opened, but no heartbeat received from the controller",
    },
    "msg_no_ports_found": {"uk": "Не знайдено жодного COM-порту", "en": "No COM ports found"},
    "msg_auto_detect_failed": {
        "uk": "Жоден порт не відповів на MAVLink heartbeat",
        "en": "No port responded to a MAVLink heartbeat",
    },

    # --- файловий менеджер SD-карти ---
    "btn_sd_files": {"uk": "Файли SD", "en": "SD Files"},
    "dlg_sd_files_title": {"uk": "Файли на SD-карті", "en": "SD card files"},
    "col_name": {"uk": "Ім'я", "en": "Name"},
    "col_size": {"uk": "Розмір, Б", "en": "Size, B"},
    "btn_download": {"uk": "Завантажити на ПК", "en": "Download to PC"},
    "btn_upload": {"uk": "Вивантажити на SD", "en": "Upload to SD"},
    "btn_delete": {"uk": "Видалити", "en": "Delete"},
    "btn_refresh": {"uk": "Оновити", "en": "Refresh"},
    "status_loading_list": {"uk": "Завантаження списку...", "en": "Loading list..."},
    "msg_select_file_not_dir": {
        "uk": "Оберіть файл (не папку) для завантаження",
        "en": "Select a file (not a folder) to download",
    },
    "status_downloading_file_fmt": {"uk": "Завантаження {name}...", "en": "Downloading {name}..."},
    "status_download_done": {"uk": "Файл завантажено", "en": "File downloaded"},
    "status_uploading_file_fmt": {"uk": "Вивантаження {name}...", "en": "Uploading {name}..."},
    "status_upload_done": {"uk": "Файл вивантажено", "en": "File uploaded"},
    "status_deleting_fmt": {"uk": "Видалення {name}...", "en": "Deleting {name}..."},
    "status_delete_done": {"uk": "Видалено", "en": "Deleted"},
    "msg_confirm_delete_fmt": {
        "uk": "Видалити «{name}» з SD-карти? Це незворотньо.",
        "en": "Delete \"{name}\" from the SD card? This cannot be undone.",
    },
    "msg_transfer_failed_fmt": {
        "uk": "Операція не вдалася:\n{error}",
        "en": "Operation failed:\n{error}",
    },

    # --- перегляд файлу без завантаження на диск, оновлений список ---
    "btn_preview": {"uk": "Перегляд", "en": "Preview"},
    "status_list_refreshed": {"uk": "Список оновлено", "en": "List refreshed"},
    "status_loading_preview_fmt": {
        "uk": "Завантаження {name} для перегляду...",
        "en": "Loading {name} for preview...",
    },
    "dlg_preview_title_fmt": {"uk": "Перегляд: {name}", "en": "Preview: {name}"},
    "msg_file_too_large_preview_fmt": {
        "uk": "Файл завеликий для перегляду ({size} Б). Скачайте його на комп'ютер.",
        "en": "File is too large to preview ({size} B). Download it to your computer instead.",
    },

    # --- людські назви кодів помилок MAVLink FTP (FtpError) ---
    "info_ftp_err_fail": {"uk": "загальна помилка", "en": "generic failure"},
    "info_ftp_err_errno": {"uk": "помилка файлової системи на контролері", "en": "filesystem error on the controller"},
    "info_ftp_err_bad_size": {"uk": "невірний розмір пакета", "en": "invalid packet size"},
    "info_ftp_err_bad_session": {
        "uk": "невірна сесія (спробуйте ще раз)",
        "en": "invalid session (try again)",
    },
    "info_ftp_err_no_sessions": {"uk": "немає вільних сесій на контролері", "en": "no free sessions on the controller"},
    "info_ftp_err_eof": {"uk": "кінець файлу", "en": "end of file"},
    "info_ftp_err_unknown_cmd": {"uk": "невідома команда", "en": "unknown command"},
    "info_ftp_err_exists": {"uk": "файл вже існує", "en": "file already exists"},
    "info_ftp_err_protected": {"uk": "файл захищено від видалення", "en": "file is protected from deletion"},
    "info_ftp_err_not_found": {
        "uk": "контролер не знайшов такий файл/шлях",
        "en": "the controller couldn't find that file/path",
    },
    "info_ftp_err_bad_args": {"uk": "невірні параметри запиту", "en": "invalid request parameters"},
    "info_ftp_err_local_open": {
        "uk": "не вдалося відкрити локальний файл на комп'ютері",
        "en": "could not open the local file on this computer",
    },
    "info_ftp_err_timeout": {"uk": "немає відповіді від контролера (тайм-аут)", "en": "no reply from the controller (timeout)"},
    "info_ftp_err_unknown_fmt": {"uk": "код помилки {code}", "en": "error code {code}"},
    "label_binary_hexdump": {"uk": "бінарний, hex", "en": "binary, hex"},

    # --- графіки датафлеш-логу (.BIN) ---
    "btn_log_graphs": {"uk": "Графіки логу", "en": "Log graphs"},
    "status_downloading_log_fmt": {
        "uk": "Завантаження {name} для аналізу...",
        "en": "Downloading {name} for analysis...",
    },
    "msg_no_log_data": {
        "uk": "У файлі не знайдено даних для графіків (не датафлеш-лог або порожній)",
        "en": "No graphable data found in the file (not a dataflash log, or empty)",
    },
    "dlg_log_graphs_title_fmt": {"uk": "Графіки логу: {name}", "en": "Log graphs: {name}"},
    "label_log_altitude": {"uk": "Висота, м", "en": "Altitude, m"},
    "label_log_speed": {"uk": "Швидкість (GPS), м/с", "en": "Speed (GPS), m/s"},
    "label_log_voltage": {"uk": "Напруга батареї, В", "en": "Battery voltage, V"},
    "ctx_open_folder": {"uk": "Відкрити", "en": "Open"},

    # --- редактор місії ---
    "btn_edit_mission": {"uk": "Редагувати", "en": "Edit"},
    "btn_stop_editing": {"uk": "Завершити редагування", "en": "Stop editing"},
    "ctx_add_waypoint": {"uk": "Додати точку", "en": "Add waypoint"},
    "ctx_add_command": {"uk": "Додати команду", "en": "Add command"},
    "dlg_choose_command_title": {"uk": "Оберіть команду", "en": "Choose command"},
    "btn_ok": {"uk": "OK", "en": "OK"},
    "box_altitude_profile": {"uk": "Профіль висот", "en": "Altitude profile"},
    "label_distance_axis": {"uk": "Дистанція вздовж маршруту", "en": "Distance along route"},
    "unit_meters_axis": {"uk": "м", "en": "m"},
    "unit_km_axis": {"uk": "км", "en": "km"},
    "unit_degrees_axis": {"uk": "°", "en": "°"},
    "label_waypoint_axis": {"uk": "№ точки", "en": "waypoint #"},
    "status_zoom_capped_fmt": {
        "uk": "Зум обмежено до {zoom} -- на цій ділянці маршруту вищий зум вимагає забагато тайлів.",
        "en": "Zoom capped to {zoom} -- higher zoom needs too many tiles for this route's area.",
    },
    "label_max_tiles": {"uk": "Ліміт тайлів карти:", "en": "Map tile limit:"},
    "hint_max_tiles": {
        "uk": "Вищий ліміт -- доступний більший зум на широких маршрутах, але довше завантаження й більше пам'яті.",
        "en": "Higher limit -- more zoom available on wide routes, but slower loading and more memory.",
    },
    "label_app_theme": {"uk": "Тема програми:", "en": "App theme:"},
    "radio_theme_dark": {"uk": "Темна", "en": "Dark"},
    "radio_theme_light": {"uk": "Світла", "en": "Light"},
    "msg_terrain_unavailable": {
        "uk": "Рельєф недоступний -- увімкніть SRTM у Конфігурації",
        "en": "Terrain unavailable -- enable SRTM in Configuration",
    },
}


def set_lang(lang: str):
    global _current_lang
    if lang in LANGS:
        _current_lang = lang


def get_lang() -> str:
    return _current_lang


def t(key: str, **kwargs) -> str:
    entry = _TR.get(key)
    if entry is None:
        return key
    text = entry.get(_current_lang) or entry.get("en") or key
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, ValueError):
            pass
    return text


def format_issue_detail(issue: dict) -> str:
    """Строит локализованный текст 'детали' проблемы по её структурированным полям."""
    kind = issue.get("type")
    extra = issue.get("extra") or {}

    if kind == "LOW_ALTITUDE":
        return t("detail_low_altitude", value=issue["value"], threshold=issue["threshold"])
    if kind == "LOW_AGL":
        return t("detail_low_agl", value=issue["value"], threshold=issue["threshold"])
    if kind == "LOW_AGL_SEGMENT":
        return t(
            "detail_low_agl_segment",
            value=issue["value"], threshold=issue["threshold"],
            from_idx=extra.get("from_idx"), to_idx=extra.get("to_idx"),
        )
    if kind == "SHARP_TURN":
        return t("detail_sharp_turn", value=issue["value"], threshold=issue["threshold"])
    if kind == "STEEP_ANGLE":
        return t(
            "detail_steep_angle",
            value=issue["value"], threshold=issue["threshold"],
            from_idx=extra.get("from_idx"), to_idx=issue["wp_index"],
        )
    if kind == "SRTM_MISSING":
        return t("detail_srtm_missing", lat=extra.get("lat", 0.0), lon=extra.get("lon", 0.0))
    return ""


def speed_type_label(speed_type: int) -> str:
    return t(f"speed_type_{speed_type}") if f"speed_type_{speed_type}" in _TR else str(speed_type)


def issue_title(kind: str, threshold: float | None) -> str:
    key = {
        "LOW_ALTITUDE": "title_low_altitude",
        "LOW_AGL": "title_low_agl",
        "LOW_AGL_SEGMENT": "title_low_agl_segment",
        "SHARP_TURN": "title_sharp_turn",
        "STEEP_ANGLE": "title_steep_angle",
        "SRTM_MISSING": "title_srtm_missing",
    }.get(kind)
    if key is None:
        return kind
    if threshold is None:
        return t(key, threshold=0.0)
    return t(key, threshold=threshold)
