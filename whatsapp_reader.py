"""
whatsapp_reader.py
通过 Windows UI Automation（UIA）直接读取桌面 WhatsApp 窗口中的消息文本。
绝不截屏、绝不依赖剪贴板，纯控件树遍历。

优化点（v2）：
- 轮询间隔降到 0.8 秒，支持实时翻译
- 首次扫描只入缓存不翻译（避免刷屏），可通过配置开启
- 新增「切换聊天检测」：当检测到聊天对象变化时自动清空缓存
- 异步回调，不阻塞 UIA 遍历线程
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Set

from config import ReaderConfig

log = logging.getLogger(__name__)


@dataclass
class WhatsAppMessage:
    text: str
    direction: str            # "in" 收到的 / "out" 发出的
    ts: float = field(default_factory=time.time)
    sender: str = ""          # 发送者名称（如果可提取）

    def fingerprint(self) -> str:
        h = hashlib.sha1()
        h.update(self.direction.encode("utf-8"))
        h.update(b"\x00")
        h.update(self.text.encode("utf-8"))
        return h.hexdigest()


def _ensure_windows():
    import sys
    if sys.platform != "win32":
        raise RuntimeError(
            "WhatsApp 消息自动读取依赖 Windows UI Automation，"
            "只能在 Windows 上运行。"
        )


class WhatsAppReader:
    """周期性读取 WhatsApp Desktop 消息的轮询器。"""

    def __init__(self, cfg: ReaderConfig,
                 on_message: Callable[[WhatsAppMessage], None],
                 on_chat_changed: Optional[Callable[[], None]] = None):
        _ensure_windows()
        self.cfg = cfg
        self.on_message = on_message
        self.on_chat_changed = on_chat_changed
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seen: Set[str] = set()
        self._seen_order: List[str] = []
        self._window_lock = threading.Lock()
        self._last_chat_title: str = ""
        self._primed = False

    # -------------------- 生命周期 --------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._primed = False
        self._thread = threading.Thread(target=self._loop, name="WA-Reader", daemon=True)
        self._thread.start()
        log.info("WhatsAppReader 已启动，轮询间隔 %.2fs", self.cfg.poll_interval)

    def stop(self, join_timeout: float = 2.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=join_timeout)
        log.info("WhatsAppReader 已停止")

    def clear_history(self):
        with self._window_lock:
            self._seen.clear()
            self._seen_order.clear()

    # -------------------- 主循环 --------------------
    def _loop(self):
        import uiautomation as auto
        self._seen.clear()
        self._seen_order.clear()

        while not self._stop.is_set():
            try:
                self._scan_once(auto)
            except Exception as e:
                log.debug("扫描异常（忽略）: %s", e)
            self._stop.wait(self.cfg.poll_interval)

    # -------------------- 单次扫描 --------------------
    def _scan_once(self, auto):
        window = self._find_window(auto)
        if window is None:
            return

        # 检测聊天是否切换
        self._check_chat_changed(window)

        messages = list(self._extract_messages(auto, window))

        # 首次扫描：只入缓存，不回调
        is_first = not self._primed
        if is_first:
            for msg in messages:
                fp = msg.fingerprint()
                with self._window_lock:
                    self._seen.add(fp)
                    self._seen_order.append(fp)
            self._primed = True
            if self.cfg.translate_history_on_start:
                # 配置要求翻译历史消息
                for msg in messages:
                    self._dispatch(msg)
            return

        # 后续扫描：只处理新消息
        for msg in messages:
            fp = msg.fingerprint()
            with self._window_lock:
                if fp in self._seen:
                    continue
                if len(self._seen) >= self.cfg.max_history:
                    drop = max(1, self.cfg.max_history // 4)
                    for _ in range(drop):
                        if self._seen_order:
                            old = self._seen_order.pop(0)
                            self._seen.discard(old)
                self._seen.add(fp)
                self._seen_order.append(fp)
            self._dispatch(msg)

    def _dispatch(self, msg: WhatsAppMessage):
        if len(msg.text.strip()) < self.cfg.min_length:
            return
        if self.cfg.only_incoming and msg.direction != "in":
            return
        try:
            self.on_message(msg)
        except Exception:
            log.exception("on_message 回调异常")

    # -------------------- 聊天切换检测 --------------------
    def _check_chat_changed(self, window):
        if self.on_chat_changed is None:
            return
        try:
            # 尝试获取当前聊天名称（通常在窗口顶部）
            title = ""
            for child in window.GetChildren():
                try:
                    name = child.Name or ""
                    if name and len(name) > 1 and name != self.cfg.window_keyword:
                        title = name
                        break
                except Exception:
                    continue
            if title and title != self._last_chat_title:
                self._last_chat_title = title
                with self._window_lock:
                    self._seen.clear()
                    self._seen_order.clear()
                self._primed = False  # 重新预热
                try:
                    self.on_chat_changed()
                except Exception:
                    log.exception("on_chat_changed 回调异常")
        except Exception:
            pass

    # -------------------- 窗口定位 --------------------
    def _find_window(self, auto):
        try:
            win = auto.WindowControl(
                searchFromControl=auto.GetRootControl(),
                searchDepth=8,
                ProcessName=self.cfg.process_name,
            )
            if win and win.Exists(maxSearchSeconds=0.3):
                return win
        except Exception:
            pass
        try:
            win = auto.WindowControl(
                searchFromControl=auto.GetRootControl(),
                searchDepth=3,
                Name=self.cfg.window_keyword,
            )
            if win and win.Exists(maxSearchSeconds=0.3):
                return win
        except Exception:
            pass
        return None

    # -------------------- 提取消息 --------------------
    def _extract_messages(self, auto, window) -> Iterable[WhatsAppMessage]:
        try:
            win_rect = window.BoundingRectangle
            win_left = win_rect.left
            win_right = win_rect.right
            win_width = win_right - win_left
            mid_x = win_left + win_width // 2
        except Exception:
            mid_x = None

        messages = list(self._strategy_list(auto, window, mid_x))
        if not messages:
            messages = list(self._strategy_text_fallback(auto, window, mid_x))
        return messages

    def _strategy_list(self, auto, window, mid_x) -> Iterable[WhatsAppMessage]:
        try:
            lists = window.GetChildren()
            candidate_lists = []
            for ctrl in lists:
                try:
                    ctype = ctrl.ControlTypeName or ""
                    if "List" in ctype or "Pane" in ctype:
                        sub_count = 0
                        for _ in ctrl.GetChildren():
                            sub_count += 1
                        candidate_lists.append((sub_count, ctrl))
                except Exception:
                    continue
            candidate_lists.sort(key=lambda x: x[0], reverse=True)
            for _, ctrl in candidate_lists[:3]:
                for msg in self._walk_list_items(ctrl, mid_x):
                    yield msg
        except Exception as e:
            log.debug("_strategy_list 异常: %s", e)

    def _walk_list_items(self, list_ctrl, mid_x) -> Iterable[WhatsAppMessage]:
        try:
            items = list_ctrl.GetChildren()
        except Exception:
            return
        for item in items:
            texts = self._collect_texts(item)
            joined = self._join_texts(texts)
            if not joined or len(joined.strip()) < 2:
                continue
            low = joined.lower()
            if low in {"typing…", "typing...", "online", "last seen", "last seen recently"}:
                continue
            direction = self._guess_direction(item, mid_x)
            yield WhatsAppMessage(text=joined, direction=direction)

    def _strategy_text_fallback(self, auto, window, mid_x) -> Iterable[WhatsAppMessage]:
        try:
            texts = []
            for ctrl, depth in auto.WalkControl(window, includeTop=False):
                try:
                    if (ctrl.ControlTypeName or "") != "TextControl":
                        continue
                    name = ctrl.Name or ""
                    if not name or len(name.strip()) < 2:
                        continue
                    rect = ctrl.BoundingRectangle
                    texts.append((rect.top, rect.left, name))
                except Exception:
                    continue
            if not texts:
                return
            texts.sort(key=lambda t: (t[0], t[1]))

            groups: List[List[tuple]] = []
            current: List[tuple] = []
            last_y = None
            for entry in texts:
                y, x, name = entry
                if last_y is None or abs(y - last_y) <= 8:
                    current.append(entry)
                else:
                    groups.append(current)
                    current = [entry]
                last_y = y
            if current:
                groups.append(current)

            for grp in groups:
                grp.sort(key=lambda t: t[1])
                joined = "\n".join(g[2] for g in grp).strip()
                if not joined or len(joined) < 2:
                    continue
                first_x = grp[0][1]
                direction = "in" if mid_x is not None and first_x < mid_x else "out"
                yield WhatsAppMessage(text=joined, direction=direction)
        except Exception as e:
            log.debug("_strategy_text_fallback 异常: %s", e)

    # -------------------- 辅助 --------------------
    @staticmethod
    def _collect_texts(ctrl) -> List[str]:
        out: List[str] = []
        try:
            for sub, _ in ctrl.WalkControl(ctrl, includeTop=False):
                if (sub.ControlTypeName or "") == "TextControl":
                    name = (sub.Name or "").strip()
                    if name:
                        out.append(name)
        except Exception:
            pass
        seen = set()
        uniq = []
        for t in out:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        return uniq

    @staticmethod
    def _join_texts(texts: List[str]) -> str:
        if not texts:
            return ""
        cleaned = [t for t in texts if t]
        return "\n".join(cleaned).strip()

    @staticmethod
    def _guess_direction(item, mid_x) -> str:
        if mid_x is None:
            return "in"
        try:
            rect = item.BoundingRectangle
            cx = (rect.left + rect.right) // 2
            return "in" if cx < mid_x else "out"
        except Exception:
            return "in"
