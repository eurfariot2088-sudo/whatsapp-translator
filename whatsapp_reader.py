"""
whatsapp_reader.py
通过 Windows UI Automation 读取桌面 WhatsApp 消息。

v5 核心修复：
- process_name 不带 .exe（uiautomation 要求）
- 加 on_debug 回调，实时输出调试信息到 UI
- 增强窗口查找：遍历所有顶层窗口，列出所有匹配的窗口名
- 简化消息提取逻辑
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
        self._last_chat_title = ""
        self._last_fps: Set[str] = set()
        self._last_count = 0
        self._scan_count = 0
        self._window_found = False

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
            self._debug("已找到 WhatsApp 窗口！")

        messages = self._extract_messages(auto, window)

        if not messages:
            if self._scan_count <= 5 or self._scan_count % 30 == 0:
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
        """查找 WhatsApp 窗口 — 遍历所有顶层窗口。"""
        try:
            root = auto.GetRootControl()
            for child in root.GetChildren():
                try:
                    name = child.Name or ""
                    cname = child.ClassName or ""
                    # 匹配 WhatsApp 窗口
                    if self.cfg.window_keyword.lower() in name.lower():
                        if self._scan_count <= 2:
                            self._debug("找到窗口: '%s' class='%s'" % (name, cname))
                        return child
                except Exception:
                    continue
        except Exception as e:
            self._debug("遍历窗口失败: %s" % e)

        # 如果第一次没找到，列出所有窗口名（帮助诊断）
        if self._scan_count == 1:
            try:
                root = auto.GetRootControl()
                names = []
                for child in root.GetChildren():
                    try:
                        n = child.Name or ""
                        if n and len(n) > 1:
                            names.append(n)
                    except Exception:
                        continue
                self._debug("所有顶层窗口: %s" % ", ".join(names[:30]))
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

            # 聊天区域：右侧
            chat_left = win_left + int(win_width * 0.30)
            chat_top = win_top + int(win_height * 0.10)
            chat_bottom = win_bottom - int(win_height * 0.08)

            if self._scan_count <= 2:
                self._debug("窗口 %dx%d, 聊天区 left=%d top=%d bot=%d mid=%d" %
                            (win_width, win_height, chat_left, chat_top, chat_bottom, mid_x))
        except Exception as e:
            self._debug("获取窗口位置失败: %s" % e)
            return []

        # 策略1: 找 List 控件中的 ListItem
        messages = self._find_list_items(window, chat_left, chat_top, chat_bottom, mid_x)
        if messages:
            if self._scan_count <= 3:
                self._debug("策略1(ListItem) 找到 %d 条" % len(messages))
            return messages

        # 策略2: 递归找所有 TextControl
        messages = self._find_text_controls(window, chat_left, chat_top, chat_bottom, mid_x)
        if messages:
            if self._scan_count <= 3:
                self._debug("策略2(TextControl) 找到 %d 条" % len(messages))
            return messages

        # 策略3: 不限制区域，找所有 TextControl
        if self._scan_count <= 5:
            self._debug("策略1和2都失败，尝试策略3（全窗口）")
        messages = self._find_text_controls(window, win_left, win_top, win_bottom, mid_x,
                                             unrestricted=True)
        if messages and self._scan_count <= 5:
            self._debug("策略3(全窗口) 找到 %d 条" % len(messages))
        return messages

    # -----------------------------------------------------------------------
    # 策略1: ListItem
    # -----------------------------------------------------------------------
    def _find_list_items(self, window, chat_left, chat_top, chat_bottom, mid_x):
        messages = []
        try:
            for child in window.GetChildren():
                try:
                    ctype = child.ControlTypeName or ""
                    if "List" not in ctype:
                        continue
                    rect = child.BoundingRectangle
                    if rect.left < chat_left - 50:
                        continue
                    if rect.height() < 80:
                        continue

                    for item in child.GetChildren():
                        try:
                            irect = item.BoundingRectangle
                            if irect.left < chat_left - 50:
                                continue
                            if irect.top < chat_top - 50 or irect.bottom > chat_bottom + 50:
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
            self._debug("_find_list_items 异常: %s" % e)

        return messages

    # -----------------------------------------------------------------------
    # 策略2/3: TextControl 递归
    # -----------------------------------------------------------------------
    def _find_text_controls(self, window, chat_left, chat_top, chat_bottom, mid_x,
                             unrestricted=False):
        messages = []
        try:
            texts = []
            self._collect_text_recursive(window, chat_left, chat_top, chat_bottom,
                                         texts, depth=0, max_depth=10,
                                         unrestricted=unrestricted)

            if not texts:
                return []

            # 按 Y 排序
            texts.sort(key=lambda t: (t[0], t[1]))

            # 分组合并
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
                    if gap <= gh * 1.5:
                        cur_msg.append(grp)
                    else:
                        merged.append(cur_msg)
                        cur_msg = [grp]
                last_bottom = max(last_bottom or 0, gbot)
            if cur_msg:
                merged.append(cur_msg)

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
                if any(kw in low for kw in ["typing", "online", "last seen", "search"]):
                    continue

                direction = "in" if (fx is not None and fx < mid_x) else "out"
                messages.append(WhatsAppMessage(text=cleaned, direction=direction, y_top=fy or 0))
        except Exception as e:
            self._debug("_find_text_controls 异常: %s" % e)

        return messages

    def _collect_text_recursive(self, ctrl, chat_left, chat_top, chat_bottom,
                                 texts, depth=0, max_depth=10, unrestricted=False):
        if depth > max_depth:
            return
        try:
            children = ctrl.GetChildren()
            for child in children:
                try:
                    ctype = child.ControlTypeName or ""
                    rect = child.BoundingRectangle

                    in_zone = unrestricted or (
                        rect.left >= chat_left - 50 and
                        rect.top >= chat_top - 50 and
                        rect.bottom <= chat_bottom + 50
                    )

                    if in_zone:
                        if ctype == "TextControl":
                            name = (child.Name or "").strip()
                            if name and len(name) >= 2:
                                texts.append((rect.top, rect.left, name, rect.height()))

                    # 不管是否在区域内，都递归子控件
                    # （因为父控件可能不在区域内，但子控件在）
                    if not in_zone and not unrestricted:
                        # 如果控件完全不在区域内，也尝试递归（有些容器很大）
                        self._collect_text_recursive(child, chat_left, chat_top, chat_bottom,
                                                     texts, depth + 1, max_depth, unrestricted)
                    else:
                        self._collect_text_recursive(child, chat_left, chat_top, chat_bottom,
                                                     texts, depth + 1, max_depth, unrestricted)
                except Exception:
                    continue
        except Exception:
            pass

    # -------------------- 辅助 --------------------
    @staticmethod
    def _get_item_text(item) -> str:
        texts = []
        try:
            for sub, _ in item.WalkControl(item, includeTop=False):
                if (sub.ControlTypeName or "") == "TextControl":
                    name = (sub.Name or "").strip()
                    if name:
                        texts.append(name)
        except Exception:
            pass
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
