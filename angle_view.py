"""
angle_view.py — отрисовка графика угла наклона траектории (набор/снижение)
на tk.Canvas. Как и elevation_view.py, ничего не знает про парсинг —
только берёт данные из MissionAnalyzer.flight_path_angle_profile() и рисует.
"""

from __future__ import annotations

import tkinter as tk

from analyzer import MissionAnalyzer
import i18n


import theme


def _colors(dark: bool) -> dict:
    cc = theme.chart_colors(dark)
    return {
        "grid_y": cc["grid"], "grid_x": cc["grid_minor"],
        "text": cc["text"], "wp_label": cc["muted"],
        "zero_line": "#777777" if dark else "#999999",
        "threshold": cc["line_primary"],
        "normal_segment": cc["line_terrain"], "over_segment": cc["line_primary"],
        "connector": cc["muted"],
    }


def draw_angle_profile(canvas: tk.Canvas, analyzer: MissionAnalyzer, dark: bool = False):
    """Полностью перерисовывает canvas графиком угла траектории."""
    canvas.delete("all")
    if analyzer is None:
        return

    c = _colors(dark)
    width = max(canvas.winfo_width(), 200)
    height = max(canvas.winfo_height(), 150)

    try:
        segments = analyzer.flight_path_angle_profile()
    except ValueError:
        return

    angles = [s["angle"] for s in segments if s["angle"] is not None]
    if not angles:
        return

    margin_l, margin_r, margin_t, margin_b = 55, 15, 25, 40
    plot_w = max(width - margin_l - margin_r, 10)
    plot_h = max(height - margin_t - margin_b, 10)

    thr = analyzer.angle_max
    y_min = min(-thr * 1.6, min(angles))
    y_max = max(thr * 1.6, max(angles))
    pad = (y_max - y_min) * 0.08
    y_min -= pad
    y_max += pad

    x_max_km = segments[-1]["dist_end"] / 1000
    if x_max_km <= 0:
        x_max_km = 1.0

    def X(d_km):
        return margin_l + d_km / x_max_km * plot_w

    def Y(ang):
        return margin_t + (1 - (ang - y_min) / (y_max - y_min)) * plot_h

    # сетка и подписи по Y
    for i in range(6):
        val = y_min + (y_max - y_min) * i / 5
        y = Y(val)
        canvas.create_line(margin_l, y, width - margin_r, y, fill=c["grid_y"])
        canvas.create_text(margin_l - 6, y, text=f"{val:+.1f}", anchor="e", font=("Arial", 8), fill=c["text"])
    canvas.create_text(
        margin_l, max(margin_t - 6, 6), text=i18n.t("unit_degrees_axis"),
        anchor="s", font=("Arial", 7), fill=c["text"],
    )

    # лёгкая вертикальная сетка по точкам маршрута + подписи номеров WP
    shown = set()
    for s in segments:
        for idx, xd in ((s["from_seq"], s["dist_start"]), (s["to_seq"], s["dist_end"])):
            if idx in shown:
                continue
            shown.add(idx)
            x = X(xd / 1000)
            canvas.create_line(x, margin_t, x, height - margin_b, fill=c["grid_x"])
            canvas.create_text(x, height - margin_b + 14, text=str(idx), font=("Arial", 8), fill=c["wp_label"])
    canvas.create_text(
        width / 2, height - margin_b + 26, text=i18n.t("label_waypoint_axis"),
        anchor="n", font=("Arial", 7), fill=c["text"],
    )

    # нулевая линия (горизонтальный полёт)
    canvas.create_line(margin_l, Y(0), width - margin_r, Y(0), fill=c["zero_line"])

    # пороговые линии +thr / -thr
    for sign in (1, -1):
        y = Y(sign * thr)
        canvas.create_line(margin_l, y, width - margin_r, y, fill=c["threshold"], width=1.5, dash=(5, 3))
        canvas.create_text(
            width - margin_r - 4, y - 10, text=f"{sign * thr:+.0f}°",
            anchor="e", font=("Arial", 8, "bold"), fill=c["threshold"],
        )

    # сам профиль угла: ступенчатый график, один сегмент = одна горизонтальная линия
    prev_end_xy = None
    for s in segments:
        if s["angle"] is None:
            prev_end_xy = None
            continue
        x1, x2 = X(s["dist_start"] / 1000), X(s["dist_end"] / 1000)
        y = Y(s["angle"])
        color = c["over_segment"] if abs(s["angle"]) > thr else c["normal_segment"]
        canvas.create_line(x1, y, x2, y, fill=color, width=2.5)
        if prev_end_xy is not None:
            px, py = prev_end_xy
            canvas.create_line(px, py, x1, y, fill=c["connector"], width=1)
        prev_end_xy = (x2, y)

    canvas.create_text(
        width / 2, 12,
        text=i18n.t("title_angle_profile"),
        font=("Arial", 11, "bold"), fill=c["text"],
    )
