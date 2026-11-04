"""
sim_joystick.py -- Джойстик: список, подключение и калибровка.

Выделено из simulation_page.py без изменения логики (примесь к SimulationPageMixin).
"""
from __future__ import annotations

import os
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")      # pygame при первом импорте печатает приветствие «pygame-ce 2.x (SDL ...)»: оно в консоли не нужно (joystick_input ниже подтягивает pygame)
import tkinter as tk
from tkinter import ttk, messagebox
import i18n
from joystick_input import (JoystickMapping, HAS_PYGAME, save_joystick_mapping, default_joystick_mapping_path,
                            load_joystick_profiles, mapping_for_device)


class JoystickMixin:
    """Джойстик: список, подключение и калибровка."""

    def _on_sim_joystick_refresh(self):
        if not HAS_PYGAME:
            self.sim_joystick_status_var.set(i18n.t("msg_pygame_not_installed"))
            return
        names = self._sim_joystick.list_joysticks()
        self.sim_joystick_combo.configure(values=names)
        if names and not self.sim_joystick_var.get():
            self.sim_joystick_var.set(names[0])
        self.sim_joystick_status_var.set(i18n.t("status_joystick_found_fmt", n=len(names)))


    def _on_sim_joystick_connect(self):
        names = list(self.sim_joystick_combo.cget("values"))
        selected = self.sim_joystick_var.get()
        if not selected or selected not in names:
            messagebox.showwarning(i18n.t("msg_weather_title"), i18n.t("msg_no_joystick_selected"))
            return
        index = names.index(selected)
        if self._sim_joystick.connect(index):
            # калібрування -- саме цього пульта (за іменем); немає власного -- загальне (старий файл) або жодного
            self._sim_joystick_device = selected
            mapping, source = mapping_for_device(load_joystick_profiles(default_joystick_mapping_path()), selected)
            self._sim_joystick_mapping = mapping
            self.sim_joystick_status_var.set(i18n.t("status_joystick_connected_fmt", name=selected)
                                             + "  " + i18n.t(f"status_joystick_profile_{source}"))
            self.sim_joystick_calibrate_btn.configure(state="normal")
        else:
            self.sim_joystick_status_var.set(i18n.t("status_joystick_connect_failed"))


    def _on_sim_joystick_calibrate(self):
        """Візуальне калібрування (Mode 2):
        - Лівий стік  -> X=Yaw (горизонталь), Y=Throttle (вертикаль)
        - Правий стік -> X=Roll (горизонталь), Y=Pitch (вертикаль)
        - Arm, Mode   -> смужки-перемикачі.

        Switch3/Switch4 прибрані: у JoystickMapping для них немає полів,
        тому показувати їх у діалозі було оманливо (користувач
        призначав, а воно ніде не зберігалось і не використовувалось).

        Захист від ДУБЛІВ: якщо одна фізична вісь призначена на два
        канали, під сіткою з'являється попередження. Це реальна
        проблема -- одна вісь на Roll і Pitch дасть "спільне" хибне
        керування, яке потім важко діагностувати.

        Збереження інверсії: чекбокс invert зберігається НАВІТЬ якщо
        канал тимчасово знято ("--"). Інакше, повернувши канал на ту
        саму вісь, користувач втратив би свою інверсію мовчки.

        Обов'язкові канали: якщо Roll/Pitch/Throttle/Yaw не призначені,
        перед збереженням питаємо -- "продовжити?" (може, це частина
        покрокового калібрування)."""
        if not self._sim_joystick.is_connected:
            messagebox.showwarning(i18n.t("msg_weather_title"), i18n.t("msg_joystick_not_connected"))
            return

        n_axes = self._sim_joystick.num_axes()
        if n_axes == 0:
            messagebox.showwarning(i18n.t("msg_weather_title"), i18n.t("msg_joystick_no_axes"))
            return

        dlg = tk.Toplevel(self)
        dlg.title(i18n.t("msg_calibrate_title"))
        dlg.transient(self)
        # Явна ширина/висота -- інакше ttk іноді обрізає нижню
        # частину діалогу (видно як неповні Combobox для Throttle
        # або зрізані кнопки).
        dlg.geometry("400x520")

        self._reg_i18n(ttk.Label(dlg), "text", "lbl_calibrate_instructions").pack(pady=(10, 4))

        grid = ttk.Frame(dlg)
        grid.pack(pady=10)

        BOX = 90

        # --- читання поточного мапінгу ---
        # Спершу стики (реальні приводи), потім перемикачі. Якщо одна
        # вісь призначена і на стік, і на перемикач (зіпсований стан) --
        # у Combobox'і показуємо стік, а перемикач відображається як
        # "--". При збереженні це прибирає "привид" прив'язки
        # перемикача до осі стика.
        current = self._sim_joystick_mapping
        channel_for_axis = {}
        invert_for_channel = {}
        for ch, axis_field, invert_field in (
            ("Roll", "roll_axis", "roll_invert"),
            ("Pitch", "pitch_axis", "pitch_invert"),
            ("Throttle", "throttle_axis", "throttle_invert"),
            ("Yaw", "yaw_axis", "yaw_invert"),
        ):
            axis_val = getattr(current, axis_field)
            if axis_val is not None and axis_val not in channel_for_axis:
                channel_for_axis[axis_val] = ch
            invert_for_channel[ch] = getattr(current, invert_field)
        if current.arm_axis is not None and current.arm_axis not in channel_for_axis:
            channel_for_axis[current.arm_axis] = "Arm"
        if current.mode_axis is not None and current.mode_axis not in channel_for_axis:
            channel_for_axis[current.mode_axis] = "Mode"

        axis_for_channel = {ch: i for i, ch in channel_for_axis.items()}

        # Перемикачі -- верхній ряд: Arm, Mode
        BARS = [("Arm", 0, 0), ("Mode", 0, 1)]
        STICKS = [
            ("LeftStick", "Yaw", "Throttle", 1, 0),   # x=Yaw, y=Throttle
            ("RightStick", "Roll", "Pitch", 1, 1),     # x=Roll, y=Pitch
        ]
        # Каналы пульта считаются с 1 (CH1 -- первый канал EdgeTX, как на самом пульте), а pygame нумерует оси с 0.
        # В окне -- CH1..CHn, внутри и в joystick_mapping.json -- прежний номер оси с 0 (старые файлы калибровки остаются верными).
        def ch_name(axis_idx):
            return f"CH{axis_idx + 1}"

        def ch_axis(name):
            return int(name[2:]) - 1

        AXIS_CHOICES = ["--"] + [ch_name(i) for i in range(n_axes)]

        bar_widgets = []     # (canvas, dot, axis_var, label)
        stick_widgets = []   # (canvas, dot, x_channel, y_channel, x_var, y_var)
        all_axis_vars = []   # усі axis_var для відстеження дублів

        for label, row, col in BARS:
            col_frame = ttk.Frame(grid)
            col_frame.grid(row=row, column=col, padx=8, pady=4)
            ttk.Label(col_frame, text=label).pack()
            BAR_W = 28
            canvas = tk.Canvas(col_frame, width=BAR_W, height=BOX, bg="#1a1a1a", highlightthickness=1, highlightbackground="#555")
            canvas.pack(pady=4)
            canvas.create_line(0, BOX / 2, BAR_W, BOX / 2, fill="#444")
            dot = canvas.create_oval(BAR_W / 2 - 6, BOX / 2 - 6, BAR_W / 2 + 6, BOX / 2 + 6, fill="#ff9500", outline="")
            axis_var = tk.StringVar(value=ch_name(axis_for_channel[label]) if label in axis_for_channel else "--")
            ttk.Combobox(col_frame, textvariable=axis_var, values=AXIS_CHOICES, state="readonly", width=6).pack()
            bar_widgets.append((canvas, dot, axis_var, label))
            all_axis_vars.append(axis_var)

        for stick_label, x_channel, y_channel, row, col in STICKS:
            col_frame = ttk.Frame(grid)
            col_frame.grid(row=row, column=col, padx=8, pady=4)
            ttk.Label(col_frame, text=stick_label).pack()

            canvas = tk.Canvas(col_frame, width=BOX, height=BOX, bg="#1a1a1a", highlightthickness=1, highlightbackground="#555")
            canvas.pack(pady=4)
            canvas.create_line(0, BOX / 2, BOX, BOX / 2, fill="#444")
            canvas.create_line(BOX / 2, 0, BOX / 2, BOX, fill="#444")
            dot = canvas.create_oval(BOX / 2 - 6, BOX / 2 - 6, BOX / 2 + 6, BOX / 2 + 6, fill="#00c853", outline="")

            picker_row = ttk.Frame(col_frame)
            picker_row.pack()
            ttk.Label(picker_row, text=x_channel).pack(side="left", padx=(0, 2))
            x_var = tk.StringVar(value=ch_name(axis_for_channel[x_channel]) if x_channel in axis_for_channel else "--")
            ttk.Combobox(picker_row, textvariable=x_var, values=AXIS_CHOICES, state="readonly", width=5).pack(side="left")
            picker_row2 = ttk.Frame(col_frame)
            picker_row2.pack()
            ttk.Label(picker_row2, text=y_channel).pack(side="left", padx=(0, 2))
            y_var = tk.StringVar(value=ch_name(axis_for_channel[y_channel]) if y_channel in axis_for_channel else "--")
            ttk.Combobox(picker_row2, textvariable=y_var, values=AXIS_CHOICES, state="readonly", width=5).pack(side="left")

            stick_widgets.append((canvas, dot, x_channel, y_channel, x_var, y_var))
            all_axis_vars.append(x_var)
            all_axis_vars.append(y_var)

        invert_frame = ttk.Frame(dlg)
        invert_frame.pack(pady=(4, 0))
        invert_vars = {ch: tk.BooleanVar(value=invert_for_channel.get(ch, False)) for ch in ("Roll", "Pitch", "Throttle", "Yaw")}
        for ch, var in invert_vars.items():
            ttk.Checkbutton(invert_frame, text=f"{i18n.t('lbl_invert')} {ch}", variable=var).pack(side="left", padx=6)

        # --- рядок попередження про конфлікти дублювання осей ---
        conflict_var = tk.StringVar(value="")
        conflict_label = ttk.Label(dlg, textvariable=conflict_var, foreground="#ff2222")
        conflict_label.pack(pady=(4, 0))

        def _collect_assignments() -> dict:
            """Повертає {axis_str: [назва_каналу, ...]} для перевірки дублів."""
            assignments: dict = {}
            for _canvas, _dot, axis_var, label in bar_widgets:
                v = axis_var.get()
                if v != "--":
                    assignments.setdefault(v, []).append(label)
            for _canvas, _dot, x_ch, y_ch, x_var, y_var in stick_widgets:
                v = x_var.get()
                if v != "--":
                    assignments.setdefault(v, []).append(x_ch)
                v = y_var.get()
                if v != "--":
                    assignments.setdefault(v, []).append(y_ch)
            return assignments

        def _on_axis_change(*_args):
            """Викликається при будь-якій зміні -- оновлює попередження."""
            assignments = _collect_assignments()
            conflicts = {axis: names for axis, names in assignments.items() if len(names) > 1}
            if conflicts:
                parts = [f"{axis}: {', '.join(names)}" for axis, names in sorted(conflicts.items(), key=lambda kv: ch_axis(kv[0]))]
                conflict_var.set("⚠ " + "; ".join(parts))
            else:
                conflict_var.set("")

        # trace після створення ВСІХ змінних -- щоб _on_axis_change бачив
        # повний набір і не викликався на кожній половині ініціалізації
        for var in all_axis_vars:
            var.trace_add("write", _on_axis_change)
        _on_axis_change()  # початковий стан

        running = [True]

        def _update_dots():
            if not running[0] or not dlg.winfo_exists():
                return
            # poll() навмисно НЕ викликаємо -- JoystickManager.poll() є
            # no-op (pygame-цикл живе у власному фоновому потоці),
            # а raw_axis() читає вже готовий кеш.
            for canvas, dot, axis_var, label in bar_widgets:
                sel = axis_var.get()
                if sel == "--":
                    continue
                v = self._sim_joystick.raw_axis(ch_axis(sel))
                BAR_W = 28
                y = BOX / 2 - v * (BOX / 2 - 8)
                canvas.coords(dot, BAR_W / 2 - 6, y - 6, BAR_W / 2 + 6, y + 6)
            for canvas, dot, x_channel, y_channel, x_var, y_var in stick_widgets:
                x_sel, y_sel = x_var.get(), y_var.get()
                vx = self._sim_joystick.raw_axis(ch_axis(x_sel)) if x_sel != "--" else 0.0
                vy = self._sim_joystick.raw_axis(ch_axis(y_sel)) if y_sel != "--" else 0.0
                x = BOX / 2 + vx * (BOX / 2 - 8)
                y = BOX / 2 - vy * (BOX / 2 - 8)
                canvas.coords(dot, x - 6, y - 6, x + 6, y + 6)
            dlg.after(30, _update_dots)

        def _on_save():
            # Обов'язкові канали -- якщо не призначені, попереджаємо,
            # але не блокуємо (покрокове калібрування -- валідний сценарій).
            assigned_channels = set()
            for _canvas, _dot, axis_var, label in bar_widgets:
                if axis_var.get() != "--":
                    assigned_channels.add(label)
            for _canvas, _dot, x_ch, y_ch, x_var, y_var in stick_widgets:
                if x_var.get() != "--":
                    assigned_channels.add(x_ch)
                if y_var.get() != "--":
                    assigned_channels.add(y_ch)
            missing = [ch for ch in ("Roll", "Pitch", "Throttle", "Yaw") if ch not in assigned_channels]
            if missing:
                proceed = messagebox.askyesno(
                    i18n.t("msg_calibrate_title"),
                    i18n.t("msg_calibrate_missing_channels_fmt", channels=", ".join(missing)),
                )
                if not proceed:
                    return  # діалог залишається відкритим, _update_dots продовжується

            running[0] = False

            mapping = JoystickMapping()
            field_by_channel = {
                "Roll": ("roll_axis", "roll_invert"),
                "Pitch": ("pitch_axis", "pitch_invert"),
                "Throttle": ("throttle_axis", "throttle_invert"),
                "Yaw": ("yaw_axis", "yaw_invert"),
            }
            for _canvas, _dot, axis_var, label in bar_widgets:
                sel = axis_var.get()
                if sel == "--":
                    continue
                axis_idx = ch_axis(sel)
                if label == "Arm":
                    mapping.arm_axis = axis_idx
                elif label == "Mode":
                    mapping.mode_axis = axis_idx
            for _canvas, _dot, x_ch, y_ch, x_var, y_var in stick_widgets:
                for channel, axis_var in ((x_ch, x_var), (y_ch, y_var)):
                    axis_field, invert_field = field_by_channel[channel]
                    # інверсію зберігаємо ЗАВЖДИ (навіть якщо канал
                    # знято "--") -- інакше повернення каналу на ту
                    # саму вісь мовчки скидало б галочку
                    setattr(mapping, invert_field, invert_vars[channel].get())
                    sel = axis_var.get()
                    if sel == "--":
                        continue
                    setattr(mapping, axis_field, ch_axis(sel))
            device = getattr(self, "_sim_joystick_device", None)
            save_joystick_mapping(default_joystick_mapping_path(), mapping, device)   # під іменем цього пульта
            self._sim_joystick_mapping = mapping
            if device:
                self.sim_joystick_status_var.set(i18n.t("status_joystick_connected_fmt", name=device)
                                                 + "  " + i18n.t("status_joystick_profile_own"))
            dlg.destroy()
            messagebox.showinfo(i18n.t("msg_calibrate_title"), i18n.t("msg_calibrate_done"))

        def _on_close():
            running[0] = False
            dlg.destroy()

        btn_row = ttk.Frame(dlg)
        btn_row.pack(pady=10)
        self._reg_i18n(ttk.Button(btn_row, command=_on_save), "text", "btn_save_profile").pack(side="left", padx=4)
        self._reg_i18n(ttk.Button(btn_row, command=_on_close), "text", "btn_sim_stop").pack(side="left", padx=4)

        dlg.protocol("WM_DELETE_WINDOW", _on_close)
        _update_dots()
