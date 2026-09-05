"""
waypoints.py — модель точки маршрута и разбор файлов .waypoints (QGC WPL 110).
Без зависимостей от GUI, только стандартная библиотека.
"""

from __future__ import annotations

from dataclasses import dataclass

# Команды MAVLink, задающие точки геометрии маршрута (у остальных, типа
# DO_SET_SERVO, DO_JUMP и т.п., координаты не относятся к траектории полёта)
NAV_COMMANDS = {
    16,  # NAV_WAYPOINT
    17,  # LOITER_UNLIM
    18,  # LOITER_TURNS
    19,  # LOITER_TIME
    20,  # RETURN_TO_LAUNCH
    21,  # LAND
    22,  # TAKEOFF
    31,  # LOITER_TO_ALT
    82,  # SPLINE_WAYPOINT
    84, 85, 86,  # VTOL nav commands
}

# Команды посадки — у них высота 0 м (касание земли) это норма, а не ошибка,
# поэтому они исключаются из проверки критической высоты
LAND_COMMANDS = {
    21,  # NAV_LAND
    85,  # NAV_VTOL_LAND
}

# DO_CHANGE_SPEED — не точка маршрута (нет координат), но задаёт скорость,
# действующую с этого места в последовательности команд и до следующей
# такой же команды
DO_CHANGE_SPEED = 178

# Человекочитаемые названия команд для подписей на графиках (стандартные
# мнемоники MAVLink, не переводятся — это технические идентификаторы)
COMMAND_NAMES = {
    16: "NAV_WAYPOINT",
    17: "NAV_LOITER_UNLIM",
    18: "NAV_LOITER_TURNS",
    19: "NAV_LOITER_TIME",
    20: "NAV_RETURN_TO_LAUNCH",
    21: "NAV_LAND",
    22: "NAV_TAKEOFF",
    31: "NAV_LOITER_TO_ALT",
    82: "NAV_SPLINE_WAYPOINT",
    84: "NAV_VTOL_TAKEOFF",
    85: "NAV_VTOL_LAND",
    86: "NAV_GUIDED_ENABLE",
    115: "CONDITION_YAW",
    177: "DO_JUMP",
    178: "DO_CHANGE_SPEED",
    183: "DO_SET_SERVO",
    201: "DO_SET_ROI",
}


def command_name(code: int) -> str:
    return COMMAND_NAMES.get(code, f"CMD_{code}")


@dataclass
class Waypoint:
    index: int
    current: int
    frame: int
    command: int
    param1: float
    param2: float
    param3: float
    param4: float
    lat: float
    lon: float
    alt: float
    autocontinue: int

    @property
    def has_position(self) -> bool:
        return self.lat != 0 or self.lon != 0

    @property
    def is_nav_point(self) -> bool:
        """Точка участвует в геометрии маршрута (дистанция, углы поворота) — нужна позиция."""
        return self.command in NAV_COMMANDS and self.has_position

    @property
    def is_altitude_point(self) -> bool:
        """Точка, где имеет смысл проверять критическую высоту (не точка посадки)."""
        return self.command in NAV_COMMANDS and self.command not in LAND_COMMANDS


def parse_waypoints(path: str) -> list[Waypoint]:
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    if not lines or not lines[0].strip().startswith("QGC WPL"):
        raise ValueError(
            "Файл не похож на QGC WPL (.waypoints): первая строка должна "
            "начинаться с 'QGC WPL <версия>'."
        )

    wps: list[Waypoint] = []
    for line_no, raw in enumerate(lines[1:], start=2):
        line = raw.strip()
        if not line:
            continue
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) < 12:
            print(f"[!] Строка {line_no}: пропущена, недостаточно полей ({len(parts)})")
            continue
        try:
            wp = Waypoint(
                index=int(parts[0]),
                current=int(parts[1]),
                frame=int(parts[2]),
                command=int(parts[3]),
                param1=float(parts[4]),
                param2=float(parts[5]),
                param3=float(parts[6]),
                param4=float(parts[7]),
                lat=float(parts[8]),
                lon=float(parts[9]),
                alt=float(parts[10]),
                autocontinue=int(parts[11]),
            )
        except ValueError as e:
            print(f"[!] Строка {line_no}: ошибка парсинга ({e})")
            continue
        wps.append(wp)

    return wps


def write_waypoints(path: str, wps: list[Waypoint]) -> None:
    """Записує список Waypoint у файл формату QGC WPL 110 (той самий
    формат, що читає parse_waypoints() -- симетрична пара). index у
    кожній точці переприсвоюється ПОСЛІДОВНО від 0 (не покладається на
    те, що вже було в wp.index -- виклик може передати список зі
    вставленими/видаленими точками, де оригінальні index уже
    неактуальні)."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("QGC WPL 110\n")
        for i, wp in enumerate(wps):
            f.write(
                f"{i}\t{wp.current}\t{wp.frame}\t{wp.command}\t"
                f"{wp.param1}\t{wp.param2}\t{wp.param3}\t{wp.param4}\t"
                f"{wp.lat}\t{wp.lon}\t{wp.alt}\t{wp.autocontinue}\n"
            )
