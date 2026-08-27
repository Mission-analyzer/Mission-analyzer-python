"""
meta.py — версія програми та історія змін (об'єднано з колишніх
version.py + changelog.py — обидва були зовсім маленькі й завжди
редагувались разом при кожному релізі, тож логічно тримати їх в
одному місці). Показується на сторінці «Довідка» (Help).

Онови VERSION і додай новий запис у ПОЧАТОК списку ENTRIES при
значимих оновленнях.
"""

from __future__ import annotations

VERSION = "1.0.1"

ENTRIES = [
    {
        "version": "1.0.1",
        "date": "2026-08-17",
        "uk": "Незначні косметичні покращення та оптимізація продуктивності.",
        "en": "Minor cosmetic improvements and performance optimizations.",
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
