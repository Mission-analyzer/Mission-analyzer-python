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
