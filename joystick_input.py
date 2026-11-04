"""
joystick_input.py -- чтение USB HID-джойстика (пульт RadioMaster Boxer
в режиме USB Joystick, и любой другой RC-передатчик в HID-режиме)
через pygame.

ЗАВИСИМОСТЬ: pip-пакет называется "pygame-ce" (Community Edition), а
НЕ "pygame" -- ВИЯВЛЕНО НА ПРАКТИЦІ: оригінальний pygame не оновлювався
з вересня 2024 і офіційно підтримує лише до Python 3.12, на новіших
версіях (3.13+) `pip install pygame` намагається збирати з вихідного
коду й падає (стара система збірки посилається на distutils, вилучений
з stdlib). pygame-ce -- активно підтримуваний форк тими самими
розробниками, з готовими wheel під нові версії Python. Пакет
встановлюється як "pygame-ce", але імпортується як звичайний
"import pygame" -- КОД тут не потребує жодних змін, лише requirements/
інструкція встановлення. ЗА ПРЯМОЮ УКАЗКОЮ пользователя ("у нас уже
миллион библиотек, подключим ещё одну"), сознательное отступление от
принципа "только стандартная библиотека", который держался всюду
остальном в проекте. main.spec теж потребує hiddenimports під
'pygame.joystick' (уже додано).

ПОЧЕМУ КАЛИБРОВКА, А НЕ ФИКСИРОВАННОЕ СООТВЕТСТВИЕ ОСЕЙ: то, какая
физическая ось джойстика (индекс в pygame) соответствует какому
каналу (крен/тангаж/газ/рысканье), зависит от конкретной прошивки
(EdgeTX), настроек порядка каналов в самом радио, и ОС. Жёстко
зашитое соответствие "ось 0 = крен" работало бы только для одной
конкретной комбинации настроек -- вместо этого даём пользователю
подвигать каждый стик по очереди, и сами определяем, какая ось
реагирует сильнее всего (стандартный подход в игровых калибровках).
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
import threading
import queue
import time

try:
    import pygame
    HAS_PYGAME = True
except ImportError:
    HAS_PYGAME = False


DEADZONE = 0.05  # ЗА ПРЯМОЮ ВКАЗІВКОЮ (типова практика для RC-осей) -- малий шум/дрейф стіка в нейтралі не повинен читатись як відхилення


@dataclasses.dataclass
class JoystickMapping:
    """Яка вісь/канал джойстика відповідає за що. Індекси -- НЕ
    гарантовано ті самі на різних пультах/ОС, тому заповнюються через
    калібрування (calibrate_axis), не жорстко заданими за замовчуванням."""
    roll_axis: int | None = None
    pitch_axis: int | None = None
    throttle_axis: int | None = None
    yaw_axis: int | None = None
    mode_axis: int | None = None  # 3-позиційний перемикач режиму (MANUAL/FBWA/AUTO), якщо доступний як вісь
    arm_axis: int | None = None  # ARM/DISARM перемикач (2-позиційний) -- ЗА ПРЯМОЮ ВКАЗІВКОЮ користувача: перший лівий перемикач на пульті

    roll_invert: bool = False
    pitch_invert: bool = False
    throttle_invert: bool = False
    yaw_invert: bool = False

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "JoystickMapping":
        known = {f: d[f] for f in JoystickMapping.__dataclass_fields__ if f in d}
        return JoystickMapping(**known)


class JoystickManager:
    """Обгортка над pygame.joystick -- єдина точка входу для решти
    програми, щоб інші модулі не залежали від pygame напряму.

    ВИЯВЛЕНО НА ПРАКТИЦІ (справжня, серйозна аварія застосунку):
    "Fatal Python error: PyEval_RestoreThread... GIL is released" --
    коли pygame.event.pump()/get_axis() викликаються НАПРЯМУ з
    колбека Tkinter (through self.after(...), тобто вкладено всередині
    Tkinter mainloop), на Windows це може пошкодити стан потоку
    Python. Це НЕ специфічно для pygame чи для Python 3.14 -- та сама
    категорія аварії відома для OpenCV, CEF, Panda3D та інших
    нативних/SDL-бібліотек, викликаних із колбеків Tkinter (завжди на
    Windows, завжди через нативне вікно повідомлень).

    Рішення: ЖОДЕН виклик pygame більше НЕ відбувається з головного
    (Tkinter) потоку. Увесь pygame живе у ВЛАСНОМУ, повністю
    ізольованому фоновому потоці з циклом опитування (init, connect,
    list, poll -- усе всередині цього потоку). Головний потік лише:
    (а) кладе команди в чергу (self._commands) і чекає на Event
    результату для одноразових дій (list/connect/disconnect), (б)
    читає ВЖЕ ГОТОВИЙ, кешований стан осей/кнопок (self._state) для
    регулярного опитування (raw_axis/mapped_value) -- це просто читання
    списку, без жодного виклику pygame."""

    def __init__(self):
        self._commands: "queue.Queue" = queue.Queue()
        self._state_lock = threading.Lock()
        self._axes: list[float] = []
        self._buttons: list[bool] = []
        self._connected = False
        self._thread = None
        if HAS_PYGAME:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self):
        """Єдиний потік, що коли-небудь торкається pygame. НАВМИСНО
        не pygame.init() (ініціалізує ВСІ підсистеми, включно зі
        звуком -- зайві ALSA-попередження на машинах без налаштованого
        звуку). display потрібен для роботи event-помпи в деяких
        версіях SDL, joystick -- для самого читання пульта."""
        try:
            pygame.display.init()
            pygame.joystick.init()
        except Exception:
            return  # без init -- потік просто завершується, is_connected лишиться False назавжди

        joystick = None
        POLL_INTERVAL_S = 0.02  # 50Hz -- з запасом частіше за потреби Tkinter-стороні (30мс/33Hz)

        while True:
            # спершу обробляємо ВСІ команди, що накопичились у черзі
            # (list/connect/disconnect) -- одноразові дії, ставимо
            # результат і сигналимо готовність через Event
            try:
                while True:
                    cmd, payload, done_event, result_box = self._commands.get_nowait()
                    try:
                        if cmd == "list":
                            pygame.joystick.quit()
                            pygame.joystick.init()
                            count = pygame.joystick.get_count()
                            items = []
                            for i in range(count):
                                js = pygame.joystick.Joystick(i)
                                try:
                                    guid = js.get_guid()
                                except Exception:
                                    guid = ""
                                items.append((js.get_name(), guid))
                            result_box.append(items)
                        elif cmd == "connect":
                            try:
                                joystick = pygame.joystick.Joystick(payload)
                                joystick.init()
                                result_box.append(True)
                            except Exception:
                                joystick = None
                                result_box.append(False)
                        elif cmd == "disconnect":
                            if joystick is not None:
                                try:
                                    joystick.quit()
                                except Exception:
                                    pass
                            joystick = None
                            result_box.append(None)
                    except Exception:
                        result_box.append(None)
                    finally:
                        done_event.set()
            except queue.Empty:
                pass

            # регулярне опитування -- те, що раніше й спричиняло аварію,
            # викликане напряму з Tkinter-колбека. Тепер це живе тут,
            # у власному циклі, ізольовано від Tkinter geть.
            try:
                pygame.event.pump()
                if joystick is not None:
                    axes = [joystick.get_axis(i) for i in range(joystick.get_numaxes())]
                    buttons = [bool(joystick.get_button(i)) for i in range(joystick.get_numbuttons())]
                    with self._state_lock:
                        self._axes = axes
                        self._buttons = buttons
                        self._connected = True
                else:
                    with self._state_lock:
                        self._axes = []
                        self._buttons = []
                        self._connected = False
            except Exception:
                with self._state_lock:
                    self._connected = False

            time.sleep(POLL_INTERVAL_S)

    def _send_command(self, cmd: str, payload=None, timeout: float = 2.0):
        """Кладе команду в чергу фонового потоку й ЧЕКАЄ на результат
        (блокуюче, але коротко -- одноразові дії типу list/connect, не
        регулярне опитування). Повертає None, якщо потік недоступний
        (pygame відсутній) чи час очікування вичерпано."""
        if self._thread is None:
            return None
        done_event = threading.Event()
        result_box = []
        self._commands.put((cmd, payload, done_event, result_box))
        if done_event.wait(timeout):
            return result_box[0] if result_box else None
        return None

    def list_joysticks(self) -> list[str]:
        """Імена всіх підключених джойстиків/пультів -- НЕ підключає
        жоден з них, лише перелічує для вибору користувачем.

        Ім'я -- справжнє, з самого пристрою (HID product string), а не з
        реєстру Windows: Windows запам'ятовує назву джойстика за парою
        VID/PID, а в усіх пультів EdgeTX вона однакова -- TX16S показувався
        «Boxer», якщо першим колись підключали Boxer. Порядок -- як у pygame
        (connect(index) бере той самий номер). Однакові імена отримують
        « (2)», « (3)»: калібрування зберігається за іменем."""
        result = self._send_command("list")
        if not result:
            return []
        names = resolve_device_names(result, hid_product_strings())
        seen: dict[str, int] = {}
        out = []
        for n in names:
            seen[n] = seen.get(n, 0) + 1
            out.append(n if seen[n] == 1 else f"{n} ({seen[n]})")
        return out

    def connect(self, index: int) -> bool:
        """Підключає джойстик за індексом (з list_joysticks). Повертає
        False, якщо pygame недоступний, індекс хибний, чи підключення
        не вдалось з будь-якої іншої причини -- НІКОЛИ не кидає виняток
        назовні."""
        result = self._send_command("connect", index)
        return bool(result)

    def disconnect(self):
        self._send_command("disconnect")

    @property
    def is_connected(self) -> bool:
        with self._state_lock:
            return self._connected

    def poll(self):
        """ЗАЛИШЕНО заради сумісності виклику (код-виклики
        self._sim_joystick.poll() лишаються по всьому проєкту) -- але
        тепер це НІЧОГО не робить: реальне опитування вже відбувається
        безперервно у фоновому потоці, головному потоку більше НІКОЛИ
        не треба самому смикати pygame.event.pump()."""
        pass

    def num_axes(self) -> int:
        with self._state_lock:
            return len(self._axes)

    def num_buttons(self) -> int:
        with self._state_lock:
            return len(self._buttons)

    def raw_axis(self, index: int) -> float:
        """Сире значення осі, від -1.0 до 1.0, БЕЗ інверсії/deadzone --
        для калібрування (де важлива саме сира реакція) використовуй
        цей метод; для реального керування -- mapped_value нижче. Лише
        ЧИТАННЯ вже готового кешу -- жодного виклику pygame тут."""
        with self._state_lock:
            if 0 <= index < len(self._axes):
                return self._axes[index]
            return 0.0

    def button(self, index: int) -> bool:
        with self._state_lock:
            if 0 <= index < len(self._buttons):
                return self._buttons[index]
            return False

    def detect_most_active_axis(self, baseline: list[float]) -> int | None:
        """Для калібрування: порівнює ПОТОЧНІ значення всіх осей із
        baseline (знятим у нейтральному положенні ДО того, як
        користувача попросили поворушити конкретний стик), повертає
        індекс осі з НАЙБІЛЬШОЮ зміною -- це і є вісь, яку користувач
        щойно рухав. None, якщо джойстик не підключено чи змін немає."""
        if not self.is_connected:
            return None
        n = self.num_axes()
        if n == 0 or len(baseline) != n:
            return None
        best_idx, best_delta = None, DEADZONE
        for i in range(n):
            delta = abs(self.raw_axis(i) - baseline[i])
            if delta > best_delta:
                best_delta = delta
                best_idx = i
        return best_idx

    def snapshot_axes(self) -> list[float]:
        """Поточні сирі значення УСІХ осей одразу -- для baseline при калібруванні."""
        return [self.raw_axis(i) for i in range(self.num_axes())]

    def mapped_value(self, axis_index: int | None, invert: bool) -> float:
        """Значення осі з урахуванням інверсії та deadzone -- те, що
        реально використовується для керування (на відміну від
        raw_axis, який лише для калібрування). Повертає 0.0, якщо
        axis_index -- None (канал ще не відкалібровано) чи джойстик не підключено."""
        if axis_index is None:
            return 0.0
        v = self.raw_axis(axis_index)
        if invert:
            v = -v
        if abs(v) < DEADZONE:
            return 0.0
        return max(-1.0, min(1.0, v))


# ----------------------------------------------------------------------------------------------------------------------
# Справжнє ім'я пульта: HID product string з самого пристрою (Windows, ctypes, без зовнішніх пакетів)
# ----------------------------------------------------------------------------------------------------------------------
def guid_vid_pid(guid: str) -> tuple[int, int] | None:
    """(VID, PID) з GUID джойстика SDL (pygame Joystick.get_guid()): байти 4-5 -- VID, 8-9 -- PID (little-endian).
    None -- у GUID їх немає (старий драйвер, віртуальний пристрій)."""
    try:
        b = bytes.fromhex(guid)
    except (TypeError, ValueError):
        return None
    if len(b) != 16 or b[6:8] != b"\x00\x00" or b[10:12] != b"\x00\x00":
        return None
    vid, pid = b[4] | (b[5] << 8), b[8] | (b[9] << 8)
    return (vid, pid) if vid else None


def resolve_device_names(items, hid_names: dict) -> list[str]:
    """[(ім'я pygame, GUID)] -> імена для списку: справжнє ім'я пристрою, якщо за VID/PID воно одне; інакше -- як дав
    pygame (два однакові пульти разом: не знати, котрий з них котрий)."""
    out = []
    for name, guid in items:
        vp = guid_vid_pid(guid)
        real = hid_names.get(vp, []) if vp else []
        out.append(real[0] if len(real) == 1 else name)
    return out


def hid_product_strings() -> dict:
    """{(VID, PID): [імена]} усіх HID-пристроїв -- з САМИХ пристроїв (HidD_GetProductString). Лише Windows; будь-яка
    помилка -- порожньо (тоді імена лишаються як дав pygame)."""
    if sys.platform != "win32":
        return {}
    try:
        return _hid_product_strings_win()
    except Exception:
        return {}


def _hid_product_strings_win() -> dict:
    import ctypes
    from ctypes import wintypes as wt

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", GUID), ("Flags", wt.DWORD), ("Reserved", ctypes.c_size_t)]

    class HIDD_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Size", wt.ULONG), ("VendorID", wt.USHORT), ("ProductID", wt.USHORT), ("VersionNumber", wt.USHORT)]

    hid, setup, k32 = ctypes.WinDLL("hid"), ctypes.WinDLL("setupapi"), ctypes.WinDLL("kernel32")
    hid.HidD_GetHidGuid.argtypes, hid.HidD_GetHidGuid.restype = [ctypes.POINTER(GUID)], None
    setup.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wt.LPCWSTR, wt.HWND, wt.DWORD]
    setup.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setup.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID), wt.DWORD,
                                                  ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
    setup.SetupDiEnumDeviceInterfaces.restype = wt.BOOL
    setup.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
                                                       ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    setup.SetupDiGetDeviceInterfaceDetailW.restype = wt.BOOL
    setup.SetupDiDestroyDeviceInfoList.argtypes, setup.SetupDiDestroyDeviceInfoList.restype = [ctypes.c_void_p], wt.BOOL
    k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p]
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CloseHandle.argtypes, k32.CloseHandle.restype = [ctypes.c_void_p], wt.BOOL
    hid.HidD_GetAttributes.argtypes, hid.HidD_GetAttributes.restype = [ctypes.c_void_p, ctypes.POINTER(HIDD_ATTRIBUTES)], wt.BOOLEAN
    hid.HidD_GetProductString.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.ULONG]
    hid.HidD_GetProductString.restype = wt.BOOLEAN

    invalid = ctypes.c_void_p(-1).value
    guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    hdev = setup.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x2 | 0x10)   # DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    if not hdev or hdev == invalid:
        return {}
    out: dict = {}
    try:
        i = 0
        while True:
            ifd = SP_DEVICE_INTERFACE_DATA()
            ifd.cbSize = ctypes.sizeof(ifd)
            if not setup.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(guid), i, ctypes.byref(ifd)):
                break
            i += 1
            need = wt.DWORD(0)
            setup.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(ifd), None, 0, ctypes.byref(need), None)
            if need.value < 8:
                continue
            buf = ctypes.create_string_buffer(need.value)
            ctypes.c_uint32.from_buffer(buf).value = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6   # cbSize структуры
            if not setup.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(ifd), buf, need, None, None):
                continue
            path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
            # доступ 0: лише атрибути й рядки, без читання звітів -- працює і для пристроїв, зайнятих іншими програмами
            h = k32.CreateFileW(path, 0, 0x1 | 0x2, None, 3, 0, None)
            if not h or h == invalid:
                continue
            try:
                attrs = HIDD_ATTRIBUTES()
                attrs.Size = ctypes.sizeof(attrs)
                if not hid.HidD_GetAttributes(h, ctypes.byref(attrs)):
                    continue
                sbuf = ctypes.create_unicode_buffer(256)
                if hid.HidD_GetProductString(h, sbuf, ctypes.sizeof(sbuf)):
                    name = sbuf.value.strip()
                    lst = out.setdefault((attrs.VendorID, attrs.ProductID), [])
                    if name and name not in lst:
                        lst.append(name)
            finally:
                k32.CloseHandle(h)
    finally:
        setup.SetupDiDestroyDeviceInfoList(hdev)
    return out


def default_joystick_mapping_path() -> str:
    """Шлях до joystick_mapping.json поруч з .exe/app.py -- та сама
    логіка, що aircraft_profiles.default_profiles_path(), продубльована
    тут незалежно з тієї самої причини (уникнення циклічного імпорту)."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "joystick_mapping.json")


# ----------------------------------------------------------------------------------------------------------------------
# Калібрування -- окремо для кожного пульта (за іменем пристрою) у тому ж joystick_mapping.json:
#   {"profiles": {"RadioMaster TX16S Joystick": {...}, ...}, "*": {...}}
# "*" -- загальне калібрування: файл старого формату (одне калібрування на всі пульти) стає ним і діє для пульта, у якого
# власного ще немає.
# ----------------------------------------------------------------------------------------------------------------------
COMMON_PROFILE = "*"


def load_joystick_profiles(path: str) -> dict:
    """{ім'я пульта або "*": JoystickMapping}. Файлу немає чи він пошкоджений -- порожньо."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    if "profiles" not in data:                                        # старий формат: одне калібрування на всі пульти
        try:
            return {COMMON_PROFILE: JoystickMapping.from_dict(data)}
        except (TypeError, KeyError):
            return {}
    out = {}
    for key, d in (data.get("profiles") or {}).items():
        try:
            out[key] = JoystickMapping.from_dict(d)
        except (TypeError, KeyError):
            pass
    if isinstance(data.get(COMMON_PROFILE), dict):
        try:
            out[COMMON_PROFILE] = JoystickMapping.from_dict(data[COMMON_PROFILE])
        except (TypeError, KeyError):
            pass
    return out


def mapping_for_device(profiles: dict, device: str | None) -> tuple[JoystickMapping, str]:
    """(калібрування, звідки): "own" -- власне цього пульта, "common" -- загальне, "none" -- жодного (порожнє)."""
    if device and device in profiles:
        return profiles[device], "own"
    if COMMON_PROFILE in profiles:
        return profiles[COMMON_PROFILE], "common"
    return JoystickMapping(), "none"


def load_joystick_mapping(path: str, device: str | None = None) -> JoystickMapping:
    """Калібрування для пульта (або загальне). Файлу немає чи він пошкоджений -- ПОРОЖНЄ (не відкаліброване), без
    винятку: перший запуск чи пошкоджений файл не мають ламати решту симуляції."""
    return mapping_for_device(load_joystick_profiles(path), device)[0]


def save_joystick_mapping(path: str, mapping: JoystickMapping, device: str | None = None) -> None:
    """Зберегти калібрування пульта device (None -- загальне), не чіпаючи калібрування інших пультів."""
    profiles = load_joystick_profiles(path)
    profiles[device or COMMON_PROFILE] = mapping
    data = {"profiles": {k: m.to_dict() for k, m in profiles.items() if k != COMMON_PROFILE}}
    if COMMON_PROFILE in profiles:
        data[COMMON_PROFILE] = profiles[COMMON_PROFILE].to_dict()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
