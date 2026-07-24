"""
gui.py
微信式对话气泡界面 —— 完全重写版。

功能：
1. 对话气泡显示：对方消息靠左白色，自己消息靠右绿色（可自定义颜色）
2. 每条消息下方显示译文（灰色小字）
3. 底部回复区：输入中文 → 选目标语言 → 翻译 → 复制
4. 截图翻译：热键 Ctrl+Alt+S 或按钮触发
5. 实时消息监听：0.8 秒轮询，新消息即时翻译显示
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Optional

from config import (LANGUAGES, AppConfig, get_settings, save_settings)
from translator import Translator, TranslateError, TranslationResult
from whatsapp_reader import WhatsAppMessage, WhatsAppReader

log = logging.getLogger(__name__)

# 全局消息队列：Reader 线程 → GUI 线程
_msg_queue: "queue.Queue[tuple]" = queue.Queue()


def _post_msg(kind: str, payload):
    _msg_queue.put((kind, payload))


# ===========================================================================
# 聊天气泡
# ===========================================================================
class ChatBubble(tk.Frame):
    """单条消息气泡。"""

    def __init__(self, parent, text: str, translation: str, direction: str,
                 colors: dict, font_family: str = "Microsoft YaHei",
                 font_size: int = 11, on_copy=None):
        """
        :param direction: "in" 收到 / "out" 发出
        :param colors: 颜色字典
        :param on_copy: 复制回调 on_copy(text)
        """
        super().__init__(parent, bg=colors["chat_bg"])
        self._on_copy = on_copy
        is_in = direction == "in"

        bubble_bg = colors["bubble_in"] if is_in else colors["bubble_out"]
        text_fg = colors["text_in"] if is_in else colors["text_out"]
        trans_bg = colors["trans_bg"]
        trans_fg = colors["trans_text"]

        max_width = 420  # 气泡最大宽度

        # 气泡容器
        bubble = tk.Frame(self, bg=bubble_bg, padx=12, pady=8)
        # 原文
        lbl_orig = tk.Label(bubble, text=text, font=(font_family, font_size),
                            fg=text_fg, bg=bubble_bg, justify=tk.LEFT,
                            wraplength=max_width, anchor=tk.W)
        lbl_orig.pack(fill=tk.X, anchor=tk.W)

        # 译文（如果有）
        if translation and translation.strip():
            sep = tk.Frame(bubble, bg=trans_bg, height=1)
            sep.pack(fill=tk.X, pady=(4, 2))
            lbl_trans = tk.Label(bubble, text=translation,
                                 font=(font_family, font_size - 1),
                                 fg=trans_fg, bg=bubble_bg, justify=tk.LEFT,
                                 wraplength=max_width, anchor=tk.W)
            lbl_trans.pack(fill=tk.X, anchor=tk.W)

        # 复制按钮（小）
        if on_copy:
            btn_copy = tk.Label(bubble, text="📋复制", font=(font_family, 8),
                                fg="#999999", bg=bubble_bg, cursor="hand2")
            btn_copy.pack(anchor=tk.E, pady=(2, 0))
            btn_copy.bind("<Button-1>", lambda e: on_copy(text))

        # 时间
        time_str = time.strftime("%H:%M")
        lbl_time = tk.Label(self, text=time_str, font=(font_family, 8),
                            fg="#999999", bg=colors["chat_bg"])
        # 排列方向
        if is_in:
            lbl_time.pack(side=tk.LEFT, padx=(4, 0), anchor=tk.S)
            bubble.pack(side=tk.LEFT, anchor=tk.W)
        else:
            lbl_time.pack(side=tk.RIGHT, padx=(0, 4), anchor=tk.S)
            bubble.pack(side=tk.RIGHT, anchor=tk.E)


# ===========================================================================
# 截图翻译结果弹窗
# ===========================================================================
class ScreenshotResultDialog(tk.Toplevel):
    def __init__(self, parent, original: str, translated: str):
        super().__init__(parent)
        self.title("截图翻译结果")
        self.geometry("500x350")
        self.translated = translated
        self._build(original, translated)

    def _build(self, original, translated):
        f = ttk.Frame(self, padding=12)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text="识别原文：").pack(anchor=tk.W)
        txt_orig = tk.Text(f, height=5, wrap=tk.WORD, font=("Consolas", 10))
        txt_orig.pack(fill=tk.X, pady=(2, 8))
        txt_orig.insert("1.0", original)
        txt_orig.configure(state=tk.DISABLED)

        ttk.Label(f, text="译文：").pack(anchor=tk.W)
        txt_trans = tk.Text(f, height=5, wrap=tk.WORD, font=("Microsoft YaHei", 11))
        txt_trans.pack(fill=tk.X, pady=(2, 8))
        txt_trans.insert("1.0", translated)
        txt_trans.configure(state=tk.DISABLED)

        btn_frame = ttk.Frame(f)
        btn_frame.pack(fill=tk.X)
        ttk.Button(btn_frame, text="复制译文",
                   command=lambda: self._copy(self.translated)).pack(side=tk.RIGHT)
        ttk.Button(btn_frame, text="复制原文",
                   command=lambda: self._copy(original)).pack(side=tk.RIGHT, padx=(0, 4))
        ttk.Button(btn_frame, text="关闭", command=self.destroy).pack(side=tk.RIGHT, padx=(0, 4))

    @staticmethod
    def _copy(text):
        try:
            import subprocess
            subprocess.run(["clip"], input=text.encode("utf-16le"), check=True)
        except Exception:
            pass


# ===========================================================================
# 主应用
# ===========================================================================
class TranslatorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg: AppConfig = get_settings()
        self.translator = Translator(self.cfg.translator)
        self.reader: Optional[WhatsAppReader] = None
        self.running = False
        self._msg_bubbles = []  # 保留引用防止 GC
        self._build_ui()
        self._poll_queue()
        self._install_hotkeys()
        self._setup_tray()
        if not self.cfg.gui.start_minimized:
            self.root.after(300, self._start_reader)
        else:
            self.root.after(300, self._hide_to_tray)

    # ==================== UI 构建 ====================
    def _build_ui(self):
        self.root.title("WhatsApp 翻译助手")
        self.root.geometry("900x680")
        self.root.minsize(700, 500)
        self._apply_theme()

        # ---- 顶部工具栏 ----
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(side=tk.TOP, fill=tk.X)
        self.btn_toggle = ttk.Button(bar, text="▶ 开始监听", width=14, command=self._toggle_reader)
        self.btn_toggle.pack(side=tk.LEFT)
        ttk.Button(bar, text="🧹 清空", width=8, command=self._clear_all).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(bar, text="📸 截图翻译", width=12, command=self._screenshot_translate).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(bar, text="⚙ 设置", width=8, command=self._open_settings).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(bar, text="⏏ 退出", width=8, command=self._quit).pack(side=tk.RIGHT)

        # ---- 聊天区域（Canvas + Scrollbar） ----
        g = self.cfg.gui
        chat_frame = tk.Frame(self.root, bg=g.chat_bg_color)
        chat_frame.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        self.canvas = tk.Canvas(chat_frame, bg=g.chat_bg_color, highlightthickness=0)
        scrollbar = ttk.Scrollbar(chat_frame, orient=tk.VERTICAL, command=self.canvas.yview)
        self.scroll_frame = tk.Frame(self.canvas, bg=g.chat_bg_color)

        self.scroll_frame.bind("<Configure>",
                               lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._canvas_window = self.canvas.create_window((0, 0), window=self.scroll_frame, anchor=tk.NW)

        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 鼠标滚轮
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._on_mousewheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

        # ---- 底部回复翻译区 ----
        reply_frame = ttk.LabelFrame(self.root, text="输入翻译", padding=(8, 6))
        reply_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(4, 0))

        # 第一行：语言下拉 + 翻译按钮
        row1 = ttk.Frame(reply_frame)
        row1.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(row1, text="目标语言:").pack(side=tk.LEFT)
        self.var_reply_lang = tk.StringVar(value=g.reply_target_lang)
        lang_names = [f"{code} - {name}" for code, name in LANGUAGES if code != "auto"]
        self.cb_reply_lang = ttk.Combobox(row1, textvariable=self.var_reply_lang,
                                          values=lang_names, state="readonly", width=20)
        self.cb_reply_lang.pack(side=tk.LEFT, padx=(4, 8))
        ttk.Button(row1, text="🔍 翻译", command=self._translate_reply).pack(side=tk.LEFT)
        ttk.Button(row1, text="📋 复制译文", command=self._copy_reply_trans).pack(side=tk.RIGHT)

        # 第二行：输入文本框
        self.reply_text = tk.Text(reply_frame, height=3, font=(g.font_family, g.font_size),
                                   wrap=tk.WORD)
        self.reply_text.pack(fill=tk.X, pady=(0, 4))

        # 第三行：译文输出
        out_frame = ttk.Frame(reply_frame)
        out_frame.pack(fill=tk.X)
        ttk.Label(out_frame, text="译文:").pack(side=tk.LEFT)
        self.reply_result = tk.StringVar(value="")
        lbl_result = tk.Label(out_frame, textvariable=self.reply_result,
                              font=(g.font_family, g.font_size), fg="#333333",
                              anchor=tk.W, wraplength=700, justify=tk.LEFT)
        lbl_result.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        # ---- 状态栏 ----
        self.status = tk.StringVar(value="就绪")
        status_bar = ttk.Label(self.root, textvariable=self.status,
                               anchor=tk.W, padding=(8, 4), relief=tk.SUNKEN)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_canvas_configure(self, event):
        width = event.width
        self.canvas.itemconfig(self._canvas_window, width=width)

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _apply_theme(self):
        style = ttk.Style()
        if "vista" in style.theme_names() and self.cfg.gui.theme == "light":
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")

    def _get_colors(self) -> dict:
        g = self.cfg.gui
        return {
            "chat_bg": g.chat_bg_color,
            "bubble_in": g.bubble_in_color,
            "bubble_out": g.bubble_out_color,
            "text_in": g.bubble_in_text,
            "text_out": g.bubble_out_text,
            "trans_bg": g.bubble_trans_color,
            "trans_text": g.bubble_trans_text,
        }

    # ==================== 消息监听 ====================
    def _toggle_reader(self):
        if self.running:
            self._stop_reader()
        else:
            self._start_reader()

    def _start_reader(self):
        if self.running:
            return
        try:
            self.reader = WhatsAppReader(
                self.cfg.reader,
                on_message=self._on_new_message,
                on_chat_changed=self._on_chat_changed,
            )
        except RuntimeError as e:
            messagebox.showerror("不支持的平台", str(e))
            return
        self.reader.start()
        self.running = True
        self.btn_toggle.configure(text="⏸ 暂停监听")
        self._set_status(f"正在监听 WhatsApp（{self.cfg.reader.poll_interval}s 轮询）→ {self.cfg.translator.target_lang}")

    def _stop_reader(self):
        if not self.running:
            return
        if self.reader:
            self.reader.stop()
        self.running = False
        self.btn_toggle.configure(text="▶ 开始监听")
        self._set_status("已暂停")

    def _clear_all(self):
        for w in self.scroll_frame.winfo_children():
            w.destroy()
        self._msg_bubbles.clear()
        if self.reader:
            self.reader.clear_history()
        self._set_status("已清空对话")

    def _on_new_message(self, msg: WhatsAppMessage):
        _post_msg("translate", msg)

    def _on_chat_changed(self):
        _post_msg("chat_changed", None)

    # ==================== 翻译 + 显示 ====================
    def _do_translate(self, msg: WhatsAppMessage):
        """在 GUI 线程中翻译并显示消息。"""
        # 先显示气泡（译文暂空，稍后更新）
        bubble = self._add_bubble(msg.text, "", msg.direction)
        # 异步翻译
        def _cb(result):
            if isinstance(result, TranslateError):
                _post_msg("update_bubble", (bubble, f"[翻译失败] {result}"))
            elif result is None:
                _post_msg("update_bubble", (bubble, "（无需翻译）"))
            else:
                _post_msg("update_bubble", (bubble, result.text))
                self._set_status(f"已翻译（{result.backend}, {result.elapsed_ms}ms）")
        self.translator.translate_async(msg.text, target=self.cfg.translator.target_lang,
                                        callback=_cb)

    def _add_bubble(self, text: str, translation: str, direction: str) -> ChatBubble:
        colors = self._get_colors()
        g = self.cfg.gui

        def _copy(t):
            try:
                import subprocess
                subprocess.run(["clip"], input=t.encode("utf-16le"), check=True)
                self._set_status("已复制到剪贴板")
            except Exception:
                pass

        bubble = ChatBubble(self.scroll_frame, text, translation, direction,
                            colors, g.font_family, g.font_size, on_copy=_copy)
        bubble.pack(fill=tk.X, padx=12, pady=4)
        self._msg_bubbles.append(bubble)

        # 限制消息数量
        if len(self._msg_bubbles) > 500:
            old = self._msg_bubbles.pop(0)
            old.destroy()

        # 自动滚到底
        self.root.after(50, self._scroll_to_bottom)
        return bubble

    def _update_bubble_translation(self, bubble: ChatBubble, translation: str):
        """更新已显示气泡的译文（销毁后重建）。"""
        # 由于 ChatBubble 在创建时固定了内容，这里采用重建方式
        idx = None
        for i, b in enumerate(self._msg_bubbles):
            if b is bubble:
                idx = i
                break
        if idx is None:
            return
        # 获取原信息
        # 简单做法：在气泡底部追加译文 Label
        try:
            # 找到 bubble 内部的 Frame
            for child in bubble.winfo_children():
                if isinstance(child, tk.Frame) and child.cget("bg") != self.cfg.gui.chat_bg_color:
                    # 这是气泡 Frame
                    sep = tk.Frame(child, bg=self.cfg.gui.bubble_trans_color, height=1)
                    sep.pack(fill=tk.X, pady=(4, 2))
                    lbl = tk.Label(child, text=translation,
                                   font=(self.cfg.gui.font_family, self.cfg.gui.font_size - 1),
                                   fg=self.cfg.gui.bubble_trans_text,
                                   bg=child.cget("bg"), justify=tk.LEFT,
                                   wraplength=420, anchor=tk.W)
                    lbl.pack(fill=tk.X, anchor=tk.W)
                    break
        except Exception:
            pass
        self.root.after(50, self._scroll_to_bottom)

    def _scroll_to_bottom(self):
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)

    # ==================== 底部回复区翻译 ====================
    def _translate_reply(self):
        text = self.reply_text.get("1.0", tk.END).strip()
        if not text:
            return
        lang = self.var_reply_lang.get().split(" - ")[0]
        self._set_status("正在翻译...")
        self.root.update()

        def _cb(result):
            if isinstance(result, TranslateError):
                _post_msg("reply_result", f"[翻译失败] {result}")
            elif result is None:
                _post_msg("reply_result", text)
            else:
                _post_msg("reply_result", result.text)

        self.translator.translate_async(text, target=lang, callback=_cb)

    def _copy_reply_trans(self):
        text = self.reply_result.get()
        if not text:
            return
        try:
            import subprocess
            subprocess.run(["clip"], input=text.encode("utf-16le"), check=True)
            self._set_status("译文已复制")
        except Exception:
            pass

    # ==================== 截图翻译 ====================
    def _screenshot_translate(self):
        self._set_status("截图翻译：请框选区域...")
        try:
            from screenshot import start_screenshot_translate
            start_screenshot_translate(
                self.translator,
                target_lang=self.cfg.translator.target_lang,
                on_result=self._on_screenshot_result,
            )
        except Exception as e:
            messagebox.showerror("截图翻译", f"启动失败：\n{e}\n\n请确保已安装 Tesseract-OCR")

    def _on_screenshot_result(self, original: str, translated: str):
        _post_msg("screenshot_result", (original, translated))

    # ==================== 队列消费 ====================
    def _poll_queue(self):
        try:
            while True:
                kind, payload = _msg_queue.get_nowait()
                if kind == "translate":
                    self._do_translate(payload)
                elif kind == "update_bubble":
                    bubble, translation = payload
                    self._update_bubble_translation(bubble, translation)
                elif kind == "reply_result":
                    self.reply_result.set(payload)
                    self._set_status("翻译完成")
                elif kind == "chat_changed":
                    self._clear_all()
                    self._set_status("检测到聊天切换，已清空")
                elif kind == "screenshot_result":
                    original, translated = payload
                    ScreenshotResultDialog(self.root, original, translated)
                    self._set_status("截图翻译完成")
                elif kind == "status":
                    self._set_status(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    # ==================== 设置 ====================
    def _open_settings(self):
        SettingsDialog(self.root, self.cfg, on_saved=self._apply_settings)

    def _apply_settings(self, new_cfg: AppConfig):
        self.cfg = new_cfg
        was_running = self.running
        if was_running:
            self._stop_reader()
        self.translator.switch_backend(new_cfg.translator)
        if was_running:
            self._start_reader()
        self._set_status("设置已保存并应用")

    # ==================== 关闭 / 托盘 ====================
    def _on_close(self):
        if self.cfg.gui.close_to_tray:
            self._hide_to_tray()
        else:
            self._quit()

    def _hide_to_tray(self):
        self.root.withdraw()

    def _show_from_tray(self):
        self.root.deiconify()
        self.root.lift()
        self._set_status("就绪")

    def _quit(self):
        self._stop_reader()
        self.translator.shutdown()
        try:
            if hasattr(self, "_tray_icon") and self._tray_icon:
                self._tray_icon.stop()
        except Exception:
            pass
        save_settings()
        self.root.destroy()
        sys.exit(0)

    # ==================== 热键 ====================
    def _install_hotkeys(self):
        try:
            import keyboard
            keyboard.add_hotkey(self.cfg.gui.hotkey_show,
                                lambda: _post_msg("status", "热键: 显示窗口"), suppress=False)
            keyboard.add_hotkey(self.cfg.gui.hotkey_screenshot,
                                lambda: self.root.after(0, self._screenshot_translate), suppress=False)
            self._set_status(f"热键: {self.cfg.gui.hotkey_show} 显示 | {self.cfg.gui.hotkey_screenshot} 截图")
        except Exception as e:
            log.info("全局热键注册失败: %s", e)

    # ==================== 托盘 ====================
    def _setup_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw

            def make_icon():
                img = Image.new("RGBA", (64, 64), (31, 58, 147, 255))
                d = ImageDraw.Draw(img)
                d.rectangle((8, 8, 56, 56), fill=(255, 255, 255, 255))
                d.text((18, 18), "译", fill=(31, 58, 147, 255))
                return img

            def on_click(icon, item):
                s = str(item)
                if s == "显示主窗口":
                    self.root.after(0, self._show_from_tray)
                elif s == "截图翻译":
                    self.root.after(0, self._screenshot_translate)
                elif s == "暂停监听":
                    self.root.after(0, self._stop_reader)
                elif s == "开始监听":
                    self.root.after(0, self._start_reader)
                elif s == "退出":
                    self.root.after(0, self._quit)

            menu = pystray.Menu(
                pystray.MenuItem("显示主窗口", on_click),
                pystray.MenuItem("截图翻译", on_click),
                pystray.MenuItem("开始监听", on_click),
                pystray.MenuItem("暂停监听", on_click),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", on_click),
            )
            self._tray_icon = pystray.Icon("whatsapp_translator", make_icon(),
                                           "WhatsApp 翻译助手", menu)
            threading.Thread(target=self._tray_icon.run, daemon=True).start()
        except Exception as e:
            log.info("托盘初始化失败: %s", e)
            self._tray_icon = None

    def _set_status(self, text: str):
        self.status.set(f"[{time.strftime('%H:%M:%S')}] {text}")


# ===========================================================================
# 设置对话框
# ===========================================================================
class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, cfg: AppConfig, on_saved):
        super().__init__(parent)
        self.title("设置")
        self.cfg = cfg
        self.on_saved = on_saved
        self.resizable(True, True)
        self.grab_set()
        self.geometry("520x680")

        # 使用 Notebook 分页
        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self._build_translate_tab(nb, cfg)
        self._build_reader_tab(nb, cfg)
        self._build_appearance_tab(nb, cfg)
        self._build_hotkey_tab(nb, cfg)

        # 底部按钮
        btn = ttk.Frame(self)
        btn.pack(fill=tk.X, padx=8, pady=(0, 8))
        ttk.Button(btn, text="保存", command=self._save).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn, text="取消", command=self.destroy).pack(side=tk.RIGHT, padx=4)

    def _build_translate_tab(self, nb, cfg):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="翻译")
        pad = {"padx": 6, "pady": 4}

        ttk.Label(f, text="翻译后端").grid(row=0, column=0, sticky=tk.W, **pad)
        self.var_backend = tk.StringVar(value=cfg.translator.backend)
        ttk.Combobox(f, textvariable=self.var_backend, state="readonly",
                     values=("google", "doubao"), width=18).grid(row=0, column=1, **pad)

        ttk.Label(f, text="自动翻译目标语言").grid(row=1, column=0, sticky=tk.W, **pad)
        self.var_target = tk.StringVar(value=cfg.translator.target_lang)
        ttk.Entry(f, textvariable=self.var_target, width=20).grid(row=1, column=1, **pad)

        ttk.Label(f, text="源语言").grid(row=2, column=0, sticky=tk.W, **pad)
        self.var_source = tk.StringVar(value=cfg.translator.source_lang)
        ttk.Entry(f, textvariable=self.var_source, width=20).grid(row=2, column=1, **pad)

        ttk.Separator(f, orient=tk.HORIZONTAL).grid(row=3, column=0, columnspan=2, sticky=tk.EW, pady=8)
        ttk.Label(f, text="--- 豆包配置 ---", font=("", 9, "bold")).grid(row=4, column=0, columnspan=2, **pad)

        ttk.Label(f, text="API Key").grid(row=5, column=0, sticky=tk.W, **pad)
        self.var_doubao_key = tk.StringVar(value=cfg.translator.doubao_api_key)
        ttk.Entry(f, textvariable=self.var_doubao_key, width=40, show="*").grid(row=5, column=1, **pad)

        ttk.Label(f, text="模型").grid(row=6, column=0, sticky=tk.W, **pad)
        self.var_doubao_model = tk.StringVar(value=cfg.translator.doubao_model)
        ttk.Entry(f, textvariable=self.var_doubao_model, width=40).grid(row=6, column=1, **pad)

        ttk.Label(f, text="Endpoint").grid(row=7, column=0, sticky=tk.W, **pad)
        self.var_doubao_ep = tk.StringVar(value=cfg.translator.doubao_endpoint)
        ttk.Entry(f, textvariable=self.var_doubao_ep, width=40).grid(row=7, column=1, **pad)

    def _build_reader_tab(self, nb, cfg):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="消息读取")
        pad = {"padx": 6, "pady": 4}

        ttk.Label(f, text="轮询间隔（秒）").grid(row=0, column=0, sticky=tk.W, **pad)
        self.var_interval = tk.StringVar(value=str(cfg.reader.poll_interval))
        ttk.Entry(f, textvariable=self.var_interval, width=10).grid(row=0, column=1, **pad)

        ttk.Label(f, text="（建议 0.5~2.0，越小越实时但 CPU 越高）").grid(row=1, column=0, columnspan=2, sticky=tk.W, **pad)

        self.var_only_in = tk.BooleanVar(value=cfg.reader.only_incoming)
        ttk.Checkbutton(f, text="只翻译收到的消息（不翻译自己发的）",
                        variable=self.var_only_in).grid(row=2, column=0, columnspan=2, sticky=tk.W, **pad)

        self.var_translate_hist = tk.BooleanVar(value=cfg.reader.translate_history_on_start)
        ttk.Checkbutton(f, text="打开聊天时翻译已有历史消息",
                        variable=self.var_translate_hist).grid(row=3, column=0, columnspan=2, sticky=tk.W, **pad)

        ttk.Label(f, text="进程名").grid(row=4, column=0, sticky=tk.W, **pad)
        self.var_proc = tk.StringVar(value=cfg.reader.process_name)
        ttk.Entry(f, textvariable=self.var_proc, width=24).grid(row=4, column=1, **pad)

    def _build_appearance_tab(self, nb, cfg):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="外观")
        pad = {"padx": 6, "pady": 4}
        g = cfg.gui

        ttk.Label(f, text="收到消息气泡颜色").grid(row=0, column=0, sticky=tk.W, **pad)
        self.var_bubble_in = tk.StringVar(value=g.bubble_in_color)
        ttk.Entry(f, textvariable=self.var_bubble_in, width=12).grid(row=0, column=1, **pad)

        ttk.Label(f, text="发出消息气泡颜色").grid(row=1, column=0, sticky=tk.W, **pad)
        self.var_bubble_out = tk.StringVar(value=g.bubble_out_color)
        ttk.Entry(f, textvariable=self.var_bubble_out, width=12).grid(row=1, column=1, **pad)

        ttk.Label(f, text="聊天背景颜色").grid(row=2, column=0, sticky=tk.W, **pad)
        self.var_chat_bg = tk.StringVar(value=g.chat_bg_color)
        ttk.Entry(f, textvariable=self.var_chat_bg, width=12).grid(row=2, column=1, **pad)

        ttk.Label(f, text="译文文字颜色").grid(row=3, column=0, sticky=tk.W, **pad)
        self.var_trans_text = tk.StringVar(value=g.bubble_trans_text)
        ttk.Entry(f, textvariable=self.var_trans_text, width=12).grid(row=3, column=1, **pad)

        ttk.Label(f, text="字体").grid(row=4, column=0, sticky=tk.W, **pad)
        self.var_font = tk.StringVar(value=g.font_family)
        ttk.Entry(f, textvariable=self.var_font, width=24).grid(row=4, column=1, **pad)

        ttk.Label(f, text="字号").grid(row=5, column=0, sticky=tk.W, **pad)
        self.var_font_size = tk.StringVar(value=str(g.font_size))
        ttk.Entry(f, textvariable=self.var_font_size, width=6).grid(row=5, column=1, **pad)

        ttk.Label(f, text="提示: 颜色用 HEX 格式，如 #95EC69").grid(row=6, column=0, columnspan=2, **pad)

        self.var_close_tray = tk.BooleanVar(value=g.close_to_tray)
        ttk.Checkbutton(f, text="关闭窗口时最小化到托盘",
                        variable=self.var_close_tray).grid(row=7, column=0, columnspan=2, **pad)

    def _build_hotkey_tab(self, nb, cfg):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="热键")
        pad = {"padx": 6, "pady": 4}
        g = cfg.gui

        ttk.Label(f, text="显示主窗口").grid(row=0, column=0, sticky=tk.W, **pad)
        self.var_hotkey_show = tk.StringVar(value=g.hotkey_show)
        ttk.Entry(f, textvariable=self.var_hotkey_show, width=20).grid(row=0, column=1, **pad)

        ttk.Label(f, text="截图翻译").grid(row=1, column=0, sticky=tk.W, **pad)
        self.var_hotkey_ss = tk.StringVar(value=g.hotkey_screenshot)
        ttk.Entry(f, textvariable=self.var_hotkey_ss, width=20).grid(row=1, column=1, **pad)

        ttk.Label(f, text="提示: 格式如 ctrl+alt+s").grid(row=2, column=0, columnspan=2, **pad)

    def _save(self):
        try:
            c = self.cfg
            c.translator.backend = self.var_backend.get().strip() or "google"
            c.translator.target_lang = self.var_target.get().strip() or "zh-CN"
            c.translator.source_lang = self.var_source.get().strip() or "auto"
            c.translator.doubao_api_key = self.var_doubao_key.get().strip()
            c.translator.doubao_model = self.var_doubao_model.get().strip() or "doubao-seed-1-6-250615"
            c.translator.doubao_endpoint = self.var_doubao_ep.get().strip()

            c.reader.poll_interval = float(self.var_interval.get() or "0.8")
            c.reader.only_incoming = bool(self.var_only_in.get())
            c.reader.translate_history_on_start = bool(self.var_translate_hist.get())
            c.reader.process_name = self.var_proc.get().strip() or "WhatsApp.exe"

            c.gui.bubble_in_color = self.var_bubble_in.get().strip() or "#FFFFFF"
            c.gui.bubble_out_color = self.var_bubble_out.get().strip() or "#95EC69"
            c.gui.chat_bg_color = self.var_chat_bg.get().strip() or "#EDEDED"
            c.gui.bubble_trans_text = self.var_trans_text.get().strip() or "#666666"
            c.gui.font_family = self.var_font.get().strip() or "Microsoft YaHei"
            c.gui.font_size = int(self.var_font_size.get() or "11")
            c.gui.close_to_tray = bool(self.var_close_tray.get())

            c.gui.hotkey_show = self.var_hotkey_show.get().strip() or "ctrl+alt+t"
            c.gui.hotkey_screenshot = self.var_hotkey_ss.get().strip() or "ctrl+alt+s"

            c.save()
            self.on_saved(c)
            self.destroy()
        except ValueError as e:
            messagebox.showerror("参数错误", f"请检查输入：{e}", parent=self)
