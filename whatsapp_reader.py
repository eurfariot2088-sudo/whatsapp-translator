"""
whatsapp_reader.py
通过 Windows UI Automation 读取桌面 WhatsApp 消息。

v8 — 增强版：
- 多策略窗口查找（标题关键词 / 进程名 / 类名 / Win32 API）
- UI Automation 多模式文本提取 + WebView2 特殊处理
- OCR 自动兜底：使用 Windows 内置 OCR（无需安装 Tesseract）
- 更详细的调试日志，方便排查
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
        self._ocr_mode = False
        self._ocr_available = None  # None=未检测, True/False

    def _debug(self, msg: str):
        log.info(msg)
        if self.on_debug:
            try:
                self.on_debug(msg)
            except Exception:
                pass

    def _check_ocr(self) -> bool:
        if self._ocr_available is not None:
            return self._ocr_available
        try:
            from ocr_engine import is_available
            self._ocr_available = is_available()
            if self._ocr_available:
                from ocr_engine import get_backend_name
                self._debug("OCR 引擎可用: %s" % get_backend_name())
            else:
                self._debug("OCR 引擎不可用，仅使用 UI Automation")
        except Exception as e:
            self._debug("OCR 检测失败: %s" % e)
            self._ocr_available = False
        return self._ocr_available

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

        # 额外：尝试找 WebView2 / Chrome / Electron 相关控件
        self._debug("[探测] === 查找 WebView/Electron 容器 ===")
        self._find_webview_containers(window, depth=0)

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

                has_name = len(name) >= 1
                is_container = ctype in ["PaneControlType", "GroupControlType", "ListControlType",
                                          "ListItemControlType", "DataItemControlType",
                                          "TabControlType", "TreeControlType", "WindowControlType",
                                          "DocumentControlType", "EditControlType", "TextControlType",
                                          "CustomControlType", "ToolBarControlType"]

                if has_name or is_container:
                    try:
                        rect = child.BoundingRectangle
                        pos_str = "(%d,%d,%d,%d)" % (rect.left, rect.top, rect.right, rect.bottom)
                    except Exception:
                        pos_str = "?"

                    prefix = "  " * depth
                    name_display = name[:60] if name else "(无名称)"
                    self._debug("[探测] %s%s | %s | %s | %s" %
                                (prefix, ctype, cname, name_display, pos_str))
            except Exception:
                continue

            self._probe_children(child, depth + 1, max_depth)

    def _find_webview_containers(self, ctrl, depth):
        if depth > 10:
            return
        try:
            cname = (ctrl.ClassName or "").lower()
            ctype = (ctrl.ControlTypeName or "").lower()
            name = (ctrl.Name or "").lower()

            keywords = ["webview", "chrome", "electron", "cef", "edge", "widgetwin", "intermediate"]
            if any(kw in cname for kw in keywords) or any(kw in name for kw in keywords):
                try:
                    rect = ctrl.BoundingRectangle
                    pos_str = "(%d,%d,%d,%d)" % (rect.left, rect.top, rect.right, rect.bottom)
                except Exception:
                    pos_str = "?"
                self._debug("[探测] 找到浏览器容器: %s | %s | %s" % (ctype, ctrl.ClassName, pos_str))

            for child in ctrl.GetChildren():
                self._find_webview_containers(child, depth + 1)
        except Exception:
            pass

    # -------------------- 主循环 --------------------
    def _loop(self):
        import uiautomation as auto
        consec_fail = 0
        while not self._stop.is_set():
            try:
                found = self._scan_once(auto)
                if not found:
                    consec_fail += 1
                    # 连续 5 次没提取到消息，尝试 OCR 兜底
                    if consec_fail >= 5 and not self._ocr_mode:
                        if self._check_ocr():
                            self._ocr_mode = True
                            self._debug("UI Automation 连续未提取到消息，切换到 OCR 模式")
                        else:
                            if consec_fail == 5:
                                self._debug("UI Automation 无法提取消息，且 OCR 不可用。请确保 WhatsApp 窗口可见。")
                else:
                    consec_fail = 0
            except Exception as e:
                self._debug("扫描异常: %s" % e)
                log.exception("扫描异常")
            self._stop.wait(self.cfg.poll_interval)

    # -------------------- 单次扫描 --------------------
    def _scan_once(self, auto) -> bool:
        self._scan_count += 1

        window = self._find_window(auto)
        if window is None:
            if self._scan_count <= 3 or self._scan_count % 20 == 0:
                self._debug("未找到 WhatsApp 窗口 (扫描 #%d)，请确保 WhatsApp 已打开" % self._scan_count)
            return False

        if not self._window_found:
            self._window_found = True
            self._window_ref = window
            self._debug("已找到 WhatsApp 窗口！标题: '%s' Class: '%s'" % (window.Name, window.ClassName))

        # 先尝试 UI Automation
        messages = self._extract_messages_uia(auto, window)

        # UI Automation 没拿到，且 OCR 模式已开启，用 OCR
        if not messages and self._ocr_mode:
            messages = self._extract_messages_ocr(window)
            if messages and self._scan_count % 10 == 0:
                self._debug("OCR 模式提取到 %d 条消息" % len(messages))

        if not messages:
            if self._scan_count <= 10 or self._scan_count % 30 == 0:
                mode = "UI Automation" if not self._ocr_mode else "OCR"
                self._debug("扫描 #%d: 窗口已找到，但未提取到消息（模式: %s）" % (self._scan_count, mode))
            return True

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
            return True

        old_count = self._last_count
        self._last_count = len(unique)
        self._last_fps = current_fps
        mode = "UI Automation" if not self._ocr_mode else "OCR"
        self._debug("扫描 #%d: %d 条消息 (上次 %d, 模式: %s)" % (self._scan_count, len(unique), old_count, mode))

        try:
            self.on_messages(unique)
        except Exception:
            log.exception("on_messages 回调异常")

        return True

    # -------------------- 窗口定位（多策略） --------------------
    def _find_window(self, auto):
        # 策略1: 按标题关键词（最常见）
        try:
            root = auto.GetRootControl()
            for child in root.GetChildren():
                try:
                    name = (child.Name or "").lower()
                    if self.cfg.window_keyword.lower() in name:
                        return child
                except Exception:
                    continue
        except Exception:
            pass

        # 策略2: 按进程名（Win32 API）
        try:
            import ctypes
            import ctypes.wintypes
            EnumWindows = ctypes.windll.user32.EnumWindows
            EnumWindowsProc = ctypes.WINFUNCTYPE(
                ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
            )
            GetWindowTextW = ctypes.windll.user32.GetWindowTextW
            GetWindowTextLengthW = ctypes.windll.user32.GetWindowTextLengthW
            IsWindowVisible = ctypes.windll.user32.IsWindowVisible
            GetWindowThreadProcessId = ctypes.windll.user32.GetWindowThreadProcessId

            found_hwnd = []

            def foreach(hwnd, lParam):
                if not IsWindowVisible(hwnd):
                    return True
                length = GetWindowTextLengthW(hwnd)
                if length == 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value

                # 检查进程名
                pid = ctypes.wintypes.DWORD()
                GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                try:
                    from psutil import Process
                    proc = Process(pid.value)
                    pname = proc.name().lower()
                    if self.cfg.process_name.lower() in pname:
                        found_hwnd.append(hwnd)
                        return False
                except Exception:
                    pass

                # 同时检查标题
                if self.cfg.window_keyword.lower() in title.lower():
                    found_hwnd.append(hwnd)
                    return False
                return True

            EnumWindows(EnumWindowsProc(foreach), 0)
            if found_hwnd:
                hwnd = found_hwnd[0]
                try:
                    return auto.ControlFromHandle(hwnd)
                except Exception:
                    pass
        except Exception:
            pass

        # 策略3: 用 uiautomation 的窗口搜索（正则匹配）
        try:
            win = auto.WindowControl(searchDepth=1, Name=auto.RegexMatcher(
                r".*" + self.cfg.window_keyword + r".*"))
            if win.Exists(0.5):
                return win
        except Exception:
            pass

        # 策略4: 按类名查找（Chrome_WidgetWin_1 等 Electron 常见类名）
        try:
            import ctypes
            import ctypes.wintypes
            FindWindow = ctypes.windll.user32.FindWindowW
            class_names = ["Chrome_WidgetWin_1", "MozillaWindowClass", "IEFrame", "Qt51514QWindowIcon"]
            for cls in class_names:
                hwnd = FindWindow(cls, None)
                if hwnd:
                    # 检查标题是否包含关键词
                    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                        if self.cfg.window_keyword.lower() in buf.value.lower():
                            try:
                                return auto.ControlFromHandle(hwnd)
                            except Exception:
                                pass
        except Exception:
            pass

        return None

    # -------------------- 提取消息（UI Automation 方式） --------------------
    def _extract_messages_uia(self, auto, window) -> List[WhatsAppMessage]:
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

        # 策略1: 全控件递归 + 多模式文本获取
        texts = []
        self._collect_all_texts(window, chat_left, chat_top, chat_bottom, texts,
                                depth=0, max_depth=20)

        if not texts:
            # 策略2: 全窗口搜索（不限制区域）
            self._collect_all_texts(window, win_left, win_top, win_bottom, texts,
                                    depth=0, max_depth=20, unrestricted=True)

        if not texts:
            # 策略3: 找 DocumentControl 并尝试获取全文
            self._collect_from_document(window, texts)

        if not texts:
            # 策略4: 按控件类型专门搜索 Text/Edit/Document
            self._collect_by_type(window, win_left, win_top, win_bottom, texts,
                                   depth=0, max_depth=20)

        if not texts:
            return []

        if self._scan_count <= 3:
            self._debug("找到 %d 个带文本的控件" % len(texts))

        # 按 Y 排序
        texts.sort(key=lambda t: (t[0], t[1]))

        # 过滤左侧好友列表区域的文字
        chat_texts = [
            t for t in texts
            if t[1] >= chat_left - 50  # left 坐标在聊天区域内
        ]

        if not chat_texts:
            chat_texts = texts  # 全用

        # 分组：按 Y 距离（同一行）
        groups = []
        current = []
        last_y = None
        for entry in chat_texts:
            y, x, text, h = entry
            if last_y is None or abs(y - last_y) <= max(h, 16) * 0.7:
                current.append(entry)
            else:
                if current:
                    groups.append(current)
                current = [entry]
            last_y = y
        if current:
            groups.append(current)

        # 合并相邻行为一条消息
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
            skip = ["typing", "online", "last seen", "search", "unread",
                    "archived", "pinned", "muted", "whatsapp", "type a message",
                    "messages", "status", "communities", "chats", "settings",
                    "calls", "new chat"]
            if any(kw in low for kw in skip):
                continue

            # 过滤纯数字/时间/特殊字符
            if all(c.isdigit() or c in ":.,!? " for c in cleaned) and len(cleaned) < 10:
                continue

            # 过滤太短的疑似 UI 元素
            if len(cleaned) < 2:
                continue

            direction = "in" if (fx is not None and fx < mid_x) else "out"
            messages.append(WhatsAppMessage(text=cleaned, direction=direction, y_top=fy or 0))

        return messages

    def _collect_all_texts(self, ctrl, chat_left, chat_top, chat_bottom,
                            texts, depth=0, max_depth=20, unrestricted=False):
        """递归收集所有有文本的控件（不限类型）。"""
        if depth > max_depth:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            return

        for child in children:
            try:
                rect = child.BoundingRectangle

                in_zone = unrestricted or (
                    rect.left >= chat_left - 50 and
                    rect.top >= chat_top - 30 and
                    rect.bottom <= chat_bottom + 30
                )

                if in_zone:
                    text = self._get_control_text(child)
                    if text and 1 <= len(text) <= 500:
                        h = rect.height() if rect.height() > 0 else 16
                        texts.append((rect.top, rect.left, text, h))

                # 递归子控件
                self._collect_all_texts(child, chat_left, chat_top, chat_bottom,
                                        texts, depth + 1, max_depth, unrestricted)
            except Exception:
                continue

    def _collect_from_document(self, ctrl, texts):
        """专门找 DocumentControl 并获取全文。"""
        try:
            ctype = (ctrl.ControlTypeName or "")
            if ctype == "DocumentControlType":
                text = self._get_control_text(ctrl)
                if text and len(text) > 10:
                    try:
                        rect = ctrl.BoundingRectangle
                        h = rect.height() if rect.height() > 0 else 16
                        # Document 的全文按行拆分
                        for i, line in enumerate(text.split("\n")):
                            line = line.strip()
                            if line:
                                texts.append((rect.top + i * 20, rect.left, line, h))
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            for child in ctrl.GetChildren():
                self._collect_from_document(child, texts)
        except Exception:
            pass

    def _collect_by_type(self, ctrl, win_left, win_top, win_bottom,
                          texts, depth=0, max_depth=20):
        """按控件类型收集文本（Text/Edit/Document/Custom）。"""
        if depth > max_depth:
            return
        try:
            ctype = ctrl.ControlTypeName or ""
            if ctype in ["TextControlType", "EditControlType", "DocumentControlType", "CustomControlType"]:
                try:
                    text = self._get_control_text(ctrl)
                    if text and 1 <= len(text) <= 500:
                        rect = ctrl.BoundingRectangle
                        h = rect.height() if rect.height() > 0 else 16
                        texts.append((rect.top, rect.left, text, h))
                except Exception:
                    pass
        except Exception:
            pass

        try:
            for child in ctrl.GetChildren():
                self._collect_by_type(child, win_left, win_top, win_bottom,
                                       texts, depth + 1, max_depth)
        except Exception:
            pass

    @staticmethod
    def _get_control_text(ctrl) -> str:
        """尝试多种方式获取控件文本。"""
        # 1. Name 属性
        try:
            name = (ctrl.Name or "").strip()
            if name:
                return name
        except Exception:
            pass

        # 2. ValuePattern
        try:
            if hasattr(ctrl, 'GetValuePattern'):
                val = ctrl.GetValuePattern().Value
                val = (val or "").strip()
                if val:
                    return val
        except Exception:
            pass

        # 3. TextPattern
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

        # 4. LegacyIAccessible
        try:
            if hasattr(ctrl, 'GetLegacyIAccessiblePattern'):
                lap = ctrl.GetLegacyIAccessiblePattern()
                if lap:
                    val = (lap.Value or "").strip()
                    if val:
                        return val
                    val = (lap.Name or "").strip()
                    if val:
                        return val
        except Exception:
            pass

        # 5. RangeValuePattern
        try:
            if hasattr(ctrl, 'GetRangeValuePattern'):
                rv = ctrl.GetRangeValuePattern()
                if rv and rv.Value is not None:
                    val = str(rv.Value).strip()
                    if val:
                        return val
        except Exception:
            pass

        # 6. 尝试获取子控件的文本（拼接）
        try:
            children = ctrl.GetChildren()
            if len(children) > 0 and len(children) <= 30:
                parts = []
                for c in children:
                    try:
                        t = (c.Name or "").strip()
                        if t and 1 <= len(t) <= 100:
                            parts.append(t)
                    except Exception:
                        continue
                if parts and len(parts) >= 2:
                    combined = " ".join(parts)
                    if len(combined) <= 500:
                        return combined
        except Exception:
            pass

        return ""

    # -------------------- 提取消息（OCR 兜底方式） --------------------
    def _extract_messages_ocr(self, window) -> List[WhatsAppMessage]:
        """用 OCR 识别聊天区域的文字。"""
        try:
            import mss  # type: ignore
            from PIL import Image
        except ImportError:
            return []

        try:
            from ocr_engine import ocr_image_with_data
        except ImportError:
            return []

        try:
            win_rect = window.BoundingRectangle
            win_left = win_rect.left
            win_right = win_rect.right
            win_top = win_rect.top
            win_bottom = win_rect.bottom
            win_width = win_right - win_left
            win_height = win_bottom - win_top

            # 聊天区域：右侧约 65% 宽度，去掉顶部标题栏和底部输入框
            chat_left = win_left + int(win_width * 0.32)
            chat_top = win_top + int(win_height * 0.08)
            chat_bottom = win_bottom - int(win_height * 0.12)
            chat_right = win_right - int(win_width * 0.03)
            mid_x = chat_left + (chat_right - chat_left) // 2

            if chat_right - chat_left < 100 or chat_bottom - chat_top < 100:
                return []

            # 截取聊天区域
            with mss.mss() as sct:
                monitor = {
                    "left": chat_left,
                    "top": chat_top,
                    "width": chat_right - chat_left,
                    "height": chat_bottom - chat_top,
                }
                raw = sct.grab(monitor)
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

            # OCR 识别
            try:
                ocr_data = ocr_image_with_data(img, languages=["en", "zh-Hans"])
            except Exception:
                # 纯英文试试
                try:
                    ocr_data = ocr_image_with_data(img, languages=["en"])
                except Exception as e:
                    if self._scan_count % 30 == 0:
                        self._debug("OCR 识别失败: %s" % e)
                    return []

            # 解析 OCR 结果
            n_boxes = len(ocr_data['text'])
            lines = {}

            for i in range(n_boxes):
                text = (ocr_data['text'][i] or "").strip()
                if not text:
                    continue
                conf = int(ocr_data['conf'][i])
                if conf < 30:
                    continue
                top = ocr_data['top'][i]
                left = ocr_data['left'][i]
                height = ocr_data['height'][i]
                width = ocr_data['width'][i]
                # 用 (top, height) 来分组行
                line_key = round(top / max(height, 10))
                if line_key not in lines:
                    lines[line_key] = []
                lines[line_key].append((top, left, text, height, width))

            # 按行排序并合并
            sorted_keys = sorted(lines.keys())
            all_lines = []
            for key in sorted_keys:
                entries = lines[key]
                entries.sort(key=lambda e: e[1])
                line_text = " ".join(e[2] for e in entries)
                avg_top = sum(e[0] for e in entries) / len(entries)
                avg_h = sum(e[3] for e in entries) / len(entries)
                min_left = min(e[1] for e in entries)
                all_lines.append((avg_top, min_left, line_text, avg_h))

            all_lines.sort(key=lambda l: l[0])

            # 分组为消息（按垂直距离和左右位置判断气泡）
            messages: List[WhatsAppMessage] = []
            cur_lines = []
            last_top = None
            last_h = None
            last_side = None  # "left" | "right"

            for top, left, text, h in all_lines:
                cleaned = self._clean(text)
                if not cleaned or len(cleaned) < 2:
                    continue

                low = cleaned.lower()
                skip = ["typing", "online", "last seen", "search", "unread",
                        "archived", "pinned", "muted", "whatsapp", "type a message",
                        "messages", "status", "communities", "chats", "settings",
                        "calls", "new chat", "you", "delivered", "read", "sent"]
                if any(kw in low for kw in skip):
                    continue

                if all(c.isdigit() or c in ":.,!? " for c in cleaned) and len(cleaned) < 10:
                    continue

                # 判断在左还是右
                chat_region_w = chat_right - chat_left
                side = "left" if left < chat_region_w * 0.5 else "right"

                if last_top is None:
                    cur_lines = [(top, left, cleaned, h)]
                    last_top = top
                    last_h = h
                    last_side = side
                else:
                    gap = top - (last_top + last_h)
                    # 同一气泡：间距小且同侧
                    if gap <= (last_h or 16) * 1.8 and side == last_side:
                        cur_lines.append((top, left, cleaned, h))
                        last_top = top
                        last_h = h
                    else:
                        # 新气泡
                        if cur_lines:
                            msg_text = "\n".join(l[2] for l in cur_lines)
                            fy = cur_lines[0][0] + chat_top
                            fx = cur_lines[0][1] + chat_left
                            direction = "in" if fx < mid_x else "out"
                            messages.append(WhatsAppMessage(
                                text=msg_text, direction=direction, y_top=fy
                            ))
                        cur_lines = [(top, left, cleaned, h)]
                        last_top = top
                        last_h = h
                        last_side = side

            # 最后一组
            if cur_lines:
                msg_text = "\n".join(l[2] for l in cur_lines)
                fy = cur_lines[0][0] + chat_top
                fx = cur_lines[0][1] + chat_left
                direction = "in" if fx < mid_x else "out"
                messages.append(WhatsAppMessage(
                    text=msg_text, direction=direction, y_top=fy
                ))

            return messages

        except Exception as e:
            if self._scan_count % 30 == 0:
                self._debug("OCR 提取异常: %s" % e)
            return []

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
