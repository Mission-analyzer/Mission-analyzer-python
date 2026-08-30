"""
MCP (Mavlink Capability Probe) -- офлайн-довідник команд MAV_CMD.

Крок 1 з двох: цей скрипт НЕ підключається до жодного обладнання --
лише читає список команд, відомих встановленій версії pymavlink (той
самий публічний реєстр MAVLink, що й на mavlink.io). Безпечно
запускати будь-де, без ризику для апаратури.

Крок 2 (окремий скрипт, на реальному підключенні до лабораторного
контролера) буде РЕАЛЬНО перевіряти, які з цих команд контролер
приймає (ACCEPTED) чи відхиляє (UNSUPPORTED/DENIED) -- з явним
винятком потенційно небезпечних команд (ARM, керування сервоприводами/
моторами) за замовчуванням, щоб не спричинити фізичний рух
обладнання під час простого опитування списку команд.

Виправлено відносно початкового варіанту: enum.entry не існує в
поточній версії pymavlink (2.4.49) -- сам enum вже є словником
{id: EnumEntry}, ітеруємо його напряму через .items().
"""

from __future__ import annotations

from pymavlink.dialects.v20 import ardupilotmega as mavlink


def get_all_commands() -> list[tuple[int, str, str]]:
    """Повертає [(id, name, description), ...], відсортовані за id."""
    enum = mavlink.enums["MAV_CMD"]
    commands = []
    for cmd_id, entry in enum.items():
        # деякі id мають "псевдо-записи" на кшталт ENUM_END -- у них
        # немає реального name/description, пропускаємо. MAV_CMD_ENUM_END
        # окремо -- службовий маркер кінця переліку (має ім'я, але не є
        # реальною командою) -- виключаємо явно за назвою.
        name = getattr(entry, "name", None)
        if not name or name == "MAV_CMD_ENUM_END":
            continue
        commands.append((cmd_id, name, entry.description or ""))
    commands.sort(key=lambda row: row[0])
    return commands


def main():
    commands = get_all_commands()

    print()
    print(f"{'ID':>6} | {'NAME':<45} | DESCRIPTION")
    print("-" * 120)
    for cmd_id, name, description in commands:
        # опис може бути багаторядковим у самій специфікації -- беремо
        # лише перший рядок, щоб не ламати табличний вивід
        first_line = description.splitlines()[0] if description else ""
        print(f"{cmd_id:6} | {name:<45} | {first_line}")
    print()
    print(f"Всього команд: {len(commands)}")


if __name__ == "__main__":
    main()
