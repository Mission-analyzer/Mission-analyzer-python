"""
Окремий скрипт: розподіл кліренсу над рельєфом для одної чи двох
місій (.waypoints), накладених на одній координатній сітці для
порівняння "було/стало". Лежить у тій самій теці, що й уся програма --
перевикористовує її ж модулі (waypoints, srtm, geo, theme) і той самий
SRTM, що й вона.

Запуск: просто python clearance_distribution.py -- дві кнопки, «Місія 1»
і «Місія 2», кожна відкриває свій діалог вибору файлу. Обидві місії
малюються одночасно на спільній сітці значень кліренсу.

Тільки стандартна бібліотека Python (той самий принцип, що й уся
програма) -- жодних matplotlib чи інших зовнішніх залежностей.
"""
import os
import sys
import json
import math
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from waypoints import parse_waypoints
from srtm import SRTMTerrain, SRTMError
from geo import haversine_m
import theme

SAMPLE_STEP_M = 25.0  # той самий крок семплювання, що й у самому алгоритмі оптимізації

# палітра для накладених ліній -- відрізняється від "було/стало"
# основної програми (там завжди рівно дві лінії), тут може бути будь-яка
# кількість файлів одразу
LINE_COLORS = [
    "#4da6ff", "#ff9500", "#7ee787", "#ff6b6b", "#c792ea", "#ffd166",
]


def _find_srtm_dir(base_dir: str) -> str:
    """Той самий спосіб, що й основна програма: шлях у settings.json
    поруч зі скриптом, інакше -- тека "srtm" поруч зі скриптом за
    замовчуванням (те саме значення за замовчуванням, що й у app.py)."""
    settings_path = os.path.join(base_dir, "settings.json")
    srtm_rel = "srtm"
    if os.path.isfile(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            srtm_rel = data.get("srtm_dir", "srtm") or "srtm"
        except (OSError, json.JSONDecodeError):
            pass  # не вдалось прочитати -- падаємо на запасне значення, не зриваємо запуск
    if os.path.isabs(srtm_rel):
        return srtm_rel
    return os.path.join(base_dir, srtm_rel)


def compute_clearance_samples(path: str, terrain: SRTMTerrain) -> list[float]:
    """Кліренс (абсолютна_висота - рельєф) у кожній семпльованій точці
    вздовж усього маршруту -- ті самі точки, що бачить сам алгоритм
    оптимізації (крок 25м уздовж кожного ребра), не тільки у вейпоінтах."""
    wps = parse_waypoints(path)
    nav_wps = [wp for wp in wps if wp.is_nav_point]
    if len(nav_wps) < 2:
        return []

    home_amsl = nav_wps[0].alt  # той самий підхід, що й MissionAnalyzer, коли home не задано окремо

    def absolute_alt(wp):
        # frame 0/2 -- абсолютна (AMSL) чи відносна до рівня моря висота
        # вже сама по собі, frame 3/10 (та інші "relative") -- додаємо home
        if wp.frame in (0, 2):
            return wp.alt
        return home_amsl + wp.alt

    samples = []
    for i in range(len(nav_wps) - 1):
        a, b = nav_wps[i], nav_wps[i + 1]
        abs_a, abs_b = absolute_alt(a), absolute_alt(b)
        dist = haversine_m(a.lat, a.lon, b.lat, b.lon)
        if dist < 1.0:
            continue
        n_steps = max(1, int(dist // SAMPLE_STEP_M))
        for s in range(n_steps + 1):
            frac = s / n_steps
            lat = a.lat + (b.lat - a.lat) * frac
            lon = a.lon + (b.lon - a.lon) * frac
            mission_alt = abs_a + (abs_b - abs_a) * frac
            try:
                ground = terrain.get_elevation(lat, lon)
            except SRTMError:
                continue  # немає тайлу саме тут -- пропускаємо цю точку, не вигадуємо число
            samples.append(mission_alt - ground)
    return samples


class ClearanceDistributionApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Розподіл кліренсу місії")
        self.geometry("1000x650")
        self.configure(bg="#1e1e1e")
        self.dark = True

        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.srtm_dir = _find_srtm_dir(self.base_dir)

        top = tk.Frame(self, bg="#1e1e1e")
        top.pack(fill="x", padx=8, pady=6)
        ttk.Button(top, text="Місія 1 (було)...", command=lambda: self._on_pick_file(0)).pack(side="left")
        ttk.Button(top, text="Місія 2 (стало)...", command=lambda: self._on_pick_file(1)).pack(side="left", padx=(6, 0))
        self.status_var = tk.StringVar(value=f"SRTM: {self.srtm_dir}")
        tk.Label(top, textvariable=self.status_var, bg="#1e1e1e", fg="#cccccc",
                 font=("Segoe UI", 9)).pack(side="left", padx=(12, 0))

        self.canvas = tk.Canvas(self, bg="#1e1e1e", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.canvas.bind("<Configure>", lambda e: self._redraw())

        # РІВНО два слоти (не довільний список) -- ЗА ПРЯМОЮ ВКАЗІВКОЮ
        # користувача: явно "місія 1" і "місія 2", а не мультивибір через
        # Ctrl+клік у діалозі, який менш очевидний. Обидва завжди
        # малюються на ОДНІЙ і тій самій координатній сітці (спільний
        # діапазон значень рахується в _redraw() з усіх завантажених
        # слотів одразу) -- це вже так само працювало й раніше, тут
        # лише інтерфейс вибору файлів став явнішим.
        self._slots = [None, None]  # кожен -- (label, [кліренси], color) чи None, якщо слот порожній

    def _on_pick_file(self, slot_idx):
        path = filedialog.askopenfilename(
            title=f"Обрати файл місії {slot_idx + 1} (.waypoints)",
            filetypes=[("Waypoints", "*.waypoints"), ("Усі файли", "*.*")],
        )
        if not path:
            return

        if not os.path.isdir(self.srtm_dir):
            messagebox.showwarning(
                "SRTM не знайдено",
                f"Тека з тайлами рельєфу не знайдена:\n{self.srtm_dir}\n\n"
                "Перевір, що скрипт лежить у тій самій теці, що й програма, "
                "і що SRTM-тека є поруч.",
            )
            return

        try:
            terrain = SRTMTerrain(self.srtm_dir, auto_download=False)
        except SRTMError as e:
            messagebox.showerror("Помилка SRTM", str(e))
            return

        try:
            samples = compute_clearance_samples(path, terrain)
        except Exception as e:
            messagebox.showerror("Помилка читання місії", f"{path}:\n{e}")
            return
        if not samples:
            messagebox.showwarning("Порожньо", f"Не вдалось отримати жодного семплу кліренсу для:\n{path}")
            return

        label = os.path.basename(path)
        color = LINE_COLORS[slot_idx % len(LINE_COLORS)]
        self._slots[slot_idx] = (label, samples, color)

        loaded = sum(1 for s in self._slots if s is not None)
        self.status_var.set(f"SRTM: {self.srtm_dir}  |  завантажено місій: {loaded}/2")
        self._redraw()

    def _redraw(self):
        self.canvas.delete("all")
        self._series = [s for s in self._slots if s is not None]
        if not self._series:
            self.canvas.create_text(
                20, 20, anchor="nw", fill="#888888", font=("Segoe UI", 11),
                text="Обери «Місія 1» і, за бажанням, «Місія 2» -- обидві намалюються на одній сітці для порівняння",
            )
            return

        width = max(self.canvas.winfo_width(), 200)
        height = max(self.canvas.winfo_height(), 150)
        margin_l, margin_r, margin_t, margin_b = 60, 20, 20, 40
        plot_w = width - margin_l - margin_r
        plot_h = height - margin_t - margin_b

        all_values = [v for _, samples, _ in self._series for v in samples]
        v_min, v_max = min(all_values), max(all_values)
        if v_max - v_min < 1.0:
            v_max = v_min + 1.0
        # трохи запасу по краях, щоб крайні стовпчики не впирались у рамку
        pad = (v_max - v_min) * 0.03
        v_min -= pad
        v_max += pad

        n_bins = 60
        bin_w = (v_max - v_min) / n_bins

        def x_of(v):
            return margin_l + (v - v_min) / (v_max - v_min) * plot_w

        def y_of(frac_count):
            return margin_t + plot_h - frac_count * plot_h

        # гістограми (як частка від загальної кількості семплів у СВОЄМУ
        # файлі -- щоб файли з різною кількістю точок/довжиною маршруту
        # порівнювались чесно, не за абсолютною кількістю)
        max_frac = 0.0
        histograms = []
        for label, samples, color in self._series:
            counts = [0] * n_bins
            for v in samples:
                b = int((v - v_min) / bin_w)
                b = max(0, min(n_bins - 1, b))
                counts[b] += 1
            total = len(samples)
            fracs = [c / total for c in counts]
            max_frac = max(max_frac, max(fracs))
            histograms.append((label, fracs, color, samples))

        c = theme.chart_colors(self.dark)

        # сітка по X (значення кліренсу)
        n_grid = 8
        for i in range(n_grid + 1):
            v = v_min + (v_max - v_min) * i / n_grid
            x = x_of(v)
            self.canvas.create_line(x, margin_t, x, margin_t + plot_h, fill=c["grid_minor"])
            self.canvas.create_text(x, margin_t + plot_h + 4, anchor="n", fill=c["text"],
                                     font=("Segoe UI", 8), text=f"{v:.0f}м")

        self.canvas.create_line(margin_l, margin_t, margin_l, margin_t + plot_h, fill=c["grid"])
        self.canvas.create_line(margin_l, margin_t + plot_h, margin_l + plot_w, margin_t + plot_h, fill=c["grid"])

        # вертикальна лінія нульового кліренсу -- завжди показуємо, якщо
        # взагалі потрапляє в діапазон, бо це фізична межа "врізались у рельєф"
        if v_min < 0 < v_max:
            x0 = x_of(0)
            self.canvas.create_line(x0, margin_t, x0, margin_t + plot_h, fill="#ff4444", dash=(3, 2), width=1)
            self.canvas.create_text(x0, margin_t - 2, anchor="s", fill="#ff4444",
                                     font=("Segoe UI", 8), text="0м (врізання)")

        # самі розподіли -- ламана лінія по серединах бінів (не суцільні
        # прямокутники, щоб кілька файлів можна було розрізнити накладеними)
        for label, fracs, color, samples in histograms:
            points = []
            for i, f in enumerate(fracs):
                v_center = v_min + bin_w * (i + 0.5)
                points.append((x_of(v_center), y_of(f / max_frac if max_frac > 0 else 0)))
            coords = [c for p in points for c in p]
            if len(points) >= 2:
                self.canvas.create_line(*coords, fill=color, width=2, smooth=True)

        # легенда -- назва файлу, колір лінії, медіана й мінімум кліренсу
        # (мінімум -- найважливіше практичне число: найгірше місце маршруту)
        ly = margin_t + 6
        for label, fracs, color, samples in histograms:
            sorted_s = sorted(samples)
            median = sorted_s[len(sorted_s) // 2]
            worst = sorted_s[0]
            self.canvas.create_rectangle(margin_l + 8, ly, margin_l + 22, ly + 10, fill=color, outline="")
            self.canvas.create_text(
                margin_l + 28, ly + 5, anchor="w", fill=c["text"], font=("Segoe UI", 9),
                text=f"{label}   медіана={median:.1f}м   мін={worst:.1f}м   n={len(samples)}",
            )
            ly += 18

        self.canvas.create_text(
            margin_l + plot_w / 2, height - 6, anchor="s", fill=c["text"],
            font=("Segoe UI", 9), text="Кліренс над рельєфом, м (частка семплів маршруту, нормовано на пік кожного файлу)",
        )


if __name__ == "__main__":
    app = ClearanceDistributionApp()
    app.mainloop()
