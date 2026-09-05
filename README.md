# Mission Analyzer

A desktop application for analyzing ArduPilot / Mission Planner flight
missions (`.waypoints`, QGC WPL 110 format) before a drone flight.

Checks for:

- critically low flight altitude — both from the mission file's own
data and from SRTM terrain (along the entire flight line between
points, not just at the points themselves);
- turns that are too sharp;
- climb/descent angle (flight path angle) out of tolerance;
- the landing approach glide slope — distance/bearing/angle over the
final legs into `LAND`, accounting for `DO_CHANGE_SPEED` commands;
- route proximity to populated areas — queries real settlement
coordinates (Overpass API, OpenStreetMap), distance is measured from
every route **leg** (not just the waypoints themselves) to the nearest
settlement, with a left/right marker relative to the direction of
flight.

And it doesn't just check — it can **fix**: if a route passes too
close to a populated area, the app can automatically build a detour —
geometrically optimal (tangent visibility graph around circular
exclusion zones, solved with Dijkstra's algorithm), with a check
against the fuel budget (ICAO Annex 6 reserve), the mission waypoint-
count limit, and whether the airframe can physically fly that turn
(turn radius from airspeed and maximum bank angle). The result is a
before/after map and a separate `.waypoints` file — the original
mission is never modified.

An aircraft profile (cruise airspeed, bank/pitch limits, fuel
consumption) is filled in once on the **Configuration** page and reused
across every Analysis calculation that needs it — no need to type it
in by hand each time. The architecture recognizes four ArduPilot
vehicle types (Plane, Copter, Rover, Sub — the same ones Mission
Planner uses), but right now the full profile is implemented and
tested only for **Plane**; the other types are correctly recognized,
but their profile fields are still in development.

## Installation (ready-made `.exe`)

You don't need to install Python or run from source — a portable
Windows build is available, nothing else to install.

1. Open the **[Releases](https://github.com/Mission-analyzer/Mission-analyzer-python/releases)**
   page and download the latest `.zip` archive (e.g.
   `MissionAnalyzerV103.zip`).
2. Unpack the archive into any folder (a USB stick works fine — the
   app is portable and writes nothing outside its own folder except
   `settings.json` next to the `.exe`).
3. Run the `.exe` inside the unpacked folder.

**Windows will show a warning — this is normal, not a virus signal.**
The app isn't signed with a commercial certificate (it costs money and
isn't necessary for a small open-source project), so on first launch
you'll see a **"Windows protected your PC"** window (SmartScreen). To
run it anyway:

1. Click **"More info"** in that window.
2. A **"Run anyway"** button will appear — click it.

This is standard Windows behavior for any unsigned `.exe`, regardless
of how safe it actually is — a signing certificate costs money per
year and has nothing to do with the actual safety of the code. If you
want to check the file yourself, the entire source is right here in
this repository.

## Requirements

(This is for running **from source**, for developers — if the ready-
made `.exe` above is enough for you, skip this section.)

- Windows, Python 3.11+ (standard library: `tkinter`, `urllib`, `json`;
optionally `Pillow` for better logo/screenshot scaling).
- Internet access to download map tiles (OpenStreetMap / Google) and
SRTM terrain tiles as needed (see below).

Install dependencies:

```
pip install -r requirements.txt
```

## Running

```
python main.py
```

## SRTM tiles (terrain)

Terrain tiles are **not stored in the repository** (that's gigabytes
of binary data, not code). Instead, `srtm.py` downloads the needed
tile itself from the public `terrain.ardupilot.org` mirror (the same
one Mission Planner uses for tile downloads) at the moment a tile is
actually needed for route analysis, and stores it in a local SRTM
folder alongside everything else.

The path to the tile folder is set on the **Configuration** page in
the app itself.

If the folder has already accumulated a full world SRTM database and
you want to keep only a specific region (e.g. Ukraine and Russia), use
`cleanup_srtm.py`:

```
python cleanup_srtm.py "path\to\SRTM\folder" --apply
```

Without `--apply` the script only shows how many files and gigabytes
would be moved (dry run).

## Project structure

| File                                                    | Responsibility                                                                                                                 |
| ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `main.py`                                               | Entry point, splash screen                                                                                                     |
| `app.py`                                                | Main window: header, navbar, page frame, language switching                                                                    |
| `mission_page.py`                                       | "Mission" page: waypoint table, `.waypoints` load/save, ArduPilot (MAVLink) connection, route map                              |
| `analysis_page.py`                                      | "Analysis" page: takeoff/route/landing/settlements/optimization tabs, weather, charts, PDF report                              |
| `route_optimizer.py`                                    | Builds detours around populated areas (tangent graph + Dijkstra), checks fuel and turn radius                                  |
| `populated_areas.py`                                    | Queries populated areas (Overpass API), computes route-to-settlement distance                                                  |
| `config_page.py`                                        | "Configuration" page: analysis parameters, SRTM/map cache paths, map provider, aircraft profiles                               |
| `aircraft_profiles.py`                                  | Aircraft profile model and storage (cruise airspeed, bank, pitch, etc.)                                                        |
| `help_page.py`                                          | "Help" page: changelog, update check                                                                                           |
| `waypoints.py`                                          | `.waypoints` (QGC WPL 110) parsing/writing                                                                                     |
| `geo.py`                                                | Geometry: distances, bearings, interpolation along the route                                                                   |
| `srtm.py`                                               | Reads terrain from SRTM tiles + auto-downloads missing tiles                                                                   |
| `analyzer.py`                                           | Core mission analysis logic, report generation                                                                                 |
| `elevation_view.py`, `angle_view.py`, `landing_view.py` | Altitude, flight-path-angle, and glide-slope charts                                                                            |
| `map_view.py`, `online_tiles.py`, `overview_map.py`     | Map tiles (OSM/Google), caching, rendering                                                                                     |
| `occupied_layer.py`                                     | Occupied-territories / front-line layer (deepstatemap.live)                                                                    |
| `ardupilot_link.py`                                     | Live MAVLink connection: Info, Commands, Parameters, SD card file manager                                                      |
| `board_ids.py`                                          | Board name and USB vendor/product ID lookup                                                                                    |
| `sd_file_manager.py`                                    | SD card file manager over MAVFTP                                                                                               |
| `mcp_script_scanner.py`                                 | Scans Lua scripts on the SD card, parses MCP markup                                                                            |
| `i18n.py`                                               | UA/EN localization                                                                                                              |
| `theme.py`, `icons.py`                                  | Mission Planner-style visual theme                                                                                             |
| `settings.py`                                           | `settings.json` save/load                                                                                                      |
| `meta.py`                                               | App version and changelog                                                                                                      |
| `updater.py`                                            | Update check and apply                                                                                                         |
| `cleanup_srtm.py`                                       | Helper script to trim the SRTM folder to specific regions                                                                      |

`settings.json` is generated by the app itself on first launch and is
not part of the repository (see `.gitignore`).

Detailed writeup of the populated-area-avoidance method (problem
statement, tangent graph, why this approach) —
`route_optimization_community_post.md` in the repository root.

## Localization

Ukrainian and English. Russian is deliberately not supported.

## License

*License: MIT — © 2026 Sergey Gorbachevsky*
