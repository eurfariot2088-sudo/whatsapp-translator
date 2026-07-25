"""
screenshot.py
截图翻译模块。

v7：
- 使用 ocr_engine 模块（Windows 内置 OCR 优先，Tesseract 备选，无需安装）
- 修复黑屏：所有 PhotoImage 引用挂到 overlay 上防止 GC 回收
- 微信式半透明遮罩：全屏原始图 + 深色蒙层，选区内清晰可见
- 不关闭/隐藏主窗口
- 截图选框后打开独立新窗口显示截图内容
- 在新窗口中直接进行 OCR + 翻译，实时更新结果
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from PIL import Image, ImageDraw

log = logging.getLogger(__name__)


def ocr_image(image: Image.Image) -> str:
    """OCR 识别图片中的文字（自动选择可用后端）。"""
    from ocr_engine import ocr_image as _ocr
    return _ocr(image, languages=["en", "zh-Hans"])


def capture_fullscreen() -> Image.Image:
    """截取全屏。"""
    import mss  # type: ignore
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        raw = sct.grab(monitor)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    return img


def _make_dark_overlay(image: Image.Image, opacity: int = 140) -> Image.Image:
    """在截图上叠加一层深色半透明蒙层，模拟微信截图效果。"""
    dark = Image.new("RGBA", image.size, (0, 0, 0, opacity))
    base = image.convert("RGBA")
    return Image.alpha_composite(base, dark)


# ============================================================================
# 选区选择器（必须在主线程调用，不隐藏主窗口）
# ============================================================================
def show_region_selector(parent_tk, callback):
    """
    显示全屏选区窗口。不隐藏主窗口。
    微信式效果：全屏深色半透明蒙层，选区内清晰显示原图。

    :param parent_tk: 父 tkinter 窗口（保持可见）
    :param callback: 选区完成后调用 callback(cropped_image: Image.Image)
    """
    import tkinter as tk
    from PIL import ImageTk

    # 1. 截屏（不隐藏主窗口，全屏选区窗口会覆盖在最上层）
    import time
    time.sleep(0.15)

    try:
        screen_img = capture_fullscreen()
    except Exception as e:
        log.error("截图失败: %s", e)
        return

    # 2. 创建全屏选区窗口
    overlay = tk.Toplevel(parent_tk)
    overlay.attributes("-fullscreen", True)
    overlay.attributes("-topmost", True)
    overlay.configure(cursor="crosshair")
    overlay.title("框选翻译区域")

    screen_w = screen_img.width
    screen_h = screen_img.height

    canvas = tk.Canvas(overlay, width=screen_w, height=screen_h,
                       highlightthickness=0, bd=0, bg="#000000")
    canvas.pack(fill=tk.BOTH, expand=True)

    # 生成带深色蒙层的背景图（微信式半透明效果）
    dark_img = _make_dark_overlay(screen_img, opacity=120)
    tk_dark = ImageTk.PhotoImage(dark_img)
    canvas.create_image(0, 0, anchor=tk.NW, image=tk_dark)

    # 把 PhotoImage 引用挂到 overlay 上，防止 GC 回收（关键！）
    overlay._tk_dark = tk_dark
    overlay._screen_img = screen_img

    # 提示
    tip_id = canvas.create_text(
        screen_w // 2, 40,
        text="拖动鼠标选择要翻译的区域 | 按 ESC 取消",
        fill="#FFFFFF", font=("Microsoft YaHei", 16, "bold"),
    )

    # 状态
    state = {
        "start_x": 0, "start_y": 0,
        "clear_id": None,     # 选区内清晰截图
        "rect_id": None,      # 选区边框
        "tip_hidden": False,
    }

    def on_press(event):
        state["start_x"] = event.x
        state["start_y"] = event.y

        if state["rect_id"]:
            canvas.delete(state["rect_id"])
            state["rect_id"] = None
        if state["clear_id"]:
            canvas.delete(state["clear_id"])
            state["clear_id"] = None

        # 隐藏提示文字
        if not state["tip_hidden"]:
            canvas.itemconfigure(tip_id, state="hidden")
            state["tip_hidden"] = True

        state["rect_id"] = canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline="#07C160", width=2,
        )

    def on_drag(event):
        x1 = min(state["start_x"], event.x)
        y1 = min(state["start_y"], event.y)
        x2 = max(state["start_x"], event.x)
        y2 = max(state["start_y"], event.y)

        # 更新选区边框
        if state["rect_id"]:
            canvas.coords(state["rect_id"], x1, y1, x2, y2)

        # 更新选区内的清晰原图（裁一块贴上去，覆盖在深色蒙层之上）
        w = max(1, x2 - x1)
        h = max(1, y2 - y1)
        crop = screen_img.crop((x1, y1, x1 + w, y1 + h))
        crop_tk = ImageTk.PhotoImage(crop)

        if state["clear_id"]:
            canvas.delete(state["clear_id"])
        state["clear_id"] = canvas.create_image(x1, y1, anchor=tk.NW, image=crop_tk)
        # 把引用存到 state 里防止 GC
        state["_crop_tk"] = crop_tk

        # 边框始终在最上层
        if state["rect_id"]:
            canvas.tag_raise(state["rect_id"])

    def on_release(event):
        x1 = min(state["start_x"], event.x)
        y1 = min(state["start_y"], event.y)
        x2 = max(state["start_x"], event.x)
        y2 = max(state["start_y"], event.y)

        overlay.destroy()

        if x2 - x1 < 5 or y2 - y1 < 5:
            return

        cropped = screen_img.crop((x1, y1, x2, y2))
        try:
            callback(cropped)
        except Exception as e:
            log.exception("截图回调异常: %s", e)

    def on_escape(event):
        overlay.destroy()

    canvas.bind("<Button-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    overlay.bind("<Escape>", on_escape)

    overlay.focus_force()


# ============================================================================
# 截图翻译结果窗口（独立窗口，实时更新）
# ============================================================================
class ScreenshotResultWindow:
    """独立的截图翻译结果窗口。先显示截图，然后实时更新 OCR 和翻译结果。"""

    @staticmethod
    def show(parent_tk, image: Image.Image):
        """创建结果窗口，先显示图片和"识别中..."状态。"""
        import tkinter as tk
        from tkinter import ttk
        from PIL import ImageTk

        dlg = tk.Toplevel(parent_tk)
        dlg.title("截图翻译")
        dlg.geometry("620x600")
        dlg.transient(parent_tk)

        f = ttk.Frame(dlg, padding=10)
        f.pack(fill=tk.BOTH, expand=True)

        # ---- 截图显示区 ----
        ttk.Label(f, text="截图内容：", font=("", 10, "bold")).pack(anchor=tk.W)
        img_frame = ttk.Frame(f, relief=tk.SUNKEN, borderwidth=1)
        img_frame.pack(fill=tk.X, pady=(2, 8))

        max_w = 580
        max_h = 200
        img_copy = image.copy()
        img_copy.thumbnail((max_w, max_h), Image.LANCZOS)
        tk_img = ImageTk.PhotoImage(img_copy)
        lbl_img = tk.Label(img_frame, image=tk_img, bg="#F0F0F0")
        lbl_img.pack(padx=4, pady=4)
        lbl_img.image = tk_img

        # ---- 原文区 ----
        ttk.Label(f, text="识别原文：", font=("", 10, "bold")).pack(anchor=tk.W)
        txt_orig = tk.Text(f, height=4, wrap=tk.WORD, font=("Microsoft YaHei", 10))
        txt_orig.pack(fill=tk.X, pady=(2, 6))
        txt_orig.insert("1.0", "正在识别文字...")
        txt_orig.configure(state=tk.DISABLED)

        # ---- 译文区 ----
        ttk.Label(f, text="译文：", font=("", 10, "bold")).pack(anchor=tk.W)
        txt_trans = tk.Text(f, height=6, wrap=tk.WORD, font=("Microsoft YaHei", 11))
        txt_trans.pack(fill=tk.BOTH, expand=True, pady=(2, 6))
        txt_trans.insert("1.0", "等待翻译...")
        txt_trans.configure(state=tk.DISABLED)

        # ---- 状态标签 ----
        lbl_status = ttk.Label(f, text="正在处理...", foreground="blue")
        lbl_status.pack(anchor=tk.W)

        # ---- 按钮区 ----
        btn_frame = ttk.Frame(f)
        btn_frame.pack(fill=tk.X, pady=(4, 0))

        def copy_to_clip(text):
            try:
                import subprocess
                subprocess.run(["clip"], input=text.encode("utf-16le"), check=True)
            except Exception:
                pass

        btn_copy_trans = ttk.Button(btn_frame, text="复制译文", state=tk.DISABLED)
        btn_copy_trans.pack(side=tk.RIGHT, padx=4)
        btn_copy_orig = ttk.Button(btn_frame, text="复制原文", state=tk.DISABLED)
        btn_copy_orig.pack(side=tk.RIGHT)
        ttk.Button(btn_frame, text="关闭", command=dlg.destroy).pack(side=tk.LEFT)

        dlg.focus_force()

        # 返回控制器，供外部更新内容
        return _ResultController(dlg, txt_orig, txt_trans, lbl_status,
                                  btn_copy_orig, btn_copy_trans, copy_to_clip)


class _ResultController:
    """用于更新结果窗口内容的控制器。"""

    def __init__(self, dlg, txt_orig, txt_trans, lbl_status,
                 btn_orig, btn_trans, copy_fn):
        self.dlg = dlg
        self.txt_orig = txt_orig
        self.txt_trans = txt_trans
        self.lbl_status = lbl_status
        self.btn_orig = btn_orig
        self.btn_trans = btn_trans
        self._copy_fn = copy_fn
        self._orig_text = ""
        self._trans_text = ""

    def set_original(self, text: str):
        self._orig_text = text
        self.txt_orig.configure(state=tk.NORMAL)
        self.txt_orig.delete("1.0", "end")
        self.txt_orig.insert("1.0", text)
        self.txt_orig.configure(state=tk.DISABLED)
        self.btn_orig.configure(state=tk.NORMAL, command=lambda: self._copy_fn(text))
        self.lbl_status.configure(text="原文识别完成，正在翻译...", foreground="blue")

    def set_translated(self, text: str):
        self._trans_text = text
        self.txt_trans.configure(state=tk.NORMAL)
        self.txt_trans.delete("1.0", "end")
        self.txt_trans.insert("1.0", text)
        self.txt_trans.configure(state=tk.DISABLED)
        self.btn_trans.configure(state=tk.NORMAL, command=lambda: self._copy_fn(text))
        self.lbl_status.configure(text="翻译完成", foreground="green")

    def set_error(self, msg: str):
        self.lbl_status.configure(text=f"错误: {msg}", foreground="red")
        self.txt_trans.configure(state=tk.NORMAL)
        self.txt_trans.delete("1.0", "end")
        self.txt_trans.insert("1.0", f"[错误] {msg}")
        self.txt_trans.configure(state=tk.DISABLED)


# ============================================================================
# 对外接口
# ============================================================================
def start_screenshot_translate(parent_tk, translator, target_lang: str):
    """
    启动截图翻译（在主线程调用）。

    流程：
    1. 显示全屏选区窗口（主窗口保持可见，微信式半透明遮罩）
    2. 用户选区后，打开独立结果窗口显示截图
    3. 子线程中 OCR + 翻译，实时更新结果窗口
    """
    def on_captured(img: Image.Image):
        # 先创建结果窗口（主线程），显示截图
        ctrl = ScreenshotResultWindow.show(parent_tk, img)

        # 子线程中执行 OCR + 翻译
        def worker():
            # OCR
            try:
                text = ocr_image(img)
            except RuntimeError as e:
                parent_tk.after(0, lambda: ctrl.set_error(str(e)))
                return

            if not text:
                text = "[未识别到文字]"
                parent_tk.after(0, lambda: ctrl.set_original(text))
                parent_tk.after(0, lambda: ctrl.set_translated("（无内容可翻译）"))
                return

            # 更新原文
            parent_tk.after(0, lambda: ctrl.set_original(text))

            # 翻译
            try:
                result = translator.translate(text, target=target_lang)
                translated = result.text if result else text
            except Exception as e:
                translated = f"[翻译失败] {e}"

            parent_tk.after(0, lambda: ctrl.set_translated(translated))

        t = threading.Thread(target=worker, name="OCR-Translate", daemon=True)
        t.start()

    # 在主线程显示选区窗口
    show_region_selector(parent_tk, on_captured)
