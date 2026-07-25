"""
gui.py
微信式对话气泡界面 —— v5

核心改进：
- 可见调试日志面板（底部折叠面板，显示 Reader 运行状态）
- 连接 on_debug 回调，实时显示窗口查找/消息提取状态
- 截图翻译在主线程运行
- 底部文本框自动翻译（KeyRelease + 防抖）
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

_msg_queue: "queue.Queue[tuple]" = queue.Queue()


def _post(kind: str, payload):
    _msg_queue.put((kind, payload))


# ============================================================================
# 聊天气泡
# ============================================================================
class ChatBubble(tk.Frame):
    def __init__(self, parent, text: str, direction: str, colors: dict,
                 font_family="Microsoft YaHei", font_size=11, on_copy=None):
        super().__init__(parent, bg=colors["chat_bg"])
        self._colors = colors
        self._font_family = font_family
        self._font_size = font_size
        is_in = direction == "in"

        bubble_bg = colors["bubble_in"] if is_in else colors["bubble_out"]
        text_fg = colors["text_in"] if is_in else colors["text_out"]

        row = tk.Frame(self, bg=colors["chat_bg"])
        row.pack(fill=tk.X, padx=8, pady=3)

        bubble = tk.Frame(row, bg=bubble_bg, padx=12, pady=8)

        self._bubble_bg = bubble_bg
        self._lbl_orig = tk.Label(
            bubble, text=text, font=(font_family, font_size),
            fg=text_fg, bg=bubble_bg, justify=tk.LEFT,
            wraplength=460, anchor=tk.W,
        )
        self._lbl_orig.pack(fill=tk.X, anchor=tk.W)

        self._trans_frame = tk.Frame(bubble, bg=bubble_bg)
        self._trans_frame.pack(fill=tk.X, anchor=tk.W)
        self._lbl_trans = None

        if on_copy:
            lbl_copy = tk.Label(
                bubble, text="复制", font=(font_family, 8),
                fg="#999999", bg=bubble_bg, cursor="hand2",
            )
            lbl_copy.pack(anchor=tk.E, pady=(2, 0))
            lbl_copy.bind("<Button-1>", lambda e: on_copy(text))

        time_str = time.strftime("%H:%M")
        lbl_time = tk.Label(row, text=time_str, font=(font_family, 8),
                            fg="#999999", bg=colors["chat_bg"])

        if is_in:
            bubble.pack(side=tk.LEFT, anchor=tk.W)
            lbl_time.pack(side=tk.LEFT, padx=(4, 0), anchor=tk.S)
        else:
            bubble.pack(side=tk.RIGHT, anchor=tk.E)
            lbl_time.pack(side=tk.RIGHT, padx=(0, 4), anchor=tk.S)

    def set_translation(self, text: str):
        if self._lbl_trans is not None:
            self._lbl_trans.destroy()
            self._lbl_trans = None
        if not text or not text.strip():
            return
        sep = tk.Frame(self._trans_frame, bg=self._bubble_bg, height=1)
        sep.pack(fill=tk.X, pady=(6, 3))
        self._lbl_trans = tk.Label(
            self._trans_frame, text=text,
            font=(self._font_family, self._font_size - 1),
            fg=self._colors["trans_text"], bg=self._bubble_bg,
            justify=tk.LEFT, wraplength=460, anchor=tk.W,
        )
        self._lbl_trans.pack(fill=tk.X, anchor=tk.W)


# ============================================================================
# 主应用
# ============================================================================
class TranslatorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.translator = Translator(target_lang="zh-CN", source_lang="auto")
        self.reader: Optional[WhatsAppReader] = None
        self.running = False

        self._msg_map: Dict[str, ChatBubble] = {}
        self._last_fp_set: set = set()

        self._colors = {
            "chat_bg": "#F5F5F5",
            "bubble_in": "#FFFFFF",
            "bubble_out": "#95EC69",
            "text_in": "#333333",
            "text_out": "#333333",
            "trans_text": "#888888",
            "trans_line": "#E0E0E0",
        }
        self._font_family = "Microsoft YaHei"
        self._font_size = 11
        self._reply_timer = None
        self._debug_lines: List[str] = []

        self._build_ui()
        self._poll_queue()
        self._install_hotkeys()
        self._setup_tray()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ==================== UI ====================
    def _build_ui(self):
        self.root.title("WhatsApp 翻译助手 v6")
        self.root.geometry("960x780")
        self.root.minsize(720, 600)

        # ===== 顶部工具栏（固定） =====
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(bar, text="源语言:").pack(side=tk.LEFT, padx=(0, 4))
        self.var_src = tk.StringVar(value="自动检测 (auto)")
        src_vals = [f"{n} ({c})" for c, n in get_language_list()]
        self.cb_src = ttk.Combobox(bar, textvariable=self.var_src, values=src_vals,
                                    state="readonly", width=16)
        self.cb_src.pack(side=tk.LEFT, padx=(0, 12))
        self.cb_src.bind("<<ComboboxSelected>>", self._on_src_changed)

        ttk.Label(bar, text="目标语言:").pack(side=tk.LEFT, padx=(0, 4))
        self.var_tgt = tk.StringVar(value="中文（简体）(zh-CN)")
        tgt_vals = [f"{n} ({c})" for c, n in get_language_list() if c != "auto"]
        self.cb_tgt = ttk.Combobox(bar, textvariable=self.var_tgt, values=tgt_vals,
                                    state="readonly", width=18)
        self.cb_tgt.pack(side=tk.LEFT, padx=(0, 12))
        self.cb_tgt.bind("<<ComboboxSelected>>", self._on_tgt_changed)

        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        self.btn_toggle = ttk.Button(bar, text="开始监听", width=10, command=self._toggle)
        self.btn_toggle.pack(side=tk.LEFT)
        ttk.Button(bar, text="探测控件", width=10, command=self._probe).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(bar, text="清空", width=6, command=self._clear).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(bar, text="截图翻译", width=10, command=self._screenshot).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(bar, text="设置", width=6, command=self._open_settings).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(bar, text="退出", width=6, command=self._quit).pack(side=tk.RIGHT)

        # ===== 主内容区：垂直 PanedWindow =====
        # 上：聊天区域    中：翻译输入+译文    下：调试日志
        main_pane = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        main_pane.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # ---------- 第1格：聊天区域 ----------
        chat_container = ttk.Frame(main_pane)
        main_pane.add(chat_container, weight=5)

        # 聊天标题栏
        title_bar = tk.Frame(chat_container, bg="#FFFFFF", height=30)
        title_bar.pack(fill=tk.X)
        title_bar.pack_propagate(False)
        self.lbl_title = tk.Label(title_bar, text="未开始监听",
                                   font=("Microsoft YaHei", 11, "bold"),
                                   bg="#FFFFFF", fg="#333333")
        self.lbl_title.pack(side=tk.LEFT, padx=12, pady=4)

        # 聊天内容
        chat_frame = tk.Frame(chat_container, bg=self._colors["chat_bg"])
        chat_frame.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(chat_frame, bg=self._colors["chat_bg"], highlightthickness=0)
        sb = ttk.Scrollbar(chat_frame, orient=tk.VERTICAL, command=self.canvas.yview)
        self.scroll_frame = tk.Frame(self.canvas, bg=self._colors["chat_bg"])
        self.scroll_frame.bind("<Configure>",
                               lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._cw = self.canvas.create_window((0, 0), window=self.scroll_frame, anchor=tk.NW)
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self._cw, width=e.width))
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))

        # ---------- 第2格：翻译输入区（再分成两个子格） ----------
        reply_pane = ttk.PanedWindow(main_pane, orient=tk.VERTICAL)
        main_pane.add(reply_pane, weight=2)

        # 上：输入框
        reply_top = ttk.LabelFrame(reply_pane, text="翻译输入框（自动翻译）", padding=(8, 4))
        reply_pane.add(reply_top, weight=1)

        r1 = ttk.Frame(reply_top)
        r1.pack(fill=tk.X, pady=(0, 2))
        ttk.Label(r1, text="目标语言:").pack(side=tk.LEFT)
        self.var_reply_lang = tk.StringVar(value="英语 (en)")
        reply_vals = [f"{n} ({c})" for c, n in get_language_list() if c != "auto"]
        ttk.Combobox(r1, textvariable=self.var_reply_lang, values=reply_vals,
                     state="readonly", width=22).pack(side=tk.LEFT, padx=(4, 8))
        ttk.Button(r1, text="复制译文", command=self._copy_reply).pack(side=tk.RIGHT)

        self.reply_input = tk.Text(reply_top, font=(self._font_family, self._font_size), wrap=tk.WORD)
        self.reply_input.pack(fill=tk.BOTH, expand=True, pady=(2, 0))
        self.reply_input.bind("<KeyRelease>", self._on_reply_input)

        # 下：译文框
        reply_bottom = ttk.LabelFrame(reply_pane, text="译文", padding=(8, 4))
        reply_pane.add(reply_bottom, weight=1)

        self.reply_output = tk.Text(reply_bottom, font=(self._font_family, self._font_size),
                                    wrap=tk.WORD, bg="#F9F9F9")
        self.reply_output.pack(fill=tk.BOTH, expand=True)
        self.reply_output.configure(state=tk.DISABLED)

        # ---------- 第3格：调试日志 ----------
        debug_container = ttk.LabelFrame(main_pane, text="调试日志", padding=(4, 2))
        main_pane.add(debug_container, weight=2)

        r2 = ttk.Frame(debug_container)
        r2.pack(fill=tk.X, pady=(0, 2))
        ttk.Button(r2, text="清空日志", width=8, command=self._clear_debug).pack(side=tk.LEFT)
        self.lbl_debug_status = ttk.Label(r2, text="等待启动...", foreground="blue")
        self.lbl_debug_status.pack(side=tk.LEFT, padx=(8, 0))

        self.debug_text = tk.Text(debug_container, font=("Consolas", 9),
                                   bg="#1E1E1E", fg="#00FF00", insertbackground="#00FF00",
                                   wrap=tk.WORD, state=tk.DISABLED)
        self.debug_text.pack(fill=tk.BOTH, expand=True)

        # ===== 状态栏（固定） =====
        self.var_status = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.var_status, anchor=tk.W,
                  padding=(8, 3), relief=tk.SUNKEN).pack(side=tk.BOTTOM, fill=tk.X)

    def _toggle_debug(self):
        pass  # 兼容旧方法，现在用 PanedWindow 直接拖拽调整

    def _clear_debug(self):
        self._debug_lines.clear()
        self.debug_text.configure(state=tk.NORMAL)
        self.debug_text.delete("1.0", tk.END)
        self.debug_text.configure(state=tk.DISABLED)

    def _append_debug(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self._debug_lines.append(line)
        if len(self._debug_lines) > 200:
            self._debug_lines = self._debug_lines[-100:]
        self.debug_text.configure(state=tk.NORMAL)
        self.debug_text.insert(tk.END, line + "\n")
        self.debug_text.see(tk.END)
        self.debug_text.configure(state=tk.DISABLED)
        self.lbl_debug_status.configure(text=msg[:80])

    # ==================== 语言切换 ====================
    def _parse_code(self, display: str) -> str:
        if "(" in display and ")" in display:
            return display.rsplit("(", 1)[1].rstrip(")")
        return display

    def _on_src_changed(self, event=None):
        self.translator.set_source(self._parse_code(self.var_src.get()))
        self._set_status("源语言已切换")

    def _on_tgt_changed(self, event=None):
        self.translator.set_target(self._parse_code(self.var_tgt.get()))
        self._set_status("目标语言已切换")

    # ==================== 消息监听 ====================
    def _toggle(self):
        if self.running:
            self._stop()
        else:
            self._start()

    def _start(self):
        if self.running:
            return
        try:
            from config import ReaderConfig
            cfg = ReaderConfig(poll_interval=0.8, max_history=1000, min_length=1)
            self.reader = WhatsAppReader(
                cfg,
                on_messages=self._on_msgs,
                on_chat_changed=self._on_chat,
                on_debug=self._on_debug,
            )
        except RuntimeError as e:
            messagebox.showerror("错误", str(e))
            return
        self.reader.start()
        self.running = True
        self.btn_toggle.configure(text="暂停")
        self._set_status("正在监听 WhatsApp...")
        self.lbl_title.configure(text="监听中...")
        self._append_debug("=== 开始监听 ===")

    def _stop(self):
        if self.reader:
            self.reader.stop()
        self.running = False
        self.btn_toggle.configure(text="开始监听")
        self._set_status("已暂停")

    def _probe(self):
        """探测 WhatsApp 窗口的控件树。"""
        if not self.reader:
            # 还没创建 reader，先创建一个临时的
            try:
                from config import ReaderConfig
                cfg = ReaderConfig(poll_interval=0.8, max_history=1000, min_length=1)
                from whatsapp_reader import WhatsAppReader
                reader = WhatsAppReader(cfg,
                                        on_messages=lambda m: None,
                                        on_debug=self._on_debug)
            except RuntimeError as e:
                messagebox.showerror("错误", str(e))
                return
        else:
            reader = self.reader

        self._append_debug("=== 开始探测控件树 ===")
        self._set_status("正在探测控件...")

        # 在子线程执行（探测可能较慢）
        def worker():
            reader.probe_controls()
            self.root.after(0, lambda: self._set_status("探测完成"))

        threading.Thread(target=worker, name="probe", daemon=True).start()

    def _clear(self):
        for w in self.scroll_frame.winfo_children():
            w.destroy()
        self._msg_map.clear()
        self._last_fp_set.clear()
        self._set_status("已清空")

    # ==================== Reader 回调（子线程） ====================
    def _on_msgs(self, msgs: List[WhatsAppMessage]):
        _post("msgs", msgs)

    def _on_chat(self, title: str):
        _post("chat", title)

    def _on_debug(self, msg: str):
        _post("debug", msg)

    # ==================== 队列消费 ====================
    def _poll_queue(self):
        try:
            while True:
                kind, payload = _msg_queue.get_nowait()
                if kind == "msgs":
                    self._handle_msgs(payload)
                elif kind == "chat":
                    self._handle_chat(payload)
                elif kind == "trans":
                    fp, text = payload
                    self._handle_trans(fp, text)
                elif kind == "reply":
                    self._handle_reply_result(payload)
                elif kind == "debug":
                    self._append_debug(payload)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    def _handle_chat(self, title: str):
        self.lbl_title.configure(text=f"聊天: {title}")
        self._clear()
        self._set_status(f"切换到: {title}")

    def _handle_msgs(self, msgs: List[WhatsAppMessage]):
        if not msgs:
            return
        current_set = {m.fingerprint() for m in msgs}
        if current_set == self._last_fp_set:
            return

        for msg in msgs:
            fp = msg.fingerprint()
            if fp not in self._msg_map:
                self._add_bubble(msg, fp)
        self._last_fp_set = current_set

    def _add_bubble(self, msg: WhatsAppMessage, fp: str):
        bubble = ChatBubble(
            self.scroll_frame, msg.text, msg.direction,
            self._colors, self._font_family, self._font_size,
            on_copy=self._copy_text,
        )
        bubble.pack(fill=tk.X, padx=8, pady=2)
        self._msg_map[fp] = bubble

        def cb(result):
            if isinstance(result, Exception):
                trans = f"[翻译失败] {result}"
            elif result is None:
                trans = "（无需翻译）"
            else:
                trans = result.text
            _post("trans", (fp, trans))

        self.translator.translate_async(
            msg.text,
            target=self.translator.target_lang,
            source=self.translator.source_lang,
            callback=cb,
        )
        self.root.after(10, self._scroll_bottom)

    def _handle_trans(self, fp: str, text: str):
        bubble = self._msg_map.get(fp)
        if bubble:
            bubble.set_translation(text)

    def _scroll_bottom(self):
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)

    @staticmethod
    def _copy_text(text: str):
        try:
            import subprocess
            subprocess.run(["clip"], input=text.encode("utf-16le"), check=True)
        except Exception:
            pass

    # ==================== 底部自动翻译 ====================
    def _on_reply_input(self, event=None):
        if self._reply_timer:
            self.root.after_cancel(self._reply_timer)
        self._reply_timer = self.root.after(500, self._do_reply_translate)

    def _do_reply_translate(self):
        text = self.reply_input.get("1.0", tk.END).strip()
        if not text:
            self.reply_output.configure(state=tk.NORMAL)
            self.reply_output.delete("1.0", tk.END)
            self.reply_output.configure(state=tk.DISABLED)
            return
        lang = self._parse_code(self.var_reply_lang.get())

        def cb(result):
            if isinstance(result, Exception):
                _post("reply", f"[翻译失败] {result}")
            elif result is None:
                _post("reply", text)
            else:
                _post("reply", result.text)

        self.translator.translate_async(text, target=lang, callback=cb)

    def _handle_reply_result(self, text: str):
        self.reply_output.configure(state=tk.NORMAL)
        self.reply_output.delete("1.0", tk.END)
        self.reply_output.insert("1.0", text)
        self.reply_output.configure(state=tk.DISABLED)

    def _copy_reply(self):
        text = self.reply_output.get("1.0", tk.END).strip()
        if not text:
            return
        try:
            import subprocess
            subprocess.run(["clip"], input=text.encode("utf-16le"), check=True)
            self._set_status("译文已复制")
        except Exception:
            pass

    # ==================== 设置对话框 ====================
    def _open_settings(self):
        self._show_settings_dialog()

    def _show_settings_dialog(self):
        from config import get_settings, save_settings
        settings = get_settings()

        dlg = tk.Toplevel(self.root)
        dlg.title("设置")
        dlg.geometry("520x420")
        dlg.transient(self.root)
        dlg.grab_set()

        nb = ttk.Notebook(dlg)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # --- 日志设置页 ---
        log_frame = ttk.Frame(nb, padding=10)
        nb.add(log_frame, text="日志")

        ttk.Label(log_frame, text="日志文件路径:").pack(anchor=tk.W)
        var_log_path = tk.StringVar(value=settings.gui.log_file_path or "")
        path_frame = ttk.Frame(log_frame)
        path_frame.pack(fill=tk.X, pady=(2, 4))
        ent_log = ttk.Entry(path_frame, textvariable=var_log_path)
        ent_log.pack(side=tk.LEFT, fill=tk.X, expand=True)

        def browse_log():
            from tkinter import filedialog
            path = filedialog.asksaveasfilename(
                parent=dlg,
                defaultextension=".log",
                filetypes=[("日志文件", "*.log"), ("所有文件", "*.*")],
                initialfile="app.log",
            )
            if path:
                var_log_path.set(path)

        ttk.Button(path_frame, text="浏览...", width=8, command=browse_log).pack(side=tk.LEFT, padx=(4, 0))

        ttk.Label(log_frame, text="留空则使用默认路径: %APPDATA%\\WhatsAppTranslator\\logs\\app.log",
                  font=("", 8), foreground="gray").pack(anchor=tk.W, pady=(0, 8))

        ttk.Label(log_frame, text="日志级别:").pack(anchor=tk.W)
        var_log_level = tk.StringVar(value=settings.gui.log_level or "DEBUG")
        ttk.Combobox(log_frame, textvariable=var_log_level,
                     values=["DEBUG", "INFO", "WARNING", "ERROR"],
                     state="readonly", width=12).pack(anchor=tk.W, pady=(2, 8))

        ttk.Separator(log_frame).pack(fill=tk.X, pady=8)

        btn_frame = ttk.Frame(log_frame)
        btn_frame.pack(fill=tk.X)

        def open_log_folder():
            import os
            if var_log_path.get():
                folder = os.path.dirname(var_log_path.get())
            else:
                from config import get_app_dir
                folder = str(get_app_dir() / "logs")
            try:
                os.startfile(folder)
            except Exception as e:
                messagebox.showerror("错误", f"无法打开文件夹:\n{e}", parent=dlg)

        def open_log_file():
            import os
            if var_log_path.get():
                fpath = var_log_path.get()
            else:
                from config import get_app_dir
                fpath = str(get_app_dir() / "logs" / "app.log")
            try:
                os.startfile(fpath)
            except Exception as e:
                messagebox.showerror("错误", f"无法打开日志文件:\n{e}\n\n路径: {fpath}", parent=dlg)

        ttk.Button(btn_frame, text="打开日志文件夹", command=open_log_folder).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="查看日志文件", command=open_log_file).pack(side=tk.LEFT, padx=(4, 0))

        # --- 读取设置页 ---
        read_frame = ttk.Frame(nb, padding=10)
        nb.add(read_frame, text="消息读取")

        ttk.Label(read_frame, text="轮询间隔（秒）:").pack(anchor=tk.W)
        var_poll = tk.StringVar(value=str(settings.reader.poll_interval))
        ttk.Entry(read_frame, textvariable=var_poll, width=10).pack(anchor=tk.W, pady=(2, 8))

        ttk.Label(read_frame, text="窗口标题关键词:").pack(anchor=tk.W)
        var_win_kw = tk.StringVar(value=settings.reader.window_keyword)
        ttk.Entry(read_frame, textvariable=var_win_kw, width=20).pack(anchor=tk.W, pady=(2, 8))

        ttk.Label(read_frame, text="进程名:").pack(anchor=tk.W)
        var_proc = tk.StringVar(value=settings.reader.process_name)
        ttk.Entry(read_frame, textvariable=var_proc, width=20).pack(anchor=tk.W, pady=(2, 8))

        # --- 翻译设置页 ---
        tr_frame = ttk.Frame(nb, padding=10)
        nb.add(tr_frame, text="翻译")

        ttk.Label(tr_frame, text="翻译后端:").pack(anchor=tk.W)
        var_backend = tk.StringVar(value=settings.translator.backend)
        ttk.Combobox(tr_frame, textvariable=var_backend,
                     values=["google", "doubao"],
                     state="readonly", width=12).pack(anchor=tk.W, pady=(2, 8))

        ttk.Label(tr_frame, text="豆包 API Key:").pack(anchor=tk.W)
        var_api = tk.StringVar(value=settings.translator.doubao_api_key)
        ttk.Entry(tr_frame, textvariable=var_api, show="*", width=40).pack(anchor=tk.W, pady=(2, 8))

        # --- 底部按钮 ---
        bottom = ttk.Frame(dlg, padding=(10, 0, 10, 10))
        bottom.pack(fill=tk.X)

        def save_and_close():
            try:
                settings.gui.log_file_path = var_log_path.get().strip()
                settings.gui.log_level = var_log_level.get()
                settings.reader.poll_interval = float(var_poll.get())
                settings.reader.window_keyword = var_win_kw.get().strip()
                settings.reader.process_name = var_proc.get().strip()
                settings.translator.backend = var_backend.get()
                settings.translator.doubao_api_key = var_api.get().strip()
                save_settings()
                messagebox.showinfo("保存成功", "设置已保存。\n\n注意：部分设置需要重启程序才能生效。", parent=dlg)
                dlg.destroy()
            except Exception as e:
                messagebox.showerror("保存失败", str(e), parent=dlg)

        ttk.Button(bottom, text="保存", command=save_and_close, width=10).pack(side=tk.RIGHT)
        ttk.Button(bottom, text="取消", command=dlg.destroy, width=10).pack(side=tk.RIGHT, padx=(0, 8))

    # ==================== 截图翻译 ====================
    def _screenshot(self):
        self._set_status("截图翻译：请框选区域...")
        try:
            from screenshot import start_screenshot_translate
            tgt = self._parse_code(self.var_tgt.get())
            start_screenshot_translate(self.root, self.translator, tgt)
        except Exception as e:
            messagebox.showerror("截图翻译", f"启动失败：\n{e}")

    # ==================== 辅助 ====================
    def _set_status(self, text: str):
        self.var_status.set(f"[{time.strftime('%H:%M:%S')}] {text}")

    def _on_close(self):
        self.root.withdraw()

    def _show_window(self):
        self.root.deiconify()
        self.root.lift()

    def _quit(self):
        if self.running:
            self._stop()
        self.translator.shutdown()
        try:
            if hasattr(self, "_tray") and self._tray:
                self._tray.stop()
        except Exception:
            pass
        self.root.destroy()
        sys.exit(0)

    def _install_hotkeys(self):
        try:
            import keyboard
            keyboard.add_hotkey("ctrl+alt+t", lambda: self.root.after(0, self._show_window))
            keyboard.add_hotkey("ctrl+alt+s", lambda: self.root.after(0, self._screenshot))
        except Exception as e:
            log.info("热键注册失败: %s", e)

    def _setup_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw

            def icon_img():
                img = Image.new("RGBA", (64, 64), (31, 58, 147, 255))
                d = ImageDraw.Draw(img)
                d.rectangle((8, 8, 56, 56), fill=(255, 255, 255, 255))
                d.text((20, 18), "译", fill=(31, 58, 147, 255))
                return img

            def on_click(icon, item):
                s = str(item)
                if s == "显示":
                    self.root.after(0, self._show_window)
                elif s == "截图翻译":
                    self.root.after(0, self._screenshot)
                elif s == "退出":
                    self.root.after(0, self._quit)

            menu = pystray.Menu(
                pystray.MenuItem("显示", on_click),
                pystray.MenuItem("截图翻译", on_click),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", on_click),
            )
            self._tray = pystray.Icon("whatsapp_translator", icon_img(),
                                       "WhatsApp 翻译助手", menu)
            threading.Thread(target=self._tray.run, daemon=True).start()
        except Exception as e:
            log.info("托盘失败: %s", e)
            self._tray = None
