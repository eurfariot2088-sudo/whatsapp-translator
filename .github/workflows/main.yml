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

from config import get_app_dir


def _setup_logging():
    log_dir = get_app_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = RotatingFileHandler(log_dir / "app.log", maxBytes=1_000_000,
                              backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(sh)


def _check_platform():
    if sys.platform != "win32":
        sys.stderr.write(
            "[错误] 本程序依赖 Windows UI Automation，只能在 Windows 上运行。\n"
            f"当前系统：{sys.platform}\n"
        )
        sys.exit(1)


def _excepthook(exc_type, exc_value, exc_tb):
    logging.error("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))
    # 仍打印到控制台方便用户看到
    traceback.print_exception(exc_type, exc_value, exc_tb)


def main():
    _check_platform()
    _setup_logging()
    sys.excepthook = _excepthook

    # 在 Windows 上，tkinter 需要 DPI 感知
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    import tkinter as tk
    from gui import TranslatorApp

    root = tk.Tk()
    TranslatorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
