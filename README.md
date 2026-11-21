# Mission Analyzer

> **README revision 2026-11-21** · describes **Mission Analyzer 1.1.3**
> (published builds — see [Releases](https://github.com/Mission-analyzer/Mission-analyzer-python/releases))

A Windows desktop application for preparing ArduPilot / Mission Planner fixed-wing missions (`.waypoints`, QGC WPL 110):
it **analyzes** a mission before flight, **fixes** the route around populated areas, and lets you **fly it in a
simulator** — with a nose camera, HUD, terrain, forest, buildings, power lines, airfields and city landmarks — before
the real aircraft leaves the ground. Everything runs offline from a USB stick once the map data for the area has been
collected.

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

### Simulation

The mission is flown by **ArduPlane SITL** (real ArduPilot firmware running on the PC); Mission Analyzer connects to
it over MAVLink like a ground station and draws everything SITL does not know about — the world around the aircraft.

- **Nose camera** — forward (FPV) and down views; colour view from satellite imagery draped over SRTM terrain, with
  optional colour correction of the imagery; **thermal imager** (white hot / black hot / Ironbow) with a day/night
  ground-temperature model that depends on land cover, cold sky, atmospheric contrast loss, sensor noise, vignetting
  and automatic gain control.
- **The world around the aircraft** — terrain, forest canopy, buildings with real heights, roads, railways, metro and
  tram lines, rivers and lakes, bridges with decks, piers and real spans, stadiums, power lines and other obstacles,
  **airfields** with runways, taxiways and aprons, and **city landmarks** (churches, stations, monuments) drawn from
  building parts or from dedicated 3D models.
- **HUD** in the X-Plane style — airspeed, ground speed, climb, altitude, AGL, heading tape, attitude, distance —
  with limit codes **TER / SPD / BNK / PIT** and audio warnings.
- **Collision detection** — terrain (SRTM), **forest canopy** (real tree heights), **buildings**, bridges, landmarks,
  OSM obstacles (power lines and pylons, masts, towers, chimneys, cranes, wind turbines) and your own **3D scene
  objects** (`.glb` / `.gltf` / `.obj` in the `models3d` folder). Obstacles are checked along the flight path segment,
  not only at sample points. Over forest the HUD shows **AGL TREES** — height above the canopy, which also drives the
  TER warning.
- **Mission map** follows the aircraft with a lead in the direction of flight, so free flights don't run off the map.
- **Control** — joystick or RC transmitter in USB HID mode (e.g. RadioMaster / EdgeTX) with channel calibration
  (CH1…CHn, as on the radio), flight modes (MANUAL, FBWA, AUTO…), SITL launch with mission upload.
- **Fly-by video** — MP4 export of the flight around 3D objects, encoded on the GPU when available (NVENC / Quick Sync /
  AMF) and on the CPU otherwise.
- **HUD in a separate window** — the same camera frame and HUD on a second monitor.

Map data for the simulation is loaded around the aircraft as it flies, not for the whole mission at once, so long
missions start quickly.

**GPU rendering.** If the PC has a suitable graphics card (OpenGL 3.3), the camera view is rendered on the GPU in a
separate process: 3D terrain with three levels of detail, forest canopy, buildings, landmarks, power lines, 3D objects
and the thermal imager. Without a GPU (or if the driver misbehaves) the same view is rendered on the CPU — the program
checks the hardware itself and falls back automatically; collision checks work the same either way.

### Map tab

Map data is tied to **the area you fly**, not to a single mission. It accumulates on the USB stick with every flight;
the **Map** tab shows on a map what is already stored for each layer (green), what the loaded mission is missing
(red outline), and downloads what is missing — for the mission or for a **region** you select with the right mouse
button (with a size estimate before downloading). The summary line shows how much space each layer takes.

**Preparing flight areas in advance** *(1.1.3)*. Roads, railways, water and airfields are aligned to the imagery
(OpenStreetMap / Overture offsets reach 40 m), runways are fitted to their markings in the imagery (axis and width
from the edge lines, ends from the threshold "piano keys"). The ground model is built from this: terrain corrected
for buildings, level roads and water, the airfield as a flat table. The camera, collision checks and SITL use the same
ground: an impact only with what is drawn.

The Map tab also opens the two reference catalogs below.

## Reference catalogs

Two built-in references describe what map data alone gets wrong or leaves out. Both use open standard formats, keep
a source for every record, and are changed only after verification; your own corrections are stored separately and
are never overwritten by a map data refresh.

| Reference | File | Format | Contents |
| --- | --- | --- | --- |
| **Airfields** | `geodata/airfields.dat` | [apt.dat 1200](https://developer.x-plane.com/article/airport-data-apt-dat-file-format-specification/) (open airport data format) | ICAO/IATA codes, runways (thresholds, width), taxiways and aprons, field elevation; program-specific data in `1302 ma_*` fields |
| **Landmarks** | `geodata/monuments.geojson` | GeoJSON with OpenStreetMap [Simple 3D Buildings](https://wiki.openstreetmap.org/wiki/Simple_3D_Buildings) properties (opens in QGIS / geojson.io) | Monuments, churches, stations: location, height, building parts with roof shapes and colours, or a link to a 3D model in `models3d` |

- **Airfields** (Map → Airfields): a table of all airfields known from the reference and from downloaded map data,
  sorted alphabetically, with rename, runway length correction and a scheme view; copy to clipboard. Runways from the
  reference are drawn in the camera and the terrain even where map data lacks them. Airfields you have flight-checked
  and corrected yourself (in `airfields.json` next to the program) keep your geometry.
- **Landmarks** (Map → Landmarks): grouped by city, with kind, height, how each landmark is drawn (OSM building parts,
  own 3D model or a simplified figure) and its data source; double-click shows the landmark as the camera does.
  Your own additions go into `monuments.json` next to the program.

## Installation (ready-made `.exe`)

You don't need Python to use the program — a portable Windows build is published on the
**[Releases](https://github.com/Mission-analyzer/Mission-analyzer-python/releases)** page.

1. Download the latest `.zip` archive.
2. Unpack it into any folder — a USB stick works fine: the app is portable and keeps its settings and data next to
   the `.exe`.
3. Run the `.exe` inside the unpacked folder.

**Windows will show a warning — this is normal, not a virus signal.** The app isn't signed with a commercial
certificate, so on first launch you'll see **"Windows protected your PC"** (SmartScreen). Click **"More info"**, then
**"Run anyway"**.

## Map data and offline use

All map data is downloaded once and stored next to the program; after that the program works without internet.

| Layer | Used for | Source | Licence / attribution |
|---|---|---|---|
| Satellite and map tiles | maps, nose camera, thermal imager | Google, OpenStreetMap | provider terms; © OpenStreetMap contributors |
| Terrain | altitude analysis, camera, collisions | NASA SRTM via `terrain.ardupilot.org` | public domain |
| Land cover (10 m) | thermal imager, impact location | ESA WorldCover 2021 | CC BY 4.0 |
| Forest canopy height (~10 m) | AGL over forest, collisions, camera | ETH Global Canopy Height 2020 (Lang et al., ETH Zürich) | CC BY 4.0 |
| Buildings (footprints, height, floors) | collisions, camera, thermal imager | Overture Maps (OpenStreetMap, Esri, Google, Microsoft) | © OpenStreetMap contributors, Overture Maps Foundation, ODbL |
| Roads, railways, bridges, water, stadiums | camera (landmarks), ground model, collisions with bridges | Overture Maps (from OpenStreetMap); Overpass as fallback | © OpenStreetMap contributors, Overture Maps Foundation, ODbL |
| Settlements, power lines, obstacles | route analysis and optimization, camera, collisions | OpenStreetMap (Overpass API, Overture) | © OpenStreetMap contributors, ODbL |
| Landmark building parts | camera, collisions | OpenStreetMap (Overpass API) | © OpenStreetMap contributors, ODbL |
| Airfield and landmark data | references | OpenStreetMap, OurAirports, Wikipedia and other cited sources | per record, see the source field |
| Occupied territories | mission map overlay | deepstatemap.live | source terms |

Notes:
- Forest heights are the 2020 state; buildings without a height in the source get a height estimated from the
  footprint area (marked "assumed" in collision messages).
- The folder layout is fixed: `srtm`, `map_cache` (obstacles, buildings, forest, land cover, roads, airfields) and
  `models3d` live next to the program. Copy them together with the program to keep everything offline.

### SRTM tiles

Terrain tiles are **not stored in the repository** (gigabytes of binary data). The program downloads a tile from the
public `terrain.ardupilot.org` mirror (the one Mission Planner uses) when it is first needed and keeps it in the
`srtm` folder next to the program.

### 3D models

Put each model in its own folder inside `models3d` (`.glb` / `.gltf` / `.obj` with its materials). Scene objects
(Configuration) and landmarks reference a model by its folder name. Models are simplified automatically to keep the
camera fast.

## Localization

Ukrainian and English. Russian is deliberately not supported.

## License

*License: MIT — © 2026 Sergey Gorbachevsky*

The landmark reference `geodata/monuments.geojson` contains data © OpenStreetMap contributors and is made available
under the [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
