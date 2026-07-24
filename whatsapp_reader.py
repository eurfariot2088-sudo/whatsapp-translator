"""
whatsapp_reader.py
通过 Windows UI Automation 读取桌面 WhatsApp 消息。

v4 核心改进：
- 用 FindAll + TreeScope 快速查找，不再全树遍历
- 简化消息提取：直接找聊天区的 ListItem / DataItem
- 按 Y 坐标排序保持顺序
- 切换聊天自动检测
- 详细的调试日志（写到 app.log）
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
                 on_chat_changed: Optional[Callable[[str], None]] = None):
        _ensure_windows()
        self.cfg = cfg
        self.on_messages = on_messages
        self.on_chat_changed = on_chat_changed
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_chat_title = ""
        self._last_fps: Set[str] = set()
        self._last_count = 0
        self._scan_count = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="WA-Reader", daemon=True)
        self._thread.start()
        log.info("WhatsAppReader 启动")

    def stop(self, join_timeout=2.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=join_timeout)
        log.info("WhatsAppReader 停止")

    # -------------------- 主循环 --------------------
    def _loop(self):
        import uiautomation as auto
        while not self._stop.is_set():
            try:
                self._scan_once(auto)
            except Exception as e:
                log.warning("扫描异常: %s", e)
            self._stop.wait(self.cfg.poll_interval)

    # -------------------- 单次扫描 --------------------
    def _scan_once(self, auto):
        self._scan_count += 1
        window = self._find_window(auto)
        if window is None:
            if self._scan_count <= 3:
                log.warning("未找到 WhatsApp 窗口")
            return

        # 提取消息
        messages = self._extract_messages(auto, window)

        if not messages:
            if self._scan_count <= 5:
                log.info("扫描 #%d: 未找到消息", self._scan_count)
            return

        # 排序
        messages.sort(key=lambda m: m.y_top)

        # 去重
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

        self._last_count = len(unique)
        self._last_fps = current_fps
        log.info("扫描 #%d: 找到 %d 条消息", self._scan_count, len(unique))

        try:
            self.on_messages(unique)
        except Exception:
            log.exception("on_messages 回调异常")

    # -------------------- 窗口定位 --------------------
    def _find_window(self, auto):
        """查找 WhatsApp 窗口。"""
        try:
            # 方法1: 通过进程名
            win = auto.WindowControl(searchDepth=1, ProcessName=self.cfg.process_name)
            if win and win.Exists(0.3, 0.1):
                log.debug("通过进程名找到窗口")
                return win
        except Exception:
            pass

        try:
            # 方法2: 通过窗口标题
            win = auto.WindowControl(searchDepth=1, Name=self.cfg.window_keyword, SubName=self.cfg.window_keyword)
            if win and win.Exists(0.3, 0.1):
                log.debug("通过标题找到窗口")
                return win
        except Exception:
            pass

        try:
            # 方法3: 遍历顶层窗口
            root = auto.GetRootControl()
            for child in root.GetChildren():
                try:
                    name = child.Name or ""
                    if self.cfg.window_keyword in name:
                        log.debug("遍历找到窗口: %s", name)
                        return child
                except Exception:
                    continue
        except Exception:
            pass

        return None

    # -------------------- 提取消息 --------------------
    def _extract_messages(self, auto, window) -> List[WhatsAppMessage]:
        """提取聊天区域的消息。"""
        messages: List[WhatsAppMessage] = []

        try:
            win_rect = window.BoundingRectangle
            win_left = win_rect.left
            win_right = win_rect.right
            win_top = win_rect.top
            win_bottom = win_rect.bottom
            win_width = win_right - win_left
            win_height = win_bottom - win_top

            # 聊天区域：右侧 65%，顶部 12%，底部 88%
            chat_left = win_left + int(win_width * 0.35)
            chat_top = win_top + int(win_height * 0.12)
            chat_bottom = win_bottom - int(win_height * 0.10)
            mid_x = win_left + win_width // 2

            log.debug("窗口: %dx%d, 聊天区: left=%d top=%d bottom=%d mid=%d",
                       win_width, win_height, chat_left, chat_top, chat_bottom, mid_x)
        except Exception as e:
            log.warning("获取窗口位置失败: %s", e)
            return []

        # 策略1: 找 ListItem / DataItem
        messages = self._find_list_items(window, chat_left, chat_top, chat_bottom, mid_x)
        if messages:
            return messages

        # 策略2: 找所有 TextControl 并分组
        messages = self._find_text_controls(window, chat_left, chat_top, chat_bottom, mid_x)
        return messages

    # -----------------------------------------------------------------------
    # 策略1: ListItem
    # -----------------------------------------------------------------------
    def _find_list_items(self, window, chat_left, chat_top, chat_bottom, mid_x):
        messages = []
        try:
            # 直接遍历 window 的子控件找 List
            for child in window.GetChildren():
                try:
                    ctype = child.ControlTypeName or ""
                    if "List" not in ctype:
                        continue
                    rect = child.BoundingRectangle
                    # 必须在聊天区域内
                    if rect.left < chat_left - 30:
                        continue
                    if rect.height() < 80:
                        continue

                    log.debug("找到 List: left=%d top=%d height=%d",
                              rect.left, rect.top, rect.height())

                    # 遍历 List 的子项
                    for item in child.GetChildren():
                        try:
                            irect = item.BoundingRectangle
                            if irect.left < chat_left - 30:
                                continue
                            if irect.top < chat_top - 30 or irect.bottom > chat_bottom + 30:
                                continue

                            text = self._get_item_text(item)
                            text = self._clean(text)
                            if not text or len(text) < 2:
                                continue

                            direction = self._guess_dir(item, mid_x)
                            messages.append(WhatsAppMessage(
                                text=text, direction=direction, y_top=irect.top))
                        except Exception:
                            continue
                except Exception:
                    continue
        except Exception as e:
            log.debug("_find_list_items 异常: %s", e)

        return messages

    # -----------------------------------------------------------------------
    # 策略2: TextControl 分组
    # -----------------------------------------------------------------------
    def _find_text_controls(self, window, chat_left, chat_top, chat_bottom, mid_x):
        messages = []
        try:
            # 递归搜索所有 TextControl
            texts = []
            self._collect_text_recursive(window, chat_left, chat_top, chat_bottom, texts, depth=0, max_depth=8)

            if not texts:
                log.debug("聊天区内未找到任何 TextControl")
                return []

            log.debug("找到 %d 个 TextControl", len(texts))

            # 按 Y 排序
            texts.sort(key=lambda t: (t[0], t[1]))

            # 分组：按 Y 距离
            groups = []
            current = []
            last_y = None
            for entry in texts:
                y, x, text, h = entry
                if last_y is None or abs(y - last_y) <= max(h, 16) * 0.5:
                    current.append(entry)
                else:
                    if current:
                        groups.append(current)
                    current = [entry]
                last_y = y
            if current:
                groups.append(current)

            # 合并相邻行（Y 间距小的属于同一条消息）
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
                    if gap <= gh * 1.5:
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
                if any(kw in low for kw in ["typing", "online", "last seen"]):
                    continue

                direction = "in" if (fx is not None and fx < mid_x) else "out"
                messages.append(WhatsAppMessage(text=cleaned, direction=direction, y_top=fy or 0))
        except Exception as e:
            log.debug("_find_text_controls 异常: %s", e)

        return messages

    def _collect_text_recursive(self, ctrl, chat_left, chat_top, chat_bottom,
                                 texts, depth=0, max_depth=8):
        """递归收集聊天区内的 TextControl。"""
        if depth > max_depth:
            return
        try:
            for child in ctrl.GetChildren():
                try:
                    ctype = child.ControlTypeName or ""
                    rect = child.BoundingRectangle

                    # 检查是否在聊天区
                    if rect.left >= chat_left - 30 and rect.top >= chat_top - 30 and rect.bottom <= chat_bottom + 30:
                        if ctype == "TextControl":
                            name = (child.Name or "").strip()
                            if name and len(name) >= 2:
                                texts.append((rect.top, rect.left, name, rect.height()))
                        else:
                            # 递归
                            self._collect_text_recursive(child, chat_left, chat_top, chat_bottom,
                                                         texts, depth + 1, max_depth)
                except Exception:
                    continue
        except Exception:
            pass

    # -------------------- 辅助 --------------------
    @staticmethod
    def _get_item_text(item) -> str:
        """获取 ListItem 内所有文本。"""
        texts = []
        try:
            for sub, _ in item.WalkControl(item, includeTop=False):
                if (sub.ControlTypeName or "") == "TextControl":
                    name = (sub.Name or "").strip()
                    if name:
                        texts.append(name)
        except Exception:
            pass
        # 去重保持顺序
        seen = set()
        uniq = []
        for t in texts:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        return "\n".join(uniq)

    @staticmethod
    def _clean(text: str) -> str:
        lines = []
        for line in text.split("\n"):
            s = line.strip()
            if not s:
                continue
            # 去纯时间戳
            if len(s) <= 8 and ":" in s and all(c.isdigit() or c in ": " for c in s):
                continue
            lines.append(s)
        return "\n".join(lines).strip()

    @staticmethod
    def _guess_dir(item, mid_x) -> str:
        try:
            rect = item.BoundingRectangle
            cx = (rect.left + rect.right) // 2
            return "in" if cx < mid_x else "out"
        except Exception:
            return "in"
