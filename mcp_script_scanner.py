"""
mcp_script_scanner.py -- парсер Lua-скриптів ArduPilot для Mission Analyzer.

=== СТАНДАРТ MCP-РОЗМІТКИ ===

Структурований блок коментарів у шапці .lua-файлу (порядок довільний,
але бажано перед кодом):

  -- MCP-NAME: "Людиночитана назва скрипта"        [обов'язково]
  -- MCP-TYPE: mission | background                 [обов'язково]
  -- MCP-VERSION: 1.2                               [опційно]
  -- MCP-DESC: Перший рядок опису.                  [опційно, багаторядковий]
  -- MCP-DESC: Продовження опису.
  -- MCP-COMMAND: 80 "Назва команди"                [тільки для mission]
  -- MCP-PARAM: param2 ключ "Підпис" одиниця        [після MCP-COMMAND]

MCP-TYPE:
  mission    -- скрипт приймає команди через vehicle:nav_script_time()
               і може бути вставлений у місію як NAV_SCRIPT_TIME (42702).
  background -- фоновий скрипт, запускається автоматично разом з ArduPilot,
               НЕ вставляється в місію як пункт (не читає nav_script_time).

MCP-DESC -- кожен рядок MCP-DESC склеюється через пробел в один абзац.

Два рівні розпізнавання:
  1. MCP-розмітка (повна інформація) -- використовується якщо є.
  2. Евристика (часткова) -- паттерни local CMD_X = 80, if cmd == 80,
     якщо розмітки немає. Дає тільки номери і технічні імена.
"""

from __future__ import annotations

import re

# --- Рівень 1: MCP-розмітка ---
_NAME_RE    = re.compile(r'--\s*MCP-NAME:\s*"([^"]*)"')
_TYPE_RE    = re.compile(r'--\s*MCP-TYPE:\s*(mission|background)', re.IGNORECASE)
_VERSION_RE = re.compile(r'--\s*MCP-VERSION:\s*(\S+)')
_DESC_RE    = re.compile(r'--\s*MCP-DESC:\s*(.*)')
_COMMAND_RE = re.compile(r'--\s*MCP-COMMAND:\s*(\d+)\s*"([^"]*)"')
_PARAM_RE   = re.compile(r'--\s*MCP-PARAM:\s*(param[1-4])\s+(\S+)\s+"([^"]*)"\s+(\S+)')

# --- Рівень 2: Евристика ---
_CONST_RE     = re.compile(r'\blocal\s+([A-Z][A-Z0-9_]*)\s*=\s*(?!0[xX])(\d+)')
_IF_CMD_RE    = re.compile(r'\b(?:if|elseif)\s+\w+\s*==\s*(\d+)')
_TABLE_KEY_RE = re.compile(r'\[\s*(\d+)\s*\]\s*=')

_MAX_HEURISTIC_CMD = 255
_SKIP_NAME_RE = re.compile(
    r'(?:PERIOD|TIMEOUT|SPEED|RATE|SIZE|MAX|MIN|COUNT|INTERVAL|LIMIT|'
    r'RANGE|SCALE|GAIN|MULT|FACTOR|OFFSET|THRESHOLD|DELAY|TIME_|_MS\b|_S\b|_M\b)',
    re.IGNORECASE,
)


def parse_script_meta(lua_text: str) -> dict:
    """Зчитує метадані скрипта (NAME, TYPE, VERSION, DESC, COMMAND/PARAM).

    Повертає словник:
    {
        "script_name":    str | None,
        "script_type":    "mission" | "background" | None,
        "script_version": str | None,
        "script_desc":    str | None,   -- склеєні рядки MCP-DESC
        "commands":       list[dict],   -- список MCP-COMMAND (для mission)
        "has_markup":     bool,
    }
    """
    script_name    = None
    script_type    = None
    script_version = None
    desc_lines: list[str] = []
    commands: list[dict] = []
    current_cmd: dict | None = None

    for line in lua_text.splitlines():
        m = _NAME_RE.search(line)
        if m:
            script_name = m.group(1)
            continue

        m = _TYPE_RE.search(line)
        if m:
            script_type = m.group(1).lower()
            continue

        m = _VERSION_RE.search(line)
        if m:
            script_version = m.group(1)
            continue

        m = _DESC_RE.search(line)
        if m:
            txt = m.group(1).strip()
            if txt:
                desc_lines.append(txt)
            continue

        m = _COMMAND_RE.search(line)
        if m:
            current_cmd = {
                "cmd":         int(m.group(1)),
                "name":        m.group(2),
                "params":      [],
                "from_markup": True,
            }
            commands.append(current_cmd)
            continue

        m = _PARAM_RE.search(line)
        if m and current_cmd is not None:
            current_cmd["params"].append({
                "slot":  m.group(1),
                "key":   m.group(2),
                "label": m.group(3),
                "unit":  m.group(4),
            })

    has_markup = any([script_name, script_type, script_version, desc_lines, commands])

    return {
        "script_name":    script_name,
        "script_type":    script_type,
        "script_version": script_version,
        "script_desc":    " ".join(desc_lines) if desc_lines else None,
        "commands":       commands,
        "has_markup":     has_markup,
    }


def parse_heuristic_commands(lua_text: str, exclude_cmds: set[int] | None = None) -> list[dict]:
    """Евристичний пошук номерів команд у тексті без MCP-розмітки."""
    exclude_cmds = exclude_cmds or set()
    found: dict[int, str | None] = {}

    for m in _CONST_RE.finditer(lua_text):
        name, num_str = m.group(1), m.group(2)
        num = int(num_str)
        if num > _MAX_HEURISTIC_CMD or _SKIP_NAME_RE.search(name):
            continue
        if num not in found:
            found[num] = name

    for m in _IF_CMD_RE.finditer(lua_text):
        num = int(m.group(1))
        if num <= _MAX_HEURISTIC_CMD and num not in found:
            found[num] = None

    for m in _TABLE_KEY_RE.finditer(lua_text):
        num = int(m.group(1))
        if num <= _MAX_HEURISTIC_CMD and num not in found:
            found[num] = None

    return [
        {
            "cmd":         cmd,
            "name":        name or f"cmd_{cmd}",
            "params":      [],
            "from_markup": False,
        }
        for cmd, name in sorted(found.items())
        if cmd not in exclude_cmds
    ]


def parse_all_commands(lua_text: str) -> list[dict]:
    """Повний аналіз: спочатку MCP-розмітка, потім евристика для решти.
    Для background-скриптів евристика не запускається -- вони не використовують
    nav_script_time(), тому знайдені числа не є кодами місіонних команд."""
    meta = parse_script_meta(lua_text)
    if meta["script_type"] == "background":
        return meta["commands"]  # завжди порожньо для background
    markup_nums = {c["cmd"] for c in meta["commands"]}
    heuristic = parse_heuristic_commands(lua_text, exclude_cmds=markup_nums)
    return meta["commands"] + heuristic


def parse_mcp_commands_from_files(files: dict[str, str]) -> list[dict]:
    """Повний аналіз декількох файлів -- повертає список команд з source_file.
    Сумісно з попередньою версією: повертає той самий формат списку команд."""
    result: list[dict] = []
    for filename, text in files.items():
        for cmd in parse_all_commands(text):
            cmd = dict(cmd)
            cmd["source_file"] = filename
            result.append(cmd)
    return result


def parse_scripts_meta_from_files(files: dict[str, str]) -> dict[str, dict]:
    """Повний аналіз декількох файлів -- повертає метадані кожного файлу.
    {filename: meta_dict} де meta_dict -- результат parse_script_meta()."""
    return {filename: parse_script_meta(text) for filename, text in files.items()}
