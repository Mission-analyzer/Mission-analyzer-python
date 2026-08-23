"""
help_page.py — сторінка "Довідка": текст довідки, changelog,
перевірка та застосування оновлень.

HelpPageMixin підмішується до класу App (app.py).
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import i18n
import meta
import theme
import updater


class HelpPageMixin:
    """Сторінка "Довідка": текст, changelog, оновлення."""

    def _build_help_page(self, content, pad):
        # === страница "Довідка" ===
        page_help = ttk.Frame(content)
        page_help.grid(row=0, column=0, sticky="nsew")
        self.pages["help"] = page_help

        help_notebook = ttk.Notebook(page_help)
        help_notebook.pack(fill="both", expand=True, padx=10, pady=10)

        c = self.palette
        sc = theme.slider_colors(self._help_is_dark_theme())

        help_tab = ttk.Frame(help_notebook)
        help_notebook.add(help_tab, text=i18n.t("tab_help"))
        self.help_text_widget = scrolledtext.ScrolledText(
            help_tab, wrap="word", font=("Segoe UI", 10),
            bg=c["panel"], fg=c["text"], insertbackground=c["text"],
        )
        self.help_text_widget.pack(fill="both", expand=True)
        self.help_text_widget.insert("end", i18n.t("help_text_body"))
        self.help_text_widget.config(state="disabled")

        changelog_tab = ttk.Frame(help_notebook)
        help_notebook.add(changelog_tab, text=i18n.t("tab_changelog"))

        update_row = ttk.Frame(changelog_tab)
        update_row.pack(fill="x", pady=(0, 6))
        self._reg_i18n(
            ttk.Button(update_row, command=self._check_for_updates), "text", "btn_check_updates",
        ).pack(side="left")
        self.update_status_var = tk.StringVar(value="")
        self._update_status_label = ttk.Label(
            update_row, textvariable=self.update_status_var, foreground=theme.chart_colors(self._help_is_dark_theme())["muted"],
        )
        self._update_status_label.pack(side="left", padx=(10, 0))

        self.changelog_text_widget = scrolledtext.ScrolledText(
            changelog_tab, wrap="word", font=("Segoe UI", 10),
            bg=c["panel"], fg=c["text"], insertbackground=c["text"],
        )
        self.changelog_text_widget.pack(fill="both", expand=True)
        self.changelog_text_widget.insert("end", f"{i18n.t('app_title')} — {i18n.t('label_version')} {meta.VERSION}")
        self.changelog_text_widget.insert("end", meta.format_changelog(i18n.get_lang()))
        self.changelog_text_widget.config(state="disabled")

        # ScrolledText -- складений віджет (Text + власний Scrollbar
        # усередині, .vbar). Той власний Scrollbar -- plain
        # tkinter.Scrollbar (не ttk), тому кольори з ЄДИНОГО джерела
        # (theme.slider_colors) застосовуються так само, як і до всіх
        # інших повзунків/смуг прокрутки програми.
        for widget in (self.help_text_widget, self.changelog_text_widget):
            widget.vbar.configure(
                bg=sc["bg"], troughcolor=sc["trough"], activebackground=sc["active"],
                highlightthickness=0, bd=0,
            )

        # Notebook.tab(text=...) -- інший API, ніж .configure(text=...),
        # тому окремий callback, а не self._reg_i18n. Текст довідки й
        # changelog теж перебудовуємо цілком (звичайний текстовий блок,
        # не окремі віджети з підписами) -- обидва дешеві, без мережі.
        def _retranslate_help_page():
            help_notebook.tab(help_tab, text=i18n.t("tab_help"))
            help_notebook.tab(changelog_tab, text=i18n.t("tab_changelog"))

            self.help_text_widget.config(state="normal")
            self.help_text_widget.delete("1.0", "end")
            self.help_text_widget.insert("end", i18n.t("help_text_body"))
            self.help_text_widget.config(state="disabled")

            self.changelog_text_widget.config(state="normal")
            self.changelog_text_widget.delete("1.0", "end")
            self.changelog_text_widget.insert(
                "end", f"{i18n.t('app_title')} — {i18n.t('label_version')} {meta.VERSION}",
            )
            self.changelog_text_widget.insert("end", meta.format_changelog(i18n.get_lang()))
            self.changelog_text_widget.config(state="disabled")

        self._retranslate_callbacks.append(_retranslate_help_page)


    def _help_is_dark_theme(self) -> bool:
        theme_var = getattr(self, "app_theme_var", None)
        return theme_var.get() == "dark" if theme_var is not None else True


    def _apply_help_theme(self):
        """Перефарбовує ВЖЕ ПОБУДОВАНУ сторінку "Довідка" під поточну
        тему -- викликається з app.py: apply_app_theme(). ScrolledText
        (Text) не підхоплює зміну ttk.Style автоматично (bg= задається
        один раз при створенні, це не ttk-стиль) -- перефарбовуємо явно,
        разом із власним Scrollbar усередині кожного ScrolledText."""
        if not hasattr(self, "help_text_widget"):
            return  # сторінку ще не побудовано
        c = self.palette
        sc = theme.slider_colors(self._help_is_dark_theme())

        for widget in (self.help_text_widget, self.changelog_text_widget):
            if widget.winfo_exists():
                widget.configure(bg=c["panel"], fg=c["text"], insertbackground=c["text"])
                widget.vbar.configure(bg=sc["bg"], troughcolor=sc["trough"], activebackground=sc["active"])

        if hasattr(self, "_update_status_label") and self._update_status_label.winfo_exists():
            self._update_status_label.configure(foreground=theme.chart_colors(self._help_is_dark_theme())["muted"])


    def _check_for_updates(self, silent: bool = False):
        """Перевіряє GitHub Releases у фоновому потоці. silent=True --
        для тихої перевірки при старті (без повідомлень про помилку/
        відсутність оновлень, лише якщо реально є новіша версія)."""
        if hasattr(self, "update_status_var"):
            self.update_status_var.set(i18n.t("status_checking_updates"))

        def worker():
            try:
                release = updater.check_latest_release()
                has_update = updater.is_newer(release["tag"], meta.VERSION)
            except updater.UpdateError as e:
                error_text = str(e)
                self.after(0, lambda: self._on_update_check_done(None, error_text, silent))
                return
            self.after(0, lambda: self._on_update_check_done(release if has_update else False, None, silent))

        threading.Thread(target=worker, daemon=True).start()


    def _on_update_check_done(self, release, error: str | None, silent: bool):
        if error:
            if hasattr(self, "update_status_var"):
                self.update_status_var.set("")
            if not silent:
                messagebox.showerror(i18n.t("msg_update_title"), i18n.t("msg_update_check_failed_body", error=error))
            return

        if release is False:
            if hasattr(self, "update_status_var"):
                self.update_status_var.set(i18n.t("status_up_to_date_fmt", version=meta.VERSION))
            elif not silent:
                messagebox.showinfo(i18n.t("msg_update_title"), i18n.t("msg_latest_version_body", version=meta.VERSION))
            return

        if hasattr(self, "update_status_var"):
            self.update_status_var.set(i18n.t("status_update_available_fmt", tag=release["tag"]))

        body = (release["body"] or "").strip()
        body_preview = (body[:400] + "…") if len(body) > 400 else body
        msg = i18n.t("msg_update_available_body_fmt", tag=release["tag"], current=meta.VERSION)
        if body_preview:
            msg += f"\n\n{i18n.t('msg_update_whats_new')}\n{body_preview}"
        msg += f"\n\n{i18n.t('msg_update_confirm_install')}"

        if messagebox.askyesno(i18n.t("msg_update_available_title"), msg):
            self._apply_update(release)


    def _apply_update(self, release: dict):
        if hasattr(self, "update_status_var"):
            self.update_status_var.set(i18n.t("status_downloading_update_fmt", tag=release["tag"]))

        def worker():
            try:
                app_dir = os.path.dirname(os.path.abspath(__file__))
                backup_dir = updater.download_and_apply_update(release["zip_url"], app_dir)
            except Exception as e:
                error_text = str(e)
                self.after(0, lambda: self._on_update_apply_done(None, error_text))
                return
            self.after(0, lambda: self._on_update_apply_done(backup_dir, None))

        threading.Thread(target=worker, daemon=True).start()


    def _on_update_apply_done(self, backup_dir, error: str | None):
        if error:
            if hasattr(self, "update_status_var"):
                self.update_status_var.set("")
            messagebox.showerror(i18n.t("msg_update_title"), i18n.t("msg_update_install_failed_body", error=error))
            return

        if hasattr(self, "update_status_var"):
            self.update_status_var.set(i18n.t("status_update_installed"))
        messagebox.showinfo(
            i18n.t("msg_update_title"),
            i18n.t("msg_update_installed_body_fmt", backup_dir=backup_dir),
        )


