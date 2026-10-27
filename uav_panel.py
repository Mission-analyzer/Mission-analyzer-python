"""
uav_panel.py -- Блок «3D-модель литака» (вкладка «Літак»): файл, единицы, ось, нос, сведения и миниатюра.

Выделено из scene_config_panel.py без изменения логики.
"""
from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog, ttk
import scene_library as lib
from scene_mesh import MeshError
from scene_object import SceneObject
import config_text
from config_common import DEFAULT_UNIT, UNIT_FACTORS, describe_result
from config_text import LABELS, label
from ui_screen import content_changed, flow
from ui_theme import theme_colors, watch_theme

NOSE_VERSION = 2          # версия правила носа в настройках: 2 = есть «авто» (по форме модели); до этого нос был просто 0° по умолчанию


class UavModelPanel(ttk.Frame):
    """3D-модель литака (БПЛА): файл модели, единицы, ось «вгору», куда смотрит нос, увеличение на картинке, сведения о модели (грани, единицы,
    размеры) и миниатюра вида сбоку (нос должен смотреть вправо). Модель нужна виду сбоку на облёт; панель стоит на вкладке «Літак»."""

    NOSE = (0, 90, 180, 270)
    PREVIEW = (360, 170)
    REFRESH_MS = 150

    def __init__(self, parent, settings: dict | None = None, on_settings=None, library_dir: str | None = None):
        super().__init__(parent)
        self.on_settings = on_settings
        self.library_dir = library_dir or lib.models_dir()
        st = dict(settings or {})
        self._texts: list = []
        self._raw_cache = None                                   # (путь, время изменения, ось) -> Mesh в исходных единицах
        self._after = None
        self._photo = None
        self._auto_model = False
        self.last_error = ""
        units0 = st.get("units") if st.get("units") in UNIT_FACTORS else DEFAULT_UNIT       # «авто» убрано: единицы задаются явно, по умолчанию мм
        self.v_units = tk.StringVar(value=label("units_" + units0))
        self.v_up = tk.StringVar(value=str(st.get("up", "Y")) if str(st.get("up", "Y")) in ("Y", "Z") else "Y")
        self.v_nose = tk.StringVar()
        try:
            self.v_magnify = tk.StringVar(value=f"{float(st.get('magnify', 1.0)):g}")
        except (TypeError, ValueError):
            self.v_magnify = tk.StringVar(value="1")
        self.v_uav = tk.StringVar()
        if "uav_ref" in st:
            self._uav_ref = str(st.get("uav_ref") or "")
        else:                                                    # настройки ещё нет: ищем XX-1 в папке моделей по имени
            found = lib.find_model_by_hint(self.library_dir, "xx1")
            self._uav_ref, self._auto_model = found or "", bool(found)
        # нос модели: «авто» (по форме модели) или явный угол. Настройки, сохранённые до появления «авто» (без отметки nose_v), не переносятся:
        # прежнее «0°» было просто значением по умолчанию, а не выбором пользователя
        self._nose_memo = None
        try:
            saved_nose = int(st["nose"]) if (st.get("nose_v") == NOSE_VERSION and st.get("nose") != "auto") else "auto"
            self._nose_pick = saved_nose if saved_nose in self.NOSE else "auto"
        except (KeyError, TypeError, ValueError):
            self._nose_pick = "auto"
        self._build()
        self.retranslate()

    # ------------------------------------------------------------------ интерфейс
    def _text(self, widget, key: str):
        self._texts.append((widget, key))
        return widget

    def _build(self) -> None:
        pad = {"padx": 4, "pady": 2}
        self.columnconfigure(1, weight=1)
        self._text(ttk.Label(self), "fl_uav").grid(row=0, column=0, sticky="e", **pad)
        self.uav_combo = ttk.Combobox(self, textvariable=self.v_uav, width=34, postcommand=self._refresh_models)
        self.uav_combo.grid(row=0, column=1, sticky="we", **pad)
        self.uav_combo.bind("<<ComboboxSelected>>", lambda _e: self._changed())
        self.uav_combo.bind("<Return>", lambda _e: self._changed())
        self.uav_combo.bind("<FocusOut>", lambda _e: self._changed())
        box = ttk.Frame(self)
        box.grid(row=0, column=2, sticky="w", **pad)
        self._text(ttk.Button(box, command=self._browse_folder), "btn_browse_folder").pack(side="left")
        self._text(ttk.Button(box, command=self._browse_file), "btn_browse").pack(side="left", padx=(4, 0))

        row = ttk.Frame(self)
        row.grid(row=1, column=0, columnspan=3, sticky="w", **pad)
        pairs: list = []                                                  # пары «подпись + поле»: ряд переносится, когда окно узкое

        def pair() -> ttk.Frame:
            p = ttk.Frame(row)
            pairs.append(p)
            return p
        p1 = pair()
        self._text(ttk.Label(p1), "fl_units").pack(side="left", padx=(8, 2))
        self.units_combo = ttk.Combobox(p1, textvariable=self.v_units, state="readonly", width=18, values=UavModelPanel._unit_values())
        self.units_combo.pack(side="left")
        p2 = pair()
        self._text(ttk.Label(p2), "fl_up").pack(side="left", padx=(10, 2))
        self.up_combo = ttk.Combobox(p2, textvariable=self.v_up, state="readonly", width=4, values=("Y", "Z"))
        self.up_combo.pack(side="left")
        p3 = pair()
        self._text(ttk.Label(p3), "fl_nose").pack(side="left", padx=(10, 2))
        self.nose_combo = ttk.Combobox(p3, textvariable=self.v_nose, state="readonly", width=16)
        self.nose_combo.pack(side="left")
        p4 = pair()
        self._text(ttk.Label(p4), "fl_magnify").pack(side="left", padx=(10, 2))
        self.magnify_entry = ttk.Entry(p4, textvariable=self.v_magnify, width=6)
        self.magnify_entry.pack(side="left")
        self._flow_rows = [(row, pairs)]
        self._flow_width = -1
        self._low_narrow = None
        for combo in (self.units_combo, self.up_combo, self.nose_combo):
            combo.bind("<<ComboboxSelected>>", lambda _e: self._changed())
        self.magnify_entry.bind("<FocusOut>", lambda _e: self._changed())
        self.magnify_entry.bind("<Return>", lambda _e: self._changed())

        self._info_bad = False
        self.info_label = ttk.Label(self, wraplength=600, justify="left")
        self.info_label.grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=(4, 2))
        low = ttk.Frame(self)
        low.grid(row=3, column=0, columnspan=3, sticky="w", padx=6, pady=(2, 4))
        self._low = low
        self.preview_label = ttk.Label(low)
        self.hint_label = ttk.Label(low, wraplength=420, justify="left")
        self._layout_low(False)
        self.bind("<Configure>", self._on_resize, add="+")
        watch_theme(self, self._recolor)                                       # цвета подсказки и ошибки -- из текущей темы (ui_theme)

    def _layout_low(self, narrow: bool) -> None:
        """Миниатюра и подсказка: рядом на широком окне, подсказка под миниатюрой на узком (< 700 px)."""
        if narrow == self._low_narrow:
            return
        self._low_narrow = narrow
        self.preview_label.grid(row=0, column=0)
        if narrow:
            self.hint_label.grid(row=1, column=0, sticky="w", pady=(4, 0), padx=0)
        else:
            self.hint_label.grid(row=0, column=1, padx=12, sticky="n")

    def _on_resize(self, event=None) -> None:
        w = self.winfo_width()
        if w <= 1 or abs(w - self._flow_width) < 4:
            return
        self._flow_width = w
        for container, items in self._flow_rows:
            flow(container, items, max(200, w - 24))
        self._layout_low(w < 700)
        self.info_label.configure(wraplength=max(160, w - 40))
        self.hint_label.configure(wraplength=max(160, w - 40) if self._low_narrow else max(160, min(420, w - self.PREVIEW[0] - 60)))
        content_changed(self)

    def _recolor(self, colors: dict) -> None:
        self.hint_label.configure(foreground=colors["dim"])
        self.info_label.configure(foreground=colors["bad"] if self._info_bad else colors["dim"])

    def _set_info(self, text: str, bad: bool) -> None:
        self._info_bad = bad
        self.info_label.configure(text=text, foreground=theme_colors(self.info_label)["bad" if bad else "dim"])

    @staticmethod
    def _unit_values() -> list[str]:
        return [label("units_" + key) for key in UNIT_FACTORS]

    def retranslate(self) -> None:
        """Подписи на текущем языке. Выбранные значения сохраняются: при первом вызове берутся из настроек, потом -- из самих полей."""
        cur_units = self._units_key()
        cur_nose = self._nose_choice() if self.v_nose.get() else self._nose_pick
        cur_uav = self._uav_ref_value() if self.v_uav.get() else self._uav_ref
        for widget, key in self._texts:
            widget.configure(text=label(key))
        self.units_combo.configure(values=UavModelPanel._unit_values())
        self.v_units.set(label("units_" + cur_units))
        self.nose_combo.configure(values=[label("fl_nose_auto")] + [label(f"fl_nose{d}") for d in self.NOSE])
        self.v_nose.set(label("fl_nose_auto") if cur_nose == "auto" else label(f"fl_nose{cur_nose}"))
        self._nose_pick = cur_nose
        self._refresh_models()
        self._uav_ref = cur_uav
        self.v_uav.set(cur_uav if cur_uav else label("fl_builtin"))
        self.hint_label.configure(text=label("ac_hint"))
        self.refresh_info()

    def _units_key(self) -> str:
        text = self.v_units.get().strip()
        for lang in ("uk", "en"):
            for key in UNIT_FACTORS:
                if text == LABELS[lang]["units_" + key]:
                    return key
        return DEFAULT_UNIT

    def _nose_choice(self):
        """Выбор в списке: "auto" или явный угол (0, 90, 180, 270)."""
        text = self.v_nose.get().strip()
        if text in (LABELS["uk"]["fl_nose_auto"], LABELS["en"]["fl_nose_auto"]):
            return "auto"
        for d in self.NOSE:
            if text in (LABELS["uk"][f"fl_nose{d}"], LABELS["en"][f"fl_nose{d}"]):
                return d
        return "auto"

    def _nose_info(self) -> dict:
        """Что определилось по форме модели: {angle, reliable, reason}. У встроенного макета нос известен (0°); ошибка загрузки -- 0° и «ненадёжно»."""
        import flyby_view as fv
        ref = self._uav_ref_value()
        if not ref:
            return {"angle": 0.0, "reliable": True, "confidence": 1.0, "reason": "встроенный макет"}
        try:
            raw = self._raw_mesh(ref)
        except Exception as exc:
            return {"angle": 0.0, "reliable": False, "confidence": 0.0, "reason": f"модель не прочиталась: {exc}"}
        if self._nose_memo is None or self._nose_memo[0] != id(raw):
            self._nose_memo = (id(raw), fv.detect_nose_offset(raw))
        return self._nose_memo[1]

    def _nose_value(self) -> float:
        """Угол, на который реально поворачивается модель: явный выбор или определённый по форме (при «авто»)."""
        choice = self._nose_choice()
        return float(self._nose_info()["angle"]) if choice == "auto" else float(choice)

    def _uav_ref_value(self) -> str:
        text = self.v_uav.get().strip()
        return "" if text in (LABELS["uk"]["fl_builtin"], LABELS["en"]["fl_builtin"], "") else text

    def _refresh_models(self) -> None:
        entries = lib.scan_models(self.library_dir)
        self.uav_combo.configure(values=[label("fl_builtin")] + [e.ref for e in entries])

    def _browse_file(self) -> None:
        path = filedialog.askopenfilename(title=label("browse_title"), initialdir=self.library_dir if os.path.isdir(self.library_dir) else None,
                                          filetypes=[(label("ft_models"), "*.obj *.glb *.gltf"), (label("ft_all"), "*.*")])
        if path:
            self.v_uav.set(lib.to_reference(path, self.library_dir))
            self._changed()

    def _browse_folder(self) -> None:
        path = filedialog.askdirectory(title=label("browse_folder_title"), initialdir=self.library_dir if os.path.isdir(self.library_dir) else None,
                                       mustexist=True)
        if path and os.path.abspath(path) != os.path.abspath(self.library_dir):
            self.v_uav.set(lib.to_reference(path, self.library_dir))
            self._changed()

    # ------------------------------------------------------------------ настройки
    def settings(self) -> dict:
        return {"uav_ref": self._uav_ref_value(), "units": self._units_key(), "up": self.v_up.get(), "nose": self._nose_choice(),
                "nose_v": NOSE_VERSION, "magnify": self.v_magnify.get()}

    def _changed(self) -> None:
        self._uav_ref = self._uav_ref_value()
        self._nose_pick = self._nose_choice()
        self._auto_model = False
        if self.on_settings is not None:
            try:
                self.on_settings(self.settings())
            except Exception:
                pass
        if self._after is not None:
            try:
                self.after_cancel(self._after)
            except tk.TclError:
                pass
        self._after = self.after(self.REFRESH_MS, self.refresh_info)

    # ------------------------------------------------------------------ модель
    def _raw_mesh(self, ref: str):
        """Модель из папки моделей в исходных единицах (коэффициент 1); читается один раз, пока не изменились файл и ось «вгору»."""
        from scene_mesh import load_mesh
        path = lib.resolve_model(ref, self.library_dir)
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            stamp = 0.0
        key = (path, stamp, self.v_up.get())
        if self._raw_cache is not None and self._raw_cache[0] == key:
            if isinstance(self._raw_cache[1], Exception):
                raise self._raw_cache[1]
            return self._raw_cache[1]
        mesh = load_mesh(path, self.v_up.get(), 1.0)
        self._raw_cache = (key, mesh)
        return mesh

    def _resolve(self):
        """(Mesh в метрах, ключ единицы или None для макета, определены ли единицы автоматически, размер исходной модели)."""
        import dataclasses
        import flyby_view as fv
        ref = self._uav_ref_value()
        if not ref:
            return fv.default_uav_mesh(), None, False
        try:
            raw = self._raw_mesh(ref)
        except MeshError as exc:
            reason = describe_result(SceneObject("x", 0.0, 0.0, 0.0, ""), (exc, None)).split(": ", 1)[-1]
            raise ValueError(label("ac_err", reason=reason))
        key = self._units_key()
        return dataclasses.replace(raw, vertices=raw.vertices * UNIT_FACTORS[key]), key, False

    def _magnify(self) -> float:
        name = label("fl_magnify").rstrip(":").rstrip(" ×,").strip()
        try:
            v = float(str(self.v_magnify.get()).strip().replace(",", "."))
        except ValueError:
            raise ValueError(label("fl_err_number", field=name))
        if not (0.1 <= v <= 200):
            raise ValueError(label("fl_err_range", field=name, lo=0.1, hi=200))
        return v

    def build_uav(self):
        """(FlybyUav, пояснение об автоопределённых единицах) для вида сбоку. ValueError с понятным текстом, если модель или число неверны."""
        import flyby_view as fv
        mesh, _key, _auto = self._resolve()
        magnify = self._magnify()
        return fv.FlybyUav(mesh, "XX-1", float(self._nose_value()), magnify), ""

    def describe(self):
        """(текст сведений о модели, это ошибка?) -- то, что видно под выбором модели."""
        import flyby_view as fv
        try:
            mesh, key, auto = self._resolve()
        except ValueError as exc:
            return str(exc), True
        length, span, height = fv.uav_dimensions(mesh, float(self._nose_value()))
        if key is None:
            return label("ac_info_builtin", span=span), False
        simp = label("ac_simplified", src=mesh.source_faces) if getattr(mesh, "simplified", False) else ""
        text = label("ac_info", ref=self._uav_ref_value(), faces=len(mesh.faces), simp=simp, unit=label("u_short_" + key),
                     auto="", length=length, span=span, height=height)
        if self._auto_model:
            text = label("fl_model_found", ref=self._uav_ref) + ". " + text
        if self._nose_choice() == "auto":                          # что решила программа и насколько уверена: решение проверяется по миниатюре
            info = self._nose_info()
            text += "\n" + (label("ac_nose_auto", angle=info["angle"]) if info["reliable"] else label("ac_nose_unsure", why=info["reason"]))
        return text, False

    def _mesh_pending(self) -> bool:
        """True -- модель ещё читается в фоне (refresh_info повторится сам, когда она будет готова). Тяжёлая модель раньше читалась
        прямо при создании панели, и программа запускалась на секунды дольше."""
        ref = self._uav_ref_value()
        if not ref:
            return False
        try:
            path = lib.resolve_model(ref, self.library_dir)
            stamp = os.path.getmtime(path)
        except Exception:
            return False                                      # файла нет: ошибку быстро покажет обычный путь
        up = self.v_up.get()
        key = (path, stamp, up)
        if self._raw_cache is not None and self._raw_cache[0] == key:
            return False
        if getattr(self, "_bg_key", None) == key:
            return True                                       # уже читается
        self._bg_key = key
        self._set_info(f"{ref} …", False)

        def work():
            from scene_mesh import load_mesh
            try:
                result = load_mesh(path, up, 1.0)
            except Exception as exc:                          # ошибка тоже кэшируется: _raw_mesh её поднимет, не читая файл снова
                result = exc
            try:
                self.after(0, lambda: self._bg_done(key, result))
            except Exception:
                pass

        # старт с задержкой: при запуске программы разбор модели не должен тормозить сборку окна (GIL)
        thread = threading.Thread(target=work, daemon=True)
        self.after(400, lambda: getattr(self, "_bg_key", None) == key and thread.start())
        return True

    def _bg_done(self, key, result) -> None:
        if getattr(self, "_bg_key", None) != key:
            return                                            # пока читалось, выбрали другую модель -- её читает свой поток
        self._bg_key = None
        self._raw_cache = (key, result)
        try:
            if self.winfo_exists():
                self.refresh_info()
        except tk.TclError:
            pass

    def refresh_info(self) -> None:
        """Обновить сведения и миниатюру (после любого изменения полей)."""
        self._after = None
        if self._mesh_pending():
            return
        import flyby_view as fv
        text, bad = self.describe()
        self.last_error = text if bad else ""
        self._set_info(text, bad)
        if bad:
            self.preview_label.configure(image="")
            self._photo = None
            return
        try:
            from PIL import ImageTk
            mesh, _key, _auto = self._resolve()
            image = fv.render_uav_preview(mesh, float(self._nose_value()), self.PREVIEW, "en" if config_text._current_lang() == "en" else "uk")
            self._photo = ImageTk.PhotoImage(image)
            self.preview_label.configure(image=self._photo)
        except Exception as exc:
            self.preview_label.configure(image="")
            self._photo = None
            self._set_info(f"{text}  [{exc}]", True)
