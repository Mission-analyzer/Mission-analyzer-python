"""
landing_view.py — отрисовка профиля глиссады захода на посадку на tk.Canvas.
Как и elevation_view.py/angle_view.py, ничего не знает про парсинг —
только берёт данные из MissionAnalyzer.landing_approach_points()/
landing_approach_profile()/landing_approach_speed_markers() и рисует.
"""

from __future__ import annotations

import tkinter as tk

from analyzer import MissionAnalyzer
import i18n


import theme


def _colors(dark: bool) -> dict:
    cc = theme.chart_colors(dark)
    if dark:
        return {
            "grid": cc["grid"], "text": cc["text"], "no_data": cc["muted"],
            "terrain_line": cc["line_terrain"],
            "profile_line": cc["line_primary"],
            "wp_dash": "#555555", "leg_label": "#cccccc",
            "speed_marker": cc["accent_green"],
            "land_marker": cc["line_primary"], "normal_marker": cc["line_terrain"],
            "marker_outline": cc["marker_outline"], "cmd_label": cc["muted"],
        }
    return {
        "grid": cc["grid"], "text": cc["text"], "no_data": cc["muted"],
        "terrain_line": cc["line_terrain"],
        "profile_line": cc["line_primary"],
        "wp_dash": "#cccccc", "leg_label": "#333333",
        "speed_marker": cc["accent_green"],
        "land_marker": cc["line_primary"], "normal_marker": cc["line_terrain"],
        "marker_outline": cc["marker_outline"], "cmd_label": cc["muted"],
    }


def draw_landing_approach(canvas: tk.Canvas, analyzer: MissionAnalyzer, n_legs: int = 3, dark: bool = False):
    """Полностью перерисовывает canvas профилем глиссады захода на посадку."""
    canvas.delete("all")
    if analyzer is None:
        return

    c = _colors(dark)
    width = max(canvas.winfo_width(), 200)
    height = max(canvas.winfo_height(), 150)

    points = analyzer.landing_approach_points(n_legs=n_legs)
    legs = analyzer.landing_approach_profile(n_legs=n_legs)
    speed_markers = analyzer.landing_approach_speed_markers(n_legs=n_legs)
    elevation = analyzer.landing_approach_elevation_profile(n_legs=n_legs)

    if not points or not legs:
        canvas.create_text(
            width / 2, height / 2, text=i18n.t("landing_no_data"),
            font=("Arial", 11), fill=c["no_data"],
        )
        return

    alts = [p["alt"] for p in points if p["alt"] is not None]
    if not alts:
        canvas.create_text(
            width / 2, height / 2, text=i18n.t("landing_no_data"),
            font=("Arial", 11), fill=c["no_data"],
        )
        return

    has_terrain = (
        analyzer.terrain is not None and elevation is not None
        and any(v is not None for v in elevation["terrain_alt"])
    )
    y_values = list(alts)
    if has_terrain:
        y_values += [v for v in elevation["terrain_alt"] if v is not None]

    by_seq = {p["seq"]: p for p in points}

    # верхняя часть холста зарезервирована под подписи: одна строка под
    # скорость, и по одной строке на каждый отрезок (лесенкой вниз, к точке
    # посадки — так видно, к какому именно участку относится подпись, даже
    # если участки короткие и подписи иначе легли бы друг на друга)
    title_y = 11
    speed_row_y = 26
    leg_rows_top = 40
    row_h = 13
    n_rows = max(len(legs), 1)
    margin_l, margin_r = 60, 20

    # на невисокому канвасі (як тепер -- окрема невелика панель під графік)
    # фіксовані відступи під підписи можуть "з'їсти" майже всю висоту, і сам
    # графік сплющується в риску. Тому підписи згори стискаємо компактніше,
    # якщо вони не влазять у розумну частку висоти (не більше половини).
    margin_b = 52
    needed_top = leg_rows_top + n_rows * row_h + 8
    available_top = height * 0.5
    if needed_top > available_top:
        scale = max(available_top / needed_top, 0.4)
        row_h = max(row_h * scale, 8)
        leg_rows_top = max(leg_rows_top * scale, 20)
        needed_top = leg_rows_top + n_rows * row_h + 8
    margin_t = needed_top
    margin_b = min(margin_b, height * 0.2)

    plot_w = max(width - margin_l - margin_r, 10)
    plot_h = max(height - margin_t - margin_b, 10)

    x_max = points[-1]["dist"] if points[-1]["dist"] > 0 else 1.0
    y_min, y_max = min(y_values), max(y_values)
    if y_max == y_min:
        y_max += 1.0
    pad = (y_max - y_min) * 0.2
    y_min -= pad
    y_max += pad

    def X(d):
        return margin_l + d / x_max * plot_w

    def Y(alt):
        return margin_t + (1 - (alt - y_min) / (y_max - y_min)) * plot_h

    def clamp_x(x, text_half_width=45):
        """Не даём подписи вылезти за левый/правый край области графика."""
        return min(max(x, margin_l + text_half_width), width - margin_r - text_half_width)

    # сетка и подписи по Y
    for i in range(6):
        val = y_min + (y_max - y_min) * i / 5
        y = Y(val)
        canvas.create_line(margin_l, y, width - margin_r, y, fill=c["grid"])
        canvas.create_text(margin_l - 6, y, text=f"{val:.0f}", anchor="e", font=("Arial", 8), fill=c["text"])
    canvas.create_text(
        margin_l, max(margin_t - 6, 6), text=i18n.t("unit_meters_axis"),
        anchor="s", font=("Arial", 7), fill=c["text"],
    )

    # вісь X -- дистанція в МЕТРАХ (не кілометрах, як на "Взліт"/
    # "Маршрут": захід на посадку -- це сотні метрів, у кілометрах усі
    # підписи звелись б до 0.0-0.3). Раніше цієї осі не було взагалі --
    # тільки окремі підписи відрізків (дистанція/азимут/кут) над самим
    # профілем, без загальної шкали.
    axis_y = height - margin_b
    canvas.create_line(margin_l, axis_y, width - margin_r, axis_y, fill=c["grid"])
    n_ticks = 5
    for i in range(n_ticks + 1):
        d = x_max * i / n_ticks
        x = X(d)
        canvas.create_line(x, axis_y, x, axis_y + 4, fill=c["grid"])
        canvas.create_text(x, axis_y + 6, text=f"{d:.0f}", anchor="n", font=("Arial", 8), fill=c["text"])
    canvas.create_text(
        width / 2, axis_y + 18, text=i18n.t("unit_meters_axis"),
        anchor="n", font=("Arial", 7), fill=c["text"],
    )

    # рельєф -- лише лінія, без заливки (та сама логіка, що й на
    # "Місія"/"Аналіз → Маршрут": просто графік рельєфу, без "плями" під ним)
    if has_terrain:
        line_pts = []
        for d, t in zip(elevation["dist"], elevation["terrain_alt"]):
            if t is None:
                continue
            line_pts.extend([X(d), Y(t)])
        if len(line_pts) >= 4:
            canvas.create_line(*line_pts, fill=c["terrain_line"], width=2)

    # линия профиля
    pts_xy = [(X(p["dist"]), Y(p["alt"])) for p in points if p["alt"] is not None]
    for i in range(len(pts_xy) - 1):
        canvas.create_line(*pts_xy[i], *pts_xy[i + 1], fill=c["profile_line"], width=2.5)

    # вертикальные пунктирные линии — от каждого вейпоинта на всю высоту
    # графика (как у команды скорости), чтобы визуально было видно, где
    # именно находится точка и к чему относятся подписи рядом с ней
    for p in points:
        if p["alt"] is None:
            continue
        x = X(p["dist"])
        canvas.create_line(x, margin_t, x, height - margin_b, fill=c["wp_dash"], dash=(2, 2))

    # подписи отрезков: дистанция / азимут / угол — лесенкой, каждый
    # следующий отрезок на строку ниже предыдущего (снижаемся к посадке
    # вместе с самим профилем, поэтому и подписи "спускаются")
    for i, leg in enumerate(legs):
        p_from = by_seq.get(leg["from_seq"])
        p_to = by_seq.get(leg["to_seq"])
        if p_from is None or p_to is None:
            continue
        xm = clamp_x((X(p_from["dist"]) + X(p_to["dist"])) / 2)
        row_y = leg_rows_top + i * row_h

        angle_txt = f"{leg['angle_deg']:+.1f}°" if leg["angle_deg"] is not None else "—"
        label = i18n.t(
            "landing_leg_label", dist=leg["distance_m"], bearing=leg["bearing_deg"], angle=angle_txt
        )
        canvas.create_text(xm, row_y, text=label, font=("Arial", 8), fill=c["leg_label"])

    # метки ограничения скорости (DO_CHANGE_SPEED) — отдельная строка выше,
    # чтобы не путаться с подписями отрезков; подписываем и саму команду
    for marker in speed_markers:
        after_pt = by_seq.get(marker["after_wp_seq"])
        if after_pt is None:
            continue
        x = clamp_x(X(after_pt["dist"]))
        canvas.create_line(x, margin_t, x, height - margin_b, fill=c["speed_marker"], dash=(3, 2))
        speed_label = i18n.t(
            "landing_speed_marker_label",
            command=marker["command_name"], speed=marker["speed"],
            speed_type=i18n.speed_type_label(marker["speed_type"]),
        )
        canvas.create_text(x, speed_row_y, text=speed_label, font=("Arial", 8, "bold"), fill=c["speed_marker"])

    # точки, подписи номеров WP и названия их собственных MAVLink-команд
    for p in points:
        if p["alt"] is None:
            continue
        x, y = X(p["dist"]), Y(p["alt"])
        marker_color = c["land_marker"] if p["is_land"] else c["normal_marker"]
        canvas.create_oval(x - 5, y - 5, x + 5, y + 5, fill=marker_color, outline=c["marker_outline"])
        canvas.create_text(x, y + 13, text=str(p["seq"]), font=("Arial", 8, "bold"), fill=c["text"])
        canvas.create_text(x, y + 25, text=p["command_name"], font=("Arial", 7), fill=c["cmd_label"])

    canvas.create_text(
        width / 2, title_y, text=i18n.t("title_landing_approach"),
        font=("Arial", 11, "bold"), fill=c["text"],
    )
