"""
route_types.py -- типи маршруту: висотні вимоги залежно від того, чия
територія під маршрутом (за фактичним контролем -- occupied_layer.py,
deepstatemap.live, НЕ державний кордон).

ВАЖЛИВО: цей модуль зараз дає ЛИШЕ ВИХІДНІ ДАНІ для майбутньої
оптимізації висоти -- сам алгоритм (визначення "де я" через
point-in-polygon, плавний перехід висоти заздалегідь перед перетином
лінії, з урахуванням climb_rate_max_ms/sink_rate_min_ms з профілю
літака) ще НЕ реалізований. Це свідомий поділ праці: спершу модель і
конфігурація, потім сам алгоритм -- окремим кроком.

Архітектура -- точна копія aircraft_profiles.py (та сама причина:
множина типів, один "поточний", окремий JSON-файл поруч із .exe),
навмисно для однорідності, а не тому що це якось особливо підходить
саме сюди.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field


# Поточна версія СХЕМИ типу маршруту (не версія конкретного маршруту).
# Той самий принцип, що в aircraft_profiles.py -- дозволяє в
# майбутньому мігрувати старі записи, коли з'являться нові поля
# (напр. параметри самого алгоритму переходу, коли до нього дійде
# черга).
ROUTE_TYPE_SCHEMA_VERSION_MAJOR = 1
ROUTE_TYPE_SCHEMA_VERSION_MINOR = 0


@dataclass
class RouteType:
    """Один тип маршруту -- висотні вимоги залежно від контролю
    території під конкретною точкою (за фактичною лінією
    розмежування, не державним кордоном -- джерело даних той самий
    шар, що вже є для карти, occupied_layer.py/deepstatemap.live).

    altitude_border_crossing_m -- ОКРЕМЕ, третє значення (не просто
    одне з двох інших чи їхнє середнє): висота, на якій має відбутись
    сам момент перетину лінії. Манёвр набору/зниження до потрібної
    (occupied/controlled) висоти починається ЗАЗДАЛЕГІДЬ, ще ДО
    перетину -- так, щоб на самій лінії літак уже був саме на цій,
    третій, висоті переходу (не десь посеред набору/зниження)."""
    version_major: int = ROUTE_TYPE_SCHEMA_VERSION_MAJOR
    version_minor: int = ROUTE_TYPE_SCHEMA_VERSION_MINOR
    name: str = ""
    altitude_controlled_m: float = 0.0       # відносна висота над СВОЄЮ (контрольованою) територією
    altitude_occupied_m: float = 0.0         # відносна висота над ОКУПОВАНОЮ територією
    altitude_border_crossing_m: float = 0.0  # висота САМЕ в момент перетину лінії розмежування
    border_maneuver_angle_max_deg: float = 2.0  # ЛІМІТ кута набору/зниження САМЕ для маневру
    # переходу кордону (окремий від загального angle_max у Конфігурації -- ЦЕЙ параметр
    # стосується виключно геометрії конкретного маневру переходу, може відрізнятись для
    # різних типів маршруту/виробів).
    max_waypoints: int = 255  # бюджет точок для ІТЕРАТИВНОЇ оптимізації висоти (не для обходу
    # заборонних зон -- окремий, незалежний бюджет саме для інерційності/облітання рельєфу).
    # Реальний ліміт MAX_MISSION_WAYPOINTS (route_optimizer.py) лишається кінцевим обмеженням
    # усієї місії -- це поле дозволяє свідомо зарезервувати частину бюджету саме під висоту.
    population_radii_km: list[tuple[float, float]] = field(default_factory=lambda: [(1.0, 1.0)])
    # РАДІУС ОБЛЬОТУ населеного пункту, ЗАЛЕЖНО ВІД ЙОГО НАСЕЛЕННЯ -- ЗА
    # ПРЯМОЮ ВКАЗІВКОЮ користувача: раніше був ОДИН радіус для всіх НП
    # одразу (сама координатна оптимізація навіть мала коментар "поки
    # що" на цьому місці) -- тепер список пар (max_population,
    # radius_km), ВІДСОРТОВАНИЙ за зростанням population. Для
    # конкретного НП береться радіус ПЕРШОЇ пари, де його population не
    # перевищує max_population; якщо населення перевищує УСІ пороги (чи
    # взагалі невідоме) -- береться радіус ОСТАННЬОЇ (найбільшої) пари.
    # За замовчуванням -- один запис (1.0, 1.0), що відтворює СТАРУ
    # поведінку (однаковий радіус 1км для всіх) для вже наявних
    # користувачів, які ще не налаштували градацію свідомо.

    def format_version(self) -> str:
        return f"{self.version_major:02d}.{self.version_minor:02d}"

    def radius_for_population(self, population: float | None) -> float:
        """Радіус обльоту (км) для КОНКРЕТНОГО населеного пункту,
        залежно від його населення -- перша пара (за зростанням
        population), де population НП не перевищує max_population;
        якщо перевищує всі пороги чи population невідоме (None) --
        радіус ОСТАННЬОЇ (найбільшої) пари."""
        sorted_radii = sorted(self.population_radii_km, key=lambda t: t[0])
        if not sorted_radii:
            return 1.0  # запасне значення, якщо список порожній (не мало б статись, але не падаємо)
        if population is None:
            return sorted_radii[-1][1]
        for max_pop, radius in sorted_radii:
            if population <= max_pop:
                return radius
        return sorted_radii[-1][1]

    def to_dict(self) -> dict:
        return {
            "version_major": self.version_major, "version_minor": self.version_minor,
            "name": self.name,
            "altitude_controlled_m": self.altitude_controlled_m,
            "altitude_occupied_m": self.altitude_occupied_m,
            "altitude_border_crossing_m": self.altitude_border_crossing_m,
            "max_waypoints": self.max_waypoints,
            "border_maneuver_angle_max_deg": self.border_maneuver_angle_max_deg,
            "population_radii_km": [[p, r] for p, r in self.population_radii_km],
        }

    @staticmethod
    def from_dict(d: dict) -> "RouteType":
        known = {f: d[f] for f in RouteType.__dataclass_fields__ if f in d}
        return RouteType(**known)


@dataclass
class RouteTypeStore:
    """Увесь набір типів маршруту + який з них ПОТОЧНИЙ (за іменем) --
    та сама термінологія, що й у aircraft_profiles.py: не постійна
    незмінна позначка "типовий", а те, з чим користувач працює зараз і
    може будь-коли перемкнути на інший."""
    route_types: list[RouteType] = field(default_factory=list)
    current_name: str | None = None

    def get_current(self) -> RouteType | None:
        if self.current_name is None:
            return None
        for rt in self.route_types:
            if rt.name == self.current_name:
                return rt
        return None

    def get_by_name(self, name: str) -> RouteType | None:
        for rt in self.route_types:
            if rt.name == name:
                return rt
        return None

    def upsert(self, route_type: RouteType) -> None:
        for i, rt in enumerate(self.route_types):
            if rt.name == route_type.name:
                self.route_types[i] = route_type
                return
        self.route_types.append(route_type)

    def remove(self, name: str) -> None:
        self.route_types = [rt for rt in self.route_types if rt.name != name]
        if self.current_name == name:
            self.current_name = None

    def to_dict(self) -> dict:
        return {
            "route_types": [rt.to_dict() for rt in self.route_types],
            "current_name": self.current_name,
        }

    @staticmethod
    def from_dict(d: dict) -> "RouteTypeStore":
        route_types = [RouteType.from_dict(rt) for rt in d.get("route_types", [])]
        return RouteTypeStore(route_types=route_types, current_name=d.get("current_name"))


def default_route_types_path() -> str:
    """Файл поруч з .exe/route_types.py -- та сама логіка, що
    aircraft_profiles.default_profiles_path(), незалежно продубльована
    (не імпортуємо з app.py), щоб уникнути циклічного імпорту."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "route_types.json")


def load_route_types(path: str) -> RouteTypeStore:
    """Пошкоджений/відсутній файл -- тихо порожній store (не падає),
    перший запуск програми не має ламати весь застосунок."""
    if not os.path.exists(path):
        return RouteTypeStore()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return RouteTypeStore.from_dict(json.load(f))
    except (json.JSONDecodeError, OSError, TypeError, KeyError):
        return RouteTypeStore()


def save_route_types(path: str, store: RouteTypeStore) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(store.to_dict(), f, ensure_ascii=False, indent=2)
