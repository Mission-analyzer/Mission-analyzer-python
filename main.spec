# -*- mode: python ; coding: utf-8 -*-
#
# Зібрано з дефолтного spec-файлу PyInstaller (`pyi-makespec main.py`),
# який сам по собі нічого не знав про цей проект. Тут виправлено 5
# конкретних проблем, які або зламали б зібраний .exe, або просто
# псували б вигляд/зручність:
#
# 1. console=True -> при запуску поруч із GUI відкривалося б чорне
#    консольне вікно. Додатку воно не потрібне (весь вивід -- у
#    status_var/messagebox, не в stdout).
# 2. hiddenimports=[] -> pymavlink і pyserial підвантажують частину
#    своїх модулів динамічно (через importlib за рядком, не звичайним
#    import), а PyInstaller бачить лише "статичні" import-и під час
#    аналізу байткоду. Без явного hiddenimports застосунок збереться
#    без помилок, але впаде з ModuleNotFoundError в момент реального
#    підключення до ArduPilot -- тобто саме тоді, коли це найгірше
#    помітити.
# 3. datas=[] -> logo.png/icon.png (лого в шапці, іконка, сплеш) не
#    потрапляли б у збірку -- програма запускалась б, але без картинок.
# 4. Немає icon= в EXE -> .exe мав би дефолтну іконку Python замість
#    свого лого в провіднику/тасктрею.
# 5. Відносні шляхи без SPECPATH -> збірка ламалась би, якщо
#    `pyinstaller main.spec` запускати не з кореня проекту.
#
# Залишено БЕЗ змін навмисно:
# - onedir-збірка (COLLECT), а не --onefile. Застосунок читає/пише
#   settings.json, папку srtm/ і кеш карти ЗА ВІДНОСНИМ шляхом поруч із
#   собою (сам файл на флешці E:\Mission analyzer). --onefile
#   розпаковував би все у тимчасову TEMP-папку при кожному запуску --
#   і всі ці відносні шляхи почали б вести в нікуди.

import os
import sys

block_cipher = None

# SPECPATH -- спеціальна змінна, яку PyInstaller підставляє сам:
# абсолютний шлях до папки, де лежить цей .spec-файл. Використовуємо
# її замість "." / відносних шляхів, щоб збірка не залежала від того,
# з якої директорії реально запущено `pyinstaller main.spec`.
PROJECT_DIR = SPECPATH

# Ім'я і папки, і .exe беремо з meta.VERSION, а не прописуємо руками --
# щоб при кожному релізі досить було оновити VERSION в meta.py (як і
# так робиться для changelog на сторінці "Довідка"), а не лізти в
# main.spec. "1.0.0" -> "MissionAnalyzerV100".
sys.path.insert(0, PROJECT_DIR)
import meta
APP_NAME = f"MissionAnalyzerV{meta.VERSION.replace('.', '')}"

# Лого/іконка можуть називатись по-різному (icon.png, logo.png,
# icon.ico) -- як і в самому App.ICON_CANDIDATES у app.py. Додаємо в
# збірку все, що реально знайшлося, замість того щоб жорстко
# прописувати одну назву і впасти, якщо файл називається інакше.
_ASSET_CANDIDATES = ("icon.png", "logo.png", "icon.ico", "logo.ico", "aircraft_profiles_seed.json")
datas = [
    (os.path.join(PROJECT_DIR, name), ".")
    for name in _ASSET_CANDIDATES
    if os.path.isfile(os.path.join(PROJECT_DIR, name))
]

# .ico саме для іконки самого EXE (Windows не вміє ставити .png як
# іконку виконуваного файлу) -- беремо перший знайдений .ico, якщо є.
_exe_icon = None
for _name in ("icon.ico", "logo.ico"):
    _candidate = os.path.join(PROJECT_DIR, _name)
    if os.path.isfile(_candidate):
        _exe_icon = _candidate
        break
# Якщо .ico немає -- це ОК, PyInstaller просто підставить свою іконку
# за замовчуванням. Щоб з'явилась своя -- достатньо покласти icon.ico
# поруч з main.py і перезібрати (конвертувати з наявного icon.png
# можна, наприклад, на https://icoconvert.com або через Pillow:
# `Image.open("icon.png").save("icon.ico", sizes=[(256,256)])`).

a = Analysis(
    ['main.py'],
    pathex=[PROJECT_DIR],
    binaries=[],
    datas=datas,
    hiddenimports=[
        # pymavlink динамічно імпортує конкретний діалект MAVLink за
        # рядковим ім'ям всередині mavutil.mavlink_connection() --
        # PyInstaller це статично не бачить. ardupilotmega -- діалект,
        # який реально використовує ArduPilot/Mission Planner.
        'pymavlink.dialects.v20.ardupilotmega',
        'pymavlink.dialects.v10.ardupilotmega',
        # pyserial: список портів на Windows підвантажує платформо-
        # залежний підмодуль так само динамічно.
        'serial',
        'serial.tools.list_ports',
        'serial.tools.list_ports_common',
        'serial.tools.list_ports_windows',
        # Pillow опційний (є try/except ImportError в коді) -- якщо
        # він встановлений у оточенні збірки, хочемо, щоб і ImageTk
        # гарантовано потрапив у збірку.
        'PIL._tkinter_finder',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # matplotlib і хвостові naukoi-бібліотеки (numpy тощо, якщо є) --
    # програма їх не використовує (графіки малюються напряму на
    # tkinter.Canvas, див. elevation_view.py/angle_view.py/landing_view.py),
    # але вони можуть тягнутися транзитивно через якийсь встановлений
    # у системі пакет (напр. pymavlink має службові скрипти з matplotlib).
    # Явно виключаємо: PyInstaller навіть не почне аналізувати їхній
    # величезний граф залежностей (де траплялась RecursionError у
    # hook-matplotlib.py), і .exe вийде помітно легшим.
    excludes=['matplotlib', 'numpy', 'scipy', 'pandas'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX стискає .exe/.dll, зменшуючи розмір збірки. Якщо антивірус
    # (Windows Defender тощо) почне помилково лаятись на готовий .exe
    # -- перше, що варто спробувати, це upx=False тут і нижче в
    # COLLECT: UPX-стиснуті бінарники часто дають false positive,
    # особливо на непідписаних .exe.
    upx=True,
    console=False,  # без службового чорного вікна поруч з GUI
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_exe_icon,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=APP_NAME,
)
