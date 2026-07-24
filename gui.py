"""
gui.py
微信式对话气泡界面 —— 完整重写版 v3

核心特性：
1. 顶部工具栏：源语言下拉框（默认自动检测）、目标语言下拉框、开始/暂停按钮、截图翻译
2. 聊天气泡：对方消息左白右绿，每条下方译文灰色小字
3. 实时翻译：切换聊天自动清空并重新翻译
4. 保持顺序：按 Y 坐标排序，不打乱消息顺序
5. 底部回复翻译区：输入中文 → 选语言 → 翻译 → 复制
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Dict, List, Optional

from translator import Translator, TranslateError, get_language_list
from whatsapp_reader import WhatsAppMessage, WhatsAppReader

log = logging.getLogger(__name__)

# 全局消息队列：子线程 → GUI 线程
_msg_queue: "queue.Queue[tuple]" = queue.Queue()


def _post_msg(kind: str, payload):
    _msg_queue.put((kind, payload))


# ============================================================================
# 聊天气泡组件
# ============================================================================
class ChatBubble(tk.Frame):
    """单个聊天气泡。"""

    def __init__(self, parent, text: str, translation: str, direction: str,
                 colors: dict, font_family: str = "Microsoft YaHei",
                 font_size: int = 11, on_copy=None):
        super().__init__(parent, bg=colors["chat_bg"])
        self._on_copy = on_copy
        is_in = direction == "in"

        bubble_bg = colors["bubble_in"] if is_in else colors["bubble_out"]
        text_fg = colors["text_in"] if is_in else colors["text_out"]
        trans_fg = colors["trans_text"]

        max_width = 460

        # 气泡外框（用于对齐）
        self.outer = tk.Frame(self, bg=colors["chat_bg"])
        self.outer.pack(fill=tk.X)

        # 气泡主体
        bubble = tk.Frame(self.outer, bg=bubble_bg, padx=12, pady=8)

        # 原文
        lbl_orig = tk.Label(
            bubble, text=text, font=(font_family, font_size),
            fg=text_fg, bg=bubble_bg, justify=tk.LEFT,
            wraplength=max_width, anchor=tk.W,
        )
        lbl_orig.pack(fill=tk.X, anchor=tk.W)

        # 译文
        if translation and translation.strip():
            sep = tk.Frame(bubble, bg=colors["trans_line"], height=1)
            sep.pack(fill=tk.X, pady=(6, 3))
            self.lbl_trans = tk.Label(
                bubble, text=translation,
                font=(font_family, font_size - 1),
                fg=trans_fg, bg=bubble_bg, justify=tk.LEFT,
                wraplength=max_width, anchor=tk.W,
            )
            self.lbl_trans.pack(fill=tk.X, anchor=tk.W)
        else:
            self.lbl_trans = None

        # 复制按钮
        if on_copy:
            lbl_copy = tk.Label(
                bubble, text="📋 复制", font=(font_family, 8),
                fg="#999999", bg=bubble_bg, cursor="hand2",
            )
            lbl_copy.pack(anchor=tk.E, pady=(4, 0))
            lbl_copy.bind("<Button-1>", lambda e: on_copy(text))

        # 时间
        time_str = time.strftime("%H:%M")
        lbl_time = tk.Label(
            self.outer, text=time_str, font=(font_family, 8),
            fg="#999999", bg=colors["chat_bg"],
        )

        # 布局：对方消息（左），自己消息（右）
        if is_in:
            bubble.pack(side=tk.LEFT, anchor=tk.W)
            lbl_time.pack(side=tk.LEFT, padx=(4, 0), anchor=tk.S)
        else:
            bubble.pack(side=tk.RIGHT, anchor=tk.E)
            lbl_time.pack(side=tk.RIGHT, padx=(0, 4), anchor=tk.S)

        self.outer.pack(fill=tk.X, pady=2)

    def update_translation(self, translation: str, colors: dict,
                           font_family: str, font_size: int):
        """更新译文（首次翻译完成后调用）。"""
        if self.lbl_trans is None:
            return
        self.lbl_trans.configure(text=translation)


# ============================================================================
# 截图翻译结果弹窗
# ============================================================================
class ScreenshotResultDialog(tk.Toplevel):
    def __init__(self, parent, original: str, translated: str):
        super().__init__(parent)
        self.title("截图翻译结果")
        self.geometry("560x400")
        self.translated = translated
        self.original = original
        self._build()

    def _build(self):
        f = ttk.Frame(self, padding=12)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text="识别原文：", font=("", 10, "bold")).pack(anchor=tk.W)
        txt_orig = tk.Text(f, height=6, wrap=tk.WORD, font=("Microsoft YaHei", 10))
        txt_orig.pack(fill=tk.X, pady=(2, 8))
        txt_orig.insert("1.0", self.original)
        txt_orig.configure(state=tk.DISABLED)

        ttk.Label(f, text="译文：", font=("", 10, "bold")).pack(anchor=tk.W)
        txt_trans = tk.Text(f, height=8, wrap=tk.WORD, font=("Microsoft YaHei", 11))
        txt_trans.pack(fill=tk.X, pady=(2, 8))
        txt_trans.insert("1.0", self.translated)
        txt_trans.configure(state=tk.DISABLED)

        btn_frame = ttk.Frame(f)
        btn_frame.pack(fill=tk.X)
        ttk.Button(btn_frame, text="复制译文", command=self._copy_trans).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_frame, text="复制原文", command=self._copy_orig).pack(side=tk.RIGHT)
        ttk.Button(btn_frame, text="关闭", command=self.destroy).pack(side=tk.LEFT)

    def _copy_to_clipboard(text):
        try:
            import subprocess
            subprocess.run(["clip"], input=text.encode("utf-16le"), check=True)
        except Exception:
            pass

    def _copy_trans(self):
        self._copy_to_clipboard(self.translated)

    def _copy_orig(self):
        self._copy_to_clipboard(self.original)


# ============================================================================
# 主应用
# ============================================================================
class TranslatorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.translator = Translator(target_lang="zh-CN", source_lang="auto")
        self.reader: Optional[WhatsAppReader] = None
        self.running = False

        # 消息存储：fingerprint -> (bubble_widget, message)
        self._msg_map: Dict[str, ChatBubble] = {}
        self._msg_order: List[str] = []  # 按顺序的 fingerprint 列表

        self._colors = self._default_colors()
        self._font_family = "Microsoft YaHei"
        self._font_size = 11

        self._build_ui()
        self._poll_queue()
        self._install_hotkeys()
        self._setup_tray()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -------------------- 默认颜色 --------------------
    @staticmethod
    def _default_colors() -> dict:
        return {
            "chat_bg": "#F5F5F5",
            "bubble_in": "#FFFFFF",
            "bubble_out": "#95EC69",
            "text_in": "#333333",
            "text_out": "#333333",
            "trans_text": "#888888",
            "trans_line": "#E8E8E8",
        }

    # -------------------- UI 构建 --------------------
    def _build_ui(self):
        self.root.title("WhatsApp 翻译助手")
        self.root.geometry("960x720")
        self.root.minsize(720, 540)

        # ===== 顶部工具栏 =====
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(side=tk.TOP, fill=tk.X)

        # 源语言
        ttk.Label(bar, text="源语言:").pack(side=tk.LEFT, padx=(0, 4))
        self.var_src_lang = tk.StringVar(value="自动检测 (auto)")
        src_values = [f"{name} ({code})" for code, name in get_language_list()]
        self.cb_src = ttk.Combobox(bar, textvariable=self.var_src_lang,
                                   values=src_values, state="readonly", width=16)
        self.cb_src.pack(side=tk.LEFT, padx=(0, 12))

        # 目标语言
        ttk.Label(bar, text="目标语言:").pack(side=tk.LEFT, padx=(0, 4))
        self.var_tgt_lang = tk.StringVar(value="中文（简体）(zh-CN)")
        tgt_values = [f"{name} ({code})" for code, name in get_language_list() if code != "auto"]
        self.cb_tgt = ttk.Combobox(bar, textvariable=self.var_tgt_lang,
                                     values=tgt_values, state="readonly", width=18)
        self.cb_tgt.pack(side=tk.LEFT, padx=(0, 12))
        self.cb_tgt.bind("<<ComboboxSelected>>", self._on_tgt_lang_changed)
        self.cb_src.bind("<<ComboboxSelected>>", self._on_src_lang_changed)

        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        # 开始/暂停
        self.btn_toggle = ttk.Button(bar, text="▶ 开始监听", width=12, command=self._toggle_reader)
        self.btn_toggle.pack(side=tk.LEFT)

        # 刷新
        ttk.Button(bar, text="🔄 刷新翻译", width=10, command=self._refresh_translations).pack(side=tk.LEFT, padx=(4, 0))

        # 清空
        ttk.Button(bar, text="🧹 清空", width=8, command=self._clear_all).pack(side=tk.LEFT, padx=(4, 0))

        # 截图翻译
        ttk.Button(bar, text="📸 截图翻译", width=12, command=self._screenshot_translate).pack(side=tk.LEFT, padx=(4, 0))

        # 设置
        ttk.Button(bar, text="⚙ 设置", width=8, command=self._open_settings).pack(side=tk.LEFT, padx=(4, 0))

        # 退出
        ttk.Button(bar, text="⏏ 退出", width=8, command=self._quit).pack(side=tk.RIGHT)

        # 聊天标题栏
        self.title_bar = tk.Frame(self.root, bg="#FFFFFF", height=36)
        self.title_bar.pack(fill=tk.X)
        self.title_bar.pack_propagate(False)
        self.lbl_chat_title = tk.Label(
            self.title_bar, text="未开始监听",
            font=("Microsoft YaHei", 11, "bold"),
            bg="#FFFFFF", fg="#333333",
        )
        self.lbl_chat_title.pack(side=tk.LEFT, padx=12, pady=6)

        # ===== 聊天区域（Canvas + Scrollbar） =====
        chat_frame = tk.Frame(self.root, bg=self._colors["chat_bg"])
        chat_frame.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(chat_frame, bg=self._colors["chat_bg"],
                                 highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(chat_frame, orient=tk.VERTICAL, command=self.canvas.yview)
        self.scroll_frame = tk.Frame(self.canvas, bg=self._colors["chat_bg"])

        self.scroll_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._canvas_window = self.canvas.create_window((0, 0), window=self.scroll_frame, anchor=tk.NW)

        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 鼠标滚轮
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)

        # ===== 底部回复翻译区 =====
        reply_frame = ttk.LabelFrame(self.root, text="翻译输入", padding=(8, 6))
        reply_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(4, 0))

        row1 = ttk.Frame(reply_frame)
        row1.pack(fill=tk.X, pady=(0, 4))

        ttk.Label(row1, text="目标语言:").pack(side=tk.LEFT)
        self.var_reply_lang = tk.StringVar(value="英语 (en)")
        reply_values = [f"{name} ({code})" for code, name in get_language_list() if code != "auto"]
        self.cb_reply_lang = ttk.Combobox(row1, textvariable=self.var_reply_lang,
                                          values=reply_values, state="readonly", width=22)
        self.cb_reply_lang.pack(side=tk.LEFT, padx=(4, 8))

        ttk.Button(row1, text="🔍 翻译", command=self._translate_reply).pack(side=tk.LEFT)
        ttk.Button(row1, text="📋 复制译文", command=self._copy_reply_trans).pack(side=tk.RIGHT)

        self.reply_text = tk.Text(reply_frame, height=3,
                                   font=(self._font_family, self._font_size),
                                   wrap=tk.WORD)
        self.reply_text.pack(fill=tk.X, pady=(0, 4))

        out_frame = ttk.Frame(reply_frame)
        out_frame.pack(fill=tk.X)
        ttk.Label(out_frame, text="译文:").pack(side=tk.LEFT)
        self.reply_result = tk.StringVar(value="")
        self.lbl_reply_result = tk.Label(
            out_frame, textvariable=self.reply_result,
            font=(self._font_family, self._font_size), fg="#333333",
            anchor=tk.W, wraplength=700, justify=tk.LEFT,
        )
        self.lbl_reply_result.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        status_bar = ttk.Label(self.root, textvariable=self.status_var,
                               anchor=tk.W, padding=(8, 3), relief=tk.SUNKEN)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self._canvas_window, width=event.width)

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _set_status(self, text: str):
        self.status_var.set(f"[{time.strftime('%H:%M:%S')}] {text}")

    # -------------------- 语言切换 --------------------
    def _parse_lang_code(self, display: str) -> str:
        # "中文（简体）(zh-CN)" → "zh-CN"
        if "(" in display and ")" in display:
            return display.rsplit("(", 1)[1].rstrip(")")
        return display

    def _on_src_lang_changed(self, event=None):
        code = self._parse_lang_code(self.var_src_lang.get())
        self.translator.set_source(code)
        self._set_status(f"源语言已切换为 {code}")
        # 重新翻译所有消息
        if self.running:
            self._refresh_translations()

    def _on_tgt_lang_changed(self, event=None):
        code = self._parse_lang_code(self.var_tgt_lang.get())
        self.translator.set_target(code)
        self._set_status(f"目标语言已切换为 {code}")
        # 重新翻译所有消息
        if self.running:
            self._refresh_translations()

    def _refresh_translations(self):
        """重新翻译所有消息（切换语言时调用）。"""
        # 暂时没消息，等待下次扫描时自动翻译。
        # 清空缓存后重新添加。
        self._clear_all(keep_reader=True)
        self._set_status("正在重新翻译...")

    # -------------------- 消息监听 --------------------
    def _toggle_reader(self):
        if self.running:
            self._stop_reader()
        else:
            self._start_reader()

    def _start_reader(self):
        if self.running:
            return
        try:
            from config import ReaderConfig
            cfg = ReaderConfig(
                poll_interval=0.6,
                max_history=1000,
                min_length=1,
            )
            self.reader = WhatsAppReader(
                cfg,
                on_messages=self._on_messages,
                on_chat_changed=self._on_chat_changed,
            )
        except RuntimeError as e:
            messagebox.showerror("不支持的平台", str(e))
            return
        self.reader.start()
        self.running = True
        self.btn_toggle.configure(text="⏸ 暂停监听")
        self._set_status("正在监听 WhatsApp...")

    def _stop_reader(self):
        if not self.running:
            return
        if self.reader:
            self.reader.stop()
        self.running = False
        self.btn_toggle.configure(text="▶ 开始监听")
        self._set_status("已暂停")

    def _clear_all(self, keep_reader: bool = False):
        for w in self.scroll_frame.winfo_children():
            w.destroy()
        self._msg_map.clear()
        self._msg_order.clear()
        if not keep_reader and self.reader:
            pass  # 不重置 reader，下次扫描会重新获取
        self._set_status("已清空")

    # -------------------- Reader 回调（子线程） --------------------
    def _on_messages(self, messages: List[WhatsAppMessage]):
        _post_msg("messages", messages)

    def _on_chat_changed(self, title: str):
        _post_msg("chat_changed", title)

    # -------------------- 队列消费（GUI 线程） --------------------
    def _poll_queue(self):
        try:
            while True:
                kind, payload = _msg_queue.get_nowait()
                if kind == "messages":
                    self._handle_messages(payload)
                elif kind == "chat_changed":
                    self._handle_chat_changed(payload)
                elif kind == "update_bubble":
                    fp, translation = payload
                    self._update_bubble(fp, translation)
                elif kind == "reply_result":
                    self.reply_result.set(payload)
                    self._set_status("翻译完成")
                elif kind == "screenshot_result":
                    orig, trans = payload
                    ScreenshotResultDialog(self.root, orig, trans)
                    self._set_status("截图翻译完成")
                elif kind == "status":
                    self._set_status(payload)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    def _handle_chat_changed(self, title: str):
        """切换聊天时清空并更新标题。"""
        self.lbl_chat_title.configure(text=f"💬 {title}")
        self._clear_all(keep_reader=True)
        self._set_status(f"切换到聊天: {title}")

    def _handle_messages(self, messages: List[WhatsAppMessage]):
        """处理一批消息（全量）。"""
        if not messages:
            return

        # 找出新消息
        new_fps = []
        for msg in messages:
            fp = msg.fingerprint()
            if fp not in self._msg_map:
                new_fps.append((fp, msg))

        # 按 Y 排序（已在 reader 端排好）

        # 重建整个列表（保持顺序）
        current_fps = [m.fingerprint() for m in messages]

        # 检查顺序变化了吗？
        if current_fps == self._msg_order:
            # 顺序没变，只加新消息
            for fp, msg in new_fps:
                self._add_bubble(msg, fp)
        else:
            # 顺序变了（滚动导致），重建
            self._rebuild_all(messages)

        self._msg_order = current_fps

    def _rebuild_all(self, messages: List[WhatsAppMessage]):
        """重建全部气泡（滚动导致顺序变化时）。"""
        # 保存已有译文
        old_trans = {}
        for fp in self._msg_order:
            if fp in self._msg_map:
                # 译文在气泡里，我们没法直接取，所以清空重建
                pass

        # 清空
        for w in self.scroll_frame.winfo_children():
            w.destroy()
        self._msg_map.clear()
        self._msg_order.clear()

        # 重新添加
        for msg in messages:
            fp = msg.fingerprint()
            self._add_bubble(msg, fp)

    def _add_bubble(self, msg: WhatsAppMessage, fp: str):
        """添加一个新气泡，并启动异步翻译。"""
        bubble = ChatBubble(
            self.scroll_frame, msg.text, "", msg.direction,
            self._colors, self._font_family, self._font_size,
            on_copy=self._copy_text,
        )
        bubble.pack(fill=tk.X, padx=10, pady=2)
        self._msg_map[fp] = bubble

        # 异步翻译
        def _cb(result):
            if isinstance(result, Exception):
                trans = f"[翻译失败] {result}"
            elif result is None:
                trans = ""
            else:
                trans = result.text
            _post_msg("update_bubble", (fp, trans))

        self.translator.translate_async(
            msg.text,
            target=self.translator.target_lang,
            source=self.translator.source_lang,
            callback=_cb,
        )

        # 自动滚到底
        self.root.after(10, self._scroll_to_bottom)

    def _update_bubble(self, fp: str, translation: str):
        """更新气泡译文。"""
        bubble = self._msg_map.get(fp)
        if bubble and bubble.lbl_trans:
            bubble.lbl_trans.configure(text=translation)

    def _scroll_to_bottom(self):
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)

    @staticmethod
    def _copy_text(text: str):
        try:
            import subprocess
            subprocess.run(["clip"], input=text.encode("utf-16le"), check=True)
        except Exception:
            pass

    # -------------------- 底部回复翻译 --------------------
    def _translate_reply(self):
        text = self.reply_text.get("1.0", tk.END).strip()
        if not text:
            return
        lang_display = self.var_reply_lang.get()
        lang = self._parse_lang_code(lang_display)
        self._set_status("正在翻译...")

        def _cb(result):
            if isinstance(result, Exception):
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

    # -------------------- 截图翻译 --------------------
    def _screenshot_translate(self):
        self._set_status("截图翻译：请框选区域...")
        self.root.withdraw()  # 隐藏主窗口
        self.root.after(200, self._do_screenshot)

    def _do_screenshot(self):
        try:
            from screenshot import start_screenshot_translate
            tgt = self._parse_lang_code(self.var_tgt_lang.get())
            start_screenshot_translate(
                self.translator,
                target_lang=tgt,
                on_result=self._on_screenshot_done,
            )
        except Exception as e:
            self.root.deiconify()
            messagebox.showerror("截图翻译", f"启动失败：\n{e}")

    def _on_screenshot_done(self, original: str, translated: str):
        self.root.deiconify()
        self.root.lift()
        _post_msg("screenshot_result", (original, translated))

    # -------------------- 设置 --------------------
    def _open_settings(self):
        messagebox.showinfo("设置", "设置功能开发中...\n\n"
                                 "当前可通过顶部下拉框切换语言。")

    # -------------------- 关闭 / 托盘 --------------------
    def _on_close(self):
        self._hide_to_tray()

    def _hide_to_tray(self):
        self.root.withdraw()

    def _show_from_tray(self):
        self.root.deiconify()
        self.root.lift()

    def _quit(self):
        self._stop_reader()
        self.translator.shutdown()
        try:
            if hasattr(self, "_tray_icon") and self._tray_icon:
                self._tray_icon.stop()
        except Exception:
            pass
        self.root.destroy()
        sys.exit(0)

    # -------------------- 热键 --------------------
    def _install_hotkeys(self):
        try:
            import keyboard
            keyboard.add_hotkey("ctrl+alt+t",
                                lambda: self.root.after(0, self._show_from_tray))
            keyboard.add_hotkey("ctrl+alt+s",
                                lambda: self.root.after(0, self._screenshot_translate))
        except Exception as e:
            log.info("全局热键注册失败: %s", e)

    # -------------------- 托盘 --------------------
    def _setup_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw

            def make_icon():
                img = Image.new("RGBA", (64, 64), (31, 58, 147, 255))
                d = ImageDraw.Draw(img)
                d.rectangle((8, 8, 56, 56), fill=(255, 255, 255, 255))
                d.text((20, 18), "译", fill=(31, 58, 147, 255))
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
            self._tray_icon = pystray.Icon(
                "whatsapp_translator", make_icon(),
                "WhatsApp 翻译助手", menu,
            )
            threading.Thread(target=self._tray_icon.run, daemon=True).start()
        except Exception as e:
            log.info("托盘初始化失败: %s", e)
            self._tray_icon = None
