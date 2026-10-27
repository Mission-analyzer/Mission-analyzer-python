#!/usr/bin/env python3
"""
Mission Analyzer — точка входа. Запуск:
    python main.py

Структура проекта: см. ARCHITECTURE.md рядом с этим файлом.
"""

import os
import sys
import tkinter as tk

from meta import VERSION, AUTHOR

SPLASH_DURATION_MS = 1300


def _find_logo() -> str | None:
    # той самий bundled-asset-шлях, що й у App._find_asset (app.py) --
    # у зібраному .exe це sys._MEIPASS (тека _internal), інакше --
    # тека поруч з цим файлом. Не sys.executable: іконка/лого -- це
    # ресурс, вкладений PyInstaller-ом у збірку, а не user-дані.
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    for name in ("icon.png", "logo.png"):
        path = os.path.join(base, name)
        if os.path.isfile(path):
            return path
    return None


def _load_logo_image(path: str, target_h: int = 170):
    """Уменьшает лого для сплэша. С Pillow — плавно, без него — грубее (subsample)."""
    try:
        from PIL import Image, ImageTk
        img = Image.open(path)
        ratio = target_h / img.height
        img = img.resize((max(1, int(img.width * ratio)), target_h), Image.LANCZOS)
        return ImageTk.PhotoImage(img)
    except ImportError:
        pass
    except Exception:
        return None

    try:
        img = tk.PhotoImage(file=path)
        h = img.height()
        if h > target_h:
            factor = max(1, h // target_h)
            img = img.subsample(factor, factor)
        return img
    except tk.TclError:
        return None


def show_splash(duration_ms: int = SPLASH_DURATION_MS):
    """Тёмный сплэш-экран с логотипом и версией — показывается на старте, как в Mission Planner."""
    splash = tk.Tk()
    splash.overrideredirect(True)  # без рамки и заголовка окна
    splash.configure(bg="black")
    try:
        splash.attributes("-topmost", True)
    except tk.TclError:
        pass

    width, height = 460, 330
    sw, sh = splash.winfo_screenwidth(), splash.winfo_screenheight()
    x, y = (sw - width) // 2, (sh - height) // 2
    splash.geometry(f"{width}x{height}+{x}+{y}")

    logo_path = _find_logo()
    if logo_path:
        img = _load_logo_image(logo_path, target_h=170)
        if img is not None:
            lbl = tk.Label(splash, image=img, bg="black")
            lbl.image = img  # держим ссылку, иначе GC съест картинку
            lbl.pack(pady=(28, 8))

    tk.Label(
        splash, text="Mission Analyzer", fg="white", bg="black",
        font=("Segoe UI", 17, "bold"),
    ).pack()
    tk.Label(
        splash, text=f"Version: {VERSION}", fg="#4CAF50", bg="black",
        font=("Segoe UI", 10, "bold"),
    ).pack(pady=(4, 0))
    tk.Label(
        splash, text=f"by {AUTHOR}", fg="#888888", bg="black",
        font=("Segoe UI", 9),
    ).pack(pady=(2, 0))

    splash.update()
    splash.after(duration_ms, splash.destroy)
    splash.mainloop()


def _set_windows_app_id():
    """Свій AppUserModelID: Windows групує вікно в таскбарі як окрему
    програму з НАШОЮ іконкою, а не як python.exe/pythonw.exe."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MissionAnalyzer.App")
    except Exception:
        pass


def main():
    _set_windows_app_id()
    show_splash()
    from app import main as app_main
    app_main()


if __name__ == "__main__":
    main()
