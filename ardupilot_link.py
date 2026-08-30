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
                            from mcp_script_scanner import parse_mcp_commands_from_files
                            scripted_commands = parse_mcp_commands_from_files(lua_texts)
                except Exception as e:
                    ftp_error = str(e)
            elif ver is not None:
                ftp_error = i18n.t("info_ftp_not_supported")

            self._scripted_commands = scripted_commands
            report = self._format_flight_info(
                conn, ver, sys_status, storage, files, ftp_error, scripted_commands, scanned_lua_files,
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
        self, conn, ver, sys_status, storage, files, ftp_error,
        scripted_commands=None, scanned_lua_files=None,
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
                lines.append(i18n.t("info_git_hash_fmt", hash=fc_hash.hex()))
        lines.append("")

        # --- SYS_STATUS: датчики ---
        lines.append(i18n.t("info_section_sensors"))
        lines.append("-" * 44)
        if sys_status is None:
            lines.append(i18n.t("info_no_response"))
        else:
            sensors = self._decode_sensor_bits(
                sys_status.onboard_control_sensors_present,
                sys_status.onboard_control_sensors_enabled,
                sys_status.onboard_control_sensors_health,
            )
            if not sensors:
                lines.append(i18n.t("info_no_sensors"))
            for name, present, enabled, healthy in sensors:
                mark = "OK" if (enabled and healthy) else ("--" if not enabled else i18n.t("info_sensor_unhealthy"))
                lines.append(f"  {name:<28} {mark}")
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
        if scanned_lua_files:
            by_file: dict[str, list[dict]] = {}
            for cmd in scripted_commands:
                by_file.setdefault(cmd["source_file"], []).append(cmd)

            for filename in scanned_lua_files:
                lines.append(i18n.t("info_script_file_fmt", name=filename))
                cmds = by_file.get(filename, [])
                if not cmds:
                    lines.append("  " + i18n.t("info_script_no_markup"))
                    continue
                for cmd in cmds:
                    lines.append("  " + i18n.t(
                        "info_script_cmd_embed_fmt", cmd=cmd["cmd"], name=cmd["name"],
                    ))
                    for p in cmd["params"]:
                        lines.append("    " + i18n.t(
                            "info_script_param_fmt",
                            slot=p["slot"], label=p["label"], unit=p["unit"],
                        ))
                lines.append("")
        else:
            lines.append(i18n.t("info_no_scripted_commands"))

        return "\n".join(lines)


    def _on_flight_info_ready(self, report: str | None, error: str | None):
        self._ardu_info_btn.configure(state="normal")
        self.status_var.set("")
        if error or report is None:
            messagebox.showerror(i18n.t("msg_update_title"), i18n.t("info_fetch_error_fmt", error=error or "?"))
            return
        self._show_flight_info_dialog(report)


    def _show_flight_info_dialog(self, report_text: str):
        dlg = tk.Toplevel(self)
        dlg.title(i18n.t("dlg_flight_info_title"))
        dlg.geometry("560x560")
        dlg.transient(self)

        text = scrolledtext.ScrolledText(dlg, wrap="word", font=("Consolas", 9))
        text.pack(fill="both", expand=True, padx=8, pady=8)
        text.insert("end", report_text)
        theme.make_text_readonly(text)

        dlg.grab_set()


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
        self._ardu_commands_btn.configure(state="normal")
        self.status_var.set("")
        if error or report is None:
            messagebox.showerror(i18n.t("msg_update_title"), i18n.t("info_fetch_error_fmt", error=error or "?"))
            return
        self._show_scripted_commands_dialog(report)


    def _show_scripted_commands_dialog(self, report_text: str):
        dlg = tk.Toplevel(self)
        dlg.title(i18n.t("dlg_scripted_commands_title"))
        dlg.geometry("560x560")
        dlg.transient(self)

        text = scrolledtext.ScrolledText(dlg, wrap="word", font=("Consolas", 9))
        text.pack(fill="both", expand=True, padx=8, pady=8)
        text.insert("end", report_text)
        theme.make_text_readonly(text)

        dlg.grab_set()


