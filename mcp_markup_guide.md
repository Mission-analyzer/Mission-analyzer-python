# Making Your ArduPilot Lua Scripts Visible in Mission Analyzer

If you write Lua scripts for ArduPilot and use them as mission items
via `NAV_SCRIPT_TIME` (MAV_CMD 42702), this post explains how to make
those scripts show up properly in Mission Analyzer — with their
commands listed, parameters labeled, and ready-to-copy embedding
instructions.

---

## The problem

ArduPilot uses a single MAVLink command — `NAV_SCRIPT_TIME` (42702) —
for *any* Lua script logic inside a mission. The protocol itself has
no concept of "which script" or "what command 80 means". That
knowledge lives only inside the `.lua` file on the SD card.

So when Mission Analyzer scans `APM/scripts/`, it sees the `.lua`
files, but without a structured way to read them, it can only say:
*"this script exists."*

---

## Two levels of recognition

Mission Analyzer reads scripts with two strategies, from best to
least informative:

### Level 1 — MCP markup (full information)

You add a small block of structured comments to your script's header.
The scanner reads those comments (it does **not** execute any Lua
code) and extracts the complete picture: command numbers, human-
readable names, parameter labels, units.

Result in the Info report:

```
snake-sine-plane.lua:
  Command 80 "Sinusoidal snake" — embed as NAV_SCRIPT_TIME (42702), param1=80:
    param2 = Duration (s)
    param3 = Amplitude (m)
    param4 = Wave period (s)
  Command 81 "Emergency abort" — embed as NAV_SCRIPT_TIME (42702), param1=81:
```

### Level 2 — Heuristic detection (partial information)

If there is no markup, the scanner looks for patterns like
`local CMD_SPRAY_START = 42` or `if cmd == 42 then` and extracts
just the numbers and any constant names it can find. No parameter
descriptions, no embedding instructions.

Result in the Info report:

```
spray.lua:
  Heuristically detected commands (no description, no parameters):
  Command 42 (CMD_SPRAY_START) — no description
  Command 43 (CMD_SPRAY_STOP) — no description
```

Better than nothing, but significantly less useful than markup.

---

## The markup format

Add a block like this to your script's header (before any Lua code).
Each line must be a Lua comment (`--`):

```lua
-- MCP-COMMAND: <number> "<human-readable name>"
-- MCP-PARAM: <slot> <key> "<label>" <unit>
-- MCP-PARAM: ...
--
-- MCP-COMMAND: <number> "<human-readable name>"
-- (no params for this one)
```

Rules:
- `MCP-COMMAND` declares one command. `<number>` is the value you
  put in `param1` of `NAV_SCRIPT_TIME`. `<name>` is in double quotes.
- `MCP-PARAM` lines belong to the **last** `MCP-COMMAND` above them
  (until the next `MCP-COMMAND` or end of block).
- `<slot>` is one of `param2`, `param3`, `param4` (these map to
  `arg1`, `arg2`, `arg3` in the Lua API).
- `<key>` is a short internal identifier (no spaces).
- `<label>` is the human-readable field name, in double quotes.
- `<unit>` is the unit string (no spaces; use `—` for dimensionless).

### Full example

```lua
-- my-script.lua
--
-- MCP-COMMAND: 10 "Start spray"
-- MCP-PARAM: param2 duration "Duration" s
-- MCP-PARAM: param3 width "Swath width" m
--
-- MCP-COMMAND: 11 "Stop spray"

local CMD_START = 10
local CMD_STOP  = 11
local UPDATE_PERIOD_MS = 500

function update()
    local id, cmd, arg1, arg2 = vehicle:nav_script_time()
    if id then
        if cmd == CMD_START then
            local duration = arg1
            local width    = arg2
            -- your logic here
            vehicle:nav_script_time_done(id)
        elseif cmd == CMD_STOP then
            vehicle:nav_script_time_done(id)
        end
    end
    return update, UPDATE_PERIOD_MS
end

return update()
```

---

## How to embed in a mission

Once the script is in `APM/scripts/` and `SCR_ENABLE = 1`, add a
`NAV_SCRIPT_TIME` item to your mission:

| Field   | Value                              |
|---------|------------------------------------|
| command | 42702 (`NAV_SCRIPT_TIME`)          |
| param1  | your command number (e.g. `10`)    |
| param2  | first argument (e.g. duration)     |
| param3  | second argument (e.g. width)       |
| param4  | third argument (if needed)         |
| lat/lon/alt | leave as 0 (not used)         |

In `.waypoints` format (QGC WPL 110), a line for command 10 with
duration=60 s and width=40 m looks like:

```
3	0	3	42702	10	60	40	0	0	0	0	1
```

(columns: `index current frame command param1 param2 param3 param4
lat lon alt autocontinue`)

---

## Why comments, not code analysis?

Lua is a full programming language. A command number could be stored
in a variable, computed at runtime, or looked up in a table. There is
no reliable way to extract semantic meaning (parameter descriptions,
units, human-readable names) by reading code alone — you'd need to
execute it.

Structured comments in a fixed format are the same approach used by
JSDoc, Python docstrings, and Doxygen: the author states intent
explicitly, the tool reads it literally. It takes two minutes to add
and works perfectly every time.

---

## Uploading your script

You can upload `.lua` files directly to `APM/scripts/` over USB
without touching the SD card physically:

- **Mission Analyzer** — SD Files → navigate to `APM/scripts/` →
  Upload
- **Mission Planner** — MAVFtp tab
- **QGroundControl** — Vehicle Setup → Storage

After uploading: reboot the flight controller (or power-cycle) so the
scripting engine picks up the new file.

---

## See also

- [snake-sine-plane.lua](../DATA/snake-sine-plane.lua) — a complete
  example with full MCP markup
- [wipe-passed-waypoints.lua](../DATA/wipe-passed-waypoints.lua) —
  a background script example (no mission commands, no markup needed)
- [ArduPilot Lua scripting docs](https://ardupilot.org/plane/docs/common-lua-scripts.html)
