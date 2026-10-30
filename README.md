# Mission Analyzer

> **README revision 2026-10-30** · describes **Mission Analyzer 1.2.0**
> (latest published release: 1.1.1 — see [Releases](https://github.com/Mission-analyzer/Mission-analyzer-python/releases))

A Windows desktop application for preparing ArduPilot / Mission Planner fixed-wing missions (`.waypoints`, QGC WPL 110):
it **analyzes** a mission before flight, **fixes** the route around populated areas, and lets you **fly it in a
simulator** — with a nose camera, HUD, terrain, forest, buildings and power lines — before the real aircraft leaves
the ground. Everything runs offline from a USB stick once the map data for the area has been collected.

## What it does

### Mission analysis

- Critically low altitude — from the mission file itself and from SRTM terrain, along the **whole flight line**
  between waypoints, not only at the points.
- Turns that are too sharp; climb/descent angle (flight path angle) out of tolerance.
- Landing approach glide slope — distance, bearing and angle over the final legs into `LAND`, accounting for
  `DO_CHANGE_SPEED`.
- Route proximity to populated areas (OpenStreetMap), measured from every route **leg** to the nearest settlement,
  with a left/right marker relative to the direction of flight.

### Route optimization

If the route passes too close to a populated area, the app builds a detour — geometrically optimal (tangent
visibility graph around circular exclusion zones + Dijkstra), checked against the fuel budget (ICAO Annex 6 reserve),
the waypoint-count limit and the turn the airframe can physically fly (turn radius from airspeed and maximum bank).
The result is a before/after map and a separate `.waypoints` file — the original mission is never modified.
Method writeup: `route_optimization_community_post.md`.

### Simulation *(new in 1.1.1, extended in 1.2.0)*

The mission is flown by **ArduPlane SITL** (real ArduPilot firmware running on the PC); Mission Analyzer connects to
it over MAVLink like a ground station and draws everything SITL does not know about — the world around the aircraft.

- **Nose camera** — forward (FPV) and down views; colour view from satellite imagery draped over SRTM terrain;
  **thermal imager** (white hot / black hot / Ironbow) with a day/night ground-temperature model, cold sky,
  atmospheric contrast loss, sensor noise, vignetting and automatic gain control.
- **HUD** in the X-Plane style — airspeed, ground speed, climb, altitude, AGL, heading tape, attitude, distance —
  with limit codes **TER / SPD / BNK / PIT** and audio warnings.
- **Collision detection** — terrain (SRTM), **forest canopy** (real tree heights), **buildings**, OSM obstacles
  (power lines and pylons, masts, towers, chimneys, cranes, wind turbines) and your own **3D scene objects**
  (`.glb` / `.gltf` / `.obj` in the `models3d` folder). Over forest the HUD shows **AGL TREES** — height above the
  canopy, which also drives the TER warning.
- **Control** — joystick or RC transmitter in USB HID mode (e.g. RadioMaster / EdgeTX) with channel calibration
  (CH1…CHn, as on the radio), flight modes (MANUAL, FBWA, AUTO…), SITL launch with mission upload.
- **Fly-by video** — MP4 export of the flight around 3D objects, encoded on the GPU when available (NVENC / Quick Sync /
  AMF) and on the CPU otherwise.
- **HUD in a separate window** — the same camera frame and HUD on a second monitor.

**GPU rendering.** If the PC has a suitable graphics card (OpenGL 3.3), the camera view is rendered on the GPU in a
separate process: 3D terrain with three levels of detail, forest canopy, buildings, power lines, 3D objects and the
thermal imager. Without a GPU (or if the driver misbehaves) the same view is rendered on the CPU — the program checks
the hardware itself and falls back automatically; collision checks work the same either way.

### Map tab *(new in 1.2.0)*

Map data is tied to **the area you fly**, not to a single mission. It accumulates on the USB stick with every flight;
the **Map** tab shows on a map what is already stored for each layer (green), what the loaded mission is missing
(red outline), and downloads what is missing — for the mission or for a **region** you select with the right mouse
button (with a size estimate before downloading). The summary line shows how much space each layer takes.

## Installation (ready-made `.exe`)

You don't need Python to use the program — a portable Windows build is published on the
**[Releases](https://github.com/Mission-analyzer/Mission-analyzer-python/releases)** page.

1. Download the latest `.zip` archive.
2. Unpack it into any folder — a USB stick works fine: the app is portable and keeps its settings and data next to
   the `.exe`.
3. Run the `.exe` inside the unpacked folder.

**Windows will show a warning — this is normal, not a virus signal.** The app isn't signed with a commercial
certificate, so on first launch you'll see **"Windows protected your PC"** (SmartScreen). Click **"More info"**, then
**"Run anyway"**. The entire source is in this repository if you want to check it.

## Running from source

Windows, **Python 3.11+** (developed on 3.14).

```
pip install -r requirements.txt
python main.py
```

The core (mission table, analysis, charts, maps) needs only the standard library; the packages in
`requirements.txt` enable the rest — PDF reports, MAVLink, the simulator, GPU rendering, video export and the forest
map. Without an optional package the related feature is simply unavailable; the program still starts.

## Map data and offline use

All map data is downloaded once and stored next to the program; after that the program works without internet.

| Layer | Used for | Source | Licence / attribution |
|---|---|---|---|
| Satellite and map tiles | maps, nose camera, thermal imager | Google, OpenStreetMap | provider terms; © OpenStreetMap contributors |
| Terrain | altitude analysis, camera, collisions | NASA SRTM via `terrain.ardupilot.org` | public domain |
| Settlements, power lines, obstacles | route analysis and optimization, camera, collisions | OpenStreetMap (Overpass API) | © OpenStreetMap contributors, ODbL |
| Forest canopy height (~10 m) | AGL over forest, collisions, camera | ETH Global Canopy Height 2020 (Lang et al., ETH Zürich) | CC BY 4.0 |
| Building footprints | collisions, camera, thermal imager | Microsoft Global ML Building Footprints | ODbL |
| Occupied territories | mission map overlay | deepstatemap.live | source terms |

Notes:
- Forest heights are the 2020 state; buildings without a height in the source get a height estimated from the
  footprint area (marked "assumed" in collision messages).
- Folders: `srtm`, and next to the map cache — `obstacles`, `canopy`, `buildings`. Copy them together with the program
  to keep everything offline.

### SRTM tiles

Terrain tiles are **not stored in the repository** (gigabytes of binary data). `srtm.py` downloads a tile from the
public `terrain.ardupilot.org` mirror (the one Mission Planner uses) when it is first needed and keeps it in the SRTM
folder set on the **Configuration** page. To trim a large SRTM folder down to specific regions:

```
python cleanup_srtm.py "path\to\SRTM\folder" --apply
```

Without `--apply` the script only shows what would be moved (dry run).

## Project structure

| Files | Responsibility |
|---|---|
| `main.py`, `app.py` | Entry point and splash screen; main window, navigation, language switching |
| `mission_page.py`, `mission_editor.py` | **Mission** page: waypoint table, `.waypoints` load/save, route map, editing |
| `analysis_page.py`, `analyzer.py` | **Analysis** page and core analysis logic, PDF report |
| `optimization_page.py`, `route_optimizer.py`, `populated_areas.py` | **Optimization**: detours around populated areas |
| `simulation_page.py`, `sim_*.py` | **Simulation** page: SITL, MAVLink, control, HUD, maps, impacts, fly-by |
| `nose_camera*.py`, `gl_camera.py` | Nose camera on the CPU (`nose_camera*`) and on the GPU (`gl_camera`), HUD window |
| `hud_*.py` | HUD rendering, telemetry, limits and settings |
| `scene_*.py`, `uav_panel.py` | 3D scene objects and the aircraft model |
| `obstacles.py`, `terrain_impact.py` | OSM obstacles, forest canopy (ETH), buildings (Microsoft); collision checks |
| `map_page.py` | **Map** page: data coverage map, download for mission or region |
| `config_page.py`, `sim_config_page.py`, `aircraft_profiles.py` | **Configuration**: analysis parameters, paths, HUD, aircraft profiles |
| `ardupilot_link.py`, `sd_file_manager.py`, `board_ids.py` | Live MAVLink connection, SD-card file manager (MAVFTP) |
| `joystick_input.py`, `video_export.py`, `alert_sound.py`, `limit_sound.py` | Joystick, MP4 export, sounds |
| `srtm.py`, `geo.py`, `online_tiles.py`, `map_view.py` | Terrain, geometry, map tiles and their cache |
| `i18n*.py`, `theme.py`, `icons.py`, `settings.py`, `meta.py`, `updater.py` | Localization, theme, settings, version and changelog, update check |
| `main.spec`, `BUILD.md` | Windows `.exe` build (PyInstaller) |

`settings.json` is created by the program on first launch and is not part of the repository.

## Localization

Ukrainian and English. Russian is deliberately not supported.

## License

*License: MIT — © 2026 Sergey Gorbachevsky*
