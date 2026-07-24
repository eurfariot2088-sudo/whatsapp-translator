"""
screenshot.py
截图翻译模块。

v4 修复：
- 截图选框窗口在调用方的 tkinter 主线程中运行（不跨线程）
- OCR 和翻译在子线程中执行，不阻塞 UI
- 结果弹窗显示截图 + 原文 + 译文
"""

from __future__ import annotations

import io
import logging
import threading
from typing import Optional

from PIL import Image

log = logging.getLogger(__name__)

_TESSERACT_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
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
    """OCR 识别图片中的文字。"""
    import pytesseract  # type: ignore
    path = _find_tesseract()
    if path:
        pytesseract.pytesseract.tesseract_cmd = path
    try:
        text = pytesseract.image_to_string(image, lang="eng+chi_sim")
    except Exception as e:
        raise RuntimeError(
            f"OCR 识别失败: {e}\n\n"
            "请确保已安装 Tesseract-OCR 引擎。\n"
            "下载地址: https://github.com/UB-Mannheim/tesseract/wiki"
        )
    return text.strip()


def capture_fullscreen() -> Image.Image:
    """截取全屏。"""
    import mss  # type: ignore
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        raw = sct.grab(monitor)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    return img


# ============================================================================
# 选区选择器（必须在主线程调用）
# ============================================================================
def show_region_selector(parent_tk, callback):
    """
    在主线程中显示全屏选区窗口。

    :param parent_tk: 父 tkinter 窗口（会暂时隐藏）
    :param callback: 选区完成后调用 callback(cropped_image: Image.Image)
    """
    import tkinter as tk
    from PIL import ImageTk

    # 1. 截屏（在隐藏父窗口后）
    parent_tk.withdraw()
    parent_tk.update()

    # 短暂等待确保窗口已隐藏
    import time
    time.sleep(0.15)

    try:
        screen_img = capture_fullscreen()
    except Exception as e:
        log.error("截图失败: %s", e)
        parent_tk.deiconify()
        return

    # 2. 创建全屏选区窗口
    overlay = tk.Toplevel()
    overlay.attributes("-fullscreen", True)
    overlay.attributes("-topmost", True)
    overlay.configure(cursor="crosshair")
    overlay.title("框选翻译区域")

    screen_w = screen_img.width
    screen_h = screen_img.height

    # 创建浅色蒙层版本（在原截图上叠加半透明白色）
    from PIL import ImageEnhance
    overlay_img = Image.new("RGBA", (screen_w, screen_h), (255, 255, 255, 60))
    dimmed = Image.alpha_composite(screen_img.convert("RGBA"), overlay_img)
    # 转回 RGB 用于 PhotoImage
    dimmed = dimmed.convert("RGB")

    canvas = tk.Canvas(overlay, width=screen_w, height=screen_h,
                       highlightthickness=0, bd=0, bg="#888888")
    canvas.pack(fill=tk.BOTH, expand=True)

    # 显示带浅色蒙层的截图
    tk_img = ImageTk.PhotoImage(dimmed)
    canvas.create_image(0, 0, anchor=tk.NW, image=tk_img)

    # 保存原始截图用于选区内显示
    tk_orig = ImageTk.PhotoImage(screen_img)

    # 提示
    canvas.create_text(
        screen_w // 2, 40,
        text="拖动鼠标选择要翻译的区域 | 按 ESC 取消",
        fill="red", font=("Microsoft YaHei", 18, "bold"),
    )

    # 状态
    state = {"start_x": 0, "start_y": 0, "rect_id": None, "orig_id": None}

    def on_press(event):
        state["start_x"] = event.x
        state["start_y"] = event.y
        if state["rect_id"]:
            canvas.delete(state["rect_id"])
        if state["orig_id"]:
            canvas.delete(state["orig_id"])
        # 选框内的原图（亮色显示）
        state["orig_id"] = canvas.create_image(event.x, event.y, anchor=tk.NW, image=tk_orig)
        state["rect_id"] = canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline="#FF0000", width=3,
        )
        # 把选框提到最上层
        canvas.tag_raise(state["rect_id"])

    def on_drag(event):
        x1 = min(state["start_x"], event.x)
        y1 = min(state["start_y"], event.y)
        x2 = max(state["start_x"], event.x)
        y2 = max(state["start_y"], event.y)
        if state["rect_id"]:
            canvas.coords(state["rect_id"], x1, y1, x2, y2)
        if state["orig_id"]:
            # 裁剪选区内的原图并显示
            crop = screen_img.crop((x1, y1, x2, y2))
            crop_tk = ImageTk.PhotoImage(crop)
            canvas.delete(state["orig_id"])
            state["orig_id"] = canvas.create_image(x1, y1, anchor=tk.NW, image=crop_tk)
            state["crop_tk"] = crop_tk  # 防止 GC
            canvas.tag_raise(state["rect_id"])

    def on_release(event):
        x1 = min(state["start_x"], event.x)
        y1 = min(state["start_y"], event.y)
        x2 = max(state["start_x"], event.x)
        y2 = max(state["start_y"], event.y)

        overlay.destroy()

        if x2 - x1 < 5 or y2 - y1 < 5:
            # 太小，取消
            parent_tk.deiconify()
            return

        # 裁剪
        cropped = screen_img.crop((x1, y1, x2, y2))

        # 恢复父窗口
        parent_tk.deiconify()

        # 回调
        try:
            callback(cropped)
        except Exception as e:
            log.exception("截图回调异常: %s", e)

    def on_escape(event):
        overlay.destroy()
        parent_tk.deiconify()

    canvas.bind("<Button-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    overlay.bind("<Escape>", on_escape)

    # 确保窗口获得焦点
    overlay.focus_force()


# ============================================================================
# 截图翻译结果弹窗
# ============================================================================
class ScreenshotResultDialog:
    """显示截图翻译结果的弹窗（含图片）。"""

    @staticmethod
    def show(parent_tk, image: Image.Image, original: str, translated: str):
        """在主线程中创建结果弹窗。"""
        import tkinter as tk
        from tkinter import ttk, messagebox
        from PIL import ImageTk

        dlg = tk.Toplevel(parent_tk)
        dlg.title("截图翻译结果")
        dlg.geometry("600x550")

        f = ttk.Frame(dlg, padding=10)
        f.pack(fill=tk.BOTH, expand=True)

        # 截图预览
        ttk.Label(f, text="截图预览：", font=("", 10, "bold")).pack(anchor=tk.W)
        # 缩放图片适应显示
        max_w = 560
        max_h = 180
        img_copy = image.copy()
        img_copy.thumbnail((max_w, max_h), Image.LANCZOS)
        tk_img = ImageTk.PhotoImage(img_copy)
        lbl_img = tk.Label(f, image=tk_img)
        lbl_img.pack(pady=(4, 8))
        lbl_img.image = tk_img  # 防止 GC

        # 原文
        ttk.Label(f, text="识别原文：", font=("", 10, "bold")).pack(anchor=tk.W)
        txt_orig = tk.Text(f, height=4, wrap=tk.WORD, font=("Microsoft YaHei", 10))
        txt_orig.pack(fill=tk.X, pady=(2, 6))
        txt_orig.insert("1.0", original)
        txt_orig.configure(state=tk.DISABLED)

        # 译文
        ttk.Label(f, text="译文：", font=("", 10, "bold")).pack(anchor=tk.W)
        txt_trans = tk.Text(f, height=4, wrap=tk.WORD, font=("Microsoft YaHei", 11))
        txt_trans.pack(fill=tk.X, pady=(2, 6))
        txt_trans.insert("1.0", translated)
        txt_trans.configure(state=tk.DISABLED)

        # 按钮
        btn_frame = ttk.Frame(f)
        btn_frame.pack(fill=tk.X)

        def copy_to_clip(text):
            try:
                import subprocess
                subprocess.run(["clip"], input=text.encode("utf-16le"), check=True)
            except Exception:
                pass

        ttk.Button(btn_frame, text="复制译文",
                   command=lambda: copy_to_clip(translated)).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_frame, text="复制原文",
                   command=lambda: copy_to_clip(original)).pack(side=tk.RIGHT)
        ttk.Button(btn_frame, text="关闭",
                   command=dlg.destroy).pack(side=tk.LEFT)

        dlg.focus_force()


# ============================================================================
# 对外接口
# ============================================================================
def start_screenshot_translate(parent_tk, translator, target_lang: str):
    """
    启动截图翻译（在主线程调用）。

    流程：
    1. 显示全屏选区窗口（主线程）
    2. 用户选区后，在子线程中 OCR + 翻译
    3. 完成后在主线程中显示结果弹窗
    """
    def on_captured(img: Image.Image):
        # 在子线程中执行 OCR + 翻译
        def worker():
            # OCR
            try:
                text = ocr_image(img)
            except RuntimeError as e:
                parent_tk.after(0, lambda: _show_error(parent_tk, str(e)))
                return

            if not text:
                text = "[未识别到文字]"
                translated = ""
                parent_tk.after(0, lambda: ScreenshotResultDialog.show(
                    parent_tk, img, text, translated))
                return

            # 翻译
            try:
                result = translator.translate(text, target=target_lang)
                translated = result.text if result else text
            except Exception as e:
                translated = f"[翻译失败] {e}"

            # 在主线程显示结果
            parent_tk.after(0, lambda: ScreenshotResultDialog.show(
                parent_tk, img, text, translated))

        t = threading.Thread(target=worker, name="OCR-Translate", daemon=True)
        t.start()

    # 在主线程显示选区窗口
    show_region_selector(parent_tk, on_captured)


def _show_error(parent_tk, msg: str):
    """显示错误弹窗。"""
    import tkinter as tk
    from tkinter import messagebox
    messagebox.showerror("截图翻译", msg, parent=parent_tk)
    parent_tk.deiconify()
