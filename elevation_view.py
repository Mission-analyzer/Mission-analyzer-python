"""
elevation_view.py — отрисовка профиля высоты миссии на tk.Canvas.
Ничего не знает про Waypoint/парсинг — только берёт готовые данные
из MissionAnalyzer.elevation_profile() и рисует.
"""

from __future__ import annotations

import math
import tkinter as tk

from analyzer import MissionAnalyzer
import i18n


import theme


def _colors(dark: bool) -> dict:
    # усі значення -- з ЄДИНОГО джерела (theme.chart_colors), лише
    # перейменовані під локальні назви, які вже використовує код
    # відмальовки нижче (щоб не переписувати кожен виклик create_*)
    cc = theme.chart_colors(dark)
    return {
        "grid_y": cc["grid"], "grid_x": cc["grid_minor"],
        "text": cc["text"],
        "terrain_line": cc["line_terrain"],
        "low_agl": "#aa3333" if dark else "#ff9999",
        "mission_line": cc["line_primary"],
        "angle_dash": cc["muted"], "angle_text": cc["text"],
    }


def draw_elevation_profile(
    canvas: tk.Canvas, analyzer: MissionAnalyzer, step_m: float = 50.0,
    max_dist_m: float | None = None, title: str | None = None,
    show_angles: bool = False, dark: bool = False,
):
    """
    Полностью перерисовывает canvas профилем высоты текущей миссии.
    Якщо задано max_dist_m -- обрізає профіль по відстані (для зльоту,
    де потрібні лише перші кілька точок, а не весь маршрут).
    Якщо show_angles=True -- додає вертикальні пунктирні лінії по
    точках і підписи кута підйому/зниження між ними (як на «Посадка»).
    dark -- палітра для темної теми (canvas.bg встановлюється ЗОВНІ,
    цей параметр лише підбирає кольори ліній/тексту, щоб лишались
    видимими на темному тлі).
    """
    canvas.delete("all")
    if analyzer is None:
        return

    c = _colors(dark)
    width = max(canvas.winfo_width(), 200)
    height = max(canvas.winfo_height(), 150)

    try:
        profile = analyzer.elevation_profile(step_m=step_m, max_dist_m=max_dist_m)
    except ValueError:
        return

    dist_m = profile["dist"]
    mission_vals = profile["mission_alt"]
    terrain_vals = profile["terrain_alt"]
    waypoints = profile["waypoints"]

    dist_km = [d / 1000 for d in dist_m]
    has_terrain = analyzer.terrain is not None and any(v is not None for v in terrain_vals)

    # якщо треба підписи кутів -- зверху потрібне місце під "сходинки"
    # підписів (одна на кожен відрізок між точками), як на графіку глісади.
    # На малому канвасі це може з'їсти забагато місця -- стискаємо компактніше.
    valid_wps = [(d, a, seq) for d, a, idx, seq in waypoints if a is not None]
    n_segs = max(len(valid_wps) - 1, 0)
    leg_rows_top, row_h = 26, 12
    margin_t = 25
    if show_angles and n_segs > 0:
        needed_top = leg_rows_top + n_segs * row_h + 6
        available_top = height * 0.4
        if needed_top > available_top:
            scale = max(available_top / needed_top, 0.4)
            row_h = max(row_h * scale, 7)
            leg_rows_top = max(leg_rows_top * scale, 16)
            needed_top = leg_rows_top + n_segs * row_h + 6
        margin_t = max(margin_t, needed_top)

    margin_l, margin_r, margin_b = 55, 15, 42
    plot_w = max(width - margin_l - margin_r, 10)
    plot_h = max(height - margin_t - margin_b, 10)

    all_alts = [v for v in mission_vals if v is not None]
    if has_terrain:
        all_alts += [v for v in terrain_vals if v is not None]
    if not all_alts:
        return

    x_min, x_max = 0.0, dist_km[-1] if dist_km[-1] > 0 else 1.0
    y_min, y_max = min(all_alts), max(all_alts)
    if y_max == y_min:
        y_max += 1.0
    y_pad = (y_max - y_min) * 0.08
    y_min -= y_pad
    y_max += y_pad

    def X(d_km):
        return margin_l + (d_km - x_min) / (x_max - x_min) * plot_w

    def Y(alt):
        return margin_t + (1 - (alt - y_min) / (y_max - y_min)) * plot_h

    # сетка и подписи
    for i in range(6):
        val = y_min + (y_max - y_min) * i / 5
        y = Y(val)
        canvas.create_line(margin_l, y, width - margin_r, y, fill=c["grid_y"])
        canvas.create_text(margin_l - 6, y, text=f"{val:.0f}", anchor="e", font=("Arial", 8), fill=c["text"])
    canvas.create_text(
        margin_l, max(margin_t - 6, 6), text=i18n.t("unit_meters_axis"),
        anchor="s", font=("Arial", 7), fill=c["text"],
    )

    for i in range(7):
        val = x_min + (x_max - x_min) * i / 6
        x = X(val)
        canvas.create_line(x, margin_t, x, height - margin_b, fill=c["grid_x"])
        canvas.create_text(x, height - margin_b + 14, text=f"{val:.1f}", anchor="n", font=("Arial", 8), fill=c["text"])
    canvas.create_text(
        width / 2, height - margin_b + 26, text=i18n.t("unit_km_axis"),
        anchor="n", font=("Arial", 7), fill=c["text"],
    )

    # рельєф -- лише лінія, без заливки (та сама логіка, що й на
    # "Місія": просто графік рельєфу, без "плями" під ним) + підсвітка
    # зон низького AGL
    if has_terrain:
        line_pts = []
        for d, t in zip(dist_km, terrain_vals):
            if t is None:
                continue
            line_pts.extend([X(d), Y(t)])
        if len(line_pts) >= 4:
            canvas.create_line(*line_pts, fill=c["terrain_line"], width=2)

        low = [
            (m is not None and t is not None and (m - t) < analyzer.alt_min)
            for m, t in zip(mission_vals, terrain_vals)
        ]
        i = 0
        while i < len(low):
            if low[i]:
                j = i
                while j < len(low) and low[j]:
                    j += 1
                x1, x2 = X(dist_km[i]), X(dist_km[min(j, len(low) - 1)])
                canvas.create_rectangle(
                    x1, margin_t, x2, height - margin_b,
                    fill=c["low_agl"], outline="", stipple="gray50",
                )
                i = j
            else:
                i += 1

    # линия высоты миссии
    pts = [(X(d), Y(m)) for d, m in zip(dist_km, mission_vals) if m is not None]
    for i in range(len(pts) - 1):
        canvas.create_line(*pts[i], *pts[i + 1], fill=c["mission_line"], width=2)

    # вертикальні пунктирні лінії по точках + підписи кута підйому/зниження
    # між ними -- як на графіку глісади (тільки при show_angles=True)
    if show_angles:
        for d, a, seq in valid_wps:
            x = X(d / 1000)
            canvas.create_line(x, margin_t, x, height - margin_b, fill=c["angle_dash"], dash=(2, 2))

        for i in range(len(valid_wps) - 1):
            d1, a1, seq1 = valid_wps[i]
            d2, a2, seq2 = valid_wps[i + 1]
            delta_d = d2 - d1
            delta_a = a2 - a1
            angle_deg = math.degrees(math.atan2(delta_a, delta_d)) if delta_d else 0.0
            xm = (X(d1 / 1000) + X(d2 / 1000)) / 2
            row_y = leg_rows_top + i * row_h
            canvas.create_text(
                xm, row_y, text=f"{angle_deg:+.1f}°",
                font=("Arial", 8), fill=c["angle_text"],
            )

    # точки waypoint'ов
    for d, a, idx, seq in waypoints:
        if a is None:
            continue
        x, y = X(d / 1000), Y(a)
        canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill=c["mission_line"], outline="")
        canvas.create_text(x, y - 10, text=str(seq), font=("Arial", 8), fill=c["text"])

    canvas.create_text(
        width / 2, 12, text=title or i18n.t("title_elevation_profile"),
        font=("Arial", 11, "bold"), fill=c["text"],
    )


def draw_takeoff_profile(
    canvas: tk.Canvas, analyzer: MissionAnalyzer, n_wps: int = 3, step_m: float = 10.0,
    dark: bool = False,
):
    """
    Профіль висоти лише для зльоту: точка старту + перші n_wps точок
    маршруту (детальніше, ніж загальний профіль -- крок 10 м замість 50).
    Вертикальні пунктирні лінії й підписи кута -- як на графіку глісади.
    """
    if analyzer is None:
        canvas.delete("all")
        return

    # Відстань до потрібної точки рахуємо НАПРЯМУ по координатах точок
    # маршруту (haversine, O(n_wps) -- фактично миттєво), а НЕ через
    # elevation_profile(): той рахує профіль ВСЬОГО маршруту з кроком
    # step_m, і виклик лише заради відстані до 2-3 перших точок на
    # довгому маршруті (сотні км) міг займати відчутний час.
    nav_wps = getattr(analyzer, "nav_wps", None) or []
    if len(nav_wps) < 2:
        max_dist = None  # замало точок -- покажемо все, що є
    else:
        from geo import haversine_m
        cutoff_idx = min(n_wps, len(nav_wps) - 1)
        dist = 0.0
        for i in range(cutoff_idx):
            dist += haversine_m(
                nav_wps[i].lat, nav_wps[i].lon,
                nav_wps[i + 1].lat, nav_wps[i + 1].lon,
            )
        max_dist = dist * 1.08  # трохи запасу праворуч від останньої точки

    draw_elevation_profile(
        canvas, analyzer, step_m=step_m, max_dist_m=max_dist,
        title="Профіль висоти — зліт", show_angles=True, dark=dark,
    )


def draw_altitude_before_after(
    canvas: tk.Canvas,
    dist_km_before: list, alt_before: list,
    dist_km_after: list, alt_after: list,
    crossing_km: float | None = None,
    terrain_dist_km: list | None = None, terrain_alt: list | None = None,
    dark: bool = False,
    bottleneck_km: float | None = None,
):
    """Графік висотного профілю "Було/Стало" (окремо від draw_
    elevation_profile -- та функція жорстко прив'язана до MissionAnalyzer
    і рахує ОДИН профіль, тут -- ДВА незалежні профілі з різними
    відстанями/точками, порівнюються на спільній осі відстані).

    УСІ висоти тут -- АБСОЛЮТНІ (AMSL), НЕ відносні (AGL) -- та сама
    умовність, що вже в draw_elevation_profile, щоб рельєф і профіль
    польоту були на одній осі й порівнювались візуально напряму
    (відносна висота сама по собі не показує, наскільки безпечно
    виглядає профіль щодо реального рельєфу під ним).

    dist_km_before/alt_before -- профіль ДО оптимізації висоти (ті самі
    точки, що були, з їхньою оригінальною висотою). dist_km_after/
    alt_after -- профіль ПІСЛЯ (з урахуванням нових/видалених точок).
    terrain_dist_km/terrain_alt -- профіль рельєфу (той самий формат,
    зазвичай -- ті самі точки, що й "було", бо рельєф не змінюється).
    crossing_km -- відстань до лінії розмежування (вертикальний
    маркер), None якщо перетину немає. bottleneck_km -- відстань до
    точки НАЙГІРШОГО кліренсу НА ЦЕЙ момент (ЗА ПРЯМОЮ ВКАЗІВКОЮ
    користувача: показувати в реальному часі, не тільки в кінці) --
    окремий маркер, іншим кольором за crossing_km, щоб не плутати."""
    canvas.delete("all")
    c = _colors(dark)
    width = max(canvas.winfo_width(), 200)
    height = max(canvas.winfo_height(), 150)

    if not dist_km_before and not dist_km_after:
        return

    margin_l, margin_r, margin_t, margin_b = 55, 15, 30, 42
    plot_w = max(width - margin_l - margin_r, 10)
    plot_h = max(height - margin_t - margin_b, 10)

    terrain_dist_km = terrain_dist_km or []
    terrain_alt = terrain_alt or []
    all_dist = dist_km_before + dist_km_after + terrain_dist_km
    all_alt = alt_before + alt_after + terrain_alt
    max_dist = max(all_dist) if all_dist else 1.0
    min_alt = min(all_alt) if all_alt else 0.0
    max_alt = max(all_alt) if all_alt else 100.0
    alt_range = max(max_alt - min_alt, 1.0)
    alt_pad = alt_range * 0.1
    min_alt -= alt_pad
    max_alt += alt_pad

    def x_of(dist_km_val):
        return margin_l + (dist_km_val / max_dist if max_dist > 0 else 0) * plot_w

    def y_of(alt_val):
        return margin_t + plot_h - (alt_val - min_alt) / (max_alt - min_alt) * plot_h

    # сітка й осі -- той самий мінімалістичний стиль, що й draw_elevation_profile
    n_y_ticks = 5
    for i in range(n_y_ticks + 1):
        alt_val = min_alt + (max_alt - min_alt) * i / n_y_ticks
        y = y_of(alt_val)
        canvas.create_line(margin_l, y, margin_l + plot_w, y, fill=c["grid_y"])
        canvas.create_text(margin_l - 6, y, text=f"{alt_val:.0f}", anchor="e", fill=c["text"], font=("Segoe UI", 8))

    n_x_ticks = 5
    for i in range(n_x_ticks + 1):
        dist_val = max_dist * i / n_x_ticks
        x = x_of(dist_val)
        canvas.create_line(x, margin_t, x, margin_t + plot_h, fill=c["grid_x"])
        canvas.create_text(x, margin_t + plot_h + 8, text=f"{dist_val:.1f}", anchor="n", fill=c["text"], font=("Segoe UI", 8))

    canvas.create_text(
        margin_l + plot_w / 2, height - 10, text=i18n.t("altitude_chart_x_label"),
        fill=c["text"], font=("Segoe UI", 8),
    )
    canvas.create_text(
        14, margin_t + plot_h / 2, text=i18n.t("altitude_chart_y_label"),
        fill=c["text"], font=("Segoe UI", 8), angle=90,
    )

    # вертикальний маркер лінії розмежування
    if crossing_km is not None:
        x = x_of(crossing_km)
        canvas.create_line(
            x, margin_t, x, margin_t + plot_h, fill=c["angle_dash"], dash=(4, 3), width=1,
        )
        canvas.create_text(
            x, margin_t - 4, text=i18n.t("altitude_chart_crossing_label"),
            fill=c["angle_dash"], font=("Segoe UI", 8), anchor="s",
        )

    # вертикальний маркер найгіршого кліренсу НА ЦЕЙ момент -- окремий
    # колір (жовтогарячий), щоб відрізнити від переходу кордону
    if bottleneck_km is not None:
        x = x_of(bottleneck_km)
        canvas.create_line(
            x, margin_t, x, margin_t + plot_h, fill="#ff9500", dash=(2, 2), width=1,
        )
        canvas.create_text(
            x, margin_t + plot_h + 4, text=i18n.t("altitude_chart_bottleneck_label"),
            fill="#ff9500", font=("Segoe UI", 8), anchor="n",
        )

    def draw_line(dist_list, alt_list, color, width_px, dash=None):
        if len(dist_list) < 2:
            return
        coords = []
        for d, a in zip(dist_list, alt_list):
            coords.extend([x_of(d), y_of(a)])
        kwargs = {"fill": color, "width": width_px, "smooth": False}
        if dash:
            kwargs["dash"] = dash
        canvas.create_line(*coords, **kwargs)

    # Рельєф -- малюється ПЕРШИМ (під лініями "Було/Стало"), той самий
    # колір, що й на графіку "Маршрут" (theme.chart_colors, узгоджено
    # по всьому проєкту).
    draw_line(terrain_dist_km, terrain_alt, c["terrain_line"], 2)

    # "Було" -- приглушений колір, пунктир (та сама умовність, що
    # crosswind/попереджувальні елементи по всьому проєкту: пунктир =
    # довідково/минуле, суцільна лінія = актуальне)
    draw_line(dist_km_before, alt_before, c["angle_dash"], 2, dash=(5, 3))
    draw_line(dist_km_after, alt_after, c["mission_line"], 2)

    legend_y = margin_t + 4
    canvas.create_line(margin_l + 8, legend_y, margin_l + 28, legend_y, fill=c["angle_dash"], width=2, dash=(5, 3))
    canvas.create_text(margin_l + 32, legend_y, text=i18n.t("altitude_chart_legend_before"), anchor="w", fill=c["text"], font=("Segoe UI", 8))
    canvas.create_line(margin_l + 140, legend_y, margin_l + 160, legend_y, fill=c["mission_line"], width=2)
    canvas.create_text(margin_l + 164, legend_y, text=i18n.t("altitude_chart_legend_after"), anchor="w", fill=c["text"], font=("Segoe UI", 8))
    canvas.create_line(margin_l + 270, legend_y, margin_l + 290, legend_y, fill=c["terrain_line"], width=2)
    canvas.create_text(margin_l + 294, legend_y, text=i18n.t("altitude_chart_legend_terrain"), anchor="w", fill=c["text"], font=("Segoe UI", 8))

