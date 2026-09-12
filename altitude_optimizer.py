"""
altitude_optimizer.py -- оптимізація ВИСОТНОГО профілю маршруту, який уже
пройшов оптимізацію по координатах (route_optimizer.py). Окремий,
незалежний крок (Варіант B з обговорення: аналітичний розрахунок плавного
переходу, а не пошук/динамічне програмування).

Джерело "чиясь територія" -- ТОЙ САМИЙ шар, що вже використовується на
карті "Місія" (occupied_layer.py, deepstatemap.live), не окремий запит чи
новий формат даних.

Метод:
    1. Знайти РЕБРО маршруту, де точки лежать по РІЗНІ боки полігону
       окупованої території (point-in-polygon на обох кінцях ребра).
    2. Знайти ТОЧНУ геометричну точку перетину цього ребра з межею
       полігону (лінійна інтерполяція вздовж ребра).
    3. Порахувати відстані маневрів по обидва боки точки перетину --
       скільки метрів потрібно на зміну висоти від поточної цільової до
       border_crossing, з урахуванням climb_rate_max_ms/sink_rate_min_ms
       профілю літака та крейсерської швидкості.
    4. Розмістити точку початку/завершення маневру на цій відстані ВІД
       точки перетину, рухаючись назад/вперед уздовж полігону маршруту.
       Якщо поруч (у межах невеликого допуску) уже є вейпоінт --
       коригується його висота замість вставки нової точки (бюджет
       вейпоінтів).
    5. Усі точки маршруту, що лежать в межах однієї зони (між
       переходами), отримують висоту цієї зони.

Наразі -- ОДИН перетин на маршрут (підтверджено користувачем як
достатньо для типового транзитного польоту). Формально не забороняє
кількадобре перетинів, але навмисно НЕ оптимізовано під частий кейс
(немає жодного механізму об'єднання переходів, що впритул один до
одного, як для горизонтального обходу).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from geo import haversine_m


# ============================================================
# Point-in-polygon і пошук перетину
# ============================================================

def point_in_polygon(lat: float, lon: float, polygon: list) -> bool:
    """Ray-casting алгоритм, стандартний, без зовнішніх залежностей.
    polygon -- список кілець [[(lon,lat), ...], ...] (формат
    occupied_layer.extract_polygons -- ring 0 зовнішній контур, решта
    дірки). Точка ВСЕРЕДИНІ, якщо вона всередині зовнішнього контуру
    і НЕ всередині жодної дірки."""
    if not polygon:
        return False
    outer = polygon[0]
    inside = _point_in_ring(lat, lon, outer)
    if not inside:
        return False
    for hole in polygon[1:]:
        if _point_in_ring(lat, lon, hole):
            return False
    return inside


def _point_in_ring(lat: float, lon: float, ring: list) -> bool:
    """Класичний ray-casting для одного кільця [(lon,lat), ...]."""
    n = len(ring)
    if n < 3:
        return False
    inside = False
    x, y = lon, lat
    x1, y1 = ring[0]
    for i in range(1, n + 1):
        x2, y2 = ring[i % n]
        if ((y1 > y) != (y2 > y)) and (y2 != y1):
            x_intersect = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_intersect:
                inside = not inside
        x1, y1 = x2, y2
    return inside


def point_in_any_polygon(lat: float, lon: float, polygons: list) -> bool:
    """True, якщо точка потрапляє в БУДЬ-ЯКИЙ з полігонів списку
    (occupied_layer може повернути кілька окремих MultiPolygon-шматків
    -- фронт лінія рідко єдиний суцільний контур)."""
    return any(point_in_polygon(lat, lon, poly) for poly in polygons)


def find_crossing_on_segment(
    lat1: float, lon1: float, lat2: float, lon2: float, polygons: list,
    steps: int = 200,
) -> tuple[float, float, float] | None:
    """Якщо (lat1,lon1) і (lat2,lon2) по РІЗНІ боки полігону -- шукає
    точку перетину БІНАРНИМ ПОШУКОМ уздовж відрізка (steps -- лише
    початкова точність сканування для визначення напрямку, сам пошук
    точки -- бінарний, тому 200 кроків дають похибку << 1м на
    типовому ребрі в кілька км).

    Повертає (lat, lon, t) де t -- частка довжини відрізка [0..1] до
    точки перетину, або None, якщо перетину немає (обидва кінці по
    один бік)."""
    in1 = point_in_any_polygon(lat1, lon1, polygons)
    in2 = point_in_any_polygon(lat2, lon2, polygons)
    if in1 == in2:
        return None  # обидва кінці по один бік -- немає перетину на ЦЬОМУ ребрі

    lo, hi = 0.0, 1.0
    lo_inside = in1
    for _ in range(40):  # 2^-40 похибки достатньо для будь-якої реалістичної довжини ребра
        mid = (lo + hi) / 2.0
        mid_lat = lat1 + (lat2 - lat1) * mid
        mid_lon = lon1 + (lon2 - lon1) * mid
        mid_inside = point_in_any_polygon(mid_lat, mid_lon, polygons)
        if mid_inside == lo_inside:
            lo = mid
        else:
            hi = mid

    t = (lo + hi) / 2.0
    cross_lat = lat1 + (lat2 - lat1) * t
    cross_lon = lon1 + (lon2 - lon1) * t
    return cross_lat, cross_lon, t


def find_route_crossing(nav_wps: list, polygons: list):
    """Проходить УВЕСЬ маршрут послідовно, повертає перший знайдений
    перетин лінії розмежування як
    (leg_index, cross_lat, cross_lon, t, entering_occupied: bool)
    або None, якщо маршрут цілком по один бік (немає перетину взагалі
    -- оптимізація висоти тоді не потрібна)."""
    if not polygons:
        return None
    for i in range(len(nav_wps) - 1):
        wp1, wp2 = nav_wps[i], nav_wps[i + 1]
        result = find_crossing_on_segment(wp1.lat, wp1.lon, wp2.lat, wp2.lon, polygons)
        if result is not None:
            cross_lat, cross_lon, t = result
            entering_occupied = not point_in_any_polygon(wp1.lat, wp1.lon, polygons)
            return i, cross_lat, cross_lon, t, entering_occupied
    return None


# ============================================================
# Розрахунок дистанцій маневру
# ============================================================

@dataclass
class ManeuverPlan:
    """Розрахований план одного переходу висоти."""
    distance_before_m: float   # відстань ПЕРЕД точкою перетину, де починається маневр
    distance_after_m: float    # відстань ПІСЛЯ точки перетину, де маневр завершується
    rate_before_ms: float      # застосована швидкість зміни висоти (climb чи sink) ДО перетину
    rate_after_ms: float       # те саме ПІСЛЯ перетину


def compute_maneuver_distances(
    alt_before_zone_m: float, alt_crossing_m: float, alt_after_zone_m: float,
    cruise_speed_ms: float, climb_rate_max_ms: float, sink_rate_min_ms: float,
) -> ManeuverPlan:
    """Скільки метрів МАРШРУТУ потрібно на кожен з двох напівманеврів
    (до і після точки перетину), щоб плавно перейти
    зона_А -> border_crossing -> зона_Б, не порушуючи climb_rate_max_ms/
    sink_rate_min_ms профілю літака.

    Напрямок (набір чи зниження) визначається знаком різниці висот --
    НЕ припускаємо заздалегідь, яка зона вища."""
    def _rate_for(delta_alt: float) -> float:
        # позитивна різниця -- потрібен НАБІР (climb_rate_max_ms),
        # від'ємна -- ЗНИЖЕННЯ (sink_rate_min_ms, за визначенням TECS
        # додатне число -- мінімальна швидкість ЗНИЖЕННЯ на крейсерській)
        return climb_rate_max_ms if delta_alt > 0 else sink_rate_min_ms

    rate_before = _rate_for(alt_crossing_m - alt_before_zone_m)
    rate_after = _rate_for(alt_after_zone_m - alt_crossing_m)

    time_before_s = abs(alt_crossing_m - alt_before_zone_m) / rate_before if rate_before > 0 else 0.0
    time_after_s = abs(alt_after_zone_m - alt_crossing_m) / rate_after if rate_after > 0 else 0.0

    return ManeuverPlan(
        distance_before_m=time_before_s * cruise_speed_ms,
        distance_after_m=time_after_s * cruise_speed_ms,
        rate_before_ms=rate_before,
        rate_after_ms=rate_after,
    )


# ============================================================
# Розміщення точки маневру вздовж маршруту (рух "назад"/"вперед" від
# точки перетину на задану відстань)
# ============================================================

def _walk_along_route(nav_wps: list, start_leg_index: int, start_t: float,
                       distance_m: float, direction: int):
    """Рухається вздовж ЛАМАНОЇ маршруту (не одного ребра) від точки
    (start_leg_index, start_t) на distance_m метрів у напрямку direction
    (+1 -- вперед по маршруту, -1 -- назад). Повертає (lat, lon,
    leg_index) кінцевої точки, чи None якщо вийшли за межі маршруту
    (маневр довший за залишок маршруту -- геометрично неможливо
    виконати повністю, викликаючий код має це обробити).

    Реалізація: явно тримає "де я зараз" як (lat, lon, leg_index,
    t_на_цьому_ребрі) і крок за кроком з'їдає відстань -- спочатку
    залишок ПОТОЧНОГО ребра, потім цілі наступні ребра, доки
    залишкова відстань не вміститься в ЧЕРГОВЕ ребро повністю."""
    leg = start_leg_index
    t = start_t

    while True:
        wp1, wp2 = nav_wps[leg], nav_wps[leg + 1]
        seg_len = haversine_m(wp1.lat, wp1.lon, wp2.lat, wp2.lon)

        if direction > 0:
            remaining_on_seg = seg_len * (1.0 - t)
        else:
            remaining_on_seg = seg_len * t

        if distance_m <= remaining_on_seg:
            if direction > 0:
                new_t = t + (distance_m / seg_len if seg_len > 0 else 0.0)
            else:
                new_t = t - (distance_m / seg_len if seg_len > 0 else 0.0)
            lat = wp1.lat + (wp2.lat - wp1.lat) * new_t
            lon = wp1.lon + (wp2.lon - wp1.lon) * new_t
            return lat, lon, leg

        distance_m -= remaining_on_seg
        if direction > 0:
            leg += 1
            if leg >= len(nav_wps) - 1:
                return None  # вийшли за кінець маршруту
            t = 0.0
        else:
            leg -= 1
            if leg < 0:
                return None  # вийшли за початок маршруту
            t = 1.0


# ============================================================
# Результат оптимізації висоти
# ============================================================

@dataclass
class AltitudeWaypointChange:
    """Одна зміна висоти -- або НОВА вставлена точка, або коригування
    ІСНУЮЧОЇ (is_new=False, existing_nav_index -- її індекс у nav_wps)."""
    lat: float
    lon: float
    altitude_m: float          # відносна висота (те саме трактування, що й у route_optimizer)
    is_new: bool
    existing_nav_index: int | None = None
    role: str = ""             # "maneuver_start" / "crossing" / "maneuver_end" / "zone_fill" / "removed"


@dataclass
class AltitudeOptimizationResult:
    crossing_found: bool
    changes: list = field(default_factory=list)     # список AltitudeWaypointChange, У ПОРЯДКУ вздовж маршруту
    maneuver_plan: ManeuverPlan | None = None
    achievable: bool = True     # False, якщо маневр не вмістився в довжину маршруту (route занадто короткий)
    failure_reason: str = ""
    before_after_rows: list = field(default_factory=list)  # для звіту -- [(назва_ділянки, було_м, стало_м, ціль_м), ...]
    actual_rate_before_ms: float | None = None  # ФАКТИЧНО досягнутий темп ДО перетину (після
    # можливого "прилипання" до наявного вейпоінта в межах допуску) -- перевіряється ОКРЕМО від
    # розрахункового плану, бо допуск _EXISTING_POINT_TOLERANCE_M може змістити реальну відстань
    actual_rate_after_ms: float | None = None   # те саме ПІСЛЯ перетину
    rate_exceeded: bool = False  # True, якщо ФАКТИЧНИЙ темп (не розрахунковий) перевищує ліміт профілю


_EXISTING_POINT_TOLERANCE_M = 40.0  # якщо вейпоінт уже стоїть ближче за це -- коригуємо його, не вставляємо нову точку


def _nearest_nav_point(nav_wps: list, leg_index: int, lat: float, lon: float):
    """Перевіряє ДВА кандидати -- кінці ребра leg_index -- чи один із
    них досить близько до (lat,lon), щоб перевикористати замість
    вставки нової точки. Повертає nav_index чи None."""
    for idx in (leg_index, leg_index + 1):
        wp = nav_wps[idx]
        if haversine_m(wp.lat, wp.lon, lat, lon) <= _EXISTING_POINT_TOLERANCE_M:
            return idx
    return None


def optimize_altitude(
    nav_wps: list,
    polygons: list,
    alt_controlled_m: float,
    alt_occupied_m: float,
    alt_border_crossing_m: float,
    cruise_speed_ms: float,
    climb_rate_max_ms: float,
    sink_rate_min_ms: float,
    original_altitudes_m: list,
) -> AltitudeOptimizationResult:
    """Головна функція. original_altitudes_m -- відносна висота КОЖНОЇ
    точки nav_wps В ОРИГІНАЛІ (для звіту "Було") -- рахується
    ВИКЛИКАЮЧИМ кодом (analyzer вже має цю логіку для перевірки
    критичної висоти), не дублюється тут."""
    crossing = find_route_crossing(nav_wps, polygons)
    if crossing is None:
        # НЕМАЄ перетину -- уся траса по ОДИН бік лінії розмежування.
        # РАНІШЕ тут просто повертався порожній результат ("нічого не
        # потрібно") -- але навіть в одній зоні початкова висота могла
        # НЕ відповідати цільовій для цієї зони (місія рахувалась без
        # знання про тип маршруту). Тому виставляємо цільову висоту
        # ЦІЄЇ зони на ВСІ точки -- оптимізація завжди щось РОБИТЬ,
        # доки відомо, у чиїй зоні перебуває маршрут.
        if not nav_wps:
            return AltitudeOptimizationResult(crossing_found=False, achievable=False)
        is_occupied = point_in_any_polygon(nav_wps[0].lat, nav_wps[0].lon, polygons)
        target_alt = alt_occupied_m if is_occupied else alt_controlled_m
        changes = [
            AltitudeWaypointChange(
                lat=wp.lat, lon=wp.lon, altitude_m=target_alt,
                is_new=False, existing_nav_index=i, role="zone_fill",
            )
            for i, wp in enumerate(nav_wps)
        ]
        zone_name = "окупована територія" if is_occupied else "контрольована територія"
        before_after_rows = [
            (f"Уся траса ({zone_name})",
             original_altitudes_m[0] if original_altitudes_m else 0.0, target_alt, target_alt),
        ]
        return AltitudeOptimizationResult(
            crossing_found=False, changes=changes, achievable=True,
            before_after_rows=before_after_rows,
        )

    leg_index, cross_lat, cross_lon, t, entering_occupied = crossing
    alt_before_zone = alt_occupied_m if not entering_occupied else alt_controlled_m
    alt_after_zone = alt_controlled_m if not entering_occupied else alt_occupied_m

    # кумулятивна відстань КОЖНОЇ точки nav_wps від початку маршруту --
    # потрібна нижче, щоб порахувати, НАСКІЛЬКИ далеко кожна проміжна
    # точка (що раніше просто ВИДАЛЯЛАСЬ) перебуває від точки перетину,
    # і призначити ЇЙ інтерпольовану висоту замість видалення.
    cum_dist = [0.0]
    for i in range(len(nav_wps) - 1):
        cum_dist.append(cum_dist[-1] + haversine_m(
            nav_wps[i].lat, nav_wps[i].lon, nav_wps[i + 1].lat, nav_wps[i + 1].lon,
        ))
    crossing_dist = cum_dist[leg_index] + t * (cum_dist[leg_index + 1] - cum_dist[leg_index])

    plan = compute_maneuver_distances(
        alt_before_zone, alt_border_crossing_m, alt_after_zone,
        cruise_speed_ms, climb_rate_max_ms, sink_rate_min_ms,
    )

    start_result = _walk_along_route(nav_wps, leg_index, t, plan.distance_before_m, direction=-1)
    end_result = _walk_along_route(nav_wps, leg_index, t, plan.distance_after_m, direction=1)

    if start_result is None or end_result is None:
        return AltitudeOptimizationResult(
            crossing_found=True, maneuver_plan=plan, achievable=False,
            failure_reason="маршрут закороткий для плавного маневру заданої довжини",
        )

    start_lat, start_lon, start_leg = start_result
    end_lat, end_lon, end_leg = end_result

    changes = []

    # --- точка початку маневру ---
    existing = _nearest_nav_point(nav_wps, start_leg, start_lat, start_lon)
    changes.append(AltitudeWaypointChange(
        lat=start_lat, lon=start_lon, altitude_m=alt_before_zone,
        is_new=existing is None, existing_nav_index=existing, role="maneuver_start",
    ))

    # --- точка перетину (border_crossing) -- ЗАВЖДИ реальна геометрична
    # точка на лінії розмежування, точність тут критична (третя змінна
    # типу маршруту), тому tolerance НЕ застосовуємо -- завжди своя
    # точка, навіть якщо щось є дуже близько.
    changes.append(AltitudeWaypointChange(
        lat=cross_lat, lon=cross_lon, altitude_m=alt_border_crossing_m,
        is_new=True, existing_nav_index=None, role="crossing",
    ))

    # --- точка завершення маневру ---
    existing_end = _nearest_nav_point(nav_wps, end_leg, end_lat, end_lon)
    changes.append(AltitudeWaypointChange(
        lat=end_lat, lon=end_lon, altitude_m=alt_after_zone,
        is_new=existing_end is None, existing_nav_index=existing_end, role="maneuver_end",
    ))

    # --- усі ІНШІ точки в межах кожної зони (до maneuver_start і після
    # maneuver_end) -- коригуємо висоту на цільову для тієї зони.
    # Точки, що потрапляють ВСЕРЕДИНУ самої зони маневру (між
    # maneuver_start і maneuver_end, СТРОГО між ними) -- ВИДАЛЯЮТЬСЯ з
    # маршруту (роль "removed"), а не мовчки лишаються на старій
    # висоті. ВИЯВЛЕНО НА ПРАКТИЦІ: без цього оригінальна точка, що
    # геометрично опиняється прямо в зоні переходу, лишалась би на
    # оригінальній висоті -- реальний, непомітний розрив у щойно
    # побудованому плавному профілі."""
    for i, wp in enumerate(nav_wps):
        if i in (existing, existing_end):
            continue
        if i <= start_leg:
            changes.append(AltitudeWaypointChange(
                lat=wp.lat, lon=wp.lon, altitude_m=alt_before_zone,
                is_new=False, existing_nav_index=i, role="zone_fill",
            ))
        elif i >= end_leg + 1:
            changes.append(AltitudeWaypointChange(
                lat=wp.lat, lon=wp.lon, altitude_m=alt_after_zone,
                is_new=False, existing_nav_index=i, role="zone_fill",
            ))
        else:
            # РАНІШЕ точки СТРОГО всередині зони маневру видалялись
            # цілком -- ВИЯВЛЕНО НА ПРАКТИЦІ (реальний польотний
            # профіль, завантажений на "Місія"): це могло створити
            # ЗАНАДТО ВЕЛИКИЙ проміжок між рештою точок, і рельєф з
            # піком ПОСЕРЕДИНІ лишався непокритим -- ArduPilot лінійно
            # інтерполює АБСОЛЮТНУ висоту між сусідніми вейпоінтами, і
            # якщо вилучені точки раніше "покривали" пік рельєфу,
            # результат міг опинитись НИЖЧЕ рельєфу -- реальна
            # небезпека, не косметика.
            #
            # Тепер замість видалення -- ІНТЕРПОЛЬОВАНА висота: точка
            # ЛИШАЄТЬСЯ на місці (зберігає щільність оригінального
            # маршруту й покриття рельєфу), просто отримує AGL-висоту,
            # яка плавно змінюється між alt_before_zone/alt_border_
            # crossing_m (до лінії) чи alt_border_crossing_m/alt_after_
            # zone (після), пропорційно її РЕАЛЬНІЙ відстані до лінії.
            point_dist = cum_dist[i]
            d_from_crossing = point_dist - crossing_dist
            if d_from_crossing <= 0:
                frac = (d_from_crossing + plan.distance_before_m) / plan.distance_before_m if plan.distance_before_m > 0 else 1.0
                frac = max(0.0, min(1.0, frac))
                interp_alt = alt_before_zone + frac * (alt_border_crossing_m - alt_before_zone)
            else:
                frac = d_from_crossing / plan.distance_after_m if plan.distance_after_m > 0 else 1.0
                frac = max(0.0, min(1.0, frac))
                interp_alt = alt_border_crossing_m + frac * (alt_after_zone - alt_border_crossing_m)
            changes.append(AltitudeWaypointChange(
                lat=wp.lat, lon=wp.lon, altitude_m=interp_alt,
                is_new=False, existing_nav_index=i, role="zone_fill",
            ))

    # --- ЯВНА перевірка ФАКТИЧНО досягнутого темпу (не просто довіра
    # побудові за розрахунком) -- за геометрією ФІНАЛЬНИХ позицій, той
    # самий принцип, що вже застосований у route_optimizer.py
    # (unstable_convergence/residual_violations): перевір, що ЗРОБИЛОСЬ,
    # а не тільки що МАЛО статись.
    actual_dist_before = haversine_m(start_lat, start_lon, cross_lat, cross_lon)
    actual_time_before = actual_dist_before / cruise_speed_ms if cruise_speed_ms > 0 else 0.0
    actual_rate_before = (
        abs(alt_border_crossing_m - alt_before_zone) / actual_time_before if actual_time_before > 0 else 0.0
    )
    limit_before = climb_rate_max_ms if alt_border_crossing_m > alt_before_zone else sink_rate_min_ms

    actual_dist_after = haversine_m(cross_lat, cross_lon, end_lat, end_lon)
    actual_time_after = actual_dist_after / cruise_speed_ms if cruise_speed_ms > 0 else 0.0
    actual_rate_after = (
        abs(alt_after_zone - alt_border_crossing_m) / actual_time_after if actual_time_after > 0 else 0.0
    )
    limit_after = climb_rate_max_ms if alt_after_zone > alt_border_crossing_m else sink_rate_min_ms

    # невеликий допуск (1%) на похибку заокруглення координат/бінарного
    # пошуку точки перетину -- не справжнє перевищення, а обчислювальний шум
    rate_exceeded = actual_rate_before > limit_before * 1.01 or actual_rate_after > limit_after * 1.01

    # --- звіт "Було/Стало" ---
    before_after_rows = [
        ("Зона до переходу", original_altitudes_m[0] if original_altitudes_m else 0.0, alt_before_zone, alt_before_zone),
        ("Точка перетину лінії", _interp_original_alt(original_altitudes_m, leg_index, t), alt_border_crossing_m, alt_border_crossing_m),
        ("Зона після переходу", original_altitudes_m[-1] if original_altitudes_m else 0.0, alt_after_zone, alt_after_zone),
    ]

    return AltitudeOptimizationResult(
        crossing_found=True, changes=changes, maneuver_plan=plan,
        achievable=True, before_after_rows=before_after_rows,
        actual_rate_before_ms=actual_rate_before, actual_rate_after_ms=actual_rate_after,
        rate_exceeded=rate_exceeded,
    )


def _interp_original_alt(original_altitudes_m: list, leg_index: int, t: float) -> float:
    if not original_altitudes_m or leg_index + 1 >= len(original_altitudes_m):
        return 0.0
    a, b = original_altitudes_m[leg_index], original_altitudes_m[leg_index + 1]
    return a + (b - a) * t


# ============================================================
# ОКРЕМИЙ, ІЗОЛЬОВАНИЙ блок -- маневр переходу державного кордону
# ============================================================
#
# За прямою вказівкою користувача: обхід населених пунктів (координатна
# оптимізація) і набір/зниження висоти на переході кордону -- ДВІ РІЗНІ
# задачі, які раніше змішувались в одному місці (детур-точки, розставлені
# щільно заради обходу перешкоди, отримували миттєвий стрибок висоти --
# 57° кут на 129м, фізично неможливий маневр). Тепер:
#   1. route_optimizer.py НЕ обходить перешкоди на ребрі, що перетинає
#      кордон (exclude_leg_indices) -- це ребро завжди пряма лінія.
#   2. ЦЕЙ блок окремо визначає геометрію самого маневру зміни висоти
#      на цій прямій лінії -- де САМЕ почати набір/зниження й де
#      завершити, щоб кут НІКОЛИ не перевищував border_maneuver_angle_
#      max_deg (окреме поле "Тип маршруту", не загальний angle_max
#      Конфігурації -- може відрізнятись для різних маршрутів/виробів).

def compute_border_crossing_maneuver(
    nav_wps: list, polygons: list,
    alt_controlled_m: float, alt_occupied_m: float, alt_border_crossing_m: float,
    maneuver_angle_max_deg: float,
) -> dict | None:
    """Знаходить перетин кордону (find_route_crossing) і розраховує
    геометрію маневру -- ТОЧКУ ПОЧАТКУ набору/зниження (назад від
    перетину) і ТОЧКУ ЗАВЕРШЕННЯ (вперед від перетину) так, щоб кут
    НІДЕ не перевищував maneuver_angle_max_deg (з невеликим запасом --
    ціль трохи ПІД лімітом, а не рівно на межі, той самий принцип, що
    вже застосований у Фазі 1 "Висоти").

    Повертає None, якщо маршрут НЕ перетинає кордон узагалі (нічого
    робити не треба). Інакше -- словник:
        {"achievable": bool,
         "crossing": {"lat","lon","target_agl","leg_index"},
         "maneuver_start": {"lat","lon","target_agl","leg_index"} чи None,
         "maneuver_end": {"lat","lon","target_agl","leg_index"} чи None,
         "distance_before_m": float, "distance_after_m": float}

    "achievable"=False -- маршрут ЗАКОРОТКИЙ для цього маневру навіть
    на всю свою довжину (не встиг знайти точку в межах маршруту) --
    виклик має ЯВНО повідомити про це, не мовчати."""
    crossing = find_route_crossing(nav_wps, polygons)
    if crossing is None:
        return None

    leg_index, cross_lat, cross_lon, t, entering_occupied = crossing
    alt_before_zone = alt_occupied_m if not entering_occupied else alt_controlled_m
    alt_after_zone = alt_controlled_m if not entering_occupied else alt_occupied_m

    # той самий принцип запасу (99% ліміту), що вже підтвердив себе у
    # Фазі 1 "Висоти" -- уникає балансування точно на межі похибки
    # округлення (раніше давало сотні марних ітерацій в іншому місці).
    tan_limit = math.tan(math.radians(maneuver_angle_max_deg * 0.99))
    dist_before = abs(alt_border_crossing_m - alt_before_zone) / tan_limit if tan_limit > 0 else 0.0
    dist_after = abs(alt_after_zone - alt_border_crossing_m) / tan_limit if tan_limit > 0 else 0.0

    start_result = _walk_along_route(nav_wps, leg_index, t, dist_before, direction=-1)
    end_result = _walk_along_route(nav_wps, leg_index, t, dist_after, direction=1)

    achievable = start_result is not None and end_result is not None

    return {
        "achievable": achievable,
        "crossing": {
            "lat": cross_lat, "lon": cross_lon,
            "target_agl": alt_border_crossing_m, "leg_index": leg_index,
        },
        "maneuver_start": (
            {"lat": start_result[0], "lon": start_result[1],
             "target_agl": alt_before_zone, "leg_index": start_result[2]}
            if start_result else None
        ),
        "maneuver_end": (
            {"lat": end_result[0], "lon": end_result[1],
             "target_agl": alt_after_zone, "leg_index": end_result[2]}
            if end_result else None
        ),
        "distance_before_m": dist_before,
        "distance_after_m": dist_after,
    }
