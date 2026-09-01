"""
ardupilot_link.py — усе, що стосується прямого зв'язку з польотним
контролером ArduPilot по MAVLink: підключення (в т.ч. AUTO-пошук
порту), Read/Write місії, кнопка Info (прошивка/датчики/SD-карта),
файловий менеджер SD-карти (MAVLink FTP -- перегляд/скачування/
вивантаження/видалення).

ArduPilotLinkMixin підмішується до класу App (app.py) поряд з
MissionPageMixin -- методи звертаються до self.* атрибутів сторінки
"Місія" (self.analyzer, self.file_var тощо) та до трьох методів з
mission_page.py (self._build_analyzer, self._finish_load,
self.render_map) при завантаженні місії з борту.

Свідомо відокремлений від mission_page.py: та частина відповідає за
таблицю вейпоінтів і карту маршруту, ця -- лише за MAVLink/FTP. Codebase
одного дня зросла вдвічі саме за рахунок цього блоку, тому винесення
в окремий файл підтримує навігацію читабельною.
"""

from __future__ import annotations

import math
import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import i18n
import theme


class ArduPilotLinkMixin:
    """Зв'язок з польотним контролером ArduPilot по MAVLink: підключення,
    Read/Write місії, Info, файловий менеджер SD-карти."""

    def _build_connect_bar(self, parent: ttk.Frame):
        """
        Порт / швидкість обміну / кнопка "Підєднатись" -- як у Mission
        Planner. Поля без підписів, вибір лише випадаючим списком.
        """
        colors = self.palette

        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="115200")

        ports = self._list_serial_ports()
        port_values = ["AUTO"] + ports
        port_combo = ttk.Combobox(
            parent, textvariable=self.port_var, values=port_values, width=10,
            state="readonly",
        )
        self.port_var.set("AUTO")
        port_combo.pack(side="left", padx=(0, 4))

        baud_combo = ttk.Combobox(
            parent, textvariable=self.baud_var, width=7, state="readonly",
            values=["4800", "9600", "19200", "38400", "57600", "115200", "230400"],
        )
        baud_combo.pack(side="left", padx=(0, 6))

        dark = colors.get("dark", False)
        # idle_bg раніше був захардкоджений СВІТЛИЙ ("#DEE3E8") незалежно
        # від теми, а idle_fg (colors["text"]) У ТЕМНІЙ темі стає світлим
        # -- разом це давало майже білий текст на майже білому фоні.
        idle_bg = "#3a3a3a" if dark else "#DEE3E8"
        idle_fg = colors["text"]
        idle_active_bg = "#4a4a4a" if dark else "#C9CFD6"
        idle_pad = (16, 6)
        active_bg, active_fg = colors["header_bg"], colors["text_light"]
        active_pad = (8, 2)
        border = colors["border"]

        self.connect_btn = tk.Button(
            parent, text=i18n.t("btn_connect"),
            bg=idle_bg, fg=idle_fg, activebackground=idle_active_bg, activeforeground=idle_fg,
            font=("Segoe UI", 9, "bold"), bd=2, relief="groove", cursor="hand2",
            padx=idle_pad[0], pady=idle_pad[1],
            highlightthickness=1, highlightbackground=border, highlightcolor=border,
            command=self._toggle_flight_connection,
        )
        self.connect_btn.pack(side="left")

        self._connect_idle_style = dict(bg=idle_bg, fg=idle_fg, padx=idle_pad[0], pady=idle_pad[1])
        self._connect_active_style = dict(bg=active_bg, fg=active_fg, padx=active_pad[0], pady=active_pad[1])

        def _retranslate_connect_btn():
            # текст кнопки залежить від стану з'єднання, а не лише мови --
            # звичайний self._reg_i18n тут не підходить, тому окремий
            # callback, який дивиться на self._flight_conn
            if self._flight_conn is not None:
                self.connect_btn.configure(text=i18n.t("btn_disconnect"))
            else:
                self.connect_btn.configure(text=i18n.t("btn_connect"))

        self._retranslate_callbacks.append(_retranslate_connect_btn)


    def _refresh_connect_btn_colors(self):
        """Перераховує стилі кнопки "Підключити"/"Відключити" під ПОТОЧНУ
        self.palette і застосовує відповідний (залежно від того,
        підключено зараз чи ні) -- викликається з apply_app_theme() при
        живому перемиканні теми, інакше кнопка лишилась би зі старими
        кольорами (той самий баг "світлий текст на світлому фоні")."""
        if not hasattr(self, "connect_btn") or not self.connect_btn.winfo_exists():
            return
        colors = self.palette
        dark = colors.get("dark", False)
        idle_bg = "#3a3a3a" if dark else "#DEE3E8"
        idle_fg = colors["text"]
        idle_active_bg = "#4a4a4a" if dark else "#C9CFD6"
        idle_pad = (16, 6)
        active_bg, active_fg = colors["header_bg"], colors["text_light"]
        active_pad = (8, 2)
        border = colors["border"]

        self._connect_idle_style = dict(bg=idle_bg, fg=idle_fg, padx=idle_pad[0], pady=idle_pad[1])
        self._connect_active_style = dict(bg=active_bg, fg=active_fg, padx=active_pad[0], pady=active_pad[1])

        if self._flight_conn is not None:
            self.connect_btn.configure(activebackground=active_bg, activeforeground=active_fg, **self._connect_active_style)
        else:
            self.connect_btn.configure(
                activebackground=idle_active_bg, activeforeground=idle_fg,
                highlightbackground=border, highlightcolor=border,
                **self._connect_idle_style,
            )


    @staticmethod
    def _list_serial_ports() -> list[str]:
        try:
            import serial.tools.list_ports as list_ports
        except ImportError:
            return []
        try:
            return [p.device for p in list_ports.comports()]
        except Exception:
            return []


    def _toggle_flight_connection(self):
        if self._flight_conn is not None:
            self._disconnect_flight_controller()
            return

        port = self.port_var.get().strip()
        baud_txt = self.baud_var.get().strip()
        if not port:
            messagebox.showwarning(i18n.t("msg_no_data_title"), i18n.t("msg_choose_port_body"))
            return
        try:
            baud = int(baud_txt)
        except ValueError:
            messagebox.showwarning(i18n.t("msg_no_data_title"), i18n.t("msg_bad_baud_body"))
            return

        status_key = "status_auto_detecting" if port == "AUTO" else "status_connecting"
        self.connect_btn.configure(text=i18n.t(status_key), state="disabled")
        threading.Thread(target=self._connect_worker, args=(port, baud), daemon=True).start()


    def _connect_worker(self, port: str, baud: int):
        """Фоновий поток: тут можна безпечно чекати на heartbeat, не підвішуючи вікно."""
        if port == "AUTO":
            conn, found_port, found_baud, error = self._auto_detect_and_connect(baud)
            self.after(
                0,
                lambda: self._on_connect_result(conn, error, found_port or "AUTO", found_baud or baud),
            )
            return

        conn = None
        error = None
        try:
            from pymavlink import mavutil
            conn = mavutil.mavlink_connection(port, baud=baud)
            hb = conn.wait_heartbeat(timeout=10)
            if hb is None:
                error = i18n.t("msg_no_heartbeat_body")
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
        except ImportError:
            # pymavlink не встановлено -- пробуємо хоча б просто відкрити порт
            try:
                import serial
                conn = serial.Serial(port, baud, timeout=2)
            except Exception as e:
                error = str(e)
        except Exception as e:
            error = str(e)
        self.after(0, lambda: self._on_connect_result(conn, error, port, baud))


    def _auto_detect_and_connect(self, preferred_baud: int):
        """
        AUTO (як у Mission Planner): перебирає доступні COM-порти й типові
        для ArduPilot швидкості обміну (спочатку ту, що обрав користувач,
        далі найпоширеніші), підключається до ПЕРШОГО порту, що реально
        відповість MAVLink heartbeat -- а не просто відкриється. Виконується
        у фоновому потоці (див. _connect_worker), щоб не підвішувати вікно.

        Повертає (conn, port, baud, error) -- conn=None і error заповнено,
        якщо жоден порт/швидкість не відповіли.
        """
        from pymavlink import mavutil

        ports = self._list_serial_ports()
        if not ports:
            return None, None, None, i18n.t("msg_no_ports_found")

        common_bauds = [57600, 115200, 921600]
        bauds = [preferred_baud] + [b for b in common_bauds if b != preferred_baud]

        last_error = None
        for port in ports:
            for baud in bauds:
                conn = None
                try:
                    conn = mavutil.mavlink_connection(port, baud=baud)
                    hb = conn.wait_heartbeat(timeout=2)
                    if hb is not None:
                        return conn, port, baud, None
                    conn.close()
                except Exception as e:
                    last_error = str(e)
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
        return None, None, None, last_error or i18n.t("msg_auto_detect_failed")


    def _on_connect_result(self, conn, error, port: str, baud: int):
        if conn is None or error:
            self.connect_btn.configure(text=i18n.t("btn_connect"), state="normal", **self._connect_idle_style)
            messagebox.showerror(
                "MAVLink",
                i18n.t("msg_connect_failed_body_fmt", port=port, baud=baud)
                + (f":\n{error}" if error else ""),
            )
            return

        self._flight_conn = conn
        # AUTO підставляє в поля реально знайдені порт/швидкість -- щоб
        # було видно, що саме знайшлося, і щоб Disconnect/повторний Connect
        # надалі працювали з конкретним портом, а не знову шукали
        self.port_var.set(port)
        self.baud_var.set(str(baud))
        self.connect_btn.configure(text=i18n.t("btn_disconnect"), state="normal", **self._connect_active_style)
        self.connect_btn.update_idletasks()
        self.status_var.set(i18n.t("status_connected_fmt", port=port, baud=baud))
        # показуємо кнопки Info/Read/Write/Files (у цьому порядку, зліва
        # направо всередині правої підгрупи)
        if hasattr(self, "_ardu_read_btn"):
            self._ardu_info_btn.pack(side="left")
            self._ardu_read_btn.pack(side="left", padx=6)
            self._ardu_write_btn.pack(side="left")
            self._ardu_files_btn.pack(side="left", padx=6)
            self._ardu_commands_btn.pack(side="left")
            self._ardu_params_btn.pack(side="left", padx=6)
            self._ardu_btns_visible = True


    def _disconnect_flight_controller(self):
        if self._flight_conn is not None:
            try:
                self._flight_conn.close()
            except Exception:
                pass
            self._flight_conn = None
        self.connect_btn.configure(text=i18n.t("btn_connect"), state="normal", **self._connect_idle_style)
        self.status_var.set("")
        # ховаємо кнопки Info/Read/Write/Files
        if hasattr(self, "_ardu_read_btn") and self._ardu_btns_visible:
            self._ardu_info_btn.pack_forget()
            self._ardu_read_btn.pack_forget()
            self._ardu_write_btn.pack_forget()
            self._ardu_files_btn.pack_forget()
            self._ardu_commands_btn.pack_forget()
            self._ardu_params_btn.pack_forget()
            self._ardu_btns_visible = False


    def _show_flight_info(self):
        """Кнопка «Info»: запитує в польотного контролера максимум
        відомостей про себе -- версію прошивки/плати, набір датчиків,
        інформацію про SD-карту (обсяг) і список файлів на ній
        (MAVLink FTP). Усе в фоновому потоці, щоб не підвішувати вікно."""
        if self._flight_conn is None:
            return
        self._ardu_info_btn.configure(state="disabled")
        self.status_var.set(i18n.t("status_fetching_info"))
        threading.Thread(target=self._fetch_flight_info_worker, daemon=True).start()


    def _fetch_flight_info_worker(self):
        from pymavlink import mavutil
        conn = self._flight_conn
        report = None
        error = None

        def set_progress(text):
            # thread-safe оновлення статус-рядка з фонового потоку --
            # Tkinter-змінні не можна чіпати напряму з не-головного
            # потоку, тому через self.after(0, ...), як і скрізь у
            # проєкті для подібних оновлень з worker-потоків.
            self.after(0, lambda: self.status_var.set(text))

        try:
            ts, tc = conn.target_system, conn.target_component

            # --- AUTOPILOT_VERSION: версія прошивки, плата, vendor/product, UID ---
            set_progress(i18n.t("status_info_step_version"))
            conn.mav.command_long_send(
                ts, tc, mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE, 0,
                mavutil.mavlink.MAVLINK_MSG_ID_AUTOPILOT_VERSION, 0, 0, 0, 0, 0, 0,
            )
            ver = conn.recv_match(type="AUTOPILOT_VERSION", blocking=True, timeout=5)

            # --- SYS_STATUS: набір датчиків (часто й так вже йде в потоці
            # телеметрії -- спочатку пробуємо просто прийняти, без запиту) ---
            set_progress(i18n.t("status_info_step_sensors"))
            sys_status = conn.recv_match(type="SYS_STATUS", blocking=True, timeout=3)
            if sys_status is None:
                conn.mav.command_long_send(
                    ts, tc, mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE, 0,
                    mavutil.mavlink.MAVLINK_MSG_ID_SYS_STATUS, 0, 0, 0, 0, 0, 0,
                )
                sys_status = conn.recv_match(type="SYS_STATUS", blocking=True, timeout=3)

            # --- SCALED_PRESSURE: абсолютний тиск і температура барометра ---
            conn.mav.command_long_send(
                ts, tc, mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE, 0,
                mavutil.mavlink.MAVLINK_MSG_ID_SCALED_PRESSURE, 0, 0, 0, 0, 0, 0,
            )
            scaled_pressure = conn.recv_match(type="SCALED_PRESSURE", blocking=True, timeout=3)

            # --- STORAGE_INFORMATION: наявність і обсяг SD-карти ---
            set_progress(i18n.t("status_info_step_storage"))
            conn.mav.command_long_send(
                ts, tc, mavutil.mavlink.MAV_CMD_REQUEST_STORAGE_INFORMATION, 0,
                0, 1, 0, 0, 0, 0, 0,
            )
            storage = conn.recv_match(type="STORAGE_INFORMATION", blocking=True, timeout=5)

            # --- список файлів на SD (MAVLink FTP, вбудований в pymavlink) ---
            files = None
            ftp_error = None
            scripted_commands = []
            scanned_lua_files = []
            has_ftp_cap = ver is not None and bool(
                ver.capabilities & mavutil.mavlink.MAV_PROTOCOL_CAPABILITY_FTP
            )
            if has_ftp_cap:
                try:
                    set_progress(i18n.t("status_info_step_files"))
                    from pymavlink.mavftp import MAVFTP
                    ftp = MAVFTP(conn, ts, tc)
                    ret = ftp.cmd_list(["/"])
                    if ret.error_code == 0:
                        files = ftp.list_result
                    else:
                        ftp_error = self._describe_ftp_error(ret.operation_name, ret.error_code)

                    # --- сканування APM/scripts/ на MCP-розмітку --
                    # ОКРЕМИЙ виклик cmd_list, ПІСЛЯ того, як files уже
                    # захопив посилання на СТАРИЙ ftp.list_result (сам
                    # ftp.list_result ПЕРЕЗАПИСУЄТЬСЯ новим списком на
                    # кожен cmd_list, тому порядок тут важливий -- інакше
                    # files (корінь SD) перетворився б на список APM/
                    # scripts/ заднім числом). Відсутність цієї папки
                    # (SCR_ENABLE=0 чи просто немає жодного скрипта) --
                    # НЕ помилка, просто scripted_commands лишається
                    # порожнім.
                    set_progress(i18n.t("status_info_step_scripts"))
                    ret_scripts = ftp.cmd_list(["/APM/scripts"])
                    if ret_scripts.error_code == 0:
                        lua_texts = {}
                        script_entries = [e for e in ftp.list_result if not e.is_dir and e.name.lower().endswith(".lua")]
                        for i, entry in enumerate(script_entries, 1):
                            set_progress(i18n.t(
                                "status_info_step_script_file_fmt",
                                name=entry.name, done=i, total=len(script_entries),
                            ))
                            remote_path = f"/APM/scripts/{entry.name}"
                            captured = {}

                            def _capture(fh, _captured=captured):
                                _captured["data"] = fh.read()

                            ftp.cmd_get([remote_path], callback=_capture)
                            ret_get = ftp.process_ftp_reply("get", timeout=30)
                            if ret_get.error_code == 0 and "data" in captured:
                                try:
                                    lua_texts[entry.name] = captured["data"].decode("utf-8", errors="replace")
                                    scanned_lua_files.append(entry.name)
                                except Exception:
                                    pass
                        if lua_texts:
                            from mcp_script_scanner import parse_mcp_commands_from_files, parse_scripts_meta_from_files
                            scripted_commands = parse_mcp_commands_from_files(lua_texts)
                            self._scripts_meta = parse_scripts_meta_from_files(lua_texts)
                except Exception as e:
                    ftp_error = str(e)
            elif ver is not None:
                ftp_error = i18n.t("info_ftp_not_supported")

            self._scripted_commands = scripted_commands
            self._scripts_meta = getattr(self, "_scripts_meta", {})
            # зберігаємо температуру барометра для HUD (оновлюється рідко,
            # тому просто при кожному натисканні Info, а не в реальному часі)
            self._hud_baro_temp = (
                scaled_pressure.temperature / 100.0 if scaled_pressure is not None else None
            )
            report = self._format_flight_info(
                conn, ver, sys_status, scaled_pressure, storage, files, ftp_error,
                scripted_commands, scanned_lua_files, self._scripts_meta,
            )
        except Exception as e:
            error = str(e)
        self.after(0, lambda: self._on_flight_info_ready(report, error))


    @staticmethod
    def _decode_firmware_version(raw: int) -> str:
        """flight_sw_version -- одне uint32: major<<24|minor<<16|patch<<8|type."""
        from pymavlink import mavutil
        major = (raw >> 24) & 0xFF
        minor = (raw >> 16) & 0xFF
        patch = (raw >> 8) & 0xFF
        fw_type = raw & 0xFF
        type_names = {
            0: "dev", 64: "alpha", 128: "beta", 192: "rc", 255: "official",
        }
        type_name = type_names.get(fw_type, f"0x{fw_type:02x}")
        return f"{major}.{minor}.{patch} ({type_name})"


    @staticmethod
    def _decode_sensor_bits(present: int, enabled: int, health: int) -> list[tuple[str, bool, bool, bool]]:
        """Розбирає бітові маски SYS_STATUS.onboard_control_sensors_* на
        список (назва, є_в_наявності, увімкнено, справний) для кожного
        біта, який реально виставлено хоча б в present."""
        from pymavlink import mavutil
        out = []
        for bit, entry in sorted(mavutil.mavlink.enums["MAV_SYS_STATUS_SENSOR"].items()):
            # тільки "чисті" одно-бітові прапорці -- ENUM_END і подібні
            # службові маркери в pymavlink НЕ є степенем двійки (напр.
            # 1073741825 = PROPULSION+1) і випадково перетинаються з
            # реальними бітами (з GYRO, біт 0) при AND, тому фільтруємо
            if bit == 0 or (bit & (bit - 1)) != 0:
                continue
            if (present & bit) == 0:
                continue
            name = entry.name.replace("MAV_SYS_STATUS_", "").replace("_", " ").title()
            out.append((name, True, bool(enabled & bit), bool(health & bit)))
        return out


    def _format_flight_info(
        self, conn, ver, sys_status, scaled_pressure=None, storage=None, files=None, ftp_error=None,
        scripted_commands=None, scanned_lua_files=None, scripts_meta=None,
    ) -> str:
        from pymavlink import mavutil
        lines = []

        # --- шапка: тип апарату/автопілота з HEARTBEAT ---
        # ВАЖЛИВО: не conn.messages["HEARTBEAT"] напряму -- pymavlink
        # зберігає ОСТАННЄ повідомлення кожного типу ПО SYSTEM ID, БЕЗ
        # урахування COMPONENT ID! Якщо на тій самій шині MAVLink є ще
        # й інші компоненти (ADS-B приймач, гімбал, компаньйон-
        # комп'ютер тощо) з ТИМ САМИМ system id, що й сам автопілот --
        # їхній HEARTBEAT перезаписує запис, і "тип апарату" міг би
        # показати "ADS-B передавач" замість реального типу -- саме це
        # й трапилось на практиці. pymavlink вже має власний, коректний
        # фільтр для цього (probably_vehicle_heartbeat -- виключає
        # ADS-B/GIMBAL/GCS/ONBOARD_CONTROLLER і MAV_AUTOPILOT_INVALID),
        # результат зберігається в conn.sysid_state[sysid].mav_type/
        # mav_autopilot -- звідси й берем, а не з сирого messages dict.
        sysid = getattr(conn, "target_system", None)
        state = conn.sysid_state.get(sysid) if sysid is not None else None
        if state is not None:
            autopilot_name = mavutil.mavlink.enums["MAV_AUTOPILOT"].get(state.mav_autopilot)
            # Тип апарату -- ПЕРЕКЛАД на мову інтерфейсу (mav_type_<ID>,
            # i18n.py), а НЕ сира англійська назва enum. Ключ за
            # ЧИСЛОВИМ ID (mav_type), не за іменем -- надійніше. Якщо
            # раптом трапиться ID поза відомим діапазоном (майбутнє
            # розширення MAVLink) -- запасний варіант, сира назва enum.
            key = f"mav_type_{state.mav_type}"
            vtype_translated = i18n.t(key)
            if vtype_translated == key:  # немає перекладу -- i18n.t() повертає сам ключ
                type_name = mavutil.mavlink.enums["MAV_TYPE"].get(state.mav_type)
                vtype_translated = type_name.name.replace("MAV_TYPE_", "") if type_name else "?"
            lines.append(i18n.t(
                "info_header_fmt",
                autopilot=autopilot_name.name.replace("MAV_AUTOPILOT_", "") if autopilot_name else "?",
                vtype=vtype_translated,
            ))
            lines.append("")

        # --- AUTOPILOT_VERSION ---
        lines.append(i18n.t("info_section_firmware"))
        lines.append("-" * 44)
        if ver is None:
            lines.append(i18n.t("info_no_response"))
        else:
            lines.append(i18n.t("info_fw_version_fmt", version=self._decode_firmware_version(ver.flight_sw_version)))
            if ver.board_version:
                lines.append(i18n.t("info_board_version_fmt", version=ver.board_version))
            if ver.vendor_id or ver.product_id:
                lines.append(i18n.t("info_vendor_product_fmt", vendor=ver.vendor_id, product=ver.product_id))
            uid = ver.uid
            if uid:
                lines.append(i18n.t("info_uid_fmt", uid=f"{uid:016x}"))
            fc_hash = bytes(ver.flight_custom_version).rstrip(b"\x00")
            if fc_hash:
                # flight_custom_version -- 8 байт, де ArduPilot зберігає
                # git-хеш САМЕ ЯК ASCII-символи (не як бінарне число).
                # .hex() давало б "3364623462633765" (hex-дамп байтів) --
                # незрозуміло людині. .decode("ascii") дає "3db4bc7e" --
                # справжній git short hash, по якому можна знайти коміт.
                try:
                    hash_str = fc_hash.decode("ascii").strip()
                except UnicodeDecodeError:
                    hash_str = fc_hash.hex()  # запасний варіант, якщо раптом не ASCII
                lines.append(i18n.t("info_git_hash_fmt", hash=hash_str))

            # --- Middleware і OS -- виробник БПЛА міг записати сюди
            # власний номер версії (та сама 8-байтна ASCII-структура що й
            # flight_custom_version). Стокова ArduPilot записує git-хеш
            # ChibiOS або нулі -- усе інше майже напевно кастомна мітка.
            def _try_decode_custom(raw_bytes):
                data = bytes(raw_bytes).rstrip(b"\x00")
                if not data:
                    return None
                try:
                    return data.decode("ascii").strip()
                except UnicodeDecodeError:
                    return data.hex()

            mw_ver = ver.middleware_sw_version
            if mw_ver:
                lines.append(i18n.t("info_middleware_version_fmt", version=mw_ver))

            mw_hash = _try_decode_custom(ver.middleware_custom_version)
            if mw_hash:
                lines.append(i18n.t("info_middleware_hash_fmt", hash=mw_hash))

            os_hash = _try_decode_custom(ver.os_custom_version)
            if os_hash:
                lines.append(i18n.t("info_os_hash_fmt", hash=os_hash))

        lines.append("")

        # --- SYS_STATUS: датчики ---
        lines.append(i18n.t("info_section_sensors"))
        lines.append("-" * 44)
        if sys_status is None:
            lines.append(i18n.t("info_no_response"))
        else:
            # зовнішні датчики: якщо несправні -- майже завжди просто
            # не підключені (а не реально зламані), тому додаємо підказку
            # "(відсутній?)" -- зі знаком питання, бо ми не можемо знати
            # напевно (можливо підключений, але реально не працює).
            from pymavlink import mavutil as _mu
            _EXTERNAL_SENSOR_BITS = frozenset([
                _mu.mavlink.MAV_SYS_STATUS_SENSOR_DIFFERENTIAL_PRESSURE,
                _mu.mavlink.MAV_SYS_STATUS_SENSOR_GPS,
                _mu.mavlink.MAV_SYS_STATUS_SENSOR_OPTICAL_FLOW,
                _mu.mavlink.MAV_SYS_STATUS_SENSOR_RC_RECEIVER,
                _mu.mavlink.MAV_SYS_STATUS_SENSOR_LASER_POSITION,
                _mu.mavlink.MAV_SYS_STATUS_SENSOR_EXTERNAL_GROUND_TRUTH,
                _mu.mavlink.MAV_SYS_STATUS_AHRS,
            ])
            present_mask = sys_status.onboard_control_sensors_present
            sensors = self._decode_sensor_bits(
                present_mask,
                sys_status.onboard_control_sensors_enabled,
                sys_status.onboard_control_sensors_health,
            )
            if not sensors:
                lines.append(i18n.t("info_no_sensors"))
            for name, present, enabled, healthy in sensors:
                if enabled and healthy:
                    mark = "OK"
                elif not enabled:
                    mark = "--"
                else:
                    # несправний -- перевіряємо, чи є зовнішнім датчиком
                    bit_val = None
                    for bit, entry in _mu.mavlink.enums["MAV_SYS_STATUS_SENSOR"].items():
                        if bit == 0 or (bit & (bit - 1)) != 0:
                            continue
                        n = entry.name.replace("MAV_SYS_STATUS_", "").replace("_", " ").title()
                        if n == name and bit in _EXTERNAL_SENSOR_BITS:
                            bit_val = bit
                            break
                    if bit_val is not None:
                        mark = i18n.t("info_sensor_unhealthy") + " " + i18n.t("info_sensor_absent_hint")
                    else:
                        mark = i18n.t("info_sensor_unhealthy")
                lines.append(f"  {name:<28} {mark}")
            lines.append("")

            # --- Барометр: абсолютний тиск і температура ---
            if scaled_pressure is not None:
                temp_c = scaled_pressure.temperature / 100.0
                lines.append(i18n.t(
                    "info_baro_fmt",
                    press=scaled_pressure.press_abs,
                    temp=temp_c,
                ))
                lines.append("")

            na = i18n.t("value_na")
            voltage_s = f"{sys_status.voltage_battery / 1000.0:.2f}" if sys_status.voltage_battery not in (0, 65535) else na
            current_s = f"{sys_status.current_battery / 100.0:.2f}" if sys_status.current_battery >= 0 else na
            remaining_s = f"{sys_status.battery_remaining}" if sys_status.battery_remaining >= 0 else na
            lines.append(i18n.t(
                "info_battery_fmt", voltage=voltage_s, current=current_s, remaining=remaining_s,
            ))
        lines.append("")

        # --- STORAGE_INFORMATION: SD-карта ---
        # STORAGE_INFORMATION -- частина Camera Protocol (для зовнішнього
        # сховища гімбала/компаньйона), а не власної SD автопілота.
        # ArduPilot часто просто не відповідає на нього змістовно (status
        # == EMPTY), навіть коли карта реально є й читається. Тому не
        # довіряємо лише цьому повідомленню -- якщо MAVFTP щойно успішно
        # прочитав список файлів (files не None), це вже пряме
        # підтвердження, що карта є і доступна, навіть без цифр обсягу.
        lines.append(i18n.t("info_section_storage"))
        lines.append("-" * 44)
        storage_ready = storage is not None and storage.status == mavutil.mavlink.STORAGE_STATUS_READY
        sd_confirmed_via_ftp = files is not None

        if storage_ready:
            lines.append(i18n.t(
                "info_storage_capacity_fmt",
                total=storage.total_capacity, used=storage.used_capacity,
                available=storage.available_capacity,
            ))
            if storage.read_speed or storage.write_speed:
                lines.append(i18n.t(
                    "info_storage_speed_fmt", read=storage.read_speed, write=storage.write_speed,
                ))
        elif sd_confirmed_via_ftp:
            lines.append(i18n.t("info_sd_present_no_capacity"))
        else:
            lines.append(i18n.t("info_no_sd_card"))
        lines.append("")

        # --- список файлів на SD ---
        lines.append(i18n.t("info_section_files"))
        lines.append("-" * 44)
        if files is not None:
            if not files:
                lines.append(i18n.t("info_no_files"))
            for entry in files:
                if entry.is_dir:
                    lines.append(f"  [DIR]  {entry.name}")
                else:
                    lines.append(f"         {entry.name}  ({entry.size_b:,} B)".replace(",", " "))
        elif ftp_error:
            lines.append(i18n.t("info_ftp_error_fmt", error=ftp_error))
        else:
            lines.append(i18n.t("info_no_response"))

        # --- скриптові команди (MCP-COMMAND у .lua на APM/scripts/) ---
        # ГРУПУЄМО ПО ФАЙЛУ (не єдиний плоский список команд) -- для
        # КОЖНОГО реально відсканованого .lua явно показуємо або "без
        # розмітки" (файл існує й прочитаний, але MCP-COMMAND у ньому
        # немає -- саме так пользувач і зловив цю ситуацію: скрипт
        # старої версії, без розмітки, на карті), або список знайдених
        # команд з готовою інструкцією "як вбудувати" (NAV_SCRIPT_TIME,
        # який param1 і що покласти в які параметри) -- а не просто
        # назву й параметри без пояснення, що з ними робити.
        lines.append("")
        lines.append(i18n.t("info_section_scripts"))
        lines.append("-" * 44)
        scanned_lua_files = scanned_lua_files or []
        scripted_commands = scripted_commands or []
        scripts_meta = scripts_meta or {}
        if scanned_lua_files:
            by_file: dict[str, list[dict]] = {}
            for cmd in scripted_commands:
                by_file.setdefault(cmd["source_file"], []).append(cmd)

            for filename in scanned_lua_files:
                meta = scripts_meta.get(filename, {})

                # --- заголовок файлу ---
                display_name = meta.get("script_name") or filename
                version_str  = f" v{meta['script_version']}" if meta.get("script_version") else ""
                lines.append(i18n.t("info_script_file_fmt", name=f"{display_name}{version_str}"))

                # --- тип скрипта ---
                stype = meta.get("script_type")
                if stype == "background":
                    lines.append("  " + i18n.t("info_script_type_background"))
                elif stype == "mission":
                    lines.append("  " + i18n.t("info_script_type_mission"))

                # --- опис ---
                if meta.get("script_desc"):
                    lines.append(f"  {meta['script_desc']}")

                cmds = by_file.get(filename, [])
                markup_cmds    = [c for c in cmds if c.get("from_markup")]
                heuristic_cmds = [c for c in cmds if not c.get("from_markup")]

                if not meta.get("has_markup") and not cmds:
                    lines.append("  " + i18n.t("info_script_no_markup"))
                    lines.append("")
                    continue

                # повна розмітка (рівень 1)
                for cmd in markup_cmds:
                    lines.append("  " + i18n.t(
                        "info_script_cmd_embed_fmt", cmd=cmd["cmd"], name=cmd["name"],
                    ))
                    for p in cmd["params"]:
                        lines.append("    " + i18n.t(
                            "info_script_param_fmt",
                            slot=p["slot"], label=p["label"], unit=p["unit"],
                        ))

                # евристика (рівень 2)
                if heuristic_cmds:
                    if markup_cmds:
                        lines.append("  " + i18n.t("info_script_heuristic_header"))
                    for cmd in heuristic_cmds:
                        lines.append("  " + i18n.t(
                            "info_script_heuristic_cmd_fmt",
                            cmd=cmd["cmd"], name=cmd["name"],
                        ))

                lines.append("")
        else:
            lines.append(i18n.t("info_no_scripted_commands"))

        return "\n".join(lines)


    def _on_flight_info_ready(self, report: str | None, error: str | None):
        self.status_var.set("")
        if error or report is None:
            self._ardu_info_btn.configure(state="normal")
            messagebox.showerror(i18n.t("msg_update_title"), i18n.t("info_fetch_error_fmt", error=error or "?"))
            return
        self._show_flight_info_dialog(report)


    def _show_flight_info_dialog(self, report_text: str):
        dlg = tk.Toplevel(self)
        dlg.title(i18n.t("dlg_flight_info_title"))
        dlg.geometry("560x780")
        dlg.transient(self)

        def _on_close():
            self._stop_hud_telemetry()
            dlg.destroy()
            self._ardu_info_btn._is_toggle_active = False
            self._refresh_toggle_action_button_colors(self._toggle_buttons_registry)
            self._ardu_info_btn.configure(state="normal")

        # --- HUD-панель (верхня частина) ---
        hud_frame = tk.Frame(dlg, bg="#111111")
        hud_frame.pack(fill="x", padx=8, pady=(8, 4))

        HUD_W, HUD_H = 300, 170
        hud_canvas = tk.Canvas(
            hud_frame, width=HUD_W, height=HUD_H,
            bg="#1a3a5a", highlightthickness=1, highlightbackground="#333",
        )
        hud_canvas.pack(side="left", padx=(0, 10))

        data_frame = tk.Frame(hud_frame, bg="#111111")
        data_frame.pack(side="left", fill="y", anchor="w")

        _DATA_FONT = ("Consolas", 11, "bold")
        _DATA_FG = "#00ff88"
        _DATA_BG = "#111111"
        roll_var  = tk.StringVar(value="ROLL:  ---")
        pitch_var = tk.StringVar(value="PITCH: ---")
        alt_var   = tk.StringVar(value="ALT:   ---")
        temp_var  = tk.StringVar(value="TEMP:  ---")

        for var in (roll_var, pitch_var, alt_var, temp_var):
            tk.Label(data_frame, textvariable=var, bg=_DATA_BG, fg=_DATA_FG,
                     font=_DATA_FONT, anchor="w").pack(anchor="w", pady=3)

        # ініціалізуємо температуру відразу -- вона вже відома з початкового
        # запиту (SCALED_PRESSURE), не потребує реального часу
        if getattr(self, "_hud_baro_temp", None) is not None:
            temp_var.set(f"TEMP:  {self._hud_baro_temp:.1f}°C")

        # --- Текстовий звіт (нижня частина) ---
        text = scrolledtext.ScrolledText(dlg, wrap="word", font=("Consolas", 9))
        text.pack(fill="both", expand=True, padx=8, pady=8)
        text.insert("end", report_text)
        theme.make_text_readonly(text)

        # --- Старт живого потоку телеметрії ---
        self._hud_state = {"roll": 0.0, "pitch": 0.0, "alt": 0.0, "active": True}
        self._start_hud_telemetry()

        def _update_hud():
            if not dlg.winfo_exists():
                return
            s = self._hud_state
            roll_r  = s.get("roll", 0.0)
            pitch_r = s.get("pitch", 0.0)
            alt     = s.get("alt", 0.0)
            self._draw_artificial_horizon(hud_canvas, roll_r, pitch_r)
            roll_var.set(f"ROLL:  {math.degrees(roll_r):+.1f}°")
            pitch_var.set(f"PITCH: {math.degrees(pitch_r):+.1f}°")
            alt_var.set(f"ALT:   {alt:.1f} м")
            dlg.after(100, _update_hud)

        dlg.after(200, _update_hud)
        dlg.protocol("WM_DELETE_WINDOW", _on_close)


    @staticmethod
    def _draw_artificial_horizon(canvas: tk.Canvas, roll_rad: float, pitch_rad: float):
        """Малює мінімальний штучний горизонт (крен/тангаж) на Canvas.
        Небо -- синє, земля -- коричнева, лінія горизонту -- біла.
        Координати canvas: x -- праворуч, y -- вниз (стандарт Tkinter)."""
        W = canvas.winfo_width()  or int(canvas.cget("width"))
        H = canvas.winfo_height() or int(canvas.cget("height"))
        cx, cy = W / 2, H / 2
        PX_PER_DEG = 2.2

        # зміщення горизонту по висоті: більший тангаж = горизонт нижче
        # (більше неба видно вгорі)
        pitch_deg = math.degrees(pitch_rad)
        hy = cy + pitch_deg * PX_PER_DEG

        # напрямок лінії горизонту (обертається разом з кутом крену)
        line_dx = math.cos(roll_rad)
        line_dy = math.sin(roll_rad)  # позитивний крен → правий бік нижче

        # нормаль "вгору" (убік неба, перпендикулярно горизонту)
        sky_nx =  math.sin(roll_rad)
        sky_ny = -math.cos(roll_rad)

        def is_sky(px, py):
            return (px - cx) * sky_nx + (py - hy) * sky_ny > 0

        # кінці лінії горизонту далеко за межі canvas
        ext = max(W, H) * 2
        hx1, hy1 = cx - ext * line_dx, hy - ext * line_dy
        hx2, hy2 = cx + ext * line_dx, hy + ext * line_dy

        corners = [(0, 0), (W, 0), (W, H), (0, H)]
        above = [is_sky(x, y) for x, y in corners]

        sky_poly = [hx1, hy1]
        for i, (x, y) in enumerate(corners):
            if above[i]: sky_poly += [x, y]
        sky_poly += [hx2, hy2]

        ground_poly = [hx2, hy2]
        for i, (x, y) in enumerate(corners):
            if not above[i]: ground_poly += [x, y]
        ground_poly += [hx1, hy1]

        canvas.delete("all")
        canvas.create_rectangle(0, 0, W, H, fill="#1B6CB7", outline="")  # небо
        if len(ground_poly) >= 6:
            canvas.create_polygon(ground_poly, fill="#8B5C20", outline="")  # земля

        # сходинки тангажу
        for deg in [-20, -15, -10, -5, 5, 10, 15, 20]:
            off = -deg * PX_PER_DEG * sky_ny, -deg * PX_PER_DEG * (-sky_nx)
            tcx = cx - deg * PX_PER_DEG * sky_nx
            tcy = hy - deg * PX_PER_DEG * sky_ny
            hw = 25 if deg % 10 == 0 else 12
            canvas.create_line(
                tcx - hw * line_dx, tcy - hw * line_dy,
                tcx + hw * line_dx, tcy + hw * line_dy,
                fill="white", width=1,
            )

        # лінія горизонту
        canvas.create_line(hx1, hy1, hx2, hy2, fill="white", width=2)

        # дуга крену вгорі + позначки
        arc_r = min(cx, cy) - 12
        for angle_deg in (0, 10, 20, 30, -10, -20, -30, 45, -45, 60, -60):
            a = math.radians(angle_deg)
            ax = cx + arc_r * math.sin(a)
            ay = cy - arc_r * math.cos(a)
            tlen = 9 if angle_deg in (0, 30, -30, 60, -60) else 5
            ax2 = cx + (arc_r - tlen) * math.sin(a)
            ay2 = cy - (arc_r - tlen) * math.cos(a)
            canvas.create_line(ax, ay, ax2, ay2, fill="#bbbbbb", width=1)

        # трикутник-покажчик крену
        tri_r = arc_r - 2
        a = roll_rad
        tx  = cx + tri_r * math.sin(a)
        ty  = cy - tri_r * math.cos(a)
        tb1x = cx + (tri_r + 10) * math.sin(a - math.radians(5))
        tb1y = cy - (tri_r + 10) * math.cos(a - math.radians(5))
        tb2x = cx + (tri_r + 10) * math.sin(a + math.radians(5))
        tb2y = cy - (tri_r + 10) * math.cos(a + math.radians(5))
        canvas.create_polygon([tx, ty, tb1x, tb1y, tb2x, tb2y], fill="white", outline="")

        # нерухомий символ ПС (центральний хрест -- фіксований, не обертається)
        canvas.create_line(cx - 38, cy, cx - 10, cy, fill="#FFD700", width=3)
        canvas.create_line(cx + 10, cy, cx + 38, cy, fill="#FFD700", width=3)
        canvas.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill="#FFD700", outline="")


    def _start_hud_telemetry(self):
        """Запитує потік ATTITUDE і VFR_HUD і запускає фоновий потік-приймач."""
        conn = self._flight_conn
        if conn is None:
            return
        from pymavlink import mavutil
        ts, tc = conn.target_system, conn.target_component
        # ATTITUDE ~ 10 Гц
        conn.mav.command_long_send(
            ts, tc, mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
            mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE, 100_000, 0, 0, 0, 0, 0,
        )
        # VFR_HUD ~ 5 Гц (для висоти)
        conn.mav.command_long_send(
            ts, tc, mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
            mavutil.mavlink.MAVLINK_MSG_ID_VFR_HUD, 200_000, 0, 0, 0, 0, 0,
        )
        threading.Thread(target=self._hud_receive_loop, daemon=True).start()


    def _stop_hud_telemetry(self):
        """Зупиняє потік телеметрії й скасовує запит на потокові повідомлення."""
        self._hud_state["active"] = False
        conn = self._flight_conn
        if conn is None:
            return
        from pymavlink import mavutil
        ts, tc = conn.target_system, conn.target_component
        # -1 = зупинити потік (повернути до дефолтної частоти)
        conn.mav.command_long_send(
            ts, tc, mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
            mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE, -1, 0, 0, 0, 0, 0,
        )
        conn.mav.command_long_send(
            ts, tc, mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
            mavutil.mavlink.MAVLINK_MSG_ID_VFR_HUD, -1, 0, 0, 0, 0, 0,
        )


    def _hud_receive_loop(self):
        """Фоновий потік: отримує ATTITUDE і VFR_HUD і кладе у self._hud_state.
        Завершується, коли hud_state["active"] стає False (діалог закрито)."""
        conn = self._flight_conn
        while self._hud_state.get("active") and conn is not None:
            msg = conn.recv_match(
                type=["ATTITUDE", "VFR_HUD"], blocking=True, timeout=0.3,
            )
            if msg is None:
                continue
            t = msg.get_type()
            if t == "ATTITUDE":
                self._hud_state["roll"]  = msg.roll
                self._hud_state["pitch"] = msg.pitch
            elif t == "VFR_HUD":
                self._hud_state["alt"] = msg.alt


    def _load_mission_from_mavlink(self):
        """Запрашивает місію з підключеного польотного контролера (MAVLink MISSION_REQUEST_LIST)."""
        self.status_var.set(i18n.t("status_downloading_mission"))
        self.connect_btn.configure(state="disabled")
        threading.Thread(target=self._mavlink_download_worker, daemon=True).start()


    def _mavlink_download_worker(self):
        try:
            from pymavlink import mavutil
            conn = self._flight_conn

            # переконуємось що знаємо цілі (target_system/component).
            # При підключенні ми вже робили wait_heartbeat -- але якщо з'єднання
            # старе і буфер переповнений, скидаємо накопичені повідомлення.
            while conn.recv_match(blocking=False) is not None:
                pass

            # відправляємо MISSION_REQUEST_LIST з повторами
            msg = None
            for attempt in range(3):
                conn.mav.mission_request_list_send(conn.target_system, conn.target_component)
                msg = conn.recv_match(type="MISSION_COUNT", blocking=True, timeout=8)
                if msg is not None:
                    break
            if msg is None:
                raise RuntimeError("Не отримано MISSION_COUNT від борту (3 спроби)")
            count = msg.count

            # завантажуємо всі точки
            import tempfile
            items = []
            for i in range(count):
                wp_msg = None
                for attempt in range(3):
                    # спочатку пробуємо INT (ArduPilot >= 3.x підтримує)
                    conn.mav.mission_request_int_send(conn.target_system, conn.target_component, i)
                    wp_msg = conn.recv_match(
                        type=["MISSION_ITEM_INT", "MISSION_ITEM"], blocking=True, timeout=5
                    )
                    if wp_msg is not None:
                        break
                if wp_msg is None:
                    raise RuntimeError(f"Не отримано точку {i}")
                items.append(wp_msg)

            conn.mav.mission_ack_send(conn.target_system, conn.target_component, 0)

            # зберігаємо у тимчасовий .waypoints файл для існуючого парсера
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".waypoints", delete=False, encoding="utf-8"
            )
            tmp.write("QGC WPL 110\n")
            for item in items:
                msg_type = item.get_type()
                if msg_type == "MISSION_ITEM_INT":
                    lat = item.x / 1e7
                    lon = item.y / 1e7
                    alt = item.z
                else:
                    lat = item.lat
                    lon = item.lon
                    alt = item.alt
                tmp.write(
                    "\t".join(str(v) for v in (
                        item.seq,
                        getattr(item, "current", 0),
                        item.frame,
                        item.command,
                        item.param1, item.param2, item.param3, item.param4,
                        lat, lon, alt,
                        getattr(item, "autocontinue", 1),
                    )) + "\n"
                )
            tmp_path = tmp.name
            tmp.close()

            self.after(0, lambda: self._on_mavlink_mission_ready(tmp_path))
        except Exception as e:
            error_text = str(e)
            self.after(0, lambda: self._on_mavlink_error(i18n.t("action_download"), error_text))


    def _on_mavlink_mission_ready(self, tmp_path: str):
        self.connect_btn.configure(state="normal")
        self.file_var.set("ArduPilot (MAVLink)")
        if self._build_analyzer(tmp_path):
            self._finish_load()
        try:
            os.unlink(tmp_path)
        except OSError:
            pass



    def _save_mission_to_mavlink(self):
        """Записує поточну місію на підключений польотний контролер (MAVLink MISSION_COUNT/ITEM)."""
        self.status_var.set(i18n.t("status_uploading_mission"))
        self.connect_btn.configure(state="disabled")
        threading.Thread(target=self._mavlink_upload_worker, daemon=True).start()


    def _mavlink_upload_worker(self):
        try:
            from pymavlink import mavutil
            conn = self._flight_conn
            wps = self.analyzer.all_wps
            count = len(wps)

            conn.mav.mission_count_send(conn.target_system, conn.target_component, count)

            for _ in range(count):
                req = conn.recv_match(
                    type=["MISSION_REQUEST", "MISSION_REQUEST_INT"],
                    blocking=True, timeout=10,
                )
                if req is None:
                    raise RuntimeError("Борт не запросив наступну точку (timeout)")
                i = req.seq
                wp = wps[i]
                use_int = (req.get_type() == "MISSION_REQUEST_INT")
                if use_int:
                    conn.mav.mission_item_int_send(
                        conn.target_system, conn.target_component,
                        wp.index, wp.frame, wp.command,
                        1 if wp.index == 0 else 0, 1,
                        wp.param1, wp.param2, wp.param3, wp.param4,
                        int(wp.lat * 1e7), int(wp.lon * 1e7), wp.alt,
                        mavutil.mavlink.MAV_MISSION_TYPE_MISSION,
                    )
                else:
                    conn.mav.mission_item_send(
                        conn.target_system, conn.target_component,
                        wp.index, wp.frame, wp.command,
                        1 if wp.index == 0 else 0, 1,
                        wp.param1, wp.param2, wp.param3, wp.param4,
                        wp.lat, wp.lon, wp.alt,
                    )

            ack = conn.recv_match(type="MISSION_ACK", blocking=True, timeout=10)
            if ack is None:
                raise RuntimeError("Не отримано підтвердження від борту (MISSION_ACK timeout)")
            if ack.type != 0:
                raise RuntimeError(f"Борт відхилив місію: код {ack.type}")

            self.after(0, self._on_mavlink_upload_done)
        except Exception as e:
            error_text = str(e)
            self.after(0, lambda: self._on_mavlink_error(i18n.t("action_write"), error_text))


    def _on_mavlink_upload_done(self):
        self.connect_btn.configure(state="normal")
        self.status_var.set(i18n.t("status_mission_uploaded"))
        messagebox.showinfo("MAVLink", i18n.t("msg_mission_uploaded_body"))


    def _on_mavlink_error(self, action: str, error: str):
        self.connect_btn.configure(state="normal")
        self.status_var.set("")
        messagebox.showerror("MAVLink", i18n.t("msg_action_failed_body", action=action, error=error))


    # ============================================================
    # "Команди" -- перевірка команд MAV_CMD САМЕ ЯК ЕЛЕМЕНТІВ МІСІЇ
    # (mission item) на РЕАЛЬНО підключеній платі. Портовано з
    # окремого standalone-скрипта mcp_probe_mission_items.py (проєкт
    # "MCP", той самий алгоритм, вже перевірений mock-бортом через
    # UDP loopback) -- тут адаптовано під self._flight_conn (вже
    # відкрите з'єднання застосунку) замість власного підключення.
    # ============================================================

    _MISSION_RESULT_NAMES = {
        0: "ACCEPTED", 1: "ERROR", 2: "UNSUPPORTED_FRAME", 3: "UNSUPPORTED",
        4: "NO_SPACE", 5: "INVALID", 6: "INVALID_PARAM1", 7: "INVALID_PARAM2",
        8: "INVALID_PARAM3", 9: "INVALID_PARAM4", 10: "INVALID_PARAM5_X",
        11: "INVALID_PARAM6_Y", 12: "INVALID_PARAM7", 13: "INVALID_SEQUENCE",
        14: "DENIED", 15: "OPERATION_CANCELLED",
    }
    # DO_JUMP_TAG посилається на ТЕГ (окрема команда MAV_CMD_JUMP_TAG
    # десь у місії з тим самим числом), не на seq -- у простій
    # 3-пунктній тестовій місії такого тегу немає, тому чесно
    # пропускаємо, а не видаємо хибний UNSUPPORTED (та сама причина,
    # що й у mcp_probe_mission_items.py).
    _MISSION_TEST_SKIP = {"MAV_CMD_DO_JUMP_TAG"}

    def _show_scripted_commands_scan(self):
        if self._flight_conn is None:
            return
        proceed = messagebox.askyesno(
            i18n.t("dlg_scripted_commands_title"),
            i18n.t("msg_scripted_commands_confirm_body"),
        )
        if not proceed:
            return
        self._ardu_commands_btn.configure(state="disabled")
        self.status_var.set(i18n.t("status_scanning_commands"))
        threading.Thread(target=self._scripted_commands_scan_worker, daemon=True).start()


    def _download_current_mission_items(self, conn, timeout: float = 10.0) -> list | None:
        from pymavlink import mavutil
        conn.mav.mission_request_list_send(conn.target_system, conn.target_component)
        msg = conn.recv_match(type="MISSION_COUNT", blocking=True, timeout=timeout)
        if msg is None:
            return None
        count = msg.count
        if count == 0:
            return []
        items = []
        for seq in range(count):
            conn.mav.mission_request_int_send(conn.target_system, conn.target_component, seq)
            item = conn.recv_match(type="MISSION_ITEM_INT", blocking=True, timeout=timeout)
            if item is None or item.seq != seq:
                return None
            items.append(item)
        conn.recv_match(type="MISSION_ACK", blocking=True, timeout=1.0)
        return items


    def _restore_mission_items(self, conn, items: list, timeout: float = 10.0) -> bool:
        if not items:
            conn.mav.mission_clear_all_send(conn.target_system, conn.target_component)
            return True
        conn.mav.mission_count_send(conn.target_system, conn.target_component, len(items), 0)
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = conn.recv_match(
                type=["MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ACK"],
                blocking=True, timeout=0.3,
            )
            if msg is None:
                continue
            if msg.get_type() == "MISSION_ACK":
                return msg.type == 0
            seq = msg.seq
            if seq >= len(items):
                continue
            orig = items[seq]
            conn.mav.mission_item_int_send(
                conn.target_system, conn.target_component, seq,
                orig.frame, orig.command, orig.current, orig.autocontinue,
                orig.param1, orig.param2, orig.param3, orig.param4,
                orig.x, orig.y, orig.z,
            )
        return False


    def _upload_test_mission_item(self, conn, cmd_id: int, timeout: float = 5.0) -> str:
        from pymavlink import mavutil
        conn.mav.mission_count_send(conn.target_system, conn.target_component, 3, 0)
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = conn.recv_match(
                type=["MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ACK"],
                blocking=True, timeout=0.3,
            )
            if msg is None:
                continue
            if msg.get_type() == "MISSION_ACK":
                return self._MISSION_RESULT_NAMES.get(msg.type, f"UNKNOWN({msg.type})")
            seq = msg.seq
            if seq == 0:
                conn.mav.mission_item_int_send(
                    conn.target_system, conn.target_component, 0,
                    mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
                    mavutil.mavlink.MAV_CMD_NAV_WAYPOINT, 1, 1,
                    0, 0, 0, 0, 0, 0, 0,
                )
            elif seq == 1:
                conn.mav.mission_item_int_send(
                    conn.target_system, conn.target_component, 1,
                    mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
                    mavutil.mavlink.MAV_CMD_NAV_WAYPOINT, 0, 1,
                    0, 0, 0, 0, 0, 0, 10,
                )
            elif seq == 2:
                if cmd_id == mavutil.mavlink.MAV_CMD_DO_JUMP:
                    param1, param2 = 1, 1
                else:
                    param1 = param2 = 0
                conn.mav.mission_item_int_send(
                    conn.target_system, conn.target_component, 2,
                    mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
                    cmd_id, 0, 1,
                    param1, param2, 0, 0, 0, 0, 10,
                )
            else:
                continue
        return "TIMEOUT"


    def _scripted_commands_scan_worker(self):
        conn = self._flight_conn
        report = None
        error = None

        def set_progress(text):
            self.after(0, lambda: self.status_var.set(text))

        try:
            from mcp_list_commands import get_all_commands
            all_commands = get_all_commands()
            to_test = [(cid, name) for cid, name, _desc in all_commands if name not in self._MISSION_TEST_SKIP]

            set_progress(i18n.t("status_scanning_backup_mission"))
            original_mission = self._download_current_mission_items(conn)
            if original_mission is None:
                error = i18n.t("msg_scripted_commands_backup_failed")
            else:
                results = []
                try:
                    for i, (cmd_id, name) in enumerate(to_test, 1):
                        set_progress(i18n.t(
                            "status_scanning_command_fmt", name=name, done=i, total=len(to_test),
                        ))
                        result = self._upload_test_mission_item(conn, cmd_id)
                        results.append((cmd_id, name, result))
                finally:
                    set_progress(i18n.t("status_scanning_restore_mission"))
                    self._restore_mission_items(conn, original_mission)

                report = self._format_scripted_commands_report(results)
        except Exception as e:
            error = str(e)

        self.after(0, lambda: self._on_scripted_commands_scan_ready(report, error))


    def _format_scripted_commands_report(self, results: list) -> str:
        lines = [i18n.t("info_section_mission_commands"), "-" * 44, ""]
        by_result: dict[str, int] = {}
        for _cid, _name, result in results:
            by_result[result] = by_result.get(result, 0) + 1
        for result, count in sorted(by_result.items(), key=lambda x: -x[1]):
            lines.append(f"  {result:<25} {count}")

        accepted = [r for r in results if r[2] == "ACCEPTED"]
        lines.append("")
        lines.append(i18n.t("info_mission_commands_accepted_fmt", count=len(accepted)))
        lines.append("-" * 44)
        for cmd_id, name, _ in accepted:
            lines.append(f"  {cmd_id:6}  {name}")
        return "\n".join(lines)


    def _on_scripted_commands_scan_ready(self, report: str | None, error: str | None):
        self.status_var.set("")
        if error or report is None:
            self._ardu_commands_btn.configure(state="normal")
            messagebox.showerror(i18n.t("msg_update_title"), i18n.t("info_fetch_error_fmt", error=error or "?"))
            return
        self._show_scripted_commands_dialog(report)


    def _show_scripted_commands_dialog(self, report_text: str):
        dlg = tk.Toplevel(self)
        dlg.title(i18n.t("dlg_scripted_commands_title"))
        dlg.geometry("560x560")
        dlg.transient(self)

        def _on_close():
            dlg.destroy()
            self._ardu_commands_btn._is_toggle_active = False
            self._refresh_toggle_action_button_colors(self._toggle_buttons_registry)
            self._ardu_commands_btn.configure(state="normal")

        text = scrolledtext.ScrolledText(dlg, wrap="word", font=("Consolas", 9))
        text.pack(fill="both", expand=True, padx=8, pady=8)
        text.insert("end", report_text)
        theme.make_text_readonly(text)

        dlg.protocol("WM_DELETE_WINDOW", _on_close)



    # ============================================================
    # "Параметри" -- читання критичних параметрів місії з борта
    # ============================================================

    # Параметри, що реально впливають на безпеку виконання місії.
    # Запитуємо кожен ОКРЕМО через PARAM_REQUEST_READ (не весь список
    # з ~1000 параметрів) -- швидко й без зайвого трафіку.
    # Формат: (назва_параметру, опис_uk, опис_en)
    # Набір параметрів ЗАЛЕЖИТЬ ВІД ТИПУ АПАРАТУ -- для крила, коптера й
    # ровера критичні РІЗНІ речі. Ключ -- набір значень MAV_TYPE з тієї
    # самої sysid_state, що вже використовується для відображення типу в
    # Info-звіті. Методи _key_params_for_vehicle() повертає потрібний список.

    _KEY_PARAMS_PLANE = [
        # --- Швидкості (ArduPlane 4.5+: нові назви й одиниці) ---
        ("AIRSPEED_CRUISE", "Крейсерська швидкість (м/с)",    "Cruise airspeed (m/s)"),
        ("AIRSPEED_MIN",    "Мін. швидкість (м/с)",           "Min airspeed (m/s)"),
        ("AIRSPEED_MAX",    "Макс. швидкість (м/с)",          "Max airspeed (m/s)"),
        ("ARSPD_TYPE",      "Тип датчика швидкості",          "Airspeed sensor type"),
        # --- Кути (впливають на маневри в AUTO/GUIDED, зокрема Змійку) ---
        # ROLL_LIMIT_DEG обмежує крен у FBWA/AUTO/RTL -- якщо замалий,
        # Змійка з великою амплітудою або коротким періодом не зможе
        # відслідкувати бокові цілі (самолот просто не долетить до них)
        ("ROLL_LIMIT_DEG",  "Макс. крен (°)",                 "Max bank angle (deg)"),
        ("PTCH_LIM_MAX_DEG","Макс. тангаж вгору (°)",         "Max pitch up (deg)"),
        ("PTCH_LIM_MIN_DEG","Макс. тангаж вниз (°)",          "Max pitch down (deg)"),
        # --- Висоти ---
        ("RTL_ALTITUDE",    "Висота RTL (м)",                 "RTL altitude (m)"),
        ("TKOFF_ALT",       "Висота набору після зльоту (м)", "Takeoff altitude (m)"),
        ("FENCE_ALT_MAX",   "Макс. висота забору (м)",        "Fence max altitude (m)"),
        # --- Failsafe ---
        ("FS_SHORT_ACTN",   "Failsafe короткий (дія)",        "Short failsafe action"),
        ("FS_LONG_ACTN",    "Failsafe довгий (дія)",          "Long failsafe action"),
        ("FS_GCS_ENABL",    "Failsafe GCS увімкнено",         "GCS failsafe enabled"),
        # --- Батарея (для крила менш критична, але знати варто) ---
        ("BATT_LOW_VOLT",   "Низький заряд (В)",              "Low battery voltage (V)"),
        ("BATT_CRT_VOLT",   "Критичний заряд (В)",            "Critical battery voltage (V)"),
        ("BATT_FS_LOW_ACT", "Дія при низькому заряді",        "Low battery action"),
        # --- Забор ---
        ("FENCE_ENABLE",    "Геозабор увімкнено",             "Geofence enabled"),
        ("FENCE_TYPE",      "Тип забору",                     "Fence type"),
        ("FENCE_ACTION",    "Дія забору",                     "Fence action"),
        # --- Посадка ---
        ("LAND_FLARE_ALT",  "Висота вирівнювання (м)",        "Flare altitude (m)"),
        ("RTL_AUTOLAND",    "Авто-посадка після RTL",         "Auto-land after RTL"),
        # --- Ідентифікація ---
        ("SYSID_THISMAV",   "MAVLink System ID борта",        "MAVLink System ID"),
        ("BRD_SERIAL_NUM",  "Серійний номер плати",           "Board serial number"),
    ]

    _KEY_PARAMS_COPTER = [
        # --- Батарея -- КРИТИЧНО для коптера (сів заряд = падіння) ---
        ("BATT_LOW_VOLT",   "Низький заряд (В)",              "Low battery voltage (V)"),
        ("BATT_CRT_VOLT",   "Критичний заряд (В)",            "Critical battery voltage (V)"),
        ("BATT_LOW_MAH",    "Низький заряд (мАг)",            "Low battery (mAh)"),
        ("BATT_FS_LOW_ACT", "Дія при низькому заряді",        "Low battery action"),
        ("BATT_FS_CRT_ACT", "Дія при критичному заряді",      "Critical battery action"),
        # --- Висоти ---
        ("RTL_ALT",         "Висота RTL (см)",                "RTL altitude (cm)"),
        ("FENCE_ALT_MAX",   "Макс. висота забору (м)",        "Fence max altitude (m)"),
        # --- Швидкості ---
        ("WPNAV_SPEED",     "Швидкість у місії (см/с)",       "Waypoint speed (cm/s)"),
        ("WPNAV_SPEED_UP",  "Швидкість підйому (см/с)",       "Climb speed (cm/s)"),
        ("WPNAV_SPEED_DN",  "Швидкість спуску (см/с)",        "Descent speed (cm/s)"),
        # --- Failsafe ---
        ("FS_THR_ENABLE",   "Failsafe RC увімкнено",          "RC failsafe enabled"),
        ("FS_THR_VALUE",     "Поріг RC failsafe (мкс)",       "RC failsafe threshold (us)"),
        ("FS_GCS_ENABLE",   "Failsafe GCS увімкнено",         "GCS failsafe enabled"),
        # --- Забор ---
        ("FENCE_ENABLE",    "Геозабор увімкнено",             "Geofence enabled"),
        ("FENCE_TYPE",      "Тип забору",                     "Fence type"),
        ("FENCE_ACTION",    "Дія забору",                     "Fence action"),
        # --- Ідентифікація ---
        ("SYSID_THISMAV",   "MAVLink System ID борта",        "MAVLink System ID"),
        ("BRD_SERIAL_NUM",  "Серійний номер плати",           "Board serial number"),
    ]

    _KEY_PARAMS_ROVER = [
        # --- Швидкості ---
        ("CRUISE_SPEED",    "Крейсерська швидкість (м/с)",    "Cruise speed (m/s)"),
        ("CRUISE_THROTTLE", "Крейсерський газ (%)",           "Cruise throttle (%)"),
        ("WP_SPEED",        "Швидкість до точки (м/с)",       "Waypoint speed (m/s)"),
        # --- Failsafe ---
        ("FS_THR_ENABLE",   "Failsafe RC увімкнено",          "RC failsafe enabled"),
        ("FS_GCS_ENABLE",   "Failsafe GCS увімкнено",         "GCS failsafe enabled"),
        # --- Батарея ---
        ("BATT_LOW_VOLT",   "Низький заряд (В)",              "Low battery voltage (V)"),
        ("BATT_CRT_VOLT",   "Критичний заряд (В)",            "Critical battery voltage (V)"),
        ("BATT_FS_LOW_ACT", "Дія при низькому заряді",        "Low battery action"),
        # --- Забор ---
        ("FENCE_ENABLE",    "Геозабор увімкнено",             "Geofence enabled"),
        ("FENCE_TYPE",      "Тип забору",                     "Fence type"),
        ("FENCE_ACTION",    "Дія забору",                     "Fence action"),
        # --- Ідентифікація ---
        ("SYSID_THISMAV",   "MAVLink System ID борта",        "MAVLink System ID"),
        ("BRD_SERIAL_NUM",  "Серійний номер плати",           "Board serial number"),
    ]

    # MAV_TYPE значення, що відповідають кожному типу апарату
    _PLANE_TYPES = frozenset([1, 19, 20, 21, 22, 23, 24, 25])   # Fixed-wing + VTOL
    _COPTER_TYPES = frozenset([2, 13, 14, 15, 16, 29, 35])       # Quad/Hexa/Octo/Tri/etc
    _ROVER_TYPES = frozenset([10, 11])                            # Rover/Boat

    def _key_params_for_vehicle(self) -> tuple[list, str]:
        """Повертає (список_параметрів, назва_типу) залежно від MAV_TYPE
        підключеного зараз апарата. Той самий sysid_state, що й у Info."""
        from pymavlink import mavutil
        conn = self._flight_conn
        sysid = getattr(conn, "target_system", None)
        state = conn.sysid_state.get(sysid) if sysid else None
        mav_type = state.mav_type if state else None

        if mav_type in self._PLANE_TYPES:
            return self._KEY_PARAMS_PLANE, i18n.t("vehicle_type_plane")
        if mav_type in self._COPTER_TYPES:
            return self._KEY_PARAMS_COPTER, i18n.t("vehicle_type_copter")
        if mav_type in self._ROVER_TYPES:
            return self._KEY_PARAMS_ROVER, i18n.t("vehicle_type_rover")
        # невідомий тип -- показуємо загальний мінімум (Plane як базовий,
        # бо Mission Analyzer орієнтований на фіксоване крило)
        return self._KEY_PARAMS_PLANE, i18n.t("vehicle_type_unknown")

    def _show_key_params(self):
        if self._flight_conn is None:
            return
        # визначаємо тип апарату В ГОЛОВНОМУ ПОТОЦІ, де sysid_state
        # гарантовано актуальний -- а не в фоновому, де можливі
        # проблеми з потоковою безпекою або порожній target_system
        params, vehicle_label = self._key_params_for_vehicle()
        self._ardu_params_btn.configure(state="disabled")
        self.status_var.set(i18n.t("status_reading_params"))
        threading.Thread(
            target=self._key_params_worker,
            args=(params, vehicle_label),
            daemon=True,
        ).start()


    def _request_one_param(self, conn, name: str, timeout: float = 2.0) -> float | None:
        """Запитує ОДИН параметр за ім'ям через PARAM_REQUEST_READ.
        Повертає float-значення або None якщо не відповів."""
        conn.mav.param_request_read_send(
            conn.target_system, conn.target_component,
            name.encode("ascii"), -1,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = conn.recv_match(type="PARAM_VALUE", blocking=True, timeout=0.3)
            if msg is None:
                continue
            if msg.param_id.rstrip("\x00") == name:
                return float(msg.param_value)
        return None


    def _key_params_worker(self, params: list, vehicle_label: str):
        conn = self._flight_conn
        results = []
        error = None

        def set_progress(text):
            self.after(0, lambda: self.status_var.set(text))

        try:
            total = len(params)
            for i, (name, desc_uk, desc_en) in enumerate(params, 1):
                set_progress(i18n.t(
                    "status_reading_param_fmt", name=name, done=i, total=total,
                ))
                value = self._request_one_param(conn, name)
                results.append((name, desc_uk, desc_en, value))
        except Exception as e:
            error = str(e)

        self.after(0, lambda: self._on_key_params_ready(results, error, vehicle_label))


    def _on_key_params_ready(self, results, error, vehicle_label=""):
        self.status_var.set("")
        if error:
            self._ardu_params_btn.configure(state="normal")
            messagebox.showerror("MAVLink", error)
            return
        self._show_key_params_dialog(results, vehicle_label)


    def _show_key_params_dialog(self, results, vehicle_label=""):
        dlg = tk.Toplevel(self)
        title = i18n.t("dlg_params_title")
        if vehicle_label:
            title += f" — {vehicle_label}"
        dlg.title(title)
        dlg.geometry("520x560")
        dlg.transient(self)

        def _on_close():
            dlg.destroy()
            self._ardu_params_btn._is_toggle_active = False
            self._refresh_toggle_action_button_colors(self._toggle_buttons_registry)
            self._ardu_params_btn.configure(state="normal")

        import theme as _theme
        _pal = _theme.PALETTE_DARK if self._is_dark_theme() else _theme.PALETTE_LIGHT
        dlg.configure(bg=_pal["bg"])

        text = scrolledtext.ScrolledText(dlg, wrap="none", font=("Consolas", 9))
        text.pack(fill="both", expand=True, padx=8, pady=8)

        desc_key = "uk" if i18n.get_lang() == "uk" else "en"
        header = title
        lines = [header, "-" * 44, ""]
        for name, desc_uk, desc_en, value in results:
            desc = desc_uk if desc_key == "uk" else desc_en
            val_str = f"{value:.6g}" if value is not None else i18n.t("param_no_response")
            lines.append(f"{name:<20} {val_str:<12} {desc}")
        text.insert("end", "\n".join(lines))
        theme.make_text_readonly(text)

        dlg.protocol("WM_DELETE_WINDOW", _on_close)
