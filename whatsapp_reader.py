"""
whatsapp_reader.py
通过 Windows UI Automation 读取桌面 WhatsApp 消息。

v6 — 探测模式：
- 加入控件树探测功能，列出 WhatsApp 窗口所有控件
- 消息提取不限制控件类型，只要有 Name/Value 就尝试
- 支持 LegacyIAccessible、TextPattern、ValuePattern 多种文本获取方式
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Set

from config import ReaderConfig

log = logging.getLogger(__name__)


@dataclass
class WhatsAppMessage:
    text: str
    direction: str
    y_top: int = 0
    ts: float = field(default_factory=time.time)

    def fingerprint(self) -> str:
        h = hashlib.sha1()
        h.update(self.direction.encode("utf-8"))
        h.update(b"\x00")
        h.update(self.text.encode("utf-8"))
        return h.hexdigest()


def _ensure_windows():
    import sys
    if sys.platform != "win32":
        raise RuntimeError("只能运行在 Windows 上")


class WhatsAppReader:
    def __init__(self, cfg: ReaderConfig,
                 on_messages: Callable[[List[WhatsAppMessage]], None],
                 on_chat_changed: Optional[Callable[[str], None]] = None,
                 on_debug: Optional[Callable[[str], None]] = None):
        _ensure_windows()
        self.cfg = cfg
        self.on_messages = on_messages
        self.on_chat_changed = on_chat_changed
        self.on_debug = on_debug
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_fps: Set[str] = set()
        self._last_count = 0
        self._scan_count = 0
        self._window_found = False
        self._window_ref = None

    def _debug(self, msg: str):
        log.info(msg)
        if self.on_debug:
            try:
                self.on_debug(msg)
            except Exception:
                pass

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="WA-Reader", daemon=True)
        self._thread.start()
        self._debug("Reader 已启动，轮询间隔 %.1fs" % self.cfg.poll_interval)

    def stop(self, join_timeout=2.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=join_timeout)
        self._debug("Reader 已停止")

    def probe_controls(self):
        """探测 WhatsApp 窗口的控件树，输出到调试日志。"""
        import uiautomation as auto
        window = self._find_window(auto)
        if window is None:
            self._debug("[探测] 未找到 WhatsApp 窗口")
            return

        self._debug("[探测] === 开始探测控件树 ===")
        self._debug("[探测] 窗口标题: '%s' Class: '%s'" % (window.Name, window.ClassName))
        try:
            rect = window.BoundingRectangle
            self._debug("[探测] 窗口大小: %dx%d 位置: (%d,%d)" %
                        (rect.width(), rect.height(), rect.left, rect.top))
        except Exception:
            pass

        # 遍历所有子控件，输出有名称的
        self._probe_children(window, depth=0, max_depth=6)
        self._debug("[探测] === 探测结束 ===")

    def _probe_children(self, ctrl, depth, max_depth):
        if depth > max_depth:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            return

        for child in children:
            try:
                name = child.Name or ""
                ctype = child.ControlTypeName or ""
                cname = child.ClassName or ""

                # 只输出有意义的控件
                has_name = len(name) >= 1
                is_container = ctype in ["PaneControlType", "GroupControlType", "ListControlType",
                                          "ListItemControlType", "DataItemControlType",
                                          "TabControlType", "TreeControlType", "WindowControlType"]

                if has_name or is_container:
                    try:
                        rect = child.BoundingRectangle
                        pos_str = "(%d,%d,%d,%d)" % (rect.left, rect.top, rect.right, rect.bottom)
                    except Exception:
                        pos_str = "?"

                    prefix = "  " * depth
                    name_display = name[:50] if name else "(无名称)"
                    self._debug("[探测] %s%s | %s | %s | %s" %
                                (prefix, ctype, cname, name_display, pos_str))
            except Exception:
                continue

            # 递归
            self._probe_children(child, depth + 1, max_depth)

    # -------------------- 主循环 --------------------
    def _loop(self):
        import uiautomation as auto
        while not self._stop.is_set():
            try:
                self._scan_once(auto)
            except Exception as e:
                self._debug("扫描异常: %s" % e)
                log.exception("扫描异常")
            self._stop.wait(self.cfg.poll_interval)

    # -------------------- 单次扫描 --------------------
    def _scan_once(self, auto):
        self._scan_count += 1

        window = self._find_window(auto)
        if window is None:
            if self._scan_count <= 3 or self._scan_count % 20 == 0:
                self._debug("未找到 WhatsApp 窗口 (扫描 #%d)" % self._scan_count)
            return

        if not self._window_found:
            self._window_found = True
            self._window_ref = window
            self._debug("已找到 WhatsApp 窗口！标题: '%s'" % window.Name)

        messages = self._extract_messages(auto, window)

        if not messages:
            if self._scan_count <= 10 or self._scan_count % 30 == 0:
                self._debug("扫描 #%d: 窗口已找到，但未提取到消息" % self._scan_count)
            return

        messages.sort(key=lambda m: m.y_top)

        seen = set()
        unique = []
        for m in messages:
            fp = m.fingerprint()
            if fp not in seen:
                seen.add(fp)
                unique.append(m)

        current_fps = {m.fingerprint() for m in unique}
        if len(unique) == self._last_count and current_fps == self._last_fps:
            return

        old_count = self._last_count
        self._last_count = len(unique)
        self._last_fps = current_fps
        self._debug("扫描 #%d: %d 条消息 (上次 %d)" % (self._scan_count, len(unique), old_count))

        try:
            self.on_messages(unique)
        except Exception:
            log.exception("on_messages 回调异常")

    # -------------------- 窗口定位 --------------------
    def _find_window(self, auto):
        try:
            root = auto.GetRootControl()
            for child in root.GetChildren():
                try:
                    name = child.Name or ""
                    if self.cfg.window_keyword.lower() in name.lower():
                        return child
                except Exception:
                    continue
        except Exception:
            pass
        return None

    # -------------------- 提取消息 --------------------
    def _extract_messages(self, auto, window) -> List[WhatsAppMessage]:
        messages: List[WhatsAppMessage] = []

        try:
            win_rect = window.BoundingRectangle
            win_left = win_rect.left
            win_right = win_rect.right
            win_top = win_rect.top
            win_bottom = win_rect.bottom
            win_width = win_right - win_left
            win_height = win_bottom - win_top
            mid_x = win_left + win_width // 2
            chat_left = win_left + int(win_width * 0.30)
            chat_top = win_top + int(win_height * 0.10)
            chat_bottom = win_bottom - int(win_height * 0.08)
        except Exception as e:
            self._debug("获取窗口位置失败: %s" % e)
            return []

        # 策略1: 找所有带文字的控件（不限类型）
        texts = []
        self._collect_all_texts(window, chat_left, chat_top, chat_bottom, texts,
                                depth=0, max_depth=12)

        if not texts:
            # 策略2: 全窗口搜索
            self._collect_all_texts(window, win_left, win_top, win_bottom, texts,
                                    depth=0, max_depth=12, unrestricted=True)

        if not texts:
            return []

        if self._scan_count <= 5:
            self._debug("找到 %d 个带文本的控件" % len(texts))

        # 按 Y 排序
        texts.sort(key=lambda t: (t[0], t[1]))

        # 分组：按 Y 距离
        groups = []
        current = []
        last_y = None
        for entry in texts:
            y, x, text, h = entry
            if last_y is None or abs(y - last_y) <= max(h, 16) * 0.6:
                current.append(entry)
            else:
                if current:
                    groups.append(current)
                current = [entry]
            last_y = y
        if current:
            groups.append(current)

        # 合并相邻行
        merged = []
        cur_msg = []
        last_bottom = None
        for grp in groups:
            grp.sort(key=lambda t: t[1])
            gy = grp[0][0]
            gh = grp[0][3] if grp else 16
            gbot = gy + gh

            if last_bottom is None:
                cur_msg.append(grp)
            else:
                gap = gy - last_bottom
                if gap <= gh * 1.8:
                    cur_msg.append(grp)
                else:
                    merged.append(cur_msg)
                    cur_msg = [grp]
            last_bottom = max(last_bottom or 0, gbot)
        if cur_msg:
            merged.append(cur_msg)

        # 生成消息
        for msg_lines in merged:
            parts = []
            fx = None
            fy = None
            for line in msg_lines:
                line.sort(key=lambda t: t[1])
                parts.append(" ".join(t[2] for t in line))
                if fx is None:
                    fx = line[0][1]
                    fy = line[0][0]

            full = "\n".join(parts).strip()
            cleaned = self._clean(full)
            if not cleaned or len(cleaned) < 2:
                continue

            low = cleaned.lower()
            skip = ["typing", "online", "last seen", "search", "unread",
                    "archived", "pinned", "muted", "whatsapp"]
            if any(kw in low for kw in skip):
                continue

            # 过滤掉纯数字和特殊字符
            if all(c.isdigit() or c in ":.,!? " for c in cleaned) and len(cleaned) < 10:
                continue

            direction = "in" if (fx is not None and fx < mid_x) else "out"
            messages.append(WhatsAppMessage(text=cleaned, direction=direction, y_top=fy or 0))

        return messages

    def _collect_all_texts(self, ctrl, chat_left, chat_top, chat_bottom,
                            texts, depth=0, max_depth=12, unrestricted=False):
        """递归收集所有有文本的控件（不限 TextControl）。"""
        if depth > max_depth:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            return

        for child in children:
            try:
                ctype = child.ControlTypeName or ""
                rect = child.BoundingRectangle

                in_zone = unrestricted or (
                    rect.left >= chat_left - 30 and
                    rect.top >= chat_top - 30 and
                    rect.bottom <= chat_bottom + 30
                )

                if in_zone:
                    # 尝试多种方式获取文本
                    text = self._get_control_text(child)
                    if text and len(text) >= 1 and len(text) <= 500:
                        h = rect.height() if rect.height() > 0 else 16
                        texts.append((rect.top, rect.left, text, h))

                # 递归子控件（不限制区域，因为父控件可能很大但子控件在区域内）
                self._collect_all_texts(child, chat_left, chat_top, chat_bottom,
                                        texts, depth + 1, max_depth, unrestricted)
            except Exception:
                continue

    @staticmethod
    def _get_control_text(ctrl) -> str:
        """尝试多种方式获取控件文本。"""
        try:
            name = (ctrl.Name or "").strip()
            if name:
                return name
        except Exception:
            pass

        # 尝试 ValuePattern
        try:
            if hasattr(ctrl, 'GetValuePattern'):
                val = ctrl.GetValuePattern().Value
                val = (val or "").strip()
                if val:
                    return val
        except Exception:
            pass

        # 尝试 TextPattern
        try:
            if hasattr(ctrl, 'GetTextPattern'):
                tp = ctrl.GetTextPattern()
                if tp:
                    val = tp.DocumentRange.GetText(-1)
                    val = (val or "").strip()
                    if val:
                        return val
        except Exception:
            pass

        # 尝试 LegacyIAccessible
        try:
            if hasattr(ctrl, 'GetLegacyIAccessiblePattern'):
                lap = ctrl.GetLegacyIAccessiblePattern()
                if lap and lap.Value:
                    val = (lap.Value or "").strip()
                    if val:
                        return val
                if lap and lap.Name:
                    val = (lap.Name or "").strip()
                    if val:
                        return val
        except Exception:
            pass

        return ""

    @staticmethod
    def _clean(text: str) -> str:
        lines = []
        for line in text.split("\n"):
            s = line.strip()
            if not s:
                continue
            if len(s) <= 8 and ":" in s and all(c.isdigit() or c in ": " for c in s):
                continue
            lines.append(s)
        return "\n".join(lines).strip()
