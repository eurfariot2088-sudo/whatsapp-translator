"""
screenshot.py
截图翻译模块。
- 全局热键 Ctrl+Alt+S 触发
- 用 mss 快速截取全屏
- tkinter 透明覆盖窗口让用户框选区域
- pytesseract OCR 识别文字
- 翻译识别结果并弹窗显示

注意：需要安装 Tesseract-OCR 引擎。
下载地址：https://github.com/UB-Mannheim/tesseract/wiki
"""

from __future__ import annotations

import io
import logging
import os
import sys
import threading
import tkinter as tk
from typing import Optional

from PIL import Image, ImageTk

log = logging.getLogger(__name__)

# Tesseract 路径自动检测
_TESSERACT_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    r"D:\Tesseract-OCR\tesseract.exe",
]


def _find_tesseract() -> Optional[str]:
    """查找 tesseract.exe 路径。"""
    # 先查环境变量
    for p in os.environ.get("PATH", "").split(os.pathsep):
        exe = os.path.join(p, "tesseract.exe")
        if os.path.isfile(exe):
            return exe
    # 再查常见路径
    for p in _TESSERACT_PATHS:
        if os.path.isfile(p):
            return p
    return None


def ocr_image(image: Image.Image, lang: str = "eng+chi_sim") -> str:
    """对 PIL Image 进行 OCR，返回识别文本。"""
    import pytesseract  # type: ignore
    tesseract_path = _find_tesseract()
    if tesseract_path:
        pytesseract.pytesseract.tesseract_cmd = tesseract_path
    try:
        text = pytesseract.image_to_string(image, lang=lang)
    except Exception as e:
        raise RuntimeError(f"OCR 识别失败: {e}\n请确保已安装 Tesseract-OCR 引擎。")
    return text.strip()


def capture_screen() -> Image.Image:
    """用 mss 截取全屏，返回 PIL Image。"""
    import mss  # type: ignore
    with mss.mss() as sct:
        monitor = sct.monitors[1]  # 主显示器
        raw = sct.grab(monitor)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    return img


class RegionSelector:
    """透明覆盖窗口，让用户用鼠标框选截图区域。"""

    def __init__(self, callback):
        """
        :param callback: 回调函数 callback(image: Image.Image)
        """
        self.callback = callback
        self.root = None
        self.canvas = None
        self.start_x = 0
        self.start_y = 0
        self.rect_id = None
        self._screen_img = None

    def show(self):
        """在独立线程中运行（因为 tkinter 不是线程安全的）。"""
        # 截取全屏
        try:
            self._screen_img = capture_screen()
        except Exception as e:
            log.error("截图失败: %s", e)
            return

        self.root = tk.Tk()
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.3)
        self.root.configure(bg="black")
        self.root.title("框选翻译区域")

        # 显示截图作为背景
        self.tk_img = ImageTk.PhotoImage(self._screen_img)
        self.canvas = tk.Canvas(self.root, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.tk_img)

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<Escape>", lambda e: self._cancel())

        # 提示文字
        self.canvas.create_text(
            self.root.winfo_screenwidth() // 2,
            30,
            text="拖动鼠标框选要翻译的区域 | ESC 取消",
            fill="red", font=("Microsoft YaHei", 16, "bold"),
        )

        self.root.mainloop()

    def _on_press(self, event):
        self.start_x = event.x
        self.start_y = event.y
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            self.start_x, self.start_y, event.x, event.y,
            outline="red", width=2
        )

    def _on_drag(self, event):
        if self.rect_id:
            self.canvas.coords(self.rect_id, self.start_x, self.start_y, event.x, event.y)

    def _on_release(self, event):
        x1 = min(self.start_x, event.x)
        y1 = min(self.start_y, event.y)
        x2 = max(self.start_x, event.x)
        y2 = max(self.start_y, event.y)
        self.root.destroy()

        if x2 - x1 < 5 or y2 - y1 < 5:
            return  # 太小，忽略

        # 裁剪选区
        cropped = self._screen_img.crop((x1, y1, x2, y2))
        try:
            self.callback(cropped)
        except Exception as e:
            log.exception("截图回调异常: %s", e)

    def _cancel(self):
        self.root.destroy()


def start_screenshot_translate(translator, target_lang: str, on_result=None):
    """启动截图翻译流程。
    :param translator: Translator 实例
    :param target_lang: 目标语言
    :param on_result: 回调 on_result(original_text, translated_text)
    """
    def _on_captured(img: Image.Image):
        # OCR
        try:
            text = ocr_image(img)
        except RuntimeError as e:
            if on_result:
                on_result("", f"[OCR 错误] {e}")
            return
        if not text:
            if on_result:
                on_result("", "[未识别到文字]")
            return
        # 翻译
        try:
            result = translator.translate(text, target=target_lang)
            translated = result.text if result else text
        except Exception as e:
            translated = f"[翻译失败] {e}"
        if on_result:
            on_result(text, translated)

    # 在独立线程中运行 tkinter（避免和主线程冲突）
    def _run():
        selector = RegionSelector(callback=_on_captured)
        selector.show()

    t = threading.Thread(target=_run, name="ScreenshotOCR", daemon=True)
    t.start()
