"""
whatsapp_reader.py
通过 Win32 API + OCR 读取桌面 WhatsApp 消息。

v9 — OCR 为主方式：
- 使用 win32gui 精确查找 WhatsApp 窗口（底层 API，绕过 UI Automation 限制）
- 直接截取聊天区域图片，用 Windows 内置 OCR 识别文字
- 根据文字位置判断消息方向（左=收到，右=发送）
- 实时更新，支持切换对话框和滚动
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
        self._window_hwnd = 0
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

    def probe_controls(self):
        """探测 WhatsApp 窗口信息，输出到调试日志。"""
        self._debug("[探测] === 开始探测 WhatsApp 窗口 ===")

        # 检查 OCR 引擎
        self._debug("[探测] === OCR 引擎状态 ===")
        try:
            from ocr_engine import is_available, get_backend_name
            if is_available():
                self._debug("[探测] OCR 引擎可用: %s" % get_backend_name())
            else:
                self._debug("[探测] OCR 引擎不可用！请检查：")
                self._debug("[探测]   - 是否 Windows 10/11 系统？")
                self._debug("[探测]   - 是否安装了 OCR 语言包？")
                self._debug("[探测]   - winsdk 是否安装？")
        except Exception as e:
            self._debug("[探测] OCR 检查失败: %s" % e)

        # 查找窗口
        hwnd = self._find_whatsapp_window()
        if hwnd == 0:
            self._debug("[探测] 未找到 WhatsApp 窗口")
            # 列出所有可见窗口供调试
            self._debug("[探测] === 所有可见窗口 ===")
            import win32gui
            import psutil
            import os
            current_pid = os.getpid()

            def list_callback(hwnd, lparam):
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                title = win32gui.GetWindowText(hwnd)
                if not title:
                    return True
                tid, pid = win32gui.GetWindowThreadProcessId(hwnd)
                is_self = pid == current_pid
                try:
                    proc = psutil.Process(pid)
                    pname = proc.name()
                except Exception:
                    pname = "unknown"
                self._debug("[探测]   标题: '%s' | 进程: %s | 句柄: 0x%08X | 自己: %s" %
                            (title, pname, hwnd, is_self))
                return True

            win32gui.EnumWindows(list_callback, 0)
        else:
            import win32gui
            import win32con
            title = win32gui.GetWindowText(hwnd)
            rect = win32gui.GetWindowRect(hwnd)
            tid, pid = win32gui.GetWindowThreadProcessId(hwnd)
            self._debug("[探测] 窗口句柄: 0x%08X" % hwnd)
            self._debug("[探测] 窗口标题: '%s'" % title)
            self._debug("[探测] 窗口位置: (%d,%d) 大小: %dx%d" %
                        (rect[0], rect[1], rect[2]-rect[0], rect[3]-rect[1]))
            try:
                import psutil
                proc = psutil.Process(pid)
                self._debug("[探测] 进程名: %s (PID: %d)" % (proc.name(), pid))
            except Exception:
                pass

            # 查找子窗口
            self._debug("[探测] === 查找子窗口 ===")
            self._enum_child_windows(hwnd, depth=0, max_depth=4)

        self._debug("[探测] === 探测结束 ===")

    def _enum_child_windows(self, hwnd, depth, max_depth):
        if depth > max_depth:
            return
        import win32gui
        import win32con

        def callback(child_hwnd, lparam):
            if not win32gui.IsWindowVisible(child_hwnd):
                return True
            try:
                class_name = win32gui.GetClassName(child_hwnd)
                title = win32gui.GetWindowText(child_hwnd)
                rect = win32gui.GetWindowRect(child_hwnd)
                prefix = "  " * depth
                title_display = title[:40] if title else "(无标题)"
                self._debug("[探测] %s类名: %s | 标题: %s | 位置: (%d,%d,%d,%d)" %
                            (prefix, class_name, title_display, rect[0], rect[1], rect[2], rect[3]))
                self._enum_child_windows(child_hwnd, depth + 1, max_depth)
            except Exception:
                pass
            return True

        win32gui.EnumChildWindows(hwnd, callback, 0)

    # -------------------- 主循环 --------------------
    def _loop(self):
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception as e:
                self._debug("扫描异常: %s" % e)
                log.exception("扫描异常")
            self._stop.wait(self.cfg.poll_interval)

    # -------------------- 单次扫描 --------------------
    def _scan_once(self):
        self._scan_count += 1

        # 1. 查找 WhatsApp 窗口
        hwnd = self._find_whatsapp_window()
        if hwnd == 0:
            if self._scan_count <= 3 or self._scan_count % 20 == 0:
                self._debug("未找到 WhatsApp 窗口 (扫描 #%d)" % self._scan_count)
            return

        # 窗口句柄变化 = 切换了窗口
        if hwnd != self._window_hwnd:
            self._window_hwnd = hwnd
            self._window_found = True
            import win32gui
            title = win32gui.GetWindowText(hwnd)
            self._debug("已找到 WhatsApp 窗口！标题: '%s' 句柄: 0x%08X" % (title, hwnd))
            if self.on_chat_changed:
                try:
                    self.on_chat_changed(title)
                except Exception:
                    pass

        # 2. 获取窗口区域
        rect = self._get_window_rect(hwnd)
        if not rect:
            return
        left, top, right, bottom = rect
        width = right - left
        height = bottom - top

        if width < 300 or height < 300:
            if self._scan_count % 20 == 0:
                self._debug("窗口太小，可能被最小化")
            return

        # 3. 截取聊天区域并 OCR 识别
        messages = self._ocr_chat_area(left, top, right, bottom)

        if not messages:
            if self._scan_count <= 5 or self._scan_count % 30 == 0:
                self._debug("扫描 #%d: 未识别到消息" % self._scan_count)
            return

        messages.sort(key=lambda m: m.y_top)

        # 4. 去重
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

        # 5. 回调
        try:
            self.on_messages(unique)
        except Exception:
            log.exception("on_messages 回调异常")

    # -------------------- 查找 WhatsApp 窗口（多策略） --------------------
    def _find_whatsapp_window(self) -> int:
        import win32gui
        import win32con
        import psutil
        import os

        current_pid = os.getpid()

        # 策略1: 优先按进程名查找（最可靠，不会找到自己）
        def callback1(hwnd, lparam):
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if not win32gui.GetWindowText(hwnd):
                return True
            tid, pid = win32gui.GetWindowThreadProcessId(hwnd)
            if pid == current_pid:
                return True
            try:
                proc = psutil.Process(pid)
                pname = proc.name().lower()
                if self.cfg.process_name.lower() in pname:
                    lparam[0] = hwnd
                    return False
            except Exception:
                pass
            return True

        result = [0]
        win32gui.EnumWindows(callback1, result)
        if result[0] != 0:
            return result[0]

        # 策略2: 按标题关键词查找，但排除自己的窗口
        def callback2(hwnd, lparam):
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if not win32gui.IsWindowEnabled(hwnd):
                return True
            tid, pid = win32gui.GetWindowThreadProcessId(hwnd)
            if pid == current_pid:
                return True
            title = win32gui.GetWindowText(hwnd).lower()
            if self.cfg.window_keyword.lower() in title:
                lparam[0] = hwnd
                return False
            return True

        result = [0]
        win32gui.EnumWindows(callback2, result)
        if result[0] != 0:
            return result[0]

        # 策略3: 按类名查找（Electron/Chrome 类名）
        class_names = ["Chrome_WidgetWin_1", "Qt51514QWindowIcon", "Chrome_WidgetWin_0"]
        for cls in class_names:
            hwnd = win32gui.FindWindow(cls, None)
            if hwnd != 0:
                tid, pid = win32gui.GetWindowThreadProcessId(hwnd)
                if pid == current_pid:
                    continue
                title = win32gui.GetWindowText(hwnd)
                if self.cfg.window_keyword.lower() in title.lower():
                    return hwnd

        return 0

    # -------------------- 获取窗口区域 --------------------
    def _get_window_rect(self, hwnd) -> Optional[tuple]:
        import win32gui
        try:
            rect = win32gui.GetWindowRect(hwnd)
            left, top, right, bottom = rect
            if right > left and bottom > top:
                return (left, top, right, bottom)
        except Exception as e:
            self._debug("获取窗口区域失败: %s" % e)
        return None

    # -------------------- OCR 识别聊天区域 --------------------
    def _ocr_chat_area(self, left, top, right, bottom) -> List[WhatsAppMessage]:
        """截取聊天区域并 OCR 识别。"""
        try:
            import mss
            from PIL import Image
        except ImportError:
            self._debug("缺少截图依赖")
            return []

        try:
            from ocr_engine import ocr_image_with_data, is_available
        except ImportError:
            self._debug("OCR 引擎不可用")
            return []

        if not is_available():
            if self._scan_count % 30 == 0:
                self._debug("OCR 引擎未就绪")
            return []

        width = right - left
        height = bottom - top

        # WhatsApp 聊天区域布局：
        # - 左侧：好友列表（约占 30% 宽度）
        # - 右侧：聊天区域（约占 70% 宽度）
        # - 顶部：标题栏（约占 8% 高度）
        # - 底部：输入框（约占 10% 高度）

        chat_left = left + int(width * 0.32)
        chat_top = top + int(height * 0.08)
        chat_right = right - int(width * 0.03)
        chat_bottom = bottom - int(height * 0.10)

        # 确保区域有效
        if chat_right - chat_left < 150 or chat_bottom - chat_top < 100:
            if self._scan_count % 30 == 0:
                self._debug("聊天区域太小: %dx%d" % (chat_right - chat_left, chat_bottom - chat_top))
            return []

        # 截取聊天区域
        try:
            with mss.mss() as sct:
                monitor = {
                    "left": chat_left,
                    "top": chat_top,
                    "width": chat_right - chat_left,
                    "height": chat_bottom - chat_top,
                }
                raw = sct.grab(monitor)
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        except Exception as e:
            self._debug("截图失败: %s" % e)
            return []

        # OCR 识别
        try:
            ocr_data = ocr_image_with_data(img, languages=["en", "zh-Hans"])
        except Exception as e:
            if self._scan_count % 20 == 0:
                self._debug("OCR 识别失败: %s" % e)
            return []

        n_boxes = len(ocr_data['text'])
        if n_boxes == 0:
            return []

        # 解析 OCR 结果，按行分组
        lines = {}
        for i in range(n_boxes):
            text = (ocr_data['text'][i] or "").strip()
            if not text:
                continue
            conf = int(ocr_data['conf'][i])
            if conf < 25:
                continue

            top = ocr_data['top'][i]
            left_in_img = ocr_data['left'][i]
            height = ocr_data['height'][i]
            width_in_img = ocr_data['width'][i]

            # 按行分组（同一行的文字会很接近）
            line_key = round(top / max(height, 12))
            if line_key not in lines:
                lines[line_key] = []
            lines[line_key].append((top, left_in_img, text, height, width_in_img))

        # 合并每行的文字
        sorted_keys = sorted(lines.keys())
        all_lines = []
        chat_region_w = chat_right - chat_left

        for key in sorted_keys:
            entries = lines[key]
            entries.sort(key=lambda e: e[1])
            line_text = " ".join(e[2] for e in entries)
            avg_top = sum(e[0] for e in entries) / len(entries)
            avg_h = sum(e[3] for e in entries) / len(entries)
            min_left = min(e[1] for e in entries)
            max_right = max(e[1] + e[4] for e in entries)

            # 判断消息方向：
            # 收到的消息（in）：左对齐，气泡在左侧
            # 发送的消息（out）：右对齐，气泡在右侧
            center_x = chat_region_w // 2
            is_right = max_right > center_x and min_left > chat_region_w * 0.3
            direction = "out" if is_right else "in"

            all_lines.append((avg_top, min_left, line_text, avg_h, direction))

        all_lines.sort(key=lambda l: l[0])

        # 合并相邻行为完整消息
        messages: List[WhatsAppMessage] = []
        cur_lines = []
        last_top = None
        last_h = None
        last_dir = None

        for top, left_in_img, text, h, direction in all_lines:
            cleaned = self._clean(text)
            if not cleaned or len(cleaned) < 2:
                continue

            # 过滤 UI 元素和无意义文字
            low = cleaned.lower()
            skip = [
                "typing", "online", "last seen", "search", "unread",
                "archived", "pinned", "muted", "whatsapp", "type a message",
                "messages", "status", "communities", "chats", "settings",
                "calls", "new chat", "you", "delivered", "read", "sent",
                "minimize", "maximize", "close", "adjust", "appwindow",
                "custom title bar", "bar"
            ]
            if any(kw in low for kw in skip):
                continue

            # 过滤纯数字/时间/特殊字符
            if all(c.isdigit() or c in ":.,!? " for c in cleaned) and len(cleaned) < 10:
                continue

            # 判断是否同一消息的延续
            is_continue = False
            if last_top is not None:
                gap = top - (last_top + last_h)
                # 同一消息：间距小且方向相同
                if gap <= (last_h or 16) * 1.5 and direction == last_dir:
                    is_continue = True

            if is_continue:
                cur_lines.append(cleaned)
            else:
                if cur_lines:
                    msg_text = "\n".join(cur_lines)
                    fy = last_top + chat_top
                    fx = left_in_img + chat_left
                    messages.append(WhatsAppMessage(
                        text=msg_text, direction=last_dir, y_top=fy
                    ))
                cur_lines = [cleaned]

            last_top = top
            last_h = h
            last_dir = direction

        # 最后一条消息
        if cur_lines:
            msg_text = "\n".join(cur_lines)
            fy = last_top + chat_top if last_top else chat_top
            fx = left_in_img + chat_left
            messages.append(WhatsAppMessage(
                text=msg_text, direction=last_dir or "in", y_top=fy
            ))

        return messages

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
