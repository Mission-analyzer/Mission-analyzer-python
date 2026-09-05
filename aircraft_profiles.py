"""
aircraft_profiles.py -- профілі літального апарата: постійні льотні
характеристики, які не міняються від місії до місії (на відміну від
ємності бака -- та може відрізнятись навіть для одного й того самого
літака залежно від конкретного вильоту, тому свідомо НЕ входить у
профіль, задається окремо щоразу в "Аналіз -> Оптимізація").

Мета -- один раз заповнити профіль у "Конфігурація", далі "Аналіз"
(зараз -- Оптимізація: паливо потребує швидкість, перевірка радіуса
повороту потребує швидкість+крен) бере ці дані з ПОТОЧНОГО профілю
(який можна будь-коли перемкнути на інший), а не питає користувача
вручну щоразу.

Зберігається як JSON поруч з settings.json (той самий _app_base_dir()),
УСІ профілі в одному файлі -- парк може містити кілька літаків.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, asdict, field


# --- Тип дрона: варіанти випадаючого списку в UI ---
# СИНХРОНІЗОВАНО з чотирма основними типами прошивки ArduPilot, які
# показує Mission Planner (Install Firmware): Plane, Copter, Rover, Sub.
# Той самий перелік, що вже використовується для розпізнавання
# підключеного борту (MAV_TYPE у ardupilot_link.py) -- тут просто інший
# контекст використання (профіль ЛА для розрахунків в "Аналізі", не
# живе підключення), тому й спеціально узгоджено, а не вигадано щось
# своє. Antenna Tracker/Blimp НЕ включені -- вони не "літальний апарат"
# у сенсі, релевантному для профілю (місію не літають).
#
# v1.01: поля профілю (зокрема roll_limit_deg/pitch_limit_*_deg) наразі
# сфокусовані на ЛІТАКОВОМУ типі -- саме для нього ці обмеження кутів
# реально застосовні й використовуються в "Аналіз -> Оптимізація"
# (перевірка радіуса повороту). "copter"/"rover"/"sub" лишені в списку
# як заготовка -- повноцінний набір релевантних полів для них (інша
# фізика руху, інші обмеження) -- це наступні версії схеми, не зараз.
DRONE_TYPES = ["plane", "copter", "rover", "sub"]

# --- Тип двигуна: варіанти випадаючого списку в UI ---
# Свідомо ЛИШЕ ДВЗ/турбіна на цьому кроці -- електричні БПЛА рахують
# витрату геть інакше (Вт·год/A, не літри/год), і резерв ICAO 5% (від
# літрів палива) до батарей напряму не застосовний. Повноцінна
# електрична підтримка -- окремий крок пізніше, не зараз.
ENGINE_TYPES = ["ice", "turbine"]

# --- Які поля профілю релевантні для якого типу ЛА ---
# Архітектура закладається ЗАРАЗ, хоча реально заповнений лише "plane" -- інші
# типи (коптер тощо) матимуть СВІЙ набір полів, коли до них дійде
# черга (інша фізика, інші обмеження). Порожній список = тип ще не
# підтримується повним профілем -- UI показує "поля ще не реалізовані"
# замість того, щоб мовчки показувати нерелевантні авіаційні поля.
PROFILE_FIELDS_BY_TYPE: dict[str, list[str]] = {
    "plane": [
        "cruise_consumption_lph",
        "airspeed_min_ms", "airspeed_cruise_ms", "airspeed_max_ms",
        "roll_limit_deg", "pitch_limit_max_deg", "pitch_limit_min_deg",
    ],
    "copter": [],
    "rover": [],
    "sub": [],
}

# Метадані кожного поля -- (ключ_підпису_i18n, ключ_одиниці_i18n).
# Свідомо лежить тут (не в config_page.py) -- це визначення СХЕМИ
# профілю, той самий модуль, що й PROFILE_FIELDS_BY_TYPE; значення --
# ключі i18n (не готовий текст), сам aircraft_profiles.py НЕ залежить
# від tkinter/i18n напряму (як і решта модуля).
PROFILE_FIELD_META: dict[str, tuple[str, str]] = {
    "cruise_consumption_lph": ("lbl_cruise_consumption", "lbl_liters_per_hour"),
    "airspeed_min_ms": ("lbl_airspeed_min", "lbl_ms"),
    "airspeed_cruise_ms": ("lbl_cruise_speed", "lbl_ms"),
    "airspeed_max_ms": ("lbl_airspeed_max", "lbl_ms"),
    "roll_limit_deg": ("lbl_roll_limit", "lbl_deg"),
    "pitch_limit_max_deg": ("lbl_pitch_limit_max", "lbl_deg"),
    "pitch_limit_min_deg": ("lbl_pitch_limit_min", "lbl_deg"),
}

# Поточна версія СХЕМИ профілю (не версія конкретного літака!) --
# записується в кожен профіль при створенні/збереженні. Схема явно
# розвиватиметься (електричні двигуни, нові поля тощо) -- version_major/
# version_minor дають майбутньому коду спосіб зрозуміти, за якою версією
# структури збережено конкретний профіль, і за потреби мігрувати старі
# записи, а не тихо ламатись чи губити дані на невідомих полях.
PROFILE_SCHEMA_VERSION_MAJOR = 1
PROFILE_SCHEMA_VERSION_MINOR = 1


@dataclass
class AircraftProfile:
    """Один профіль літака. tank_capacity_l СВІДОМО відсутній -- див.
    docstring модуля.

    version_major/version_minor -- версія СХЕМИ цього запису (двозначні
    числа, формат відображення "XX.XX" -- див. format_version()), НЕ
    номер версії самого літака. Профілі, збережені старішим кодом БЕЗ
    цих полів, автоматично отримують ПОТОЧНІ значення за замовчуванням
    при завантаженні (from_dict бере лише відомі ключі) -- трактуються
    як версія 1.0, що коректно відображає реальність (версії до
    впровадження цього поля версіонування взагалі не існувало)."""
    name: str
    version_major: int = PROFILE_SCHEMA_VERSION_MAJOR
    version_minor: int = PROFILE_SCHEMA_VERSION_MINOR
    drone_type: str = "plane"       # один з DRONE_TYPES
    engine_type: str = "ice"        # один з ENGINE_TYPES
    cruise_consumption_lph: float = 0.0   # витрата на крейсерській, л/год
    airspeed_min_ms: float = 0.0
    airspeed_cruise_ms: float = 0.0
    airspeed_max_ms: float = 0.0
    roll_limit_deg: float = 0.0     # максимальний крен (як ROLL_LIMIT_DEG)
    pitch_limit_max_deg: float = 0.0  # максимальний кут набору (PTCH_LIM_MAX_DEG)
    pitch_limit_min_deg: float = 0.0  # максимальний кут зниження (PTCH_LIM_MIN_DEG, зазвичай від'ємний)

    def format_version(self) -> str:
        """"XX.XX" -- двозначні major/minor, як просив користувач
        (напр. version_major=1, version_minor=3 -> "01.03")."""
        return f"{self.version_major:02d}.{self.version_minor:02d}"

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "AircraftProfile":
        # ігноруємо невідомі ключі (напр. зі старішої/новішої версії
        # файлу) -- не падаємо на зайвому полі, беремо тільки відомі
        known = {f: d[f] for f in AircraftProfile.__dataclass_fields__ if f in d}
        return AircraftProfile(**known)


@dataclass
class AircraftProfileStore:
    """Увесь набір профілів + який з них ПОТОЧНИЙ (за іменем) -- не
    постійна незмінна позначка "типовий", а те, з чим користувач працює
    зараз і може будь-коли перемкнути на інший профіль."""
    profiles: list[AircraftProfile] = field(default_factory=list)
    current_name: str | None = None

    def get_current(self) -> AircraftProfile | None:
        if self.current_name is None:
            return None
        for p in self.profiles:
            if p.name == self.current_name:
                return p
        return None

    def get_by_name(self, name: str) -> AircraftProfile | None:
        for p in self.profiles:
            if p.name == name:
                return p
        return None

    def upsert(self, profile: AircraftProfile) -> None:
        """Додає новий профіль, або ЗАМІНЮЄ існуючий з тим самим ім'ям."""
        for i, p in enumerate(self.profiles):
            if p.name == profile.name:
                self.profiles[i] = profile
                return
        self.profiles.append(profile)

    def remove(self, name: str) -> None:
        self.profiles = [p for p in self.profiles if p.name != name]
        if self.current_name == name:
            self.current_name = None

    def to_dict(self) -> dict:
        return {
            "profiles": [p.to_dict() for p in self.profiles],
            "current_name": self.current_name,
        }

    @staticmethod
    def from_dict(d: dict) -> "AircraftProfileStore":
        profiles = [AircraftProfile.from_dict(p) for p in d.get("profiles", [])]
        return AircraftProfileStore(profiles=profiles, current_name=d.get("current_name"))


def default_profiles_path() -> str:
    """Шлях до aircraft_profiles.json поруч з .exe/app.py -- та сама
    логіка, що app.py._app_base_dir() для settings.json, продубльована
    тут НЕЗАЛЕЖНО (не імпортуємо з app.py), щоб уникнути циклічного
    імпорту (app.py підмішує ConfigPageMixin, який використовує цей
    модуль)."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "aircraft_profiles.json")


def load_profiles(path: str) -> AircraftProfileStore:
    """Читає профілі з JSON-файлу. Якщо файл відсутній чи пошкоджений --
    повертає ПОРОЖНІЙ store (не піднімає виняток) -- перший запуск
    програми, чи файл випадково затерто, не мають ламати весь застосунок,
    просто користувачу доведеться заповнити профіль заново."""
    if not os.path.exists(path):
        return AircraftProfileStore()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return AircraftProfileStore.from_dict(json.load(f))
    except (json.JSONDecodeError, OSError, TypeError, KeyError):
        return AircraftProfileStore()


def save_profiles(path: str, store: AircraftProfileStore) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(store.to_dict(), f, ensure_ascii=False, indent=2)
