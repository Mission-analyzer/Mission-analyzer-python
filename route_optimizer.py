"""
route_optimizer.py -- побудова обходу населених пунктів методом графа
дотичних (tangent visibility graph) навколо кругових перешкод, з
перевіркою паливного бюджету.

=== КРИТЕРІЙ ОПТИМАЛЬНОСТІ ===

Мінімізується ЗАГАЛЬНА ДОВЖИНА МАРШРУТУ. Населені пункти -- НЕ доданок
цільової функції (не "сума відстаней до НП", не "сума квадратів") --
вони формують ЗАБОРОНЕНІ ЗОНИ (тверде обмеження): жодна точка шляху не
може опинитись ближче за threshold_km до жодного НП. Серед УСІХ шляхів,
що задовольняють цю умову, обирається НАЙКОРОТШИЙ. Наближення до НП
понад поріг НІЯК не штрафується -- 1.5х порогу і 10х порогу еквівалентні
з точки зору цільової функції, важлива лише сама допустимість.

Паливо -- окреме, ПОСЛІДОВНЕ тверде обмеження (не частина цільової
функції побудови шляху): спочатку будується найкоротший геометрично
допустимий маршрут, ПОТІМ перевіряється, чи вкладається він у паливний
бюджет. Якщо ні -- скорочення маршруту НЕ входить у завдання цього
модуля (це вже інша задача -- послабити поріг обходу, чи відмовитись
від частини місії -- рішення за оператором, не автоматичне).

=== МЕТОД: ГРАФ ДОТИЧНИХ (TANGENT VISIBILITY GRAPH) ===

Класична задача обчислювальної геометрії -- найкоротший шлях серед
кругових перешкод (Rohnert 1986; численні сучасні роботи, зокрема
"Shortest Paths for Disc Obstacles" та ін.). Кожен НП -- коло радіусом
threshold_km. Будується граф:
  - вузли: точка старту, точка фінішу, точки дотику на кожному колі
  - ребра: прямі лінії, дотичні до кіл, які вони зачіпають (+ дуги
    вздовж кола, якщо потрібно обійти частину його межі)
Дейкстра на цьому графі дає ГЛОБАЛЬНО оптимальний обхід УСІХ перешкод
одразу -- саме тому "обійшли один НП, зненацька виник інший" НЕ може
статись: усі релевантні перешкоди вже в графі з самого початку.

Обробка ПО РЕБРАХ (як пропонував користувач): для кожного проблемного
ребра маршруту -- окремий виклик optimize_leg() із НП, зібраними в
достатньому радіусі навколо цього ребра (не лише вже знайдені
порушення для прямої лінії -- обхідний шлях може підійти близько і до
НП, що раніше не заважав).

Зона посадки (останні N ребер) -- ВИКЛЮЧАЄТЬСЯ з оптимізації свідомо:
приліт до злітно-посадкової смуги біля НП часто неминучий.

=== ПАЛИВНИЙ БЮДЖЕТ ===

Стандарт ICAO Annex 6 (contingency fuel): резерв = max(5% від палива
на маршрут, 5 хвилин польоту на крейсерській витраті). Користувач
задає ОДИН РАЗ перед оптимізацією: ємність бака (л) і середню витрату
на крейсерській швидкості (л/год чи л/км) -- резерв рахується
автоматично, окремо не питається.

=== МАЙБУТНІЙ РЕЖИМ: ПОБУДОВА З ДВОХ ТОЧОК (takeoff/land) ===

Поточна версія ЛИШЕ покращує вже готову місію (obходить проблемні
ребра наявного маршруту). У перспективі планується режим, де
користувач задає тільки точку зльоту й точку посадки, а весь маршрут
між ними будується з нуля.

Це НЕ вимагає окремого модуля чи іншої логіки: технічно це той самий
optimize_leg() -- просто ОДНЕ "ребро" (пряма лінія takeoff->land) на
ВЕСЬ маршрут, замість багатьох коротких ребер наявної місії. build_
tangent_graph() і shortest_path_around_obstacles() однаково коректно
працюють і для короткого відрізка в кілька км, і для прямої на всю
довжину маршруту -- єдина відмінність лише в тому, ЗВІДКИ беруться
wp1/wp2 (з наявної місії, чи напряму від користувача). Тому цю
декларацію фіксуємо вже зараз, щоб RouteOptimizationResult/
LegOptimizationResult не довелось переробляти пізніше під новий режим
-- обидва вже описують "ребро" абстрактно (просто пара точок),
незалежно від того, звідки вони взялись.

=== ЛІМІТ ТОЧОК МІСІЇ ===

MAX_MISSION_WAYPOINTS = 255. Офіційна документація ArduPilot дає ~650
елементів місії як реальний максимум (сучасні плати, Cube+ включно) --
255 узятий як РОБОЧИЙ ліміт з запасом: 650 це межа для ВСІХ елементів
разом (home, зліт, посадка, DO_-команди), а не тільки для точок обходу,
які додає цей модуль понад уже наявні. Перевищення ліміту НЕ зупиняє
розрахунок -- позначається прапорцем у результаті, рішення що робити
далі лишається за оператором."""

from __future__ import annotations

import math
from dataclasses import dataclass

from geo import haversine_m


# Реальний ліміт ArduPilot (сучасні плати, включно з Cube+) -- ~650
# елементів місії в EEPROM/flash (офіційна документація ArduPilot,
# однакова для Plane/Copter/Rover/Sub). Беремо 255 як РОБОЧИЙ ліміт з
# запасом -- 650 це МАКСИМУМ для ВСІХ елементів місії разом (home,
# зліт, посадка, DO_-команди типу DO_CHANGE_SPEED/DO_SET_SERVO, самі
# NAV-точки), а не тільки для точок обходу НП, які додає цей модуль.
# 255 лишає суттєвий запас на решту вмісту місії й додатково збігається
# з природною межею uint8 (деякі протокольні поля MAVLink, historically
# сумісні з молодшими платами -- безпечний орієнтир навіть якщо
# конкретна місія піде на менш потужну плату, ніж Cube+).
MAX_MISSION_WAYPOINTS = 255


# ============================================================
# Локальна декартова проєкція (та сама рівнокутна апроксимація, що й
# populated_areas.py _point_to_segment_m -- узгоджено з рештою проєкту,
# похибка нехтовно мала для відстаней у одиниці/десятки км).
# ============================================================

def _project(lat: float, lon: float, ref_lat: float) -> tuple[float, float]:
    """(lat, lon) -> (x, y) метри в локальній системі з опорною широтою."""
    mlat = 111320.0
    mlon = 111320.0 * math.cos(math.radians(ref_lat))
    return (lon * mlon, lat * mlat)


def _unproject(x: float, y: float, ref_lat: float) -> tuple[float, float]:
    """(x, y) метри -> (lat, lon), обернене до _project()."""
    mlat = 111320.0
    mlon = 111320.0 * math.cos(math.radians(ref_lat))
    return (y / mlat, x / mlon)


# ============================================================
# Дані
# ============================================================

@dataclass
class ObstacleCircle:
    """Населений пункт як кругова перешкода для планування шляху."""
    name: str
    lat: float
    lon: float
    radius_km: float  # = порогова відстань (threshold_km), однакова для всіх НП поки що


@dataclass
class FuelBudget:
    """Паливні параметри, які користувач вводить ОДИН РАЗ перед
    запуском оптимізації всього маршруту."""
    tank_capacity_l: float       # ємність бака, літри
    cruise_consumption_lph: float  # витрата на крейсерській швидкості, л/год
    cruise_speed_kmh: float      # крейсерська швидкість, км/год -- потрібна,
                                  # щоб перевести довжину маршруту (км) у час
                                  # польоту (год), а час -- у витрачене паливо


@dataclass
class FuelCheckResult:
    """Результат перевірки паливного бюджету для ЗАГАЛЬНОЇ довжини
    маршруту (після оптимізації, не для окремого ребра)."""
    trip_fuel_l: float       # паливо на сам маршрут (без резерву)
    reserve_l: float         # ICAO contingency (5% чи 5 хв, що більше)
    required_total_l: float  # trip_fuel_l + reserve_l
    tank_capacity_l: float
    feasible: bool           # required_total_l <= tank_capacity_l
    margin_l: float          # tank_capacity_l - required_total_l (від'ємне якщо не влазить)


@dataclass
class TurnRadiusCheck:
    """Перевірка ФІЗИЧНОЇ можливості літака виконати поворот радіусом
    threshold_km на заданій швидкості з заданим максимальним креном.

    Дуга обходу (навколо кожного НП) має радіус РІВНО threshold_km --
    геометрично правильний обхід ще не означає, що літак може його
    ФІЗИЧНО пролетіти. Формула для координованого повороту:
        R_min = V² / (g * tan(крен))
    Якщо R_min > threshold_km -- літак НЕ ЗМОЖЕ втримати таке щільне
    коло на цій швидкості з цим креном. На практиці це означає, що
    автопілот "зріже кут" повороту -- і реальна траєкторія опиниться
    БЛИЖЧЕ до НП, ніж запланований поріг, саме там, де ми найбільше
    намагались цього уникнути. Це причина, чому перевірка ОКРЕМА від
    самої геометричної побудови обходу -- геометрія та фізика можуть
    давати різні відповіді, і про розбіжність треба знати ДО польоту."""
    airspeed_ms: float
    roll_limit_deg: float
    threshold_m: float
    min_turn_radius_m: float
    feasible: bool     # min_turn_radius_m <= threshold_m
    margin_m: float    # threshold_m - min_turn_radius_m (від'ємне якщо неможливо)


def compute_turn_radius_check(
    airspeed_ms: float, roll_limit_deg: float, threshold_km: float,
) -> TurnRadiusCheck:
    """R_min = V²/(g·tan(крен)) -- стандартна формула координованого
    повороту літака (той самий фізичний принцип, що й у авіації
    загалом, не специфічний для ArduPilot). g=9.81 м/с² -- прискорення
    вільного падіння, стала."""
    g = 9.81
    roll_rad = math.radians(roll_limit_deg)
    min_r = airspeed_ms ** 2 / (g * math.tan(roll_rad))
    threshold_m = threshold_km * 1000.0
    return TurnRadiusCheck(
        airspeed_ms=airspeed_ms, roll_limit_deg=roll_limit_deg,
        threshold_m=threshold_m, min_turn_radius_m=min_r,
        feasible=min_r <= threshold_m, margin_m=threshold_m - min_r,
    )


@dataclass
class LegOptimizationResult:
    """Результат обходу ОДНОГО ребра маршруту."""
    leg_index: int
    original_distance_km: float
    new_distance_km: float           # може бути == original, якщо обхід не потрібен
    inserted_waypoints: list[tuple[float, float]]  # (lat, lon) точок обходу, В ПОРЯДКУ вставки -- БЕЗ висоти (див. optimize_leg)
    obstacles_considered: list[ObstacleCircle]      # які НП враховані при побудові графа для цього ребра
    failed: bool = False             # True якщо shortest_path_around_obstacles не знайшов шляху
    failure_reason: str | None = None  # текст помилки, якщо failed=True (для звіту користувачу)


@dataclass
class RouteOptimizationResult:
    """Результат оптимізації ВСЬОГО маршруту (всі ребра, крім зони посадки).

    original_route/new_route -- ПОВНІ послідовності (lat, lon) точок,
    готові напряму для відображення на карті (UI: "Було/Стало" -- ОДНА
    карта, ДВА маршрути одна поверх одної, різними кольорами -- не дві
    окремі карти). Дублюють інформацію з legs[].inserted_waypoints
    (яка лишається для детального звіту по ребрах), але зібрану в
    готовий для малювання вигляд -- щоб UI не мусив сам склеювати
    original_wps + вставки в правильному порядку."""
    legs: list[LegOptimizationResult]
    original_route: list[tuple[float, float]]  # весь маршрут ДО оптимізації
    new_route: list[tuple[float, float]]        # весь маршрут ПІСЛЯ (з обходами)
    total_original_distance_km: float
    total_new_distance_km: float
    added_distance_km: float          # total_new - total_original
    fuel_check: FuelCheckResult | None  # None якщо FuelBudget не задано
    turn_check: TurnRadiusCheck | None  # None якщо roll_limit_deg не задано
    total_waypoints: int              # оригінальні nav_wps + УСІ вставлені точки обходу
    waypoint_limit_exceeded: bool     # total_waypoints > MAX_MISSION_WAYPOINTS


# ============================================================
# Геометрія графа дотичних
# ============================================================

def tangent_points_from_external_point(
    px: float, py: float, cx: float, cy: float, r: float,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Дві точки дотику від зовнішньої точки P до кола (cx, cy, r).
    Повертає None, якщо P знаходиться ВСЕРЕДИНІ кола (дотичних не існує
    -- у нашому контексті означало б, що сам вейпоінт лежить у забороненій
    зоні, це окремий випадок, який має оброблятись РАНІШЕ, не тут).

    Працює в ЛОКАЛЬНИХ декартових координатах (метри, вже спроєктовані
    з lat/lon -- та сама апроксимація, що й у populated_areas.py
    _point_to_segment_m, узгоджено з рештою проєкту).

    Геометрія: у прямокутному трикутнику P-T-C (T -- точка дотику, кут
    при T = 90° бо радіус перпендикулярний дотичній) гіпотенуза PC = d,
    прилеглий до кута при C катет CT = r, тому cos(кут при C) = r/d.
    Дві точки дотику -- відхилення від напрямку C->P на цей кут в обидва боки."""
    dx, dy = px - cx, py - cy
    d = math.hypot(dx, dy)
    if d <= r:
        return None
    theta = math.atan2(dy, dx)
    alpha = math.acos(r / d)
    t1 = (cx + r * math.cos(theta + alpha), cy + r * math.sin(theta + alpha))
    t2 = (cx + r * math.cos(theta - alpha), cy + r * math.sin(theta - alpha))
    return (t1, t2)


def external_tangent_lines(
    c1x: float, c1y: float, r1: float,
    c2x: float, c2y: float, r2: float,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Спільні зовнішні дотичні між двома колами (для ребер графа між
    ДВОМА перешкодами, коли шлях проходить повз обидва). До 2 ліній
    (можуть збігатись чи не існувати при повному перекритті кіл --
    у нашому контексті кола НЕ повинні перекриватись за замовчуванням,
    оскільки населені пункти зазвичай на відстані один від одного
    більшій за 2×threshold_km; якщо перекриваються -- окремий випадок,
    обробити в build_tangent_graph, не тут).

    r1=0 чи r2=0 коректно повертає дотичні від точки до кола (start/end
    трактуються в build_tangent_graph як "кола нульового радіуса" --
    той самий код працює однаково для обох випадків).

    Виведення: у системі координат де C1 в початку, C2 на осі X (відстань
    D), пряма лінія ax+by=c з одиничною нормаллю (a,b) торкається обох
    кіл з ОДНАКОВОЮ стороною (зовнішня дотична, на відміну від
    внутрішньої/перехресної): -c=r1, a*D-c=r2 => a=(r2-r1)/D,
    b=±sqrt(1-a²). Точки дотику -- проекції центрів на цю пряму.
    Потім результат повертається в оригінальну (нерозвернуту) систему."""
    dx, dy = c2x - c1x, c2y - c1y
    d = math.hypot(dx, dy)
    if d < 1e-9:
        return []  # центри збігаються -- дотичних не існує
    a = (r2 - r1) / d
    if abs(a) >= 1.0:
        return []  # одне коло повністю всередині іншого -- зовнішньої дотичної немає
    b_mag = math.sqrt(1.0 - a * a)
    phi = math.atan2(dy, dx)
    cos_p, sin_p = math.cos(phi), math.sin(phi)

    lines = []
    for sign in (1.0, -1.0):
        b = b_mag * sign
        # точки дотику в РОЗВЕРНУТІЙ системі (C1 у початку, C2 на осі X)
        t1x_r, t1y_r = -r1 * a, -r1 * b
        t2x_r, t2y_r = d - r2 * a, -r2 * b
        # обертаємо назад (стандартна матриця повороту на кут phi) і зсуваємо на C1
        t1x = c1x + t1x_r * cos_p - t1y_r * sin_p
        t1y = c1y + t1x_r * sin_p + t1y_r * cos_p
        t2x = c1x + t2x_r * cos_p - t2y_r * sin_p
        t2y = c1y + t2x_r * sin_p + t2y_r * cos_p
        lines.append(((t1x, t1y), (t2x, t2y)))
    return lines


def segment_intersects_circle(
    ax: float, ay: float, bx: float, by: float, cx: float, cy: float, r: float,
) -> bool:
    """Чи перетинає відрізок A-B коло (cx, cy, r). Використовується при
    побудові графа -- ребро (пряма лінія між двома вузлами графа)
    ДОПУСТИМЕ лише якщо не перетинає ЖОДНОГО кола (крім тих двох, яким
    сама дотична належить, де вона торкається, а не перетинає).

    "Перетинає" означає СТРОГО заходить усередину (відстань < r), не
    просто дотикається (відстань == r) -- інакше власні дотичні лінії
    ребра завжди позначались би як "перетин" самого свого кола через
    похибку округлення. eps -- невеликий допуск під цю похибку."""
    dx, dy = bx - ax, by - ay
    seg_len2 = dx * dx + dy * dy
    if seg_len2 < 1e-12:
        d = math.hypot(cx - ax, cy - ay)
    else:
        t = ((cx - ax) * dx + (cy - ay) * dy) / seg_len2
        t = max(0.0, min(1.0, t))
        px, py = ax + t * dx, ay + t * dy
        d = math.hypot(cx - px, cy - py)
    eps = 1e-6
    return d < r - eps


def build_tangent_graph(
    start: tuple[float, float], end: tuple[float, float],
    obstacles: list[ObstacleCircle],
) -> dict:
    """Будує граф дотичних: вузли (старт, фініш, точки дотику на
    кожному колі), ребра (дотичні лінії + дуги кіл де потрібно),
    відфільтровані segment_intersects_circle() від недопустимих
    (що перетинають ІНШІ перешкоди).

    Повертає структуру графа, придатну для Дейкстри:
    {"nodes": {node_id: (x, y)}, "edges": {node_id: [(сусід, вага), ...]},
     "ref_lat": float} -- координати вузлів у ЛОКАЛЬНИХ метрах (не
    lat/lon), ref_lat потрібна для перетворення назад у shortest_path_
    around_obstacles.

    ВІДОМЕ ОБМЕЖЕННЯ: якщо start чи end САМІ лежать УСЕРЕДИНІ якогось
    obstacles (tangent_points_from_external_point повертає None для
    цієї пари) -- ця перешкода просто пропускається як недосяжна з
    відповідного кінця, БЕЗ явної помилки. Це означає, що сам вейпоінт
    (не лінія між вейпоінтами) порушує поріг -- інша задача, має
    оброблятись раніше (перевірка вейпоінтів окремо від відрізків),
    тут лише не падає."""
    all_lats = [start[0], end[0]] + [o.lat for o in obstacles]
    ref_lat = sum(all_lats) / len(all_lats)

    sx, sy = _project(start[0], start[1], ref_lat)
    ex, ey = _project(end[0], end[1], ref_lat)
    obs_local = [
        (_project(o.lat, o.lon, ref_lat), o.radius_km * 1000.0, o)
        for o in obstacles
    ]

    nodes: dict[str, tuple[float, float]] = {"__start__": (sx, sy), "__end__": (ex, ey)}
    points_on_circle: dict[int, list[str]] = {}
    edges: dict[str, list[tuple[str, float]]] = {}
    counter = [0]

    def add_node(x: float, y: float, owner: int) -> str:
        nid = f"obs{owner}_{counter[0]}"
        counter[0] += 1
        nodes[nid] = (x, y)
        points_on_circle.setdefault(owner, []).append(nid)
        return nid

    def add_edge(a: str, b: str, w: float) -> None:
        edges.setdefault(a, []).append((b, w))
        edges.setdefault(b, []).append((a, w))

    def blocked_by_other_circles(p1: tuple[float, float], p2: tuple[float, float],
                                  skip_indices: set[int]) -> bool:
        for idx, ((ox, oy), orad, _obs) in enumerate(obs_local):
            if idx in skip_indices:
                continue
            if segment_intersects_circle(p1[0], p1[1], p2[0], p2[1], ox, oy, orad):
                return True
        return False

    # 1. пряма start->end -- якщо жодна перешкода не заважає (тривіальний
    # випадок, коли обхід узагалі не потрібен -- граф все одно будується
    # повністю, Дейкстра сама обере пряму лінію як найкоротшу якщо вона
    # допустима)
    if not blocked_by_other_circles((sx, sy), (ex, ey), set()):
        add_edge("__start__", "__end__", math.hypot(ex - sx, ey - sy))

    # 2. start/end -> дотичні до кожної перешкоди
    for idx, ((ox, oy), orad, _obs) in enumerate(obs_local):
        for base_id, (bx, by) in (("__start__", (sx, sy)), ("__end__", (ex, ey))):
            tp = tangent_points_from_external_point(bx, by, ox, oy, orad)
            if tp is None:
                continue  # base-точка всередині перешкоди -- див. docstring
            for t in tp:
                if blocked_by_other_circles((bx, by), t, {idx}):
                    continue
                nid = add_node(t[0], t[1], idx)
                add_edge(base_id, nid, math.hypot(t[0] - bx, t[1] - by))

    # 3. перешкода <-> перешкода (зовнішні дотичні між кожною парою)
    n = len(obs_local)
    for i in range(n):
        (xi, yi), ri, _oi = obs_local[i]
        for j in range(i + 1, n):
            (xj, yj), rj, _oj = obs_local[j]
            for (t1, t2) in external_tangent_lines(xi, yi, ri, xj, yj, rj):
                if blocked_by_other_circles(t1, t2, {i, j}):
                    continue
                nid1 = add_node(t1[0], t1[1], i)
                nid2 = add_node(t2[0], t2[1], j)
                add_edge(nid1, nid2, math.hypot(t2[0] - t1[0], t2[1] - t1[1]))

    # 4. дуги навколо кожної перешкоди -- з'єднують УСІ точки дотику на
    # ній послідовно за кутом, дозволяючи "обійти частину кола" переходом
    # з одного дотичного вузла на сусідній вздовж межі (довжина дуги = r*кут)
    for idx, ((ox, oy), orad, _obs) in enumerate(obs_local):
        pts = points_on_circle.get(idx, [])
        if len(pts) < 2:
            continue
        angled = sorted(pts, key=lambda nid: math.atan2(nodes[nid][1] - oy, nodes[nid][0] - ox))
        m = len(angled)
        for k in range(m):
            a, b = angled[k], angled[(k + 1) % m]
            ang_a = math.atan2(nodes[a][1] - oy, nodes[a][0] - ox)
            ang_b = math.atan2(nodes[b][1] - oy, nodes[b][0] - ox)
            dtheta = (ang_b - ang_a) % (2 * math.pi)
            add_edge(a, b, orad * dtheta)

    # node_owner: якому колу належить вузол (None для start/end) --
    # потрібно в shortest_path_around_obstacles, щоб при відновленні
    # шляху розпізнати "це ребро було ДУГОЮ" (обидва сусідні вузли на
    # ОДНОМУ колі) і розбити дугу на проміжні точки, а не з'єднувати
    # їх прямою лінією, що ріже навпростець крізь заборонену зону.
    node_owner: dict[str, int | None] = {"__start__": None, "__end__": None}
    for owner_idx, pts in points_on_circle.items():
        for nid in pts:
            node_owner[nid] = owner_idx

    circles_local = [((ox, oy), orad) for (ox, oy), orad, _obs in obs_local]

    return {
        "nodes": nodes, "edges": edges, "ref_lat": ref_lat,
        "node_owner": node_owner, "circles_local": circles_local,
    }


def shortest_path_around_obstacles(
    start: tuple[float, float], end: tuple[float, float],
    obstacles: list[ObstacleCircle],
) -> list[tuple[float, float]]:
    """Дейкстра на графі дотичних. Повертає ПОВНИЙ шлях (список
    (lat, lon), включно зі старт/фініш) -- найкоротший серед усіх, що
    не заходять у ЖОДНЕ коло-перешкоду.

    Якщо obstacles порожній -- повертає [start, end] (пряма лінія,
    оптимізація не потрібна)."""
    if not obstacles:
        return [start, end]

    graph = build_tangent_graph(start, end, obstacles)
    nodes, edges, ref_lat = graph["nodes"], graph["edges"], graph["ref_lat"]
    node_owner, circles_local = graph["node_owner"], graph["circles_local"]

    import heapq
    dist = {nid: float("inf") for nid in nodes}
    prev: dict[str, str | None] = {nid: None for nid in nodes}
    dist["__start__"] = 0.0
    pq = [(0.0, "__start__")]
    visited = set()

    while pq:
        d, u = heapq.heappop(pq)
        if u in visited:
            continue
        visited.add(u)
        if u == "__end__":
            break
        for v, w in edges.get(u, []):
            nd = d + w
            if nd < dist[v]:
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))

    if dist["__end__"] == float("inf"):
        # немає допустимого шляху взагалі (напр. перешкоди повністю
        # оточують старт чи фініш) -- чесно повідомляємо про це через
        # виняток, а не мовчки повертаємо пряму лінію крізь заборонену
        # зону (це була б тиха, небезпечна відмова)
        raise RuntimeError(
            "Не знайдено допустимого шляху навколо перешкод "
            "(можливо, старт чи фініш оточені з усіх боків)"
        )

    # відновлюємо шлях від __end__ до __start__ через prev, розвертаємо
    path_ids = []
    cur: str | None = "__end__"
    while cur is not None:
        path_ids.append(cur)
        cur = prev[cur]
    path_ids.reverse()

    # будуємо ФІНАЛЬНИЙ список точок у ЛОКАЛЬНИХ метрах -- для кожної
    # пари сусідніх вузлів перевіряємо, чи це була ДУГА (обидва вузли
    # належать ОДНОМУ й тому самому колу) -- якщо так, НЕ з'єднуємо їх
    # прямою лінією (вона ріже крізь заборонену зону!), а вставляємо
    # проміжні точки вздовж дуги з кроком ~10° (похибка хорди/sagitta
    # для типових порогів у сотні метрів-кілометри -- одиниці метрів,
    # нехтовно мала порівняно з самим порогом безпеки).
    MAX_ARC_STEP_RAD = math.radians(10.0)
    points_xy: list[tuple[float, float]] = [nodes[path_ids[0]]]

    for i in range(len(path_ids) - 1):
        a_id, b_id = path_ids[i], path_ids[i + 1]
        owner_a, owner_b = node_owner[a_id], node_owner[b_id]
        ax, ay = nodes[a_id]
        bx, by = nodes[b_id]

        if owner_a is not None and owner_a == owner_b:
            (ocx, ocy), orad = circles_local[owner_a]
            ang_a = math.atan2(ay - ocy, ax - ocx)
            ang_b = math.atan2(by - ocy, bx - ocx)
            # ЗНАКОВИЙ найкоротший кут (від -π до +π) -- ребро графа
            # завжди представляє КОРОТШУ дугу між цими двома точками
            # (саме так вага ребра рахувалась при побудові графа), тому
            # тут теж беремо коротший напрямок, а не завжди "за
            # годинниковою" -- інакше можна помилково піти дугою майже
            # на все коло замість короткого відрізка між сусідніми
            # точками дотику.
            dtheta = (ang_b - ang_a) % (2 * math.pi)
            if dtheta > math.pi:
                dtheta -= 2 * math.pi
            n_steps = max(1, math.ceil(abs(dtheta) / MAX_ARC_STEP_RAD))
            # ГЕОМЕТРИЧНА КОМПЕНСАЦІЯ SAGITTA: точки РІВНО на колі (радіус
            # orad), з'єднані прямими хордами (бо ArduPilot літає прямими
            # лініями між вейпоінтами, не дугами) -- хорда МІЖ двома
            # точками на межі кола завжди проходить ТРОХИ ВСЕРЕДИНІ кола
            # (sagitta > 0 для будь-якого ненульового кроку -- базова
            # геометрія, не помилка). Компенсуємо: розміщуємо підточки НЕ
            # на orad, а трохи ЗОВНІ (orad / cos(крок/2)) -- тоді сама
            # хорда торкається САМЕ orad, а не заходить глибше. Перевірено
            # окремим розрахунком: апофема хорди = r_inflated*cos(крок/2)
            # = orad точно.
            step_rad = dtheta / n_steps
            r_inflated = orad / math.cos(step_rad / 2.0)
            # ПЕРЕПИСУЄМО щойно додану попередню точку (початок цієї
            # дуги, ang_a) тим самим роздутим радіусом -- інакше вона
            # лишається на ТОЧНОМУ orad (з дотичної лінії до неї), а
            # перший підвідрізок дуги (від неї до першої підточки на
            # r_inflated) знову має неузгоджені радіуси на кінцях і
            # хорда так само заходить углиб. Узгоджений радіус по ВСІЙ
            # дузі -- єдиний надійний спосіб гарантувати жодного відрізка
            # з порушенням.
            points_xy[-1] = (ocx + r_inflated * math.cos(ang_a), ocy + r_inflated * math.sin(ang_a))
            for k in range(1, n_steps + 1):
                ang = ang_a + dtheta * k / n_steps
                points_xy.append((ocx + r_inflated * math.cos(ang), ocy + r_inflated * math.sin(ang)))
        else:
            points_xy.append((bx, by))

    return [_unproject(x, y, ref_lat) for x, y in points_xy]


# ============================================================
# Оптимізація ребра / маршруту
# ============================================================

def optimize_leg(
    wp1_lat: float, wp1_lon: float, wp2_lat: float, wp2_lon: float,
    nearby_settlements: list[dict], threshold_km: float,
    leg_index: int,
) -> LegOptimizationResult:
    """Обхід ОДНОГО ребра. nearby_settlements -- ВЖЕ відфільтрований
    список (той самий формат, що повертає populated_areas.fetch_
    settlements) у достатньому радіусі навколо цього ребра -- не лише
    settlements, що вже порушували поріг для прямої лінії (обхідний
    шлях може наблизитись і до інших).

    ВИСОТА вставлених точок НЕ рахується тут -- цей модуль свідомо не
    залежить від SRTM/analyzer (чиста геометрія). Висоту призначає
    ВИКЛИКАЮЧИЙ код (analysis_page.py, де є доступ до рельєфу) за
    формулою: висота_рельєфу(нова_точка) + середнє_відносних_висот
    (wp1, wp2) -- НЕ лінійна інтерполяція абсолютної висоти, оскільки
    рельєф під маршрутом може суттєво відрізнятись від прямої лінії
    між висотами кінців (наприклад, ландшафт горбистий, а політ
    відбувається на приблизно постійній висоті НАД рельєфом).

    ВИЯВЛЕНЕ НА ТЕСТАХ ОБМЕЖЕННЯ: якщо саме ребро КОРОТШЕ за ~2×
    threshold_km, обхід може бути ГЕОМЕТРИЧНО НЕМОЖЛИВИМ навіть коли
    формально settlement не заходить УСЕРЕДИНУ жодної кінцевої точки --
    будь-яка точка на такому короткому відрізку просто фізично не може
    бути одночасно далі порога від ОБОХ кінців. У такому разі
    shortest_path_around_obstacles піднімає RuntimeError -- виклик має
    бути готовий це зловити (на боці UI: показати повідомлення "поріг
    задовеликий для цього короткого ребра", не падати мовчки)."""
    obstacles = [
        ObstacleCircle(name=s["name"], lat=s["lat"], lon=s["lon"], radius_km=threshold_km)
        for s in nearby_settlements
    ]

    start = (wp1_lat, wp1_lon)
    end = (wp2_lat, wp2_lon)
    original_distance_m = haversine_m(wp1_lat, wp1_lon, wp2_lat, wp2_lon)

    path = shortest_path_around_obstacles(start, end, obstacles)

    new_distance_m = sum(
        haversine_m(path[i][0], path[i][1], path[i + 1][0], path[i + 1][1])
        for i in range(len(path) - 1)
    )

    # inserted_waypoints -- лише ВСТАВЛЕНІ точки, БЕЗ самих start/end
    # (це оригінальні вейпоінти, вони й так лишаються на своїх місцях
    # у наявній місії -- сенс мають тільки НОВІ точки посередині).
    # (lat, lon) БЕЗ висоти -- див. docstring вище.
    inserted = path[1:-1]

    return LegOptimizationResult(
        leg_index=leg_index,
        original_distance_km=original_distance_m / 1000.0,
        new_distance_km=new_distance_m / 1000.0,
        inserted_waypoints=inserted,
        obstacles_considered=obstacles,
    )


def optimize_route(
    nav_wps: list, settlements_fetcher, threshold_km: float,
    exclude_last_n_legs: int = 0,
    fuel_budget: FuelBudget | None = None,
    roll_limit_deg: float | None = None,
    progress_callback=None,
) -> RouteOptimizationResult:
    """Оптимізує ВЕСЬ маршрут, ребро за ребром (послідовне покращення,
    як пропонував користувач) -- ЩО НЕ ОЗНАЧАЄ "обійшли один НП,
    перевірили результат, знайшли інший, повторили": КОЖНЕ ребро
    обробляється ОДНИМ викликом build_tangent_graph() з УСІМА
    релевантними НП одразу (немає циклу "виправ-перевір-виправ" в
    межах одного ребра).

    exclude_last_n_legs -- скільки останніх ребер (зона посадки) НЕ
    оптимізувати -- приліт біля НП часто неминучий.

    settlements_fetcher -- callable(lat_min, lat_max, lon_min, lon_max)
    -> list[dict], зазвичай populated_areas.fetch_settlements з
    прив'язаними параметрами (не сам fetch_settlements напряму, щоб
    можна було підмінити для тестів без мережі).

    progress_callback -- необов'язковий callable(done: int, total: int,
    leg_result: LegOptimizationResult), викликається ПІСЛЯ обробки
    кожного ребра (включно з виключеними -- вони теж "оброблені",
    просто без обходу). Населені пункти запитуються ОДНИМ мережевим
    зверненням на весь маршрут (не по одному на ребро), але сама
    геометрична обробка (граф дотичних, Дейкстра) для довгого маршруту
    все одно займає час -- без цього колбека користувач не має жодного
    індикатора, що відбувається (виглядає як "зависло", хоча насправді
    рахує). leg_result передається, щоб виклик міг прогресивно
    домальовувати карту по мірі готовності кожного ребра (не чекаючи
    повного завершення розрахунку всього маршруту).

    ЛІМІТ ТОЧОК (MAX_MISSION_WAYPOINTS=255): рахується ПІСЛЯ побудови
    ВСІХ обходів (не переривається на середині маршруту). Якщо
    total_waypoints > 255 -- waypoint_limit_exceeded=True в результаті,
    АЛЕ сам результат все одно повертається повністю (не відкидається
    мовчки) -- рішення, що робити далі (підняти поріг, спростити обхід,
    прийняти ризик і залишити частину ребер без обходу), лишається за
    оператором на боці UI, не приймається автоматично в цьому модулі.

    ГЕОМЕТРИЧНО НЕМОЖЛИВІ РЕБРА: якщо конкретне ребро занадто коротке
    відносно threshold_km (виявлено на тестах -- відрізок коротший за
    ~2×поріг фізично не може мати точку одночасно далі порога від ОБОХ
    кінців) чи перешкоди оточують кінець ребра з усіх боків --
    optimize_leg() підніме RuntimeError ЛИШЕ для ЦЬОГО ребра.
    optimize_route() ловить це ЛОКАЛЬНО (не валить весь розрахунок):
    ребро лишається без змін (пряма лінія), позначається failed=True з
    текстом причини -- решта маршруту оптимізується як зазвичай."""
    import populated_areas as _pa

    n_legs = len(nav_wps) - 1
    n_optimizable = max(0, n_legs - exclude_last_n_legs)

    # запас навколо порога, у межах якого населений пункт з fetch_
    # settlements (обмежений bbox ребра) вважається релевантним для
    # ГРАФА цього ребра -- та сама логіка/множник, що й у analysis_
    # page.py DRAW_RADIUS_MULT (там для відображення на карті, тут для
    # включення в геометрію обходу) -- вузли, помітно далі за поріг,
    # не впливають на найкоротший шлях, лише зайво ускладнюють граф
    NEARBY_MARGIN_MULT = 3.0

    legs_results: list[LegOptimizationResult] = []
    new_route: list[tuple[float, float]] = [(nav_wps[0].lat, nav_wps[0].lon)]
    total_original_m = 0.0
    total_new_m = 0.0

    # ОДИН запит до Overpass на ВЕСЬ маршрут (та сама логіка, що вже
    # надійно працює в "Обліт НП" -- populated_areas.fetch_settlements
    # з межами всього маршруту одразу), А НЕ окремий запит на кожне
    # ребро. Раніше було по одному мережевому зверненню на ребро --
    # для маршруту з 29 ребер це 29 незалежних точок відмови, і на
    # практиці саме тому "Обліт НП" (1 запит) стабільно проходив увесь
    # маршрут, а "Оптимізація" (29 запитів) періодично падала на
    # випадковому ребрі через тимчасове перевантаження Overpass.
    # Подальша фільтрація "які НП релевантні для ЦЬОГО ребра" лишається
    # локальною (без мережі), як і раніше.
    optimizable_wps = nav_wps[:n_optimizable + 1] if n_optimizable > 0 else []
    settlements = []
    if optimizable_wps:
        route_lat_min = min(wp.lat for wp in optimizable_wps)
        route_lat_max = max(wp.lat for wp in optimizable_wps)
        route_lon_min = min(wp.lon for wp in optimizable_wps)
        route_lon_max = max(wp.lon for wp in optimizable_wps)
        settlements = settlements_fetcher(route_lat_min, route_lat_max, route_lon_min, route_lon_max)

    for i in range(n_legs):
        wp1, wp2 = nav_wps[i], nav_wps[i + 1]

        if i < n_optimizable:
            nearby = [
                s for s in settlements
                if _pa._point_to_segment_m(s["lat"], s["lon"], wp1.lat, wp1.lon, wp2.lat, wp2.lon)
                < threshold_km * 1000 * NEARBY_MARGIN_MULT
            ]
            try:
                leg_result = optimize_leg(wp1.lat, wp1.lon, wp2.lat, wp2.lon, nearby, threshold_km, i)
            except RuntimeError as e:
                # геометрично неможливо обійти (напр. ребро закоротке за
                # 2×threshold_km, чи перешкоди оточують кінець ребра) --
                # НЕ валимо ВЕСЬ розрахунок через ОДНЕ проблемне ребро:
                # лишаємо його без змін (пряма лінія як була), позначаємо
                # failed=True для звіту користувачу, і йдемо далі.
                #
                # obstacles_considered = nearby (НЕ порожній список!) --
                # саме ці НП спричинили провал, їх ОБОВ'ЯЗКОВО треба
                # показати в таблиці "Було/Стало" (виявлений реальний
                # баг: порожній список тут повністю ХОВАВ невдалі ребра
                # з таблиці, ніби там взагалі не було жодного НП поруч --
                # оманливо, коли насправді саме там і сталась відмова).
                d_km = haversine_m(wp1.lat, wp1.lon, wp2.lat, wp2.lon) / 1000.0
                failed_obstacles = [
                    ObstacleCircle(name=s["name"], lat=s["lat"], lon=s["lon"], radius_km=threshold_km)
                    for s in nearby
                ]
                leg_result = LegOptimizationResult(
                    leg_index=i, original_distance_km=d_km, new_distance_km=d_km,
                    inserted_waypoints=[], obstacles_considered=failed_obstacles,
                    failed=True, failure_reason=str(e),
                )
        else:
            # виключене ребро (зона посадки) -- пряма лінія без обходу
            d_km = haversine_m(wp1.lat, wp1.lon, wp2.lat, wp2.lon) / 1000.0
            leg_result = LegOptimizationResult(
                leg_index=i, original_distance_km=d_km, new_distance_km=d_km,
                inserted_waypoints=[], obstacles_considered=[],
            )

        legs_results.append(leg_result)
        total_original_m += leg_result.original_distance_km * 1000.0
        total_new_m += leg_result.new_distance_km * 1000.0
        # new_route -- лише (lat, lon) для малювання карти (той самий
        # формат, що й original_route); ПОВНА версія з висотою -- в
        # legs_results[].inserted_waypoints, для майбутнього експорту
        new_route.extend(leg_result.inserted_waypoints)
        new_route.append((wp2.lat, wp2.lon))

        if progress_callback is not None:
            progress_callback(i + 1, n_legs, leg_result)

    original_route = [(wp.lat, wp.lon) for wp in nav_wps]
    total_waypoints = len(nav_wps) + sum(len(lr.inserted_waypoints) for lr in legs_results)

    fuel_check = compute_fuel_check(total_new_m / 1000.0, fuel_budget) if fuel_budget else None

    turn_check = None
    if roll_limit_deg is not None and fuel_budget is not None:
        airspeed_ms = fuel_budget.cruise_speed_kmh / 3.6
        turn_check = compute_turn_radius_check(airspeed_ms, roll_limit_deg, threshold_km)

    return RouteOptimizationResult(
        legs=legs_results,
        original_route=original_route,
        new_route=new_route,
        total_original_distance_km=total_original_m / 1000.0,
        total_new_distance_km=total_new_m / 1000.0,
        added_distance_km=(total_new_m - total_original_m) / 1000.0,
        fuel_check=fuel_check,
        turn_check=turn_check,
        total_waypoints=total_waypoints,
        waypoint_limit_exceeded=total_waypoints > MAX_MISSION_WAYPOINTS,
    )


# ============================================================
# Паливо
# ============================================================

def compute_fuel_check(
    total_distance_km: float, budget: FuelBudget,
) -> FuelCheckResult:
    """ICAO Annex 6 contingency fuel: резерв = max(5% від trip_fuel,
    паливо на 5 хвилин крейсерської витрати). trip_fuel рахується як
    (total_distance_km / cruise_speed_kmh) * cruise_consumption_lph --
    ПРИПУСКАЄМО політ на постійній крейсерській швидкості (немає
    окремого обліку зльоту/посадки/набору висоти в цій версії --
    спрощення, яке варто позначити користувачу явно в звіті)."""
    flight_time_h = total_distance_km / budget.cruise_speed_kmh
    trip_fuel_l = flight_time_h * budget.cruise_consumption_lph

    five_percent_l = trip_fuel_l * 0.05
    five_min_fuel_l = budget.cruise_consumption_lph * (5.0 / 60.0)
    reserve_l = max(five_percent_l, five_min_fuel_l)

    required_total_l = trip_fuel_l + reserve_l
    margin_l = budget.tank_capacity_l - required_total_l

    return FuelCheckResult(
        trip_fuel_l=trip_fuel_l,
        reserve_l=reserve_l,
        required_total_l=required_total_l,
        tank_capacity_l=budget.tank_capacity_l,
        feasible=required_total_l <= budget.tank_capacity_l,
        margin_l=margin_l,
    )
