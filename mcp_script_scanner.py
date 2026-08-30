"""
mcp_script_scanner.py -- парсер MCP-розмітки (структурованих коментарів)
з тексту .lua-скриптів на SD-карті польотного контролера.

НЕ виконує Lua-код -- лише сканує ТЕКСТ файлу regex'ом на предмет
рядків спеціального формату (див. приклад у snake-sine-plane.lua):

    -- MCP-COMMAND: 80 "Старт змійки"
    -- MCP-PARAM: param2 timeout "Тривалість" с
    -- MCP-PARAM: param3 amplitude "Амплітуда" м

Чому НЕ повний аналіз Lua-логіки: Lua -- повноцінна мова програмування,
значення param1 могло б обчислюватись динамічно (через змінні, умови)
-- надійно "зрозуміти", що робить довільний скрипт, неможливо в
загальному випадку. Натомість -- проста, документована угода про
коментарі, яку сканує звичайний текстовий пошук: авторам скриптів
досить дотримуватись формату, і Mission Analyzer покаже їхні команди
з підписаними полями замість сирих param1..4.
"""

from __future__ import annotations

import re

_COMMAND_RE = re.compile(r'--\s*MCP-COMMAND:\s*(\d+)\s*"([^"]*)"')
_PARAM_RE = re.compile(r'--\s*MCP-PARAM:\s*(param[1-4])\s+(\S+)\s+"([^"]*)"\s+(\S+)')


def parse_mcp_commands(lua_text: str) -> list[dict]:
    """Розбирає MCP-розмітку з тексту одного .lua-файлу.

    Повертає список команд у порядку появи в файлі:
        [{"cmd": int, "name": str, "params": [
            {"slot": "param2", "key": "timeout", "label": "Тривалість", "unit": "с"},
            ...
        ]}, ...]

    MCP-PARAM рядки належать ОСТАННЬОМУ побаченому MCP-COMMAND вище по
    файлу (до наступного MCP-COMMAND чи кінця файлу) -- той самий
    принцип, що й у прикладі snake-sine-plane.lua. MCP-PARAM без
    попереднього MCP-COMMAND у файлі -- ігнорується (немає команди, до
    якої його прикріпити).
    """
    commands: list[dict] = []
    current: dict | None = None

    for line in lua_text.splitlines():
        m_cmd = _COMMAND_RE.search(line)
        if m_cmd:
            current = {
                "cmd": int(m_cmd.group(1)),
                "name": m_cmd.group(2),
                "params": [],
            }
            commands.append(current)
            continue

        m_param = _PARAM_RE.search(line)
        if m_param and current is not None:
            current["params"].append({
                "slot": m_param.group(1),
                "key": m_param.group(2),
                "label": m_param.group(3),
                "unit": m_param.group(4),
            })

    return commands


def parse_mcp_commands_from_files(files: dict[str, str]) -> list[dict]:
    """Те саме, для декількох файлів одразу -- files: {ім'я_файлу: текст}.
    До кожної знайденої команди додається "source_file" -- з якого саме
    файлу вона взята (кілька скриптів можуть лежати в APM/scripts/
    одночасно, корисно знати походження при показі користувачу)."""
    result: list[dict] = []
    for filename, text in files.items():
        for cmd in parse_mcp_commands(text):
            cmd = dict(cmd)
            cmd["source_file"] = filename
            result.append(cmd)
    return result
