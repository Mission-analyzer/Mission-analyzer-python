"""
sd_file_manager.py — файловий менеджер SD-карти польотного контролера
через MAVLink FTP: навігація по папках, перегляд файлів (текст/hex),
графіки датафлеш-логів (.BIN, через pymavlink.DFReader), скачування на
ПК, вивантаження на SD, видалення.

SDFileManagerMixin підмішується до класу App (app.py) поряд із
ArduPilotLinkMixin -- методи звертаються до self._flight_conn
(встановлюється ArduPilotLinkMixin._on_connect_result) та до
self._describe_ftp_error/self._ftp_retry (там само).

Свідомо відокремлений від ardupilot_link.py: той файл відповідає за
підключення й Read/Write місії, цей -- лише за роботу з файлами на SD.
Виріс за один день до ~630 рядків (майже стільки ж, скільки
config_page.py + help_page.py + srtm.py + overview_map.py разом), тому
винесення в окремий файл підтримує навігацію читабельною -- той самий
принцип, що й для mission_editor.py.
"""

from __future__ import annotations

import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import i18n


class SDFileManagerMixin:
    """Файловий менеджер SD-карти польотного контролера (MAVLink FTP)."""

    # ---------------------------------------------------------- SD-карта --

    @staticmethod
    def _ftp_join(base: str, name: str) -> str:
        if base in ("", "/"):
            return "/" + name
        return base.rstrip("/") + "/" + name

    @staticmethod
    def _ftp_parent(path: str) -> str:
        if path in ("", "/"):
            return "/"
        trimmed = path.rstrip("/")
        parent = trimmed.rsplit("/", 1)[0]
        return parent if parent else "/"


    @staticmethod
    def _describe_ftp_error(operation_name: str, error_code: int) -> str:
        """
        Перетворює {operation_name}/{error_code} з MAVFTP на людський
        текст замість голого числа -- коди йдуть прямо зі специфікації
        MAVLink FTP (pymavlink.mavftp.FtpError), контролер сам їх
        повертає у відповідь на команду, це НЕ помилка обриву зв'язку
        (та мала б інший код -- RemoteReplyTimeout).
        """
        names = {
            1: "info_ftp_err_fail", 2: "info_ftp_err_errno", 3: "info_ftp_err_bad_size",
            4: "info_ftp_err_bad_session", 5: "info_ftp_err_no_sessions",
            6: "info_ftp_err_eof", 7: "info_ftp_err_unknown_cmd",
            8: "info_ftp_err_exists", 9: "info_ftp_err_protected",
            10: "info_ftp_err_not_found", 70: "info_ftp_err_bad_args",
            72: "info_ftp_err_local_open", 73: "info_ftp_err_timeout",
        }
        key = names.get(error_code)
        reason = i18n.t(key) if key else i18n.t("info_ftp_err_unknown_fmt", code=error_code)
        return f"{operation_name}: {reason}"


    @staticmethod
    def _ftp_retry(attempt_fn, retries: int = 2, delay: float = 0.4):
        """
        Виконує attempt_fn() (повертає MAVFTPReturn) до retries+1 разів
        із паузою між спробами. Реальний серійний радіоканал телеметрії
        іноді губить окремий пакет -- одинична спроба недостатньо
        надійна для живого польотного контролера (на відміну від тестів
        із заглушками, де відповідь завжди миттєва й безпомилкова).
        Повертає результат ОСТАННЬОЇ спроби (успішної чи ні).
        """
        last = None
        for i in range(retries + 1):
            last = attempt_fn()
            if last.error_code == 0:
                return last
            if i < retries:
                time.sleep(delay)
        return last


    def _show_sd_files(self):
        """Кнопка «Файли SD»: відкриває вікно з файловим менеджером SD-карти
        польотного контролера (перегляд папок, скачування, вивантаження,
        видалення, попередній перегляд) через MAVLink FTP."""
        if self._flight_conn is None:
            return

        from pymavlink import mavutil
        ver = getattr(self._flight_conn, "messages", {}).get("AUTOPILOT_VERSION")
        if ver is not None and not (ver.capabilities & mavutil.mavlink.MAV_PROTOCOL_CAPABILITY_FTP):
            messagebox.showwarning(i18n.t("dlg_sd_files_title"), i18n.t("info_ftp_not_supported"))
            return

        # ОДИН спільний MAVFTP на весь час, поки вікно відкрите -- НЕ
        # створюємо новий об'єкт на кожну операцію. У новоствореного
        # MAVFTP сесія завжди починається з 0, а контролер міг ще
        # пам'ятати іншу сесію з попереднього запиту (наприклад, щойно
        # відкритого списку папки) -- звідси й помилка "mavlink_packet: 4"
        # (FtpError.InvalidSession) при спробі скачати файл одразу після
        # перегляду списку.
        from pymavlink.mavftp import MAVFTP
        ftp = MAVFTP(self._flight_conn, self._flight_conn.target_system, self._flight_conn.target_component)

        dlg = tk.Toplevel(self)
        dlg.title(i18n.t("dlg_sd_files_title"))
        dlg.geometry("640x520")
        dlg.transient(self)

        path_var = tk.StringVar(value="/")
        status_var = tk.StringVar(value="")

        top = ttk.Frame(dlg)
        top.pack(fill="x", padx=8, pady=(8, 4))
        up_btn = ttk.Button(top, text="⬆", width=3)
        up_btn.pack(side="left")
        ttk.Label(top, textvariable=path_var, font=("Consolas", 10, "bold")).pack(side="left", padx=8)

        columns = ("size",)
        tree = ttk.Treeview(dlg, columns=columns, show="tree headings", selectmode="browse")
        tree.heading("#0", text=i18n.t("col_name"))
        tree.heading("size", text=i18n.t("col_size"))
        tree.column("#0", width=420)
        tree.column("size", width=120, anchor="e")
        tree.pack(fill="both", expand=True, padx=8, pady=4)

        btn_row = ttk.Frame(dlg)
        btn_row.pack(fill="x", padx=8, pady=(4, 4))
        preview_btn = ttk.Button(btn_row, text=i18n.t("btn_preview"))
        graph_btn = ttk.Button(btn_row, text=i18n.t("btn_log_graphs"))
        download_btn = ttk.Button(btn_row, text=i18n.t("btn_download"))
        upload_btn = ttk.Button(btn_row, text=i18n.t("btn_upload"))
        delete_btn = ttk.Button(btn_row, text=i18n.t("btn_delete"))
        refresh_btn = ttk.Button(btn_row, text=i18n.t("btn_refresh"))
        for b in (preview_btn, graph_btn, download_btn, upload_btn, delete_btn, refresh_btn):
            b.pack(side="left", padx=(0, 6))

        ttk.Label(dlg, textvariable=status_var, foreground="#555").pack(fill="x", padx=8)
        ttk.Button(dlg, text=i18n.t("btn_close"), command=dlg.destroy).pack(pady=(4, 8))

        all_buttons = [up_btn, preview_btn, graph_btn, download_btn, upload_btn, delete_btn, refresh_btn]

        def set_busy(busy: bool, msg: str = ""):
            if not dlg.winfo_exists():
                return
            state = "disabled" if busy else "normal"
            for b in all_buttons:
                b.configure(state=state)
            status_var.set(msg)

        def selected_entry():
            sel = tree.selection()
            if not sel:
                return None
            return tree.item(sel[0], "values"), tree.item(sel[0], "text"), sel[0]

        def refresh():
            set_busy(True, i18n.t("status_loading_list"))
            threading.Thread(target=list_worker, args=(path_var.get(),), daemon=True).start()

        def list_worker(path: str):
            error = None
            entries = []
            try:
                ret = self._ftp_retry(lambda: ftp.cmd_list([path]))
                if ret.error_code == 0:
                    entries = ftp.list_result
                else:
                    error = self._describe_ftp_error(ret.operation_name, ret.error_code)
            except Exception as e:
                error = str(e)
            self.after(0, lambda: on_list_ready(path, entries, error))

        def on_list_ready(path, entries, error):
            # діалог могли закрити, поки фоновий потік ще працював над
            # запитом -- тоді всі його віджети вже знищені, і будь-яке
            # звернення до них (tree.delete тощо) впаде з TclError
            # "invalid command name". Просто тихо виходимо.
            if not dlg.winfo_exists():
                return
            path_var.set(path)
            tree.delete(*tree.get_children())
            if error:
                set_busy(False, i18n.t("info_ftp_error_fmt", error=error))
                up_btn.configure(state="disabled" if path == "/" else "normal")
                return
            for entry in sorted(entries, key=lambda e: (not e.is_dir, e.name.lower())):
                label = f"📁 {entry.name}" if entry.is_dir else entry.name
                size_txt = "" if entry.is_dir else f"{entry.size_b:,}".replace(",", " ")
                tree.insert(
                    "", "end", text=label, values=(size_txt,),
                    tags=("dir" if entry.is_dir else "file", str(entry.size_b)),
                )
            set_busy(False, i18n.t("status_list_refreshed"))
            # set_busy(False, ...) вмикає ВСІ кнопки одразу (в т.ч. up_btn) --
            # тому власну умову "вимкнено в корені" застосовуємо ПІСЛЯ,
            # інакше set_busy її одразу перезатирає на "normal"
            up_btn.configure(state="disabled" if path == "/" else "normal")

        def on_double_click(_event=None):
            item = selected_entry()
            if item is None:
                return
            _values, text, iid = item
            if "dir" not in tree.item(iid, "tags"):
                return
            name = text.replace("📁 ", "", 1)
            path_var.set(self._ftp_join(path_var.get(), name))
            refresh()

        def on_up():
            path_var.set(self._ftp_parent(path_var.get()))
            refresh()

        def _selected_file():
            """Повертає (remote_path, name, size_b) для обраного ФАЙЛУ (не
            папки), або None + показує підказку користувачу."""
            item = selected_entry()
            if item is None:
                return None
            _values, text, iid = item
            tags = tree.item(iid, "tags")
            if "dir" in tags:
                messagebox.showinfo(i18n.t("dlg_sd_files_title"), i18n.t("msg_select_file_not_dir"))
                return None
            size_b = int(tags[1]) if len(tags) > 1 else 0
            return self._ftp_join(path_var.get(), text), text, size_b

        def on_download():
            sel = _selected_file()
            if sel is None:
                return
            remote_path, name, _size = sel
            local_path = filedialog.asksaveasfilename(initialfile=name)
            if not local_path:
                return
            set_busy(True, i18n.t("status_downloading_file_fmt", name=name))
            threading.Thread(target=download_worker, args=(remote_path, local_path), daemon=True).start()

        def download_worker(remote_path, local_path):
            # НЕ передаємо filename=local_path у cmd_get -- сам pymavlink
            # у "звичайному" (не callback) режимі пише проміжний файл у
            # ЖОРСТКО зашитий Unix-шлях self.temp_filename =
            # "/tmp/temp_mavftp_file" (див. mavftp.py), якого на Windows
            # просто не існує -- звідси "No such file or directory:
            # '/tmp/temp_mavftp_file'". Той самий обхід, що й для
            # Перегляду: тягнемо в пам'ять через callback і самі пишемо
            # на диск куди попросив користувач.
            error = None
            captured = {}

            def _capture(fh):
                captured["data"] = fh.read()

            def attempt():
                captured.clear()
                ftp.cmd_get([remote_path], callback=_capture)
                return ftp.process_ftp_reply("get", timeout=120)

            try:
                ret = self._ftp_retry(attempt)
                if ret.error_code != 0:
                    error = self._describe_ftp_error(ret.operation_name, ret.error_code)
                else:
                    with open(local_path, "wb") as f:
                        f.write(captured.get("data", b""))
            except Exception as e:
                error = str(e)
            self.after(0, lambda: on_transfer_done(error, i18n.t("status_download_done")))

        # понад цей розмір -- не намагаємось показати вміст напряму, лише
        # пропонуємо скачати (датафлеш-логи ArduPilot можуть бути бінарні
        # й на десятки МБ -- відкривати їх у текстовому віджеті безглуздо)
        PREVIEW_SIZE_LIMIT = 256 * 1024

        def on_preview():
            sel = _selected_file()
            if sel is None:
                return
            remote_path, name, size_b = sel
            if size_b > PREVIEW_SIZE_LIMIT:
                messagebox.showwarning(
                    i18n.t("dlg_sd_files_title"),
                    i18n.t("msg_file_too_large_preview_fmt", size=f"{size_b:,}".replace(",", " ")),
                )
                return
            set_busy(True, i18n.t("status_loading_preview_fmt", name=name))
            threading.Thread(target=preview_worker, args=(remote_path, name), daemon=True).start()

        def preview_worker(remote_path, name):
            # НЕ передаємо filename="-" -- у самому pymavlink для цього
            # режиму жорстко зашито print(fh.read().decode('utf-8')) БЕЗ
            # errors="replace" (див. mavftp.py __check_read_finished),
            # і воно падає з UnicodeDecodeError на будь-якому бінарному
            # файлі (а STRG_BAK/*.bak, дефолтні логи -- саме бінарні).
            # callback=... іде ІНШОЮ гілкою в тому ж коді -- жодного
            # друку/декодування всередині pymavlink, повний контроль тут.
            error = None
            captured = {}

            def _capture(fh):
                captured["data"] = fh.read()

            def attempt():
                captured.clear()
                ftp.cmd_get([remote_path], callback=_capture)
                return ftp.process_ftp_reply("get", timeout=60)

            try:
                ret = self._ftp_retry(attempt)
                if ret.error_code == 0:
                    content = captured.get("data", b"")
                else:
                    content = None
                    error = self._describe_ftp_error(ret.operation_name, ret.error_code)
            except Exception as e:
                content = None
                error = str(e)
            self.after(0, lambda: on_preview_ready(name, content, error))

        def on_preview_ready(name, content, error):
            if not dlg.winfo_exists():
                return
            set_busy(False, "")
            if error:
                messagebox.showerror(i18n.t("dlg_sd_files_title"), i18n.t("msg_transfer_failed_fmt", error=error))
                return
            self._show_file_preview(name, content)

        def on_upload():
            local_path = filedialog.askopenfilename()
            if not local_path:
                return
            name = os.path.basename(local_path)
            remote_path = self._ftp_join(path_var.get(), name)
            set_busy(True, i18n.t("status_uploading_file_fmt", name=name))
            threading.Thread(target=upload_worker, args=(local_path, remote_path), daemon=True).start()

        def upload_worker(local_path, remote_path):
            error = None

            def attempt():
                ftp.cmd_put([local_path, remote_path])
                return ftp.process_ftp_reply("put", timeout=120)

            try:
                ret = self._ftp_retry(attempt)
                if ret.error_code != 0:
                    error = self._describe_ftp_error(ret.operation_name, ret.error_code)
            except Exception as e:
                error = str(e)
            self.after(0, lambda: on_transfer_done(error, i18n.t("status_upload_done")))

        def on_transfer_done(error, ok_msg):
            if not dlg.winfo_exists():
                return
            if error:
                set_busy(False, "")
                messagebox.showerror(i18n.t("dlg_sd_files_title"), i18n.t("msg_transfer_failed_fmt", error=error))
            else:
                set_busy(False, ok_msg)
            refresh()

        def on_delete():
            item = selected_entry()
            if item is None:
                return
            _values, text, iid = item
            is_dir = "dir" in tree.item(iid, "tags")
            name = text.replace("📁 ", "", 1) if is_dir else text
            remote_path = self._ftp_join(path_var.get(), name)
            if not messagebox.askyesno(i18n.t("dlg_sd_files_title"), i18n.t("msg_confirm_delete_fmt", name=name)):
                return
            set_busy(True, i18n.t("status_deleting_fmt", name=name))
            threading.Thread(target=delete_worker, args=(remote_path, is_dir), daemon=True).start()

        def delete_worker(remote_path, is_dir):
            error = None

            def attempt():
                return ftp.cmd_rmdir([remote_path]) if is_dir else ftp.cmd_rm([remote_path])

            try:
                ret = self._ftp_retry(attempt)
                if ret.error_code != 0:
                    error = self._describe_ftp_error(ret.operation_name, ret.error_code)
            except Exception as e:
                error = str(e)
            self.after(0, lambda: on_transfer_done(error, i18n.t("status_delete_done")))

        def on_log_graphs():
            sel = _selected_file()
            if sel is None:
                return
            remote_path, name, size_b = sel
            set_busy(True, i18n.t("status_downloading_log_fmt", name=name))
            threading.Thread(target=log_graphs_worker, args=(remote_path, name), daemon=True).start()

        def log_graphs_worker(remote_path, name):
            import tempfile
            error = None
            samples = None
            tmp_path = None
            try:
                fd, tmp_path = tempfile.mkstemp(suffix=".bin")
                os.close(fd)

                def attempt():
                    ftp.cmd_get([remote_path, tmp_path])
                    return ftp.process_ftp_reply("get", timeout=300)

                ret = self._ftp_retry(attempt)
                if ret.error_code != 0:
                    error = self._describe_ftp_error(ret.operation_name, ret.error_code)
                else:
                    samples = self._parse_dataflash_log(tmp_path)
            except Exception as e:
                error = str(e)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
            self.after(0, lambda: on_log_graphs_ready(name, samples, error))

        def on_log_graphs_ready(name, samples, error):
            if not dlg.winfo_exists():
                return
            set_busy(False, "")
            if error:
                messagebox.showerror(i18n.t("dlg_sd_files_title"), i18n.t("msg_transfer_failed_fmt", error=error))
                return
            if samples is None or not any(samples.values()):
                messagebox.showwarning(i18n.t("dlg_sd_files_title"), i18n.t("msg_no_log_data"))
                return
            self._show_log_graphs(name, samples)

        up_btn.configure(command=on_up)
        preview_btn.configure(command=on_preview)
        graph_btn.configure(command=on_log_graphs)
        download_btn.configure(command=on_download)
        upload_btn.configure(command=on_upload)
        delete_btn.configure(command=on_delete)
        refresh_btn.configure(command=refresh)
        tree.bind("<Double-1>", on_double_click)

        # контекстне меню по правому кліку -- ті самі дії, що й кнопки
        # внизу, тільки прямо на рядку (як у звичайному провіднику файлів).
        # Перевикористовуємо ВЖЕ готові обробники (on_preview, on_download
        # тощо) -- вони самі читають tree.selection(), тому досить
        # виділити рядок під курсором ПЕРЕД показом меню.
        context_menu = tk.Menu(dlg, tearoff=0)

        def on_right_click(event):
            row_id = tree.identify_row(event.y)
            context_menu.delete(0, "end")
            if row_id:
                tree.selection_set(row_id)
                is_dir = "dir" in tree.item(row_id, "tags")
                if is_dir:
                    context_menu.add_command(label=i18n.t("ctx_open_folder"), command=on_double_click)
                    context_menu.add_separator()
                    context_menu.add_command(label=i18n.t("btn_delete"), command=on_delete)
                else:
                    context_menu.add_command(label=i18n.t("btn_preview"), command=on_preview)
                    context_menu.add_command(label=i18n.t("btn_log_graphs"), command=on_log_graphs)
                    context_menu.add_command(label=i18n.t("btn_download"), command=on_download)
                    context_menu.add_separator()
                    context_menu.add_command(label=i18n.t("btn_delete"), command=on_delete)
            else:
                context_menu.add_command(label=i18n.t("btn_refresh"), command=refresh)
            context_menu.tk_popup(event.x_root, event.y_root)

        tree.bind("<Button-3>", on_right_click)

        dlg.grab_set()
        refresh()


    # понад стільки байт бінарний вміст показуємо як hex-дамп -- більше
    # просто повільно рендериться в текстовому віджеті без жодної користі
    HEX_DUMP_LIMIT = 16 * 1024

    @staticmethod
    def _hex_dump(data: bytes, limit: int) -> str:
        chunk = data[:limit]
        lines = []
        for offset in range(0, len(chunk), 16):
            row = chunk[offset:offset + 16]
            hex_part = " ".join(f"{b:02x}" for b in row)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
            lines.append(f"{offset:08x}  {hex_part:<47}  {ascii_part}")
        if len(data) > limit:
            lines.append(f"... {len(data) - limit} B ...")
        return "\n".join(lines)


    def _show_file_preview(self, name: str, content: bytes):
        """Показує вміст файлу (уже скачаного в пам'ять) у текстовому
        вікні. Текстові файли (логи, параметри у форматі txt, скрипти)
        показуються як текст; якщо вміст НЕ є валідним UTF-8 (типово для
        бінарних .bak/.bin -- параметри, датафлеш-логи) -- показуємо
        hex-дамп замість того, щоб силоміць "декодувати" бінарні дані
        в нечитний текст із символами заміни."""
        try:
            text_content = content.decode("utf-8")
            is_binary = False
        except UnicodeDecodeError:
            text_content = self._hex_dump(content, self.HEX_DUMP_LIMIT)
            is_binary = True

        dlg = tk.Toplevel(self)
        title = i18n.t("dlg_preview_title_fmt", name=name)
        if is_binary:
            title += f" ({i18n.t('label_binary_hexdump')})"
        dlg.title(title)
        dlg.geometry("720x560" if is_binary else "640x520")
        dlg.transient(self)

        widget = scrolledtext.ScrolledText(dlg, wrap="none" if is_binary else "word", font=("Consolas", 9))
        widget.pack(fill="both", expand=True, padx=8, pady=8)
        widget.insert("end", text_content)
        widget.config(state="disabled")

        ttk.Button(dlg, text=i18n.t("btn_close"), command=dlg.destroy).pack(pady=(0, 8))
        dlg.grab_set()


    @staticmethod
    def _parse_dataflash_log(path: str) -> dict:
        """
        Розбирає датафлеш-лог ArduPilot (.BIN) через pymavlink.DFReader
        і повертає прості часові ряди для графіків: висота (з CTUN.Alt,
        якщо є -- це баро/EKF-висота, точніша за GPS; інакше GPS.Alt),
        швидкість (GPS.Spd) і напруга батареї (BAT.Volt). Кожен ряд --
        список (час_с, значення), обрізаний до ~2000 точок для швидкого
        рендеру (повний лог може мати сотні тисяч записів).
        """
        from pymavlink import DFReader

        ctun_alt, gps_alt, spd, volt = [], [], [], []
        try:
            reader = DFReader.DFReader_binary(path)
        except Exception:
            return {"alt": [], "spd": [], "volt": []}

        while True:
            try:
                m = reader.recv_msg()
            except Exception:
                break
            if m is None:
                break
            t = getattr(m, "_timestamp", None)
            if t is None:
                continue
            mtype = m.get_type()
            if mtype == "CTUN":
                a = getattr(m, "Alt", None)
                if a is not None:
                    ctun_alt.append((t, float(a)))
            elif mtype == "GPS":
                a = getattr(m, "Alt", None)
                if a is not None:
                    gps_alt.append((t, float(a)))
                s = getattr(m, "Spd", None)
                if s is not None:
                    spd.append((t, float(s)))
            elif mtype == "BAT":
                v = getattr(m, "Volt", None)
                if v is not None:
                    volt.append((t, float(v)))

        def _cap(series, limit=2000):
            if len(series) <= limit:
                return series
            step = max(len(series) // limit, 1)
            return series[::step]

        alt = ctun_alt if ctun_alt else gps_alt
        return {"alt": _cap(alt), "spd": _cap(spd), "volt": _cap(volt)}


    @staticmethod
    def _draw_log_series(canvas: tk.Canvas, series: list, color: str):
        """Малює простий лінійний графік однієї часової серії на канвасі --
        той самий підхід (сітка + підписи осі + полілінія), що й
        elevation_view.py/angle_view.py, без жодних сторонніх бібліотек
        (matplotlib свідомо не використовується в проєкті)."""
        canvas.delete("all")
        w = max(canvas.winfo_width(), 100)
        h = max(canvas.winfo_height(), 100)
        if not series:
            canvas.create_text(w // 2, h // 2, text=i18n.t("msg_no_log_data"), fill="#999")
            return

        margin_l, margin_r, margin_t, margin_b = 55, 15, 10, 20
        plot_w = max(w - margin_l - margin_r, 10)
        plot_h = max(h - margin_t - margin_b, 10)

        t0 = series[0][0]
        times = [t - t0 for t, _ in series]
        values = [v for _, v in series]
        t_max = max(times) or 1.0
        v_min, v_max = min(values), max(values)
        if v_max - v_min < 1e-6:
            v_max = v_min + 1.0
        pad = (v_max - v_min) * 0.08
        v_min -= pad
        v_max += pad

        def X(t):
            return margin_l + t / t_max * plot_w

        def Y(v):
            return margin_t + (1 - (v - v_min) / (v_max - v_min)) * plot_h

        for i in range(5):
            val = v_min + (v_max - v_min) * i / 4
            y = Y(val)
            canvas.create_line(margin_l, y, w - margin_r, y, fill="#eee")
            canvas.create_text(margin_l - 6, y, text=f"{val:.1f}", anchor="e", font=("Arial", 8))

        points = []
        for t, v in zip(times, values):
            points.append(X(t))
            points.append(Y(v))
        if len(points) >= 4:
            canvas.create_line(*points, fill=color, width=1.5)


    def _show_log_graphs(self, name: str, samples: dict):
        dlg = tk.Toplevel(self)
        dlg.title(i18n.t("dlg_log_graphs_title_fmt", name=name))
        dlg.geometry("700x760")
        dlg.transient(self)

        specs = [
            ("alt", i18n.t("label_log_altitude"), "#1f77b4"),
            ("spd", i18n.t("label_log_speed"), "#2ca02c"),
            ("volt", i18n.t("label_log_voltage"), "#d62728"),
        ]
        for key, title, color in specs:
            box = ttk.LabelFrame(dlg, text=title)
            box.pack(fill="both", expand=True, padx=8, pady=4)
            canvas = tk.Canvas(box, bg="white", height=200)
            canvas.pack(fill="both", expand=True)
            series = samples.get(key) or []
            canvas.bind(
                "<Configure>",
                lambda _e, c=canvas, s=series, col=color: self._draw_log_series(c, s, col),
            )

        ttk.Button(dlg, text=i18n.t("btn_close"), command=dlg.destroy).pack(pady=8)
        dlg.grab_set()


