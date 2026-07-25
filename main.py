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
        from config import get_app_dir, get_settings
        settings = get_settings()

        # 日志文件路径
        if settings.gui.log_file_path:
            log_path = Path(settings.gui.log_file_path)
            log_dir = log_path.parent
        else:
            log_dir = get_app_dir() / "logs"
            log_path = log_dir / "app.log"
        log_dir.mkdir(parents=True, exist_ok=True)

        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # 文件日志
        fh = RotatingFileHandler(log_path, maxBytes=2_000_000,
                                backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)

        # 级别
        level_str = (settings.gui.log_level or "DEBUG").upper()
        level = getattr(logging, level_str, logging.DEBUG)

        root = logging.getLogger()
        root.setLevel(level)
        root.addHandler(fh)

        log = logging.getLogger(__name__)
        log.info("日志初始化完成，日志文件: %s", log_path)
    except Exception as e:
        import traceback
        traceback.print_exc()


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
