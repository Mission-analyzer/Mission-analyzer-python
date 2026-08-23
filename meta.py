"""
meta.py — версія програми та історія змін (об'єднано з колишніх
version.py + changelog.py — обидва були зовсім маленькі й завжди
редагувались разом при кожному релізі, тож логічно тримати їх в
одному місці). Показується на сторінці «Довідка» (Help).

Онови VERSION і додай новий запис у ПОЧАТОК списку ENTRIES при
значимих оновленнях.
"""

from __future__ import annotations

VERSION = "1.0.2"

ENTRIES = [
    {
        "version": "1.0.2",
        "date": "2026-08-22",
        "uk": (
            "Редактор місії на «Місія»: перетягування точок мишею просто на карті, "
            "редагування комірок таблиці подвійним кліком, перетягування висоти на "
            "профілі висот, додавання/видалення точок і команд через праву кнопку миші. "
            "Вмикається окремою кнопкою «Редагувати» — усі зміни спрацьовують лише в "
            "цьому режимі, звичайний перегляд і панорамування карти лишаються "
            "недоторканими.\n\n"
            "Карта на «Місія» тепер поводиться як у Mission Planner: після завантаження "
            "місії весь маршрут вміщується в екран без прокрутки, зум можна підняти аж "
            "до 24 (раніше впирався в ліміт тайлів на широких маршрутах) — на високому "
            "зумі довантажується лише видима область навколо точки огляду, а не весь "
            "маршрут одразу. Кнопка-піктограма у верхньому лівому куті карти повертає до "
            "початкового вигляду з будь-якого зуму чи положення. Панорамування більше не "
            "мигтить порожнім екраном під час руху.\n\n"
            "Темна/світла тема на весь застосунок (перемикається в Конфігурації, "
            "застосовується одразу): фон, таблиця місії, графіки висоти/кута/глісади, "
            "PDF-звіт завжди зберігається світлим для друку незалежно від активної теми "
            "екрана.\n\n"
            "Список команд у редакторі місії звужено з ~188 до 47 — лише ті, що реально "
            "валідні як елемент місії ArduPilot (як у Mission Planner), решта (ARM/DISARM, "
            "preflight-калібрування, точки geofence тощо) прибрана як така, що ніколи не "
            "з'являється в .waypoints-файлі.\n\n"
            "Графіки висоти на «Місія» і «Аналіз» тепер узгоджені за кольором: маршрут/"
            "висота польоту завжди червоні, рельєф — завжди синій, без заливки під ним."
        ),
        "en": (
            "Mission editor on \\\"Mission\\\": drag waypoints directly on the map, "
            "double-click to edit table cells, drag altitude on the elevation profile, "
            "add/delete waypoints and commands via right-click. Enabled with a separate "
            "\\\"Edit\\\" button — all changes only take effect in this mode, normal viewing "
            "and map panning stay untouched.\n\n"
            "The map on \\\"Mission\\\" now behaves like Mission Planner: after loading a "
            "mission the whole route fits the screen with no scrolling, zoom can go up to "
            "24 (previously capped by the tile limit on wide routes) — at high zoom only "
            "the visible area around the viewpoint loads, not the whole route at once. An "
            "icon button in the map's top-left corner resets the view from any zoom or "
            "position. Panning no longer flashes an empty screen mid-drag.\n\n"
            "App-wide dark/light theme (switch in Configuration, applies instantly): "
            "background, mission table, elevation/angle/glide-slope charts, PDF reports "
            "always stay light for printing regardless of the active screen theme.\n\n"
            "The mission editor's command list is narrowed from ~188 to 47 — only the ones "
            "actually valid as a mission item in ArduPilot (matching Mission Planner), the "
            "rest (ARM/DISARM, preflight calibration, geofence points, etc.) removed as "
            "never appearing in a .waypoints file.\n\n"
            "Elevation charts on \\\"Mission\\\" and \\\"Analysis\\\" now share consistent colors: "
            "route/flight altitude is always red, terrain is always blue, no fill "
            "underneath it."
        ),
    },
    {
        "version": "1.0.1",
        "date": "2026-08-16",
        "uk": (
            "Карта на «Місія» і «Аналіз → Маршрут»: тепер завжди рівно по ширині "
            "екрана, без сірих полів по боках — масштабується під реальні пропорції "
            "маршруту, а не підганяється під фіксований контейнер. Зум підбирається "
            "автоматично під розмір канваса при кожному завантаженні місії, замість "
            "старого значення з налаштувань.\n\n"
            "Графік кута нахилу траєкторії: точка LAND більше не враховується — різкий "
            "кут заходу на посадку (десятки градусів) більше не «з'їдає» масштаб графіка "
            "для решти маршруту (для самого заходу є окрема перевірка — глісада).\n\n"
            "Виправлено висоту шапки — раніше вона «стрибала» між сторінками "
            "«Конфігурація»/«Довідка» і «Місія»/«Аналіз».\n\n"
            "Повна локалізація UA/EN: кнопки, підписи вкладок, статусні повідомлення, "
            "текст погоди (зліт/посадка), заголовки PDF-звіту — раніше частина "
            "інтерфейсу лишалась українською навіть в англійському режимі. Заразом "
            "виправлено розподіл звіту по вкладках (висота/кут/глісада), який у "
            "англійському режимі міг класти все не туди."
        ),
        "en": (
            "Map on \"Mission\" and \"Analysis → Route\": now always exactly screen-width, "
            "no gray bars on the sides — scales to the route's real proportions instead of "
            "being forced into a fixed container. Zoom is picked automatically for the "
            "canvas size on every mission load, instead of reusing the old saved value.\n\n"
            "Flight path angle graph: the LAND point is no longer included — the steep "
            "landing approach angle (tens of degrees) no longer skews the scale for the "
            "rest of the route (landing approach has its own dedicated check — glide "
            "slope).\n\n"
            "Fixed header height — it used to \"jump\" between the \"Configuration\"/\"Help\" "
            "pages and \"Mission\"/\"Analysis\".\n\n"
            "Full UA/EN localization: buttons, tab labels, status messages, weather text "
            "(takeoff/landing), PDF report headings — some of the interface previously "
            "stayed in Ukrainian even in English mode. Also fixed report distribution "
            "across tabs (elevation/angle/glide), which could misfile everything in "
            "English mode."
        ),
    },
    {
        "version": "1.0.0",
        "date": "2026-08-02",
        "uk": (
            "Перший реліз.\n\n"
            "Аналіз місії (.waypoints, QGC WPL 110):\n"
            "- критично низька висота (за файлом і за рельєфом SRTM — як у самих точках, "
            "так і вздовж усієї лінії польоту між ними)\n"
            "- критично гострі кути повороту\n"
            "- кут нахилу траєкторії (набір/зниження) поза допуском\n"
            "- глісада заходу на посадку: дистанція/азимут/кут по останніх відрізках до LAND, "
            "з урахуванням команд DO_CHANGE_SPEED\n\n"
            "Графіки: висота (з рельєфом), кут траєкторії, глісада — усі з наскрізною нумерацією "
            "реальних точок маршруту.\n\n"
            "Карта: тайли з інтернету (OpenStreetMap / Google Maps / Google Satellite / Google "
            "Гібрид) із диск-кешем по провайдерах, зум колесом миші, шар окупованих територій / "
            "лінії зіткнення (deepstatemap.live).\n\n"
            "Таблиця місії на сторінці «Місія» — усі команди файлу, як у Mission Planner.\n\n"
            "Інтерфейс: 4 сторінки (Місія / Аналіз / Конфігурація / Довідка) з навігацією іконка+"
            "підпис, тема оформлення у стилі Mission Planner, локалізація UA/EN без російської, "
            "автозбереження налаштувань між запусками, сплеш-екран зі версією при старті."
        ),
        "en": (
            "First release.\n\n"
            "Mission analysis (.waypoints, QGC WPL 110):\n"
            "- critically low altitude (from the file and from SRTM terrain — both at the points "
            "themselves and along the whole flight line between them)\n"
            "- critically sharp turn angles\n"
            "- flight path angle (climb/descent) out of tolerance\n"
            "- landing approach glide slope: distance/bearing/angle for the final legs before "
            "LAND, including DO_CHANGE_SPEED commands\n\n"
            "Graphs: elevation (with terrain), flight path angle, glide slope — all with "
            "consistent numbering of the real route points.\n\n"
            "Map: tiles from the internet (OpenStreetMap / Google Maps / Google Satellite / "
            "Google Hybrid) with a per-provider disk cache, mouse-wheel zoom, occupied-"
            "territories / line-of-contact layer (deepstatemap.live).\n\n"
            "Mission table on the Mission page — every command in the file, like in Mission "
            "Planner.\n\n"
            "Interface: 4 pages (Mission / Analysis / Configuration / Help) with icon+label "
            "navigation, Mission-Planner-style theme, UA/EN localization (no Russian), settings "
            "auto-saved between runs, splash screen with version on startup."
        ),
    },
]


def format_changelog(lang: str) -> str:
    parts = []
    for e in ENTRIES:
        text = e.get(lang) or e.get("en", "")
        parts.append(f"{e['version']} — {e['date']}\n\n{text}")
    return "\n\n" + ("\n\n" + "=" * 40 + "\n\n").join(parts)
