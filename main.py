"""
main.py
程序入口。负责：
- 平台检查（非 Windows 直接退出并提示）
- 初始化日志
- 启动 tkinter 主循环
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _setup_logging():
    try:
        from config import get_app_dir
        log_dir = get_app_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        fh = RotatingFileHandler(log_dir / "app.log", maxBytes=1_000_000,
                                  backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(fh)
    except Exception:
        pass


def _check_platform():
    if sys.platform != "win32":
        sys.stderr.write(
            "[错误] 本程序依赖 Windows UI Automation，只能在 Windows 上运行。\n"
            f"当前系统：{sys.platform}\n"
        )
        sys.exit(1)


def _show_error_dialog(title: str, message: str):
    """在无控制台模式下用 messagebox 显示错误。"""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        # 连 tkinter 都起不来，只能写文件
        try:
            from config import get_app_dir
            err_file = get_app_dir() / "crash.log"
            with open(err_file, "w", encoding="utf-8") as f:
                f.write(f"{title}\n\n{message}")
        except Exception:
            pass


def main():
    _check_platform()
    _setup_logging()

    # 在 Windows 上，tkinter 需要 DPI 感知
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    try:
        import tkinter as tk
        from gui import TranslatorApp

        root = tk.Tk()
        TranslatorApp(root)
        root.mainloop()
    except Exception as e:
        tb = traceback.format_exc()
        logging.error("启动失败: %s", tb)
        _show_error_dialog("程序启动失败", f"{e}\n\n{tb}")


if __name__ == "__main__":
    main()
