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
#
# !!! ПЕРЕД КОЖНОЮ ПЕРЕЗБІРКОЮ, ЯКЩО ЗМІНЕНО datas/_ASSET_CANDIDATES
# ВИЩЕ (додано новий файл на кшталт route_types_seed.json) АБО
# hiddenimports НИЖЧЕ -- ВИДАЛИ теки build/ і dist/ ПОВНІСТЮ ПЕРЕД
# `pyinstaller main.spec`. ВИЯВЛЕНО НА ПРАКТИЦІ: PyInstaller кешує
# проміжні результати аналізу в build/, і без очищення новий файл у
# datas може НЕ потрапити в готову збірку навіть попри те, що сам
# .spec змінено правильно -- виглядає так, ніби seed-файл узагалі не
# спрацював, хоча насправді код коректний, просто стара збірка не
# перезаписалась повністю. Якщо змінено ЛИШЕ код (main.py чи інші
# .py-файли проєкту, без змін datas/hiddenimports) -- очищення НЕ
# обов'язкове, PyInstaller коректно підхоплює зміни самого коду й без
# цього.

import os
import shutil
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
_ASSET_CANDIDATES = ("icon.png", "logo.png", "icon.ico", "logo.ico", "aircraft_profiles_seed.json", "route_types_seed.json")
datas = [
    (os.path.join(PROJECT_DIR, name), ".")
    for name in _ASSET_CANDIDATES
    if os.path.isfile(os.path.join(PROJECT_DIR, name))
]

# imageio-ffmpeg (експорт відео MP4 у симуляції, video_export.py) несе
# ffmpeg.exe як ДАНІ пакета (imageio_ffmpeg/binaries/), а не як модуль --
# без цього рядка відео-експорт у .exe мовчки не знайде ffmpeg. Пакет
# опційний: немає його в оточенні збірки -- просто пропускаємо.
try:
    from PyInstaller.utils.hooks import collect_data_files
    datas += collect_data_files("imageio_ffmpeg")
except Exception:
    pass

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
        # pygame-ce (встановлюється як "pygame-ce", імпортується як
        # звичайний "import pygame" -- оригінальний pygame не годиться,
        # див. joystick_input.py) -- ЗА ПРЯМОЮ ВКАЗІВКОЮ користувача
        # (керування джойстиком/пультом RadioMaster Boxer у симуляції),
        # той самий опційний патерн try/except ImportError, що й Pillow
        # (joystick_input.py НЕ падає, якщо pygame відсутній -- просто
        # немає керування джойстиком). pygame зазвичай постачається з
        # власними PyInstaller-хуками (виявляє основні C-розширення
        # сам), але явно додаємо підмодуль джойстика -- саме він нам
        # реально потрібен, і саме його PyInstaller частіше пропускає
        # в мінімальних білдах.
        'pygame.joystick',
        # imageio-ffmpeg імпортується ЛІНИВО всередині функції
        # (video_export.find_ffmpeg) -- явно, щоб точно потрапив.
        'imageio_ffmpeg',
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
    # numpy БІЛЬШЕ НЕ у списку виключень -- ВИЯВЛЕНО НА ПРАКТИЦІ: сам
    # pygame (тепер потрібен для джойстика/пульта в симуляції) імпортує
    # numpy вже у власному __init__.py, навіть якщо використовується
    # лише pygame.joystick -- виключення numpy зламало б запуск .exe
    # рантайм-помилкою імпорту. matplotlib/scipy/pandas й далі не
    # використовуються ніде в проєкті -- залишаються виключеними.
    excludes=['matplotlib', 'scipy', 'pandas'],
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

# ======================================================================
# ЗОВНІШНІ ТЕКИ ПОРУЧ З .exe (симулятор, v1.1.1)
#
# Програма шукає ці ресурси НЕ в _internal (sys._MEIPASS), а поруч із
# самим .exe (os.path.dirname(sys.executable) -- scene_library.base_dir(),
# limit_sound.sound_dir(), simulation_page._sitl_cwd, video_export):
# користувач сам докладає туди свої моделі/звуки, тому через datas їх
# класти НЕ МОЖНА -- у _internal програма їх просто не побачить.
# Тому після COLLECT копіюємо їх у dist\<APP_NAME>\ самі -- замість
# ручного копіювання перед архівацією (BUILD.md, кроки 2.3/2.4/4.1).
#
# (джерело в проєкті, ім'я поруч з .exe). Немає теки -- пропускаємо з
# повідомленням у консолі збірки, збірка не падає.
#   base_maps -> map_cache: результат download_base_maps.py. Робочий
#   map_cache\ НЕ копіюємо: там увесь ваш накопичений кеш (гігабайти й
#   райони ваших польотів) -- у публічний реліз йому не можна.
# ======================================================================
EXTERNAL_DIRS = [
    ("models3d", "models3d"),      # 3D-моделі (.glb/.gltf/.obj) -- scene_library
    ("sounds", "sounds"),          # stall/terrain/explosion .mp3/.wav -- limit_sound, alert_sound
    ("DATA", "DATA"),              # навчальна місія, Lua-скрипти
    ("ffmpeg", "ffmpeg"),          # свій ffmpeg.exe, якщо є (інакше -- з imageio-ffmpeg)
    ("base_maps", "map_cache"),    # базові тайли zoom=8
]

# SITL (ArduPlane.elf + xx1.json / xx1.parm / scripts) -- ВИМКНЕНО за
# замовчуванням: це параметри й модель КОНКРЕТНОГО апарата. Вмикати
# свідомо, лише якщо їх справді можна публікувати.
SHIP_SITL = False
if SHIP_SITL:
    EXTERNAL_DIRS.append(("SITL", "SITL"))

# Сміття, яке не повинно потрапити в реліз: логи, бекапи, eeprom SITL
# (параметри, накопичені при ваших запусках) тощо.
_EXTERNAL_IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.log", "*.tlog", "*.bak", "*.tmp",
    "logs", "eeprom.bin", "Thumbs.db", "desktop.ini",
)

_dist_app_dir = os.path.join(DISTPATH, APP_NAME)
print("\n[main.spec] Зовнішні теки поруч з .exe:")
for _src_name, _dst_name in EXTERNAL_DIRS:
    _src = os.path.join(PROJECT_DIR, _src_name)
    if not os.path.isdir(_src):
        print(f"  -- пропущено, теки немає: {_src_name}\\")
        continue
    _dst = os.path.join(_dist_app_dir, _dst_name)
    shutil.copytree(_src, _dst, ignore=_EXTERNAL_IGNORE, dirs_exist_ok=True)
    _n = sum(len(_f) for _, _, _f in os.walk(_dst))
    print(f"  OK  {_src_name}\\ -> {_dst_name}\\  ({_n} файлів)")
print()
