"""
theme.py — визуальная тема "а-ля Mission Planner": тёмно-синяя шапка и
статус-бар, синие акцентные кнопки, вкладки с подсветкой активной.
Ничего не знает про конкретные виджеты приложения — просто настраивает
ttk.Style и возвращает палитру цветов для точечного использования в app.py.

Підтримує ДВІ палітри (світла/темна, apply_theme(root, dark=...)) --
шапка/статус-бар/акценти лишаються тими самими в обох (вони й так темні
за задумом, "а-ля Mission Planner"), змінюється лише робоча область:
фон сторінок, поля вводу/списки, колір тексту.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# Палитра в духе Mission Planner: тёмно-синяя шапка/тулбар, синий акцент
# на активных элементах. Те частини, що спільні для обох тем (шапка,
# статус-бар, зелений акцент) -- НЕ дублюються нижче в PALETTE_DARK,
# лишаються тими самими.
NAVY_DARK = "#0F2438"   # тёмно-синий — навигационная панель, статус-бар, активные вкладки
NAVY = "#173553"        # чуть светлее — неактивные тёмные элементы
HEADER_BG = "#000000"   # чёрный — фон шапки (лого/заголовок/переключатель языка)
# зелёный акцент для кнопок -- взят из логотипа (маршрут/точки/"ANALYZER")
GREEN_ACCENT = "#5A961E"
GREEN_ACCENT_HOVER = "#6FB026"
GREEN_DARK = "#355E12"
TEXT_LIGHT = "#FFFFFF"
TEXT_MUTED = "#9FB6CC"  # приглушённый текст на тёмном фоне (статус-бар)

# --- світла тема (за замовчуванням раніше) ---
LIGHT_BG = "#F1F3F5"    # фон рабочей области
LIGHT_PANEL = "#FFFFFF"    # фон полей ввода/списков
LIGHT_TEXT = "#1B1B1B"
LIGHT_BORDER = "#C9CFD6"

# --- темна тема ---
DARK_BG = "#151515"
DARK_PANEL = "#262626"
DARK_TEXT = "#E8E8E8"
DARK_BORDER = "#444444"

PALETTE_LIGHT = {
    "navy_dark": NAVY_DARK,
    "navy": NAVY,
    "header_bg": HEADER_BG,
    "blue": GREEN_ACCENT,        # оставлено под старым ключом ради обратной совместимости в app.py
    "blue_hover": GREEN_ACCENT_HOVER,
    "green": GREEN_ACCENT,
    "green_hover": GREEN_ACCENT_HOVER,
    "green_dark": GREEN_DARK,
    "bg": LIGHT_BG,
    "panel": LIGHT_PANEL,
    "text": LIGHT_TEXT,
    "text_light": TEXT_LIGHT,
    "text_muted": TEXT_MUTED,
    "border": LIGHT_BORDER,
    "dark": False,
}

PALETTE_DARK = {
    "navy_dark": NAVY_DARK,
    "navy": NAVY,
    "header_bg": HEADER_BG,
    "blue": GREEN_ACCENT,
    "blue_hover": GREEN_ACCENT_HOVER,
    "green": GREEN_ACCENT,
    "green_hover": GREEN_ACCENT_HOVER,
    "green_dark": GREEN_DARK,
    "bg": DARK_BG,
    "panel": DARK_PANEL,
    "text": DARK_TEXT,
    "text_light": TEXT_LIGHT,
    "text_muted": TEXT_MUTED,
    "border": DARK_BORDER,
    "dark": True,
}

# лишено заради зворотної сумісності з будь-яким кодом, що досі імпортує
# theme.PALETTE напряму (тепер -- це просто світла тема за замовчуванням)
PALETTE = PALETTE_LIGHT

# ============================================================
# Єдині кольори для графіків висоти/кута/глісади -- ОДНЕ джерело
# правди для "Місія" (mission_page.py/mission_editor.py: профіль
# висот) і "Аналіз" (elevation_view.py/angle_view.py/landing_view.py:
# профіль зльоту/маршруту/кута/глісади). Раніше кожен файл тримав
# власну копію цих самих кольорів -- і вони випадково розійшлись між
# "Місія" й "Аналіз" (лінія рельєфу/польоту відрізнялась кольором),
# довелось звіряти вручну й синхронізувати. Тепер кожен файл читає
# звідси (chart_colors(dark)), а не тримає свій хардкод.
CHART_COLORS_DARK = {
    "line_primary": "#ff2222",   # висота польоту/профіль маршруту/точка посадки -- усе, що стосується самого польоту
    "line_terrain": "#3b82f6",   # рельєф (лінія, без заливки)
    "grid": "#3a3a3a",
    "grid_minor": "#2a2a2a",
    "text": "#e0e0e0",
    "muted": "#888888",
    "accent_green": "#5ecb5e",   # інформаційні мітки (напр. DO_CHANGE_SPEED)
    "marker_outline": "#dddddd",
}
CHART_COLORS_LIGHT = {
    "line_primary": "#d62728",
    "line_terrain": "#1f77b4",
    "grid": "#e0e0e0",
    "grid_minor": "#f0f0f0",
    "text": "#000000",
    "muted": "#666666",
    "accent_green": "#2ca02c",
    "marker_outline": "white",
}


def chart_colors(dark: bool) -> dict:
    """Повертає CHART_COLORS_DARK/LIGHT -- єдине джерело кольорів для
    ВСІХ графіків профілю висоти/кута/глісади в програмі."""
    return CHART_COLORS_DARK if dark else CHART_COLORS_LIGHT


# ============================================================
# Єдині кольори для ВСІХ повзунків/смуг прокрутки в програмі --
# tk.Scale (зум карти на "Місія") і tk.Scrollbar (зовнішня прокрутка
# сторінки "Місія", прокрутка таблиці місії, прокрутка вкладок
# "Аналіз") усі мають виглядати ОДНАКОВО. Свідомо ЗАВЖДИ tk.Scale/
# tk.Scrollbar, ніколи ttk.Scale/ttk.Scrollbar -- на Windows нативна
# тема ttk часто ігнорує кольори, задані через ttk.Style/опції, для
# цих двох класів віджетів (та сама причина, що й для інших "чорних"
# контейнерів на "Місія" раніше в цьому проєкті).
SLIDER_COLORS_DARK = {
    "bg": "#2b2b2b", "trough": "#d9d9d9", "active": "#444444",
}
SLIDER_COLORS_LIGHT = {
    "bg": "#888888", "trough": "#e6e6e6", "active": "#666666",
}


def slider_colors(dark: bool) -> dict:
    """Повертає SLIDER_COLORS_DARK/LIGHT -- єдине джерело кольорів для
    ВСІХ tk.Scale/tk.Scrollbar у програмі."""
    return SLIDER_COLORS_DARK if dark else SLIDER_COLORS_LIGHT

_FONT_FAMILY = "Segoe UI"  # на Windows есть всегда; на других ОС ttk сам подберёт похожий


def apply_theme(root: tk.Tk, dark: bool = False) -> dict:
    """
    Настраивает ttk.Style под тему Mission Planner. Повертає палітру
    кольорів. Можна викликати ПОВТОРНО (при перемиканні світла/темна в
    Конфігурації) -- ttk.Style().configure() одразу перефарбовує УСІ вже
    створені ttk-віджети зі стандартними іменами стилів (TFrame/TLabel/
    TButton тощо), без потреби перебудовувати їх заново. Віджети з
    прямим bg= (не через ttk.Style) не підхоплюють це автоматично -- їх
    перефарбовує сам виклик (app.py: apply_app_theme()).
    """
    colors = PALETTE_DARK if dark else PALETTE_LIGHT

    style = ttk.Style(root)
    # 'clam' — единственная встроенная тема, которая реально позволяет
    # перекрашивать фон/акценты кросс-платформенно (native-темы Windows
    # игнорируют часть настроек цвета)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    root.configure(bg=colors["bg"])

    base_font = (_FONT_FAMILY, 9)
    bold_font = (_FONT_FAMILY, 9, "bold")

    style.configure(".", background=colors["bg"], foreground=colors["text"], font=base_font)
    style.configure("TFrame", background=colors["bg"])
    style.configure("TLabel", background=colors["bg"], foreground=colors["text"])
    style.configure("TCheckbutton", background=colors["bg"], foreground=colors["text"])
    style.map("TCheckbutton", background=[("active", colors["bg"])])

    style.configure("TLabelframe", background=colors["bg"], bordercolor=colors["border"])
    style.configure(
        "TLabelframe.Label", background=colors["bg"],
        foreground=NAVY if not dark else "#7fa8d9", font=bold_font,
    )

    # Treeview -- БЕЗЫМЕННИЙ (дефолтний) стиль, для будь-якого дерева/
    # таблиці в проєкті, що НЕ використовує власний іменований стиль
    # (напр. mission_table навмисне має власний "MissionBlack.Treeview"
    # з додатковими кольорами виділення -- цей блок його не чіпає).
    # ВАЖЛИВО: у темі "clam" ttk.Treeview має ОКРЕМИЙ від загального
    # "background" параметр -- "fieldbackground" (колір області з самими
    # рядками) -- він НЕ успадковується від style.configure(".", ...)
    # вище автоматично. Без явного налаштування САМЕ "fieldbackground"
    # тут -- дерево лишається з дефолтним (зазвичай білим) кольором
    # реалізації теми "clam", тоді як текст (foreground) успадковується
    # коректно -- звідси "світлий текст на білому тлі" в темній темі
    # (реальний баг, знайдений і підтверджений на живому застосунку:
    # діалог "Файли SD" -- sd_file_manager.py, там Treeview створюється
    # без style= взагалі).
    style.configure(
        "Treeview", background=colors["bg"], fieldbackground=colors["bg"], foreground=colors["text"],
    )
    style.map(
        "Treeview",
        background=[("selected", colors["blue"])],
        foreground=[("selected", "white")],
    )
    style.configure(
        "Treeview.Heading", background=colors["panel"], foreground=colors["text"], relief="flat",
    )
    style.map("Treeview.Heading", background=[("active", colors["border"])])

    style.configure("TEntry", fieldbackground=colors["panel"], foreground=colors["text"], bordercolor=colors["border"])
    style.configure(
        "TSpinbox", fieldbackground=colors["panel"], foreground=colors["text"],
        bordercolor=colors["border"], arrowsize=12,
    )
    style.configure("TCombobox", fieldbackground=colors["panel"], foreground=colors["text"], bordercolor=colors["border"])
    style.map("TCombobox", fieldbackground=[("readonly", colors["panel"])])
    # список випадаючого combobox -- окремий шар (Tk listbox всередині
    # popdown-вікна), style.map тут не діє, задається напряму опцією
    root.option_add("*TCombobox*Listbox.background", colors["panel"])
    root.option_add("*TCombobox*Listbox.foreground", colors["text"])

    # обычные кнопки — зелёный акцент (як в лого), як основні дії в MP
    style.configure(
        "TButton", background=GREEN_ACCENT, foreground=TEXT_LIGHT,
        borderwidth=0, focusthickness=0, padding=(12, 6), font=bold_font,
    )
    style.map(
        "TButton",
        background=[("disabled", "#B7C4D1"), ("pressed", GREEN_DARK), ("active", GREEN_ACCENT_HOVER)],
        foreground=[("disabled", "#E8ECEF")],
    )

    # нейтральные кнопки — тот же спокойный вид, что у вкладок на страницах
    # "Аналіз"/"Довідка" (без синей заливки), для действий, которые не
    # нужно акцентировать как основные (напр. "Завантажити"/"Зберегти").
    # При нажатии — та же логика, что у остальных элементов навигации:
    # тёмный/чёрный фон + "утопленный" вид кнопки (чуть меньше на вид).
    secondary_bg = "#DEE3E8" if not dark else "#3a3a3a"
    secondary_fg = colors["text"]
    style.configure(
        "Secondary.TButton", background=secondary_bg, foreground=secondary_fg,
        borderwidth=0, focusthickness=0, padding=(12, 6), font=bold_font,
    )
    style.map(
        "Secondary.TButton",
        background=[
            ("disabled", "#EDEFF1" if not dark else "#2a2a2a"),
            ("pressed", HEADER_BG),
            ("active", "#C9CFD6" if not dark else "#4a4a4a"),
        ],
        foreground=[("disabled", "#9AA5AE"), ("pressed", TEXT_LIGHT)],
        padding=[("pressed", (10, 5))],
    )

    # вкладки блокнота — светло-серые/тёмно-серые неактивные, тёмно-синие активные
    tab_idle_bg = "#DEE3E8" if not dark else "#2b2b2b"
    style.configure("TNotebook", background=colors["bg"], borderwidth=0, tabmargins=(2, 4, 2, 0))
    style.configure(
        "TNotebook.Tab", background=tab_idle_bg, foreground=colors["text"],
        padding=(14, 7), font=bold_font, borderwidth=0,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", NAVY_DARK)],
        foreground=[("selected", TEXT_LIGHT)],
        expand=[("selected", (1, 1, 1, 0))],
    )

    # полосы прокрутки — нейтральные, но не выбивающиеся из темы
    style.configure(
        "TScrollbar", background=tab_idle_bg, troughcolor=colors["bg"],
        bordercolor=colors["border"], arrowsize=12,
    )

    # шапка окна -- та сама в обох темах (вона й так темна за задумом)
    style.configure("Header.TFrame", background=HEADER_BG)
    style.configure("Header.TLabel", background=HEADER_BG, foreground=TEXT_LIGHT, font=(_FONT_FAMILY, 13, "bold"))
    style.configure("HeaderSub.TLabel", background=HEADER_BG, foreground=TEXT_MUTED, font=(_FONT_FAMILY, 9))

    # статус-бар -- та сама в обох темах
    style.configure("Status.TFrame", background=NAVY_DARK)
    style.configure("Status.TLabel", background=NAVY_DARK, foreground=TEXT_MUTED, font=base_font)

    # переключатель языка — маленькие кнопки-тумблеры на тёмном фоне шапки
    style.configure(
        "LangToggle.TButton", background=GREEN_DARK, foreground=TEXT_MUTED,
        borderwidth=0, padding=(8, 4), font=bold_font,
    )
    style.map(
        "LangToggle.TButton",
        background=[("pressed", GREEN_ACCENT), ("active", HEADER_BG)],
        foreground=[("pressed", TEXT_LIGHT)],
    )
    style.configure(
        "LangToggleActive.TButton", background=GREEN_ACCENT, foreground=TEXT_LIGHT,
        borderwidth=0, padding=(8, 4), font=bold_font,
    )
    style.map("LangToggleActive.TButton", background=[("active", GREEN_ACCENT_HOVER)])

    return colors


def set_window_icon(root: tk.Tk, path: str) -> bool:
    """
    Пробует установить иконку окна из файла (.png/.gif через PhotoImage,
    .ico через iconbitmap на Windows). Возвращает True при успехе, иначе
    молча отступает — отсутствие иконки не должно ронять программу.
    """
    try:
        if path.lower().endswith(".ico"):
            root.iconbitmap(path)
        else:
            img = tk.PhotoImage(file=path)
            root.iconphoto(True, img)
            root._icon_ref = img  # держим ссылку, иначе GC съест картинку
        return True
    except (tk.TclError, FileNotFoundError):
        return False
