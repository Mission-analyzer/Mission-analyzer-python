# MCP Markup: Making Your ArduPilot Lua Scripts Visible in Mission Analyzer

Mission Analyzer scans `APM/scripts/` on the SD card and shows what
Lua scripts are loaded — their purpose, type, and (for mission scripts)
how to embed them. This requires a small block of structured comments
in the script header, called **MCP markup**.

---

## Script types

ArduPilot Lua scripts fall into two categories:

**Mission scripts** use `vehicle:nav_script_time()` to receive commands
from a `NAV_SCRIPT_TIME` (42702) mission item. The flight controller
pauses at that waypoint, runs the script logic, and continues when the
script calls `vehicle:nav_script_time_done()`.

**Background scripts** start automatically with ArduPilot and run
continuously alongside the mission. They cannot be triggered by a
waypoint. Examples: telemetry forwarders, engine controllers, LED
managers.

Mission Analyzer needs to know which type your script is — and for
mission scripts, which command numbers it accepts and what each
parameter means.

---

## The markup format

Add this block to the **top of your script**, before any Lua code.
Every line is a standard Lua comment (`--`):

```lua
-- MCP-NAME: "Human-readable script name"     [required]
-- MCP-TYPE: mission | background              [required]
-- MCP-VERSION: 1.0                            [optional]
-- MCP-DESC: First line of description.        [optional, repeatable]
-- MCP-DESC: Continuation — each MCP-DESC line is joined with a space.
--
-- MCP-COMMAND: <number> "<command name>"      [mission scripts only]
-- MCP-PARAM: <slot> <key> "<label>" <unit>    [after MCP-COMMAND]
```

**Field reference:**

| Tag | Required | Description |
|-----|----------|-------------|
| `MCP-NAME` | yes | Short human-readable name shown in the Info report |
| `MCP-TYPE` | yes | `mission` or `background` |
| `MCP-VERSION` | no | Your version string, shown next to the name |
| `MCP-DESC` | no | Description text; repeat for multiple lines |
| `MCP-COMMAND` | mission only | Declares one command: number + quoted name |
| `MCP-PARAM` | after MCP-COMMAND | One line per parameter of that command |

**MCP-PARAM syntax:**
`MCP-PARAM: <slot> <key> "<label>" <unit>`
- `<slot>` — `param2`, `param3`, or `param4`
  (these are `arg1`, `arg2`, `arg3` in the Lua API)
- `<key>` — short internal identifier, no spaces
- `<label>` — human-readable name, in double quotes
- `<unit>` — unit string, no spaces (`s`, `m`, `deg`, `—` for dimensionless)

`MCP-PARAM` lines belong to the last `MCP-COMMAND` above them.

---

## Example: mission script

```lua
-- my-spray.lua
--
-- MCP-NAME: "Spray system controller"
-- MCP-TYPE: mission
-- MCP-VERSION: 2.1
-- MCP-DESC: Controls the spray pump via NAV_SCRIPT_TIME mission items.
-- MCP-DESC: Command 10 starts spraying, command 11 stops it.
--
-- MCP-COMMAND: 10 "Start spray"
-- MCP-PARAM: param2 duration "Duration" s
-- MCP-PARAM: param3 width "Swath width" m
--
-- MCP-COMMAND: 11 "Stop spray"

local CMD_START        = 10
local CMD_STOP         = 11
local UPDATE_PERIOD_MS = 500

function update()
    local id, cmd, arg1, arg2 = vehicle:nav_script_time()
    if id then
        if cmd == CMD_START then
            start_spray(arg1, arg2)           -- duration, width
            vehicle:nav_script_time_done(id)
        elseif cmd == CMD_STOP then
            stop_spray()
            vehicle:nav_script_time_done(id)
        end
    end
    return update, UPDATE_PERIOD_MS
end

return update()
```

What Mission Analyzer shows in the Info report:

```
Spray system controller v2.1
  type: mission item (embed as NAV_SCRIPT_TIME)
  Controls the spray pump via NAV_SCRIPT_TIME mission items. Command 10 starts spraying, command 11 stops it.
  Command 10 "Start spray" — embed as NAV_SCRIPT_TIME (42702), param1=10:
    param2 = Duration (s)
    param3 = Swath width (m)
  Command 11 "Stop spray" — embed as NAV_SCRIPT_TIME (42702), param1=11:
```

---

## Example: background script

```lua
-- engine-telemetry.lua
--
-- MCP-NAME: "Engine Telemetry"
-- MCP-TYPE: background
-- MCP-VERSION: 1.0
-- MCP-DESC: Reads RPM and EGT from the engine ECU over serial,
-- MCP-DESC: forwards data to GCS via NAMED_VALUE_FLOAT.
-- MCP-DESC: Starts automatically with ArduPilot. Not a mission item.

local UPDATE_PERIOD_MS = 50

function update()
    -- read serial, forward telemetry
    return update, UPDATE_PERIOD_MS
end

return update()
```

What Mission Analyzer shows:

```
Engine Telemetry v1.0
  type: background (not a mission item)
  Reads RPM and EGT from the engine ECU over serial, forwards data
  to GCS via NAMED_VALUE_FLOAT. Starts automatically with ArduPilot.
  Not a mission item.
```

---

## Embedding a mission script in a waypoint file

Once the script is in `APM/scripts/` and `SCR_ENABLE = 1`, add a
`NAV_SCRIPT_TIME` item to your mission:

| Field | Value |
|-------|-------|
| command | `42702` (`NAV_SCRIPT_TIME`) |
| param1 | your command number (e.g. `10`) |
| param2 | first argument |
| param3 | second argument |
| param4 | third argument (if needed) |
| lat / lon / alt | `0` (not used) |

In `.waypoints` format (QGC WPL 110) — command 10, duration=60 s,
width=40 m, as waypoint index 3:

```
3	0	3	42702	10	60	40	0	0	0	0	1
```

Columns: `index current frame command param1 param2 param3 param4 lat lon alt autocontinue`

---

## Without markup: heuristic detection

If a script has no MCP markup, Mission Analyzer still tries to find
command numbers using code patterns:

- `local CMD_SPRAY_START = 42` → extracts number and constant name
- `if cmd == 42 then` → extracts number only

Result in the Info report:

```
my-script.lua
  Heuristically detected commands (no description, no parameters):
  Command 42 (CMD_SPRAY_START) — no description
```

Heuristic detection only works for mission scripts — background
scripts are not scanned this way, since their numeric constants are
not mission command IDs.

---

## Why comments, not code analysis?

Lua is a full programming language. A command number can be stored in
a variable, computed at runtime, or looked up in a table. Reliably
extracting semantic meaning — parameter descriptions, units,
human-readable names — from arbitrary Lua code would require executing
it. That is not safe to do on arbitrary third-party scripts.

Structured comments are the same approach used by JSDoc, Python
docstrings, and Doxygen: the author states intent explicitly, the tool
reads it literally. Two minutes to write, works every time.

---

## Uploading scripts to the SD card

Upload `.lua` files to `APM/scripts/` over USB without removing the
SD card:

- **Mission Analyzer** — SD Files → navigate to `APM/scripts/` → Upload
- **Mission Planner** — MAVFtp tab
- **QGroundControl** — Vehicle Setup → Storage

After uploading, reboot the flight controller so the scripting engine
picks up the new file (`SCR_ENABLE = 1` must be set).

---

## See also

- [`snake-sine-plane.lua`](../DATA/snake-sine-plane.lua) —
  sinusoidal snake maneuver, full MCP markup example
- [`wipe-passed-waypoints.lua`](../DATA/wipe-passed-waypoints.lua) —
  background script, no mission commands
- [ArduPilot Lua scripting docs](https://ardupilot.org/plane/docs/common-lua-scripts.html)
- [ArduPilot `NAV_SCRIPT_TIME` reference](https://ardupilot.org/plane/docs/common-mavlink-mission-command-messages-mav_cmd.html)
