"""
whatsapp_reader.py
通过 Windows UI Automation（UIA）直接读取桌面 WhatsApp 窗口中的消息文本。
绝不截屏、绝不依赖剪贴板，纯控件树遍历。

v3 改进：
- 精准定位右侧聊天区域（排除左侧好友列表）
- 按 Y 坐标排序，保持消息顺序
- 切换聊天自动检测并清空
- 滚动时重新检测全部可见消息
- 首次扫描也翻译（配置控制）
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from config import ReaderConfig

log = logging.getLogger(__name__)


@dataclass
class WhatsAppMessage:
    text: str
    direction: str            # "in" 收到的 / "out" 发出的
    y_top: int = 0            # 顶部 Y 坐标，用于排序
    ts: float = field(default_factory=time.time)
    sender: str = ""

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
            "WhatsApp 消息自动读取依赖 Windows UI Automation，只能在 Windows 上运行。"
        )


class WhatsAppReader:
    """周期性读取 WhatsApp Desktop 消息的轮询器。"""

    def __init__(self, cfg: ReaderConfig,
                 on_messages: Callable[[List[WhatsAppMessage]], None],
                 on_chat_changed: Optional[Callable[[str], None]] = None):
        """
        :param on_messages: 回调，参数为当前所有消息（按顺序）的列表
        :param on_chat_changed: 回调，参数为当前聊天标题
        """
        _ensure_windows()
        self.cfg = cfg
        self.on_messages = on_messages
        self.on_chat_changed = on_chat_changed
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_chat_title: str = ""
        self._last_msg_count: int = 0
        self._last_fp_set: Set[str] = set()

    # -------------------- 生命周期 --------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._last_chat_title = ""
        self._last_msg_count = 0
        self._thread = threading.Thread(target=self._loop, name="WA-Reader", daemon=True)
        self._thread.start()
        log.info("WhatsAppReader 已启动，轮询间隔 %.2fs", self.cfg.poll_interval)

    def stop(self, join_timeout: float = 2.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=join_timeout)
        log.info("WhatsAppReader 已停止")

    def refresh_now(self):
        """立即触发一次扫描（不等待轮询）。"""
        pass  # 轮询已经很快，不需要额外触发

    # -------------------- 主循环 --------------------
    def _loop(self):
        import uiautomation as auto

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

        # 获取聊天标题
        chat_title = self._get_chat_title(window)
        if chat_title and chat_title != self._last_chat_title:
            self._last_chat_title = chat_title
            log.info("检测到聊天切换: %s", chat_title)
            if self.on_chat_changed:
                try:
                    self.on_chat_changed(chat_title)
                except Exception:
                    log.exception("on_chat_changed 回调异常")

        # 提取消息
        messages = list(self._extract_messages(window))
        if not messages:
            return

        # 按 Y 坐标排序（从上到下）
        messages.sort(key=lambda m: m.y_top)

        # 过滤过短消息
        messages = [m for m in messages if len(m.text.strip()) >= self.cfg.min_length]

        if not messages:
            return

        # 去重（保留顺序）
        seen = set()
        unique_msgs = []
        for m in messages:
            fp = m.fingerprint()
            if fp not in seen:
                seen.add(fp)
                unique_msgs.append(m)

        if not unique_msgs:
            return

        # 判断是否有变化（数量变了 或 内容变了）
        current_fps = {m.fingerprint() for m in unique_msgs}
        if len(unique_msgs) == self._last_msg_count and current_fps == self._last_fp_set:
            return  # 没有变化，跳过

        self._last_msg_count = len(unique_msgs)
        self._last_fp_set = current_fps

        # 回调
        try:
            self.on_messages(unique_msgs)
        except Exception:
            log.exception("on_messages 回调异常")

    # -------------------- 窗口定位 --------------------
    def _find_window(self, auto):
        try:
            win = auto.WindowControl(
                searchFromControl=auto.GetRootControl(),
                searchDepth=8,
                ProcessName=self.cfg.process_name,
            )
            if win and win.Exists(maxSearchSeconds=0.2):
                return win
        except Exception:
            pass
        try:
            win = auto.WindowControl(
                searchFromControl=auto.GetRootControl(),
                searchDepth=3,
                Name=self.cfg.window_keyword,
            )
            if win and win.Exists(maxSearchSeconds=0.2):
                return win
        except Exception:
            pass
        return None

    # -------------------- 聊天标题 --------------------
    def _get_chat_title(self, window) -> str:
        """尝试获取当前聊天对象的名称。"""
        try:
            win_rect = window.BoundingRectangle
            win_top = win_rect.top
            win_bottom = win_rect.bottom
            win_height = win_bottom - win_top
            # 标题通常在顶部 15% 区域内
            title_zone_bottom = win_top + int(win_height * 0.15)

            # 找顶部区域的 TextControl
            for ctrl, depth in self._walk_all(window, max_depth=6):
                try:
                    if (ctrl.ControlTypeName or "") != "TextControl":
                        continue
                    name = (ctrl.Name or "").strip()
                    if not name or len(name) < 2:
                        continue
                    rect = ctrl.BoundingRectangle
                    if rect.top < title_zone_bottom and rect.top > win_top:
                        return name
                except Exception:
                    continue
        except Exception:
            pass
        return ""

    # -------------------- 提取消息（核心：只取右侧聊天区） --------------------
    def _extract_messages(self, window) -> Iterable[WhatsAppMessage]:
        try:
            win_rect = window.BoundingRectangle
            win_left = win_rect.left
            win_right = win_rect.right
            win_width = win_right - win_left
            win_top = win_rect.top
            win_bottom = win_rect.bottom
            win_height = win_bottom - win_top

            # 聊天区域通常在右侧
            # WhatsApp 布局：左侧约 30% 是好友列表，右侧 70% 是聊天区
            # 我们从右侧 60% 开始算聊天区
            chat_left = win_left + int(win_width * 0.35)
            mid_x = win_left + win_width // 2

            # 顶部 15% 是标题栏，底部 10% 是输入框
            chat_top = win_top + int(win_height * 0.12)
            chat_bottom = win_bottom - int(win_height * 0.10)
        except Exception:
            return

        # 策略1：找聊天区里的 ListItem
        found = list(self._strategy_list_items(window, chat_left, chat_top, chat_bottom, mid_x))
        if found:
            for m in found:
                yield m
            return

        # 策略2：找聊天区里的所有 TextControl，按行分组
        found = list(self._strategy_text_fallback(
            window, chat_left, chat_top, chat_bottom, mid_x))
        for m in found:
            yield m

    # -----------------------------------------------------------------------
    # 策略1: ListItem
    # -----------------------------------------------------------------------
    def _strategy_list_items(self, window, chat_left, chat_top, chat_bottom, mid_x):
        try:
            # 找 List 控件
            lists = []
            for ctrl, depth in self._walk_all(window, max_depth=6):
                try:
                    ctype = ctrl.ControlTypeName or ""
                    if "List" in ctype:
                        rect = ctrl.BoundingRectangle
                        # 聊天列表应该在右侧且高度较大
                        if (rect.left >= chat_left - 20 and
                                rect.top >= chat_top - 20 and
                                rect.bottom <= chat_bottom + 20 and
                                rect.height() > 100):
                            lists.append(ctrl)
                except Exception:
                    continue

            if not lists:
                return

            # 取最大的那个 List（消息列表）
            lists.sort(key=lambda l: l.BoundingRectangle.height(), reverse=True)
            msg_list = lists[0]

            # 遍历子项
            children = msg_list.GetChildren()
            for child in children:
                try:
                    ctype = child.ControlTypeName or ""
                    if "ListItem" not in ctype and "Pane" not in ctype:
                        continue
                    rect = child.BoundingRectangle
                    if rect.left < chat_left - 20:
                        continue
                    if rect.top < chat_top - 20 or rect.bottom > chat_bottom + 20:
                        continue

                    texts = self._collect_texts(child)
                    joined = self._clean_message("\n".join(texts))
                    if not joined or len(joined) < 2:
                        continue

                    low = joined.lower().strip()
                    skip_keywords = {"typing", "online", "last seen", "you",
                                     "unread", "pinned", "archived"}
                    if any(kw in low for kw in skip_keywords):
                        continue

                    direction = self._guess_direction(child, mid_x)
                    yield WhatsAppMessage(
                        text=joined,
                        direction=direction,
                        y_top=rect.top,
                    )
                except Exception:
                    continue
        except Exception as e:
            log.debug("_strategy_list_items 异常: %s", e)

    # -----------------------------------------------------------------------
    # 策略2: TextControl 兜底
    # -----------------------------------------------------------------------
    def _strategy_text_fallback(self, window, chat_left, chat_top, chat_bottom, mid_x):
        try:
            texts = []  # (y, x, text)
            for ctrl, depth in self._walk_all(window, max_depth=8):
                try:
                    if (ctrl.ControlTypeName or "") != "TextControl":
                        continue
                    name = (ctrl.Name or "").strip()
                    if not name or len(name) < 2:
                        continue
                    rect = ctrl.BoundingRectangle
                    if rect.left < chat_left - 20:
                        continue
                    if rect.top < chat_top - 20 or rect.bottom > chat_bottom + 20:
                        continue
                    texts.append((rect.top, rect.left, name, rect.height()))
                except Exception:
                    continue

            if not texts:
                return

            # 按 Y 排序
            texts.sort(key=lambda t: (t[0], t[1]))

            # 按 Y 分组（同一行的归为一条消息）
            groups: List[List[tuple]] = []
            current: List[tuple] = []
            last_y = None
            last_h = None
            for entry in texts:
                y, x, name, h = entry
                if last_y is None:
                    current.append(entry)
                    last_y = y
                    last_h = h
                else:
                    # Y 差在行高的 50% 以内认为是同一行
                    if abs(y - last_y) <= max(h, last_h) * 0.5:
                        current.append(entry)
                    else:
                        groups.append(current)
                        current = [entry]
                        last_y = y
                        last_h = h
            if current:
                groups.append(current)

            # 合并同一消息的多行
            # 不同的消息之间 Y 间距较大，我们用较大的 Y 阈值来合并
            merged: List[List[List[tuple]]] = []
            current_msg: List[List[tuple]] = []
            last_bottom = None
            for grp in groups:
                grp.sort(key=lambda t: t[1])  # 按 X 排序
                grp_y = grp[0][0]
                grp_h = grp[0][3] if grp else 16
                grp_bottom = grp_y + grp_h

                if last_bottom is None:
                    current_msg.append(grp)
                    last_bottom = grp_bottom
                else:
                    gap = grp_y - last_bottom
                    if gap <= grp_h * 1.5:
                        # 间距小，属于同一条消息的多行
                        current_msg.append(grp)
                    else:
                        # 间距大，是新消息
                        merged.append(current_msg)
                        current_msg = [grp]
                    last_bottom = max(last_bottom, grp_bottom)
            if current_msg:
                merged.append(current_msg)

            # 生成消息
            for msg_lines in merged:
                text_parts = []
                first_x = None
                first_y = None
                for line in msg_lines:
                    line.sort(key=lambda t: t[1])
                    line_text = " ".join(t[2] for t in line)
                    text_parts.append(line_text)
                    if first_x is None:
                        first_x = line[0][1]
                        first_y = line[0][0]

                full_text = "\n".join(text_parts).strip()
                cleaned = self._clean_message(full_text)
                if not cleaned or len(cleaned) < 2:
                    continue

                low = cleaned.lower().strip()
                skip_keywords = {"typing", "online", "last seen", "unread",
                                 "pinned", "archived", "muted"}
                if any(kw in low for kw in skip_keywords):
                    continue

                direction = "in" if (first_x is not None and first_x < mid_x) else "out"
                yield WhatsAppMessage(
                    text=cleaned,
                    direction=direction,
                    y_top=first_y or 0,
                )
        except Exception as e:
            log.debug("_strategy_text_fallback 异常: %s", e)

    # -------------------- 辅助 --------------------
    @staticmethod
    def _walk_all(root, max_depth: int = 5):
        """广度优先遍历控件树。"""
        import uiautomation as auto
        try:
            for ctrl, depth in auto.WalkControl(root, includeTop=False):
                if depth > max_depth:
                    continue
                yield ctrl, depth
        except Exception:
            pass

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
        # 去重保持顺序
        seen = set()
        uniq = []
        for t in out:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        return uniq

    @staticmethod
    def _clean_message(text: str) -> str:
        lines = []
        for line in text.split("\n"):
            s = line.strip()
            if not s:
                continue
            # 去掉纯时间戳
            if len(s) <= 8 and (":" in s) and all(c.isdigit() or c in ": " for c in s):
                continue
            lines.append(s)
        return "\n".join(lines).strip()

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
