"""
sim_scene.py -- 3D-объекты на сцене: загрузка моделей, высота основания по SRTM, итог на старте, красная точка в камере.

Выделено из simulation_page.py без изменения логики (примесь к SimulationPageMixin).
"""
from __future__ import annotations

import math
import os
import threading
import traceback
import scene_library
import scene_store
from scene_config_panel import describe_result
from scene_mesh import MeshError, load_mesh
from scene_project import local_offset_m
from scene_renderer import LoadedObject, object_center_pixel, ZNEAR


class SceneMixin:
    """3D-объекты на сцене: загрузка моделей, высота основания по SRTM, итог на старте, красная точка в камере."""

    def _scene_start_summary(self):
        """Что будет на сцене, одной строкой для первой секунды полёта: (текст, есть ли проблема). Вызывается, когда в списке есть объекты."""
        objs = list(getattr(self, "_scene_objects", []))
        enabled = [(i, o) for i, o in enumerate(objs) if o.enabled]
        if not enabled:
            return "3D: все объекты ВЫКЛЮЧЕНЫ (галочка «Показувати»), на сцене ничего не будет", True
        results = list(getattr(self, "_scene_results", []))
        home = self.analyzer.nav_wps[0] if self.analyzer is not None and self.analyzer.nav_wps else None
        parts, bad = [], False
        for i, o in enabled:
            result = results[i] if i < len(results) else (None, None)
            if result[0] is not None:                                      # модель не загрузилась: причина как в панели настроек
                parts.append("МОДЕЛЬ НЕ ЗАГРУЖЕНА: " + describe_result(o, result))
                bad = True
            else:
                where = "" if home is None else f", {math.hypot(*local_offset_m(home.lat, home.lon, o.lat, o.lon)):.0f} м от дома"
                parts.append(f"«{o.name}»: модель загружена{where}")
        return "3D: " + "; ".join(parts), bad

    OBJECT_DOT_RADIUS = 6

    def _sim_object_dots(self):
        """Красная точка на каждом 3D-объекте, центр которого попал в кадр камеры (вид «вперёд», кадр показан). Считается по позе
        показанного кадра. Отключается галочкой «Червона точка на 3D-об'єкті» в настройках HUD."""
        canvas = getattr(self, "sim_map_canvas", None)
        if canvas is None:
            return
        canvas.delete("object_dot")
        try:
            cfg = getattr(self, "_sim_hud_cfg", None)
            if cfg is not None and not cfg.is_on("object_dot"):
                return
            camera, video_h = self._sim_hud_camera()
            pose = getattr(self, "_sim_shown_pose", None)
            items = [it for it in getattr(self, "_scene_loaded", []) if it.obj.enabled]
            if camera is None or not video_h or pose is None or not items:
                return
            k = float(video_h) / camera.height
            cx, cy = canvas.winfo_width() / 2.0, canvas.winfo_height() / 2.0
            r = self.OBJECT_DOT_RADIUS
            for it in items:
                u, v, depth = object_center_pixel(it, pose, camera)
                if depth > ZNEAR and 0.0 <= u <= camera.width and 0.0 <= v <= camera.height:
                    x, y = cx + (u - camera.cx) * k, cy + (v - camera.cy) * k
                    canvas.create_oval(x - r, y - r, x + r, y + r, fill="#ff2020", outline="white", width=1, tags="object_dot")
            canvas.tag_raise("object_dot")
        except Exception:
            if not getattr(self, "_object_dot_err", False):
                self._object_dot_err = True
                traceback.print_exc()

    def _scene_init(self):
        if getattr(self, "_scene_objects", None) is not None:
            return
        self._scene_mesh_cache = {}
        # список хранится в СВОЁМ файле scene_objects.json; старые записи из settings.json переносятся один раз
        self._scene_objects, self._scene_store_info = scene_store.load(legacy=self._load_pref("scene_objects", []))
        self._scene_store_error = None
        if self._scene_store_info.source in ("legacy", "backup"):          # завершить перенос/восстановление: записать основной файл
            ok, err = scene_store.save(self._scene_objects)
            self._scene_store_error = None if ok else err
        self._scene_loaded = []
        self._scene_tick_n = 0
        self._scene_last_error = None
        # Модели читаются В ФОНЕ: тяжёлая модель (сотни тысяч граней) раньше держала запуск программы на секунды -- окно не появлялось,
        # пока все объекты не разобраны и не упрощены. Пока фон работает, результатов нет (панель покажет их, когда фон закончит).
        self._scene_results = []
        self._scene_bg_load()

    def _scene_bg_load(self):
        """Фоновое чтение моделей в кэш (_scene_mesh). Tk здесь не трогаем: итог собирает _scene_bg_done в главном потоке."""
        objs = list(self._scene_objects)

        def work():
            for obj in objs:
                try:
                    self._scene_mesh(obj)
                except Exception:
                    pass                                   # ошибку модели покажет _scene_reload в главном потоке
            try:
                self.after(0, self._scene_bg_done)
            except Exception:
                pass                                       # программу закрыли, пока читались модели

        self._scene_bg_thread = threading.Thread(target=work, daemon=True)
        # старт с задержкой: разбор модели на Python занимает GIL и тормозил бы сборку окна -- сначала окно, потом модели
        t = self._scene_bg_thread
        self.after(400, lambda: self._scene_bg_thread is t and t.ident is None and t.start())

    def _scene_bg_done(self):
        """Модели в кэше: собрать сцену (мгновенно) и показать итог в панели и на карте. Повторный вызов -- ничего не делает."""
        if getattr(self, "_scene_bg_thread", None) is None:
            return
        self._scene_bg_thread = None
        self._scene_results = self._scene_reload()
        panel = getattr(self, "_scene_panel", None)
        if panel is not None:
            try:
                panel.set_objects(self._scene_objects, self._scene_results)
            except Exception:
                traceback.print_exc()
        if hasattr(self, "_redraw_mission_overview"):
            try:
                self._redraw_mission_overview()
            except Exception:
                traceback.print_exc()

    def _scene_wait_ready(self):
        """Старт симуляции раньше, чем фон дочитал модели: дождаться (только остаток времени) и собрать сцену."""
        t = getattr(self, "_scene_bg_thread", None)
        if t is None:
            return
        if t.ident is None:                                # ещё не стартовал (старт отложен) -- запускаем сейчас
            t.start()
        t.join()
        self._scene_bg_done()

    def _scene_mesh(self, obj):
        path = scene_library.resolve_model(obj.model_path)       # короткая ссылка (относительно models3d) или абсолютный путь
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            stamp = None
        key = (path, stamp, obj.up_axis, obj.scale)
        mesh = self._scene_mesh_cache.get(key)
        if mesh is None:
            mesh = load_mesh(path, obj.up_axis, obj.scale)
            if len(self._scene_mesh_cache) > 16:
                self._scene_mesh_cache.clear()
            self._scene_mesh_cache[key] = mesh
        return mesh

    def _scene_ground_at(self, lat, lon):
        """Высота земли AMSL в точке по SRTM -- только из уже загруженных тайлов (иначе None: сеть не трогаем)."""
        try:
            terr, _home = self._sim_terrain_info()
            if terr is None or not terr.has_tile_cached(lat, lon):
                return None
            value = terr.get_elevation(lat, lon)
            return None if value is None else float(value)
        except Exception:
            return None

    def _scene_terrain_base(self, obj):
        """Высота земли AMSL под объектом по SRTM (None, пока тайл не загружен)."""
        return self._scene_ground_at(obj.lat, obj.lon)

    def _scene_reload(self):
        """Перечитывает модели по списку объектов. Возвращает [(MeshError | None, Mesh | None), ...] по каждому объекту."""
        loaded, results = [], []
        for obj in self._scene_objects:
            try:
                mesh = self._scene_mesh(obj)
                loaded.append(LoadedObject.create(obj, mesh, self._scene_terrain_base(obj)))
                results.append((None, mesh))
            except MeshError as exc:
                results.append((exc, None))
            except Exception as exc:
                results.append((MeshError("parse", f"{type(exc).__name__}: {exc}"), None))
        self._scene_loaded = loaded                       # список подменяется целиком: рабочий поток читает его без блокировок
        return results

    def _scene_refresh_bases(self):
        """Высота основания «по рельефу» может появиться позже, когда догрузится тайл SRTM: подхватываем её."""
        for it in self._scene_loaded:
            if it.auto_base and it.base_amsl is None:
                it.base_amsl = self._scene_terrain_base(it.obj)
