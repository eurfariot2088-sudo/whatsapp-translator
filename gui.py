"""
gui.py
tkinter 主界面：上半区显示原文/译文对照表，下半区是状态栏与控制按钮。
- 「启动 / 暂停」控制 WhatsAppReader
- 「清空」清空历史与显示
- 「设置」打开后端/语言/热键配置
- 关闭按钮 → 最小化到托盘（可在设置中关闭）
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

from config import (AppConfig, GuiConfig, ReaderConfig, TranslatorConfig,
                    get_settings, save_settings)
from translator import Translator, TranslateError, TranslationResult
from whatsapp_reader import WhatsAppMessage, WhatsAppReader

log = logging.getLogger(__name__)


# 全局消息队列：Reader 线程 → GUI 线程
_msg_queue: "queue.Queue[tuple]" = queue.Queue()


def _post_msg(kind: str, payload):
    _msg_queue.put((kind, payload))


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class TranslatorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg: AppConfig = get_settings()
        self.translator = Translator(self.cfg.translator)
        self.reader: Optional[WhatsAppReader] = None
        self.running = False
        self._build_ui()
        self._poll_queue()
        self._install_hotkey()
        self._setup_tray()
        # 启动时根据配置自动开始
        if not self.cfg.gui.start_minimized:
            self.root.after(300, self._start_reader)
        else:
            self.root.after(300, self._hide_to_tray)

    # -------------------- UI --------------------
    def _build_ui(self):
        self.root.title("WhatsApp 翻译助手")
        self.root.geometry("820x560")
        self.root.minsize(640, 400)
        self._apply_theme()

        # 顶部工具栏
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(side=tk.TOP, fill=tk.X)
        self.btn_toggle = ttk.Button(bar, text="▶ 开始监听", width=14, command=self._toggle_reader)
        self.btn_toggle.pack(side=tk.LEFT)
        ttk.Button(bar, text="🧹 清空", width=8, command=self._clear_all).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(bar, text="⚙ 设置", width=8, command=self._open_settings).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(bar, text="⏏ 退出", width=8, command=self._quit).pack(side=tk.RIGHT)

        # 主体：Treeview 双列
        body = ttk.Frame(self.root, padding=(8, 0))
        body.pack(fill=tk.BOTH, expand=True)
        cols = ("time", "dir", "src", "dst")
        self.tree = ttk.Treeview(body, columns=cols, show="headings", height=20)
        self.tree.heading("time", text="时间")
        self.tree.heading("dir", text="方向")
        self.tree.heading("src", text="原文")
        self.tree.heading("dst", text="译文")
        self.tree.column("time", width=130, anchor=tk.W)
        self.tree.column("dir", width=60, anchor=tk.CENTER)
        self.tree.column("src", width=300, anchor=tk.W)
        self.tree.column("dst", width=300, anchor=tk.W)
        self.tree.tag_configure("in", foreground="#1f3a93")
        self.tree.tag_configure("out", foreground="#5b6e58")
        self.tree.tag_configure("err", foreground="#b00020")
        ysb = ttk.Scrollbar(body, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ysb.pack(side=tk.RIGHT, fill=tk.Y)

        # 状态栏
        self.status = tk.StringVar(value="就绪")
        status_bar = ttk.Label(self.root, textvariable=self.status,
                               anchor=tk.W, padding=(8, 4), relief=tk.SUNKEN)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        # 关闭行为
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _apply_theme(self):
        style = ttk.Style()
        if "vista" in style.theme_names() and self.cfg.gui.theme == "light":
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Treeview", rowheight=26, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

    # -------------------- 行为 --------------------
    def _toggle_reader(self):
        if self.running:
            self._stop_reader()
        else:
            self._start_reader()

    def _start_reader(self):
        if self.running:
            return
        try:
            self.reader = WhatsAppReader(self.cfg.reader, on_message=self._on_new_message)
        except RuntimeError as e:
            messagebox.showerror("不支持的平台", str(e))
            return
        self.reader.start()
        self.running = True
        self.btn_toggle.configure(text="⏸ 暂停监听")
        self._set_status(f"正在监听 WhatsApp 窗口（{self.cfg.reader.process_name}），"
                         f"后端 = {self.cfg.translator.backend} → {self.cfg.translator.target_lang}")

    def _stop_reader(self):
        if not self.running:
            return
        if self.reader:
            self.reader.stop()
        self.running = False
        self.btn_toggle.configure(text="▶ 开始监听")
        self._set_status("已暂停")

    def _clear_all(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        if self.reader:
            self.reader.clear_history()
        self._set_status("已清空")

    def _on_new_message(self, msg: WhatsAppMessage):
        # 切到 GUI 线程
        _post_msg("translate", msg)

    def _do_translate(self, msg: WhatsAppMessage):
        try:
            res: TranslationResult = self.translator.translate(msg.text)
            if res is None:
                # 不需要翻译（如已是目标语言）
                self._append_row(time.strftime("%H:%M:%S"), msg.direction,
                                 msg.text, "（无需翻译）")
                return
            self._append_row(time.strftime("%H:%M:%S"), msg.direction,
                             msg.text, res.text)
            self._set_status(f"已翻译（{res.backend}, {res.elapsed_ms} ms）")
        except TranslateError as e:
            self._append_row(time.strftime("%H:%M:%S"), msg.direction,
                             msg.text, f"[翻译失败] {e}", err=True)
            log.warning("翻译失败: %s", e)

    def _append_row(self, t: str, direction: str, src: str, dst: str, err: bool = False):
        tag = "err" if err else direction
        self.tree.insert("", tk.END,
                         values=(t, "收到的" if direction == "in" else "发出的", src, dst),
                         tags=(tag,))
        # 自动滚到底
        children = self.tree.get_children()
        if children:
            self.tree.see(children[-1])
        # 上限保护
        if len(children) > 1000:
            for iid in children[:200]:
                self.tree.delete(iid)

    # -------------------- 队列消费 --------------------
    def _poll_queue(self):
        try:
            while True:
                kind, payload = _msg_queue.get_nowait()
                if kind == "translate":
                    self._do_translate(payload)
                elif kind == "status":
                    self._set_status(payload)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)

    # -------------------- 设置 --------------------
    def _open_settings(self):
        SettingsDialog(self.root, self.cfg, on_saved=self._apply_settings)

    def _apply_settings(self, new_cfg: AppConfig):
        old = self.cfg
        self.cfg = new_cfg
        # 重启 reader
        was_running = self.running
        if was_running:
            self._stop_reader()
        self.translator.switch_backend(new_cfg.translator)
        if was_running:
            self._start_reader()
        self._apply_theme()
        self._set_status("设置已保存并应用")

    # -------------------- 关闭 / 托盘 --------------------
    def _on_close(self):
        if self.cfg.gui.close_to_tray:
            self._hide_to_tray()
        else:
            self._quit()

    def _hide_to_tray(self):
        try:
            self.root.withdraw()
            self._set_status("已最小化到托盘，双击托盘图标可恢复")
        except Exception:
            pass

    def _show_from_tray(self):
        self.root.deiconify()
        self.root.lift()
        self._set_status("就绪")

    def _quit(self):
        self._stop_reader()
        try:
            if hasattr(self, "_tray_icon") and self._tray_icon:
                self._tray_icon.stop()
        except Exception:
            pass
        save_settings()
        self.root.destroy()
        sys.exit(0)

    # -------------------- 热键 --------------------
    def _install_hotkey(self):
        try:
            import keyboard  # type: ignore
            keyboard.add_hotkey(self.cfg.gui.hotkey_show, lambda: _post_msg("status", "热键已触发"),
                                suppress=False)
            self._set_status(f"已注册热键 {self.cfg.gui.hotkey_show}")
        except Exception as e:
            log.info("全局热键注册失败（可忽略）: %s", e)

    # -------------------- 托盘 --------------------
    def _setup_tray(self):
        try:
            import pystray  # type: ignore
            from PIL import Image, ImageDraw  # type: ignore

            def make_icon() -> "Image.Image":
                img = Image.new("RGBA", (64, 64), (31, 58, 147, 255))
                d = ImageDraw.Draw(img)
                d.rectangle((8, 8, 56, 56), fill=(255, 255, 255, 255))
                d.text((18, 18), "译", fill=(31, 58, 147, 255))
                return img

            def on_click(icon, item):
                if str(item) == "显示主窗口":
                    self.root.after(0, self._show_from_tray)
                elif str(item) == "暂停监听":
                    self.root.after(0, self._stop_reader)
                elif str(item) == "开始监听":
                    self.root.after(0, self._start_reader)
                elif str(item) == "退出":
                    self.root.after(0, self._quit)

            menu = pystray.Menu(
                pystray.MenuItem("显示主窗口", on_click),
                pystray.MenuItem("开始监听", on_click),
                pystray.MenuItem("暂停监听", on_click),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", on_click),
            )
            self._tray_icon = pystray.Icon("whatsapp_translator", make_icon(),
                                           "WhatsApp 翻译助手", menu)
            threading.Thread(target=self._tray_icon.run, daemon=True).start()
        except Exception as e:
            log.info("托盘初始化失败（可忽略）: %s", e)
            self._tray_icon = None

    # -------------------- 工具 --------------------
    def _set_status(self, text: str):
        self.status.set(f"[{time.strftime('%H:%M:%S')}] {text}")


# ---------------------------------------------------------------------------
# 设置对话框
# ---------------------------------------------------------------------------
class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, cfg: AppConfig, on_saved):
        super().__init__(parent)
        self.title("设置")
        self.cfg = cfg
        self.on_saved = on_saved
        self.resizable(False, False)
        self.grab_set()

        pad = {"padx": 8, "pady": 4}
        f = ttk.Frame(self, padding=12)
        f.pack(fill=tk.BOTH, expand=True)

        # ---- 翻译后端 ----
        ttk.Label(f, text="翻译后端").grid(row=0, column=0, sticky=tk.W, **pad)
        self.var_backend = tk.StringVar(value=cfg.translator.backend)
        ttk.Combobox(f, textvariable=self.var_backend, state="readonly",
                     values=("google", "doubao"), width=18).grid(row=0, column=1, **pad)

        ttk.Label(f, text="目标语言").grid(row=1, column=0, sticky=tk.W, **pad)
        self.var_target = tk.StringVar(value=cfg.translator.target_lang)
        ttk.Entry(f, textvariable=self.var_target, width=20).grid(row=1, column=1, **pad)

        ttk.Label(f, text="源语言（auto 自动）").grid(row=2, column=0, sticky=tk.W, **pad)
        self.var_source = tk.StringVar(value=cfg.translator.source_lang)
        ttk.Entry(f, textvariable=self.var_source, width=20).grid(row=2, column=1, **pad)

        # ---- 豆包 ----
        ttk.Separator(f, orient=tk.HORIZONTAL).grid(row=3, column=0, columnspan=2, sticky=tk.EW, pady=6)
        ttk.Label(f, text="豆包 API Key").grid(row=4, column=0, sticky=tk.W, **pad)
        self.var_doubao_key = tk.StringVar(value=cfg.translator.doubao_api_key)
        ttk.Entry(f, textvariable=self.var_doubao_key, width=44, show="*").grid(row=4, column=1, **pad)

        ttk.Label(f, text="豆包模型").grid(row=5, column=0, sticky=tk.W, **pad)
        self.var_doubao_model = tk.StringVar(value=cfg.translator.doubao_model)
        ttk.Entry(f, textvariable=self.var_doubao_model, width=44).grid(row=5, column=1, **pad)

        ttk.Label(f, text="豆包 Endpoint").grid(row=6, column=0, sticky=tk.W, **pad)
        self.var_doubao_ep = tk.StringVar(value=cfg.translator.doubao_endpoint)
        ttk.Entry(f, textvariable=self.var_doubao_ep, width=44).grid(row=6, column=1, **pad)

        # ---- 读取 ----
        ttk.Separator(f, orient=tk.HORIZONTAL).grid(row=7, column=0, columnspan=2, sticky=tk.EW, pady=6)
        ttk.Label(f, text="轮询间隔（秒）").grid(row=8, column=0, sticky=tk.W, **pad)
        self.var_interval = tk.StringVar(value=str(cfg.reader.poll_interval))
        ttk.Entry(f, textvariable=self.var_interval, width=10).grid(row=8, column=1, sticky=tk.W, **pad)

        self.var_only_in = tk.BooleanVar(value=cfg.reader.only_incoming)
        ttk.Checkbutton(f, text="只翻译收到的消息",
                        variable=self.var_only_in).grid(row=9, column=0, columnspan=2, sticky=tk.W, **pad)

        # ---- GUI ----
        ttk.Separator(f, orient=tk.HORIZONTAL).grid(row=10, column=0, columnspan=2, sticky=tk.EW, pady=6)
        self.var_close_tray = tk.BooleanVar(value=cfg.gui.close_to_tray)
        ttk.Checkbutton(f, text="关闭窗口时最小化到托盘",
                        variable=self.var_close_tray).grid(row=11, column=0, columnspan=2, sticky=tk.W, **pad)
        ttk.Label(f, text="全局热键").grid(row=12, column=0, sticky=tk.W, **pad)
        self.var_hotkey = tk.StringVar(value=cfg.gui.hotkey_show)
        ttk.Entry(f, textvariable=self.var_hotkey, width=20).grid(row=12, column=1, **pad)

        # ---- 按钮 ----
        btn = ttk.Frame(f)
        btn.grid(row=13, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(btn, text="保存", command=self._save).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn, text="取消", command=self.destroy).pack(side=tk.LEFT, padx=4)

    def _save(self):
        try:
            self.cfg.translator.backend = self.var_backend.get().strip() or "google"
            self.cfg.translator.target_lang = self.var_target.get().strip() or "zh-CN"
            self.cfg.translator.source_lang = self.var_source.get().strip() or "auto"
            self.cfg.translator.doubao_api_key = self.var_doubao_key.get().strip()
            self.cfg.translator.doubao_model = self.var_doubao_model.get().strip() or "doubao-lite-32k"
            self.cfg.translator.doubao_endpoint = self.var_doubao_ep.get().strip()
            self.cfg.reader.poll_interval = float(self.var_interval.get() or "1.5")
            self.cfg.reader.only_incoming = bool(self.var_only_in.get())
            self.cfg.gui.close_to_tray = bool(self.var_close_tray.get())
            self.cfg.gui.hotkey_show = self.var_hotkey.get().strip() or "ctrl+alt+t"
            self.cfg.save()
            self.on_saved(self.cfg)
            self.destroy()
        except ValueError as e:
            messagebox.showerror("参数错误", f"请检查输入：{e}", parent=self)
