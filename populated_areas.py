"""
populated_areas.py -- пошук населених пунктів поблизу маршруту (Overpass
API, точки place=city/town/village/hamlet) і розрахунок мінімальної
відстані від кожного ВІДРІЗКА маршруту (не тільки самих вейпоінтів,
а й ліній між ними) до найближчого населеного пункту.

Джерело даних: OSM Overpass API, точки (не полігони меж забудови).
Полігони меж у OSM мапляться непослідовно між регіонами й часто відсутні
для малих сіл -- точки place=* є практично завжди і надійно. Похибка:
для великого міста точка -- це географічний центр, а не край забудови,
тому реальна відстань до найближчого будинку може бути МЕНШОЮ за
розраховану. Прийнятне обмеження для першого кроку аналізу.
"""

from __future__ import annotations

import json
import math
import time
import urllib.request
import urllib.parse
import urllib.error

# Список серверів у порядку спроб. overpass-api.de (офіційний
# "головний" сервер) НЕ перший навмисно -- за задокументованими звітами
# спільноти OSM він періодично нестабільний під навантаженням, тоді як
# kumi.systems прямо рекомендують спільнотою як більш надійний вибір
# САМЕ для програмних інструментів (не інтерактивного використання
# людиною). z.overpass-api.de -- третій, окремий фізичний бекенд
# (gall.openstreetmap.de) для додаткової надійності на випадок, якщо
# ОБИДВА перших одночасно перевантажені (підтверджено на практиці --
# траплялось, що і основний, і kumi.systems відмовляли в один момент).
OVERPASS_SERVERS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
]
OVERPASS_TIMEOUT = 25  # секунд на HTTP-запит

# place-теги, що вважаються "населеним пунктом" для цієї перевірки.
# "isolated_dwelling"/"farm" свідомо НЕ включені -- це поодинокі
# будівлі, не населений пункт у звичному розумінні.
PLACE_TAGS = ("city", "town", "village", "hamlet")


class OverpassError(Exception):
    """Мережева помилка або помилка відповіді Overpass API."""
    pass


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Відстань між двома точками (метри) по великому колу."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _point_to_segment_m(plat, plon, alat, alon, blat, blon) -> float:
    """Мінімальна відстань (метри) від точки P до відрізка A-B.

    Працює в локальній рівнокутній апроксимації (декартові координати,
    масштабовані під широту) -- для відстаней у одиниці/десятки км
    похибка проекції нехтовно мала, а рахувати набагато швидше й
    простіше за точну сферичну геометрію відрізка."""
    lat0 = (alat + blat) / 2.0
    mlat = 111320.0  # метрів на градус широти (майже стала)
    mlon = 111320.0 * math.cos(math.radians(lat0))

    px, py = plon * mlon, plat * mlat
    ax, ay = alon * mlon, alat * mlat
    bx, by = blon * mlon, blat * mlat

    dx, dy = bx - ax, by - ay
    seg_len2 = dx * dx + dy * dy
    if seg_len2 < 1e-9:
        return math.hypot(px - ax, py - ay)

    t = ((px - ax) * dx + (py - ay) * dy) / seg_len2
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def side_of_travel(plat, plon, alat, alon, blat, blon) -> str:
    """Ліворуч чи праворуч точка P відносно напрямку руху A->B.

    Векторний добуток у площині (x=довгота, y=широта): додатний --
    точка ЛІВОРУЧ (стандартна математична орієнтація проти годинникової
    стрілки), від'ємний -- ПРАВОРУЧ. Перевірено на контрольованому
    прикладі: рух строго на схід, точка на півночі -- ЛІВОРУЧ (як і
    мало б бути -- дивлячись уперед по курсу, північ ліворуч)."""
    dlat, dlon = blat - alat, blon - alon
    px, py = plon - alon, plat - alat
    cross = dlon * py - dlat * px
    return "L" if cross > 0 else "R"


def _query_overpass(url: str, query: str) -> dict:
    """Один HTTP-запит до конкретного Overpass endpoint. Піднімає
    OverpassError при мережевій помилці чи некоректній JSON-відповіді."""
    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            # overpass-api.de з 2026 посилив захист від ботів/скрейперів
            # і блокує запити зі стандартним User-Agent urllib
            # ("Python-urllib/3.x") кодом 406 -- потрібен описовий
            # User-Agent і явний Accept, інакше сервер відхиляє запит
            # ще до обробки самого запиту (задокументовано, свіжа
            # й поширена проблема в спільноті OSM з 2026 року).
            "User-Agent": "MissionAnalyzer/1.0 (ArduPilot ground station tool)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=OVERPASS_TIMEOUT + 5) as resp:
            raw = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # TimeoutError (вбудований клас Python) виникає окремо від
        # urllib.error.URLError -- перевірено реальним тестом: тайм-аут
        # ПІД ЧАС читання відповіді (з'єднання вже встановлено, сервер
        # просто довго не відповідає) дає саме TimeoutError, тоді як
        # URLError -- лише для помилок ВСТАНОВЛЕННЯ з'єднання. Без цього
        # тайм-аут читання не ловився взагалі й падав як необроблений
        # виняток, зриваючи весь розрахунок оптимізації маршруту
        # (виявлено на реальному 29-ребровому маршруті, ребро 8/29).
        # OSError -- батьківський клас обох, про всяк випадок теж ловимо.
        raise OverpassError(f"Мережева помилка Overpass API ({url}): {e}") from e

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise OverpassError(f"Некоректна відповідь Overpass API ({url}): {e}") from e


_settlements_cache: dict[tuple, list[dict]] = {}


def clear_settlements_cache() -> None:
    """Очищує кеш fetch_settlements() -- викликати, якщо потрібен
    гарантовано свіжий запит (напр. після зміни даних в OSM, чи щоб
    перевірити мережеву поведінку заново, не з кешу)."""
    _settlements_cache.clear()


def fetch_settlements(lat_min: float, lat_max: float, lon_min: float, lon_max: float,
                       margin_km: float = 3.0, use_cache: bool = True) -> list[dict]:
    """Запитує Overpass API на точки place=city/town/village/hamlet у
    прямокутнику (з запасом margin_km з кожного боку). Повертає список
    {"name": str, "lat": float, "lon": float, "place": str, "population": int|None}.

    Пробує сервери з OVERPASS_SERVERS по черзі (кожен -- 2 спроби з
    паузою, на випадок тимчасового перевантаження), поки один не
    відповість. Піднімає OverpassError лише якщо ВСІ сервери
    недоступні -- виклик МАЄ бути обгорнутий у try/except на боці UI.

    use_cache=True (за замовчуванням) -- результат кешується В ПАМ'ЯТІ
    процесу за координатами bbox. Корисно при повторних запусках
    оптимізації на ТІЙ САМІЙ місії під час тестування логіки -- не
    треба щоразу чекати на мережу й ризикувати тайм-аутами Overpass.
    Кеш живе, доки не перезапущено програму -- clear_settlements_cache()
    для примусового скидання."""
    cache_key = (round(lat_min, 5), round(lat_max, 5), round(lon_min, 5), round(lon_max, 5), margin_km)
    if use_cache and cache_key in _settlements_cache:
        return _settlements_cache[cache_key]

    dlat = margin_km / 111.0
    dlon = margin_km / (111.0 * math.cos(math.radians((lat_min + lat_max) / 2.0)))
    bbox = (lat_min - dlat, lon_min - dlon, lat_max + dlat, lon_max + dlon)

    place_filter = "|".join(PLACE_TAGS)
    query = (
        f'[out:json][timeout:{OVERPASS_TIMEOUT}];\n'
        f'node["place"~"^({place_filter})$"]'
        f'({bbox[0]:.6f},{bbox[1]:.6f},{bbox[2]:.6f},{bbox[3]:.6f});\n'
        f'out body;\n'
    )

    # Кожен сервер отримує ДВІ спроби (з паузою) перш ніж переходити до
    # наступного -- 504/тайм-аут читання часто означають ТИМЧАСОВЕ
    # перевантаження сервера, яке минає за кілька секунд, а не постійну
    # відмову. Без повторної спроби навіть короткочасний збій одразу
    # переводив на дзеркало (чи взагалі провалював запит, якщо і
    # дзеркало саме в цю мить теж перевантажене) -- підтверджено на
    # реальному запуску (основний: 504 Gateway Timeout, дзеркало: read
    # timeout, обидва в межах кількох секунд одне від одного).
    RETRY_DELAY_S = 3.0
    errors = []
    for url in OVERPASS_SERVERS:
        for attempt in (1, 2):
            try:
                payload = _query_overpass(url, query)
                break
            except OverpassError as e:
                errors.append(str(e))
                if attempt == 1:
                    time.sleep(RETRY_DELAY_S)
        else:
            continue
        break
    else:
        raise OverpassError("; ".join(errors))

    result = []
    for el in payload.get("elements", []):
        if el.get("type") != "node":
            continue
        tags = el.get("tags", {})
        name = tags.get("name") or tags.get("name:uk") or tags.get("name:en")
        if not name:
            continue
        pop_raw = tags.get("population")
        population = None
        if pop_raw:
            try:
                population = int(str(pop_raw).replace(" ", "").replace(",", ""))
            except ValueError:
                population = None
        result.append({
            "name": name,
            "lat": el["lat"],
            "lon": el["lon"],
            "place": tags.get("place", "?"),
            "population": population,
        })

    if use_cache:
        _settlements_cache[cache_key] = result
    return result


def check_route_settlement_distances(nav_wps: list, settlements: list[dict],
                                      threshold_km: float = 1.0, progress_callback=None) -> list[dict]:
    """Для кожного відрізка маршруту (пари сусідніх навігаційних точок)
    рахує мінімальну відстань до кожного населеного пункту зі списку.

    Повертає список ТІЛЬКИ порушень (де min_distance_m < threshold_km*1000),
    відсортований за зростанням відстані (найкритичніші -- перші):
        [{"settlement": dict, "leg_index": int,
          "wp1_index": int, "wp2_index": int, "distance_m": float}, ...]

    nav_wps -- список Waypoint (з .lat/.lon/.index), як analyzer.nav_wps.

    progress_callback -- необов'язковий callable(done: int, total: int,
    leg_violations: list[dict]), викликається ПІСЛЯ обробки кожного
    ребра з порушеннями САМЕ ЦЬОГО ребра -- дозволяє виклику прогресивно
    оновлювати UI/карту, не чекаючи завершення розрахунку по ВСЬОМУ
    маршруту (корисно для довгих маршрутів із сотнями населених
    пунктів, де сам розрахунок відстаней помітно триває)."""
    threshold_m = threshold_km * 1000.0
    violations = []
    n_legs = len(nav_wps) - 1

    for i in range(n_legs):
        wp1, wp2 = nav_wps[i], nav_wps[i + 1]
        leg_violations = []
        for s in settlements:
            d = _point_to_segment_m(s["lat"], s["lon"], wp1.lat, wp1.lon, wp2.lat, wp2.lon)
            if d < threshold_m:
                v = {
                    "settlement": s,
                    "leg_index": i,
                    "wp1_index": wp1.index,
                    "wp2_index": wp2.index,
                    "distance_m": d,
                }
                leg_violations.append(v)
                violations.append(v)
        if progress_callback is not None:
            progress_callback(i + 1, n_legs, leg_violations)

    violations.sort(key=lambda v: v["distance_m"])
    return violations


def min_distance_per_settlement(nav_wps: list, settlements: list[dict]) -> dict[int, float]:
    """Для КОЖНОГО населеного пункту (за id -- індекс у списку settlements)
    рахує мінімальну відстань до маршруту в ЦІЛОМУ (по всіх відрізках).
    Використовується для кольорового позначення точок на карті -- кожна
    точка позначається ОДНИМ кольором за своєю найближчою відстанню,
    незалежно від того, скільки відрізків до неї близько."""
    result = {}
    for idx, s in enumerate(settlements):
        best = float("inf")
        for i in range(len(nav_wps) - 1):
            wp1, wp2 = nav_wps[i], nav_wps[i + 1]
            d = _point_to_segment_m(s["lat"], s["lon"], wp1.lat, wp1.lon, wp2.lat, wp2.lon)
            best = min(best, d)
        result[idx] = best
    return result
