"""
screenshot.py
截图翻译模块。

功能：
- mss 快速截屏（全屏）
- tkinter 全屏覆盖窗口，鼠标拖动选框
- pytesseract OCR 识别文字
- 翻译识别结果

修复选框问题：
- 不使用 alpha 透明，改用截图背景 + 蒙层
- 确保鼠标事件能正确传递到 Canvas
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from PIL import Image

log = logging.getLogger(__name__)

_TESSERACT_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    r"D:\Tesseract-OCR\tesseract.exe",
]


def _find_tesseract() -> Optional[str]:
    import os
    for p in os.environ.get("PATH", "").split(os.pathsep):
        exe = os.path.join(p, "tesseract.exe")
        if os.path.isfile(exe):
            return exe
    for p in _TESSERACT_PATHS:
        if os.path.isfile(p):
            return p
    return None


def ocr_image(image: Image.Image) -> str:
    import pytesseract  # type: ignore
    tesseract_path = _find_tesseract()
    if tesseract_path:
        pytesseract.pytesseract.tesseract_cmd = tesseract_path
    try:
        # 多语言识别：英文+中文简体
        text = pytesseract.image_to_string(image, lang="eng+chi_sim")
    except Exception as e:
        raise RuntimeError(
            f"OCR 识别失败: {e}\n\n"
            "请确保已安装 Tesseract-OCR 引擎。\n"
            "下载地址: https://github.com/UB-Mannheim/tesseract/wiki"
        )
    return text.strip()


def capture_fullscreen() -> Image.Image:
    import mss  # type: ignore
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        raw = sct.grab(monitor)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    return img


# ============================================================================
# 选区选择器
# ============================================================================
class RegionSelector:
    """全屏选区选择器。

    实现方式：
    1. 截取全屏
    2. 创建全屏 tkinter 窗口
    3. 用 Canvas 显示截图作为背景
    4. 鼠标拖动时绘制选框（半透明黑色蒙层 + 红色边框）
    """

    def __init__(self, callback):
        """
        :param callback: callback(image: Image.Image, x1, y1, x2, y2)
        """
        self.callback = callback
        self._screen_img = None
        self._start_x = 0
        self._start_y = 0
        self._end_x = 0
        self._end_y = 0
        self._rect_id = None
        self._overlay_id = None

    def run(self):
        import tkinter as tk
        from PIL import ImageTk

        # 1. 截屏
        try:
            self._screen_img = capture_fullscreen()
        except Exception as e:
            log.error("截图失败: %s", e)
            return

        # 2. 创建全屏窗口
        root = tk.Tk()
        root.attributes("-fullscreen", True)
        root.attributes("-topmost", True)
        root.configure(cursor="crosshair")
        root.title("框选翻译区域 (ESC 取消)")

        screen_w = self._screen_img.width
        screen_h = self._screen_img.height

        # 3. Canvas 显示截图背景
        canvas = tk.Canvas(root, width=screen_w, height=screen_h,
                           highlightthickness=0, bd=0, bg="black")
        canvas.pack(fill=tk.BOTH, expand=True)

        self._tk_img = ImageTk.PhotoImage(self._screen_img)
        canvas.create_image(0, 0, anchor=tk.NW, image=self._tk_img)

        # 提示文字
        canvas.create_text(
            screen_w // 2, 40,
            text="拖动鼠标选择要翻译的区域 | 按 ESC 取消",
            fill="red", font=("Microsoft YaHei", 18, "bold"),
        )

        # 4. 绑定鼠标事件
        canvas.bind("<Button-1>", self._on_press)
        canvas.bind("<B1-Motion>", self._on_drag)
        canvas.bind("<ButtonRelease-1>", self._on_release)
        root.bind("<Escape>", lambda e: self._cancel(root))

        self._canvas = canvas
        self._root = root
        root.mainloop()

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------
    def _on_press(self, event):
        self._start_x = event.x
        self._start_y = event.y
        self._end_x = event.x
        self._end_y = event.y

        # 删除旧选框
        if self._rect_id:
            self._canvas.delete(self._rect_id)

        # 画新选框
        self._rect_id = self._canvas.create_rectangle(
            self._start_x, self._start_y, event.x, event.y,
            outline="red", width=2,
        )

    def _on_drag(self, event):
        self._end_x = event.x
        self._end_y = event.y
        if self._rect_id:
            self._canvas.coords(
                self._rect_id,
                self._start_x, self._start_y,
                event.x, event.y,
            )

    def _on_release(self, event):
        x1 = min(self._start_x, self._end_x)
        y1 = min(self._start_y, self._end_y)
        x2 = max(self._start_x, self._end_x)
        y2 = max(self._start_y, self._end_y)

        # 区域太小，忽略
        if x2 - x1 < 5 or y2 - y1 < 5:
            self._cancel(self._root)
            return

        # 裁剪
        cropped = self._screen_img.crop((x1, y1, x2, y2))

        # 关闭窗口
        self._root.destroy()

        # 回调
        try:
            self.callback(cropped, x1, y1, x2, y2)
        except Exception as e:
            log.exception("截图回调异常: %s", e)

    def _cancel(self, root):
        root.destroy()


# ============================================================================
# 对外接口
# ============================================================================
def start_screenshot_translate(translator, target_lang: str, on_result=None):
    """启动截图翻译流程。

    :param translator: Translator 实例
    :param target_lang: 目标语言代码
    :param on_result: 回调 on_result(original_text, translated_text)
    """
    def _on_captured(img, x1, y1, x2, y2):
        # OCR
        try:
            text = ocr_image(img)
        except RuntimeError as e:
            if on_result:
                on_result("", str(e))
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

    def _run():
        selector = RegionSelector(callback=_on_captured)
        selector.run()

    t = threading.Thread(target=_run, name="ScreenshotOCR", daemon=True)
    t.start()
