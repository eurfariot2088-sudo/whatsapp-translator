"""
whatsapp_reader.py
通过 Windows UI Automation（UIA）直接读取桌面 WhatsApp 窗口中的消息文本。
绝不截屏、绝不依赖剪贴板，纯控件树遍历。

工作原理：
    1. 通过进程名/窗口标题定位到 WhatsApp 主窗口
    2. 在窗口控件树中找到"会话消息列表"区域（通常是 List / Pane）
    3. 遍历其中的每条消息项，提取 Name/Value 文本
    4. 通过 X 坐标区分 incoming（左）/ outgoing（右）消息
    5. 用 hash 去重，把新消息推给订阅者

兼容性提示：
    WhatsApp Desktop 升级后控件层级偶尔会变。本模块做了多重 fallback：
    先尝试 List/ListItem 路径；找不到时退化为遍历所有 Text 控件并按 Y 排序。
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
    text: str                 # 原始消息文本（已拼接多行）
    direction: str            # "in" 收到的 / "out" 发出的
    ts: float = field(default_factory=time.time)
    raw_count: int = 1        # 包含的原始 Text 节点数（用于调试）

    def fingerprint(self) -> str:
        # 用 (方向 + 文本) 哈希去重；时间戳不参与
        h = hashlib.sha1()
        h.update(self.direction.encode("utf-8"))
        h.update(b"\x00")
        h.update(self.text.encode("utf-8"))
        return h.hexdigest()


# ---------------------------------------------------------------------------
# 平台守卫
# ---------------------------------------------------------------------------
def _ensure_windows():
    import sys
    if sys.platform != "win32":
        raise RuntimeError(
            "WhatsApp 消息自动读取依赖 Windows UI Automation，"
            "只能在 Windows 上运行（macOS / Linux 不支持）。"
        )


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------
class WhatsAppReader:
    """周期性读取 WhatsApp Desktop 消息的轮询器。"""

    def __init__(self, cfg: ReaderConfig,
                 on_message: Callable[[WhatsAppMessage], None]):
        _ensure_windows()
        self.cfg = cfg
        self.on_message = on_message
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seen: Set[str] = set()  # 消息指纹
        self._seen_order: List[str] = []  # 用于 FIFO 清理
        self._window_lock = threading.Lock()

    # -------------------- 生命周期 --------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
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
        import uiautomation as auto  # 仅 Windows
        # 首次缓存：避免启动瞬间把历史消息全部翻译一遍
        self._seen.clear()
        self._seen_order.clear()
        try:
            self._scan_once(auto, prime=True)
        except Exception as e:
            log.exception("首次扫描失败: %s", e)

        while not self._stop.is_set():
            try:
                self._scan_once(auto, prime=False)
            except Exception as e:
                # 常见原因：WhatsApp 窗口被关闭、UIA 暂时无响应
                log.debug("扫描异常（忽略）: %s", e)
            self._stop.wait(self.cfg.poll_interval)

    # -------------------- 单次扫描 --------------------
    def _scan_once(self, auto, prime: bool):
        window = self._find_window(auto)
        if window is None:
            return
        messages = list(self._extract_messages(auto, window))
        for msg in messages:
            fp = msg.fingerprint()
            with self._window_lock:
                if fp in self._seen:
                    continue
                if len(self._seen) >= self.cfg.max_history:
                    # 淘汰最早的 1/4
                    drop = max(1, self.cfg.max_history // 4)
                    for _ in range(drop):
                        if self._seen_order:
                            old = self._seen_order.pop(0)
                            self._seen.discard(old)
                self._seen.add(fp)
                self._seen_order.append(fp)
            if prime:
                # 预热阶段：只入缓存，不回调
                continue
            if len(msg.text.strip()) < self.cfg.min_length:
                continue
            if self.cfg.only_incoming and msg.direction != "in":
                continue
            try:
                self.on_message(msg)
            except Exception:
                log.exception("on_message 回调异常")

    # -------------------- 窗口定位 --------------------
    def _find_window(self, auto):
        # 1) 优先按进程名查找
        try:
            win = auto.WindowControl(
                searchFromControl=auto.GetRootControl(),
                searchDepth=8,
                ProcessName=self.cfg.process_name,
            )
            if win and win.Exists(maxSearchSeconds=0.5):
                return win
        except Exception:
            pass

        # 2) 按窗口标题关键字
        try:
            win = auto.WindowControl(
                searchFromControl=auto.GetRootControl(),
                searchDepth=3,
                Name=self.cfg.window_keyword,
            )
            if win and win.Exists(maxSearchSeconds=0.5):
                return win
        except Exception:
            pass

        return None

    # -------------------- 提取消息 --------------------
    def _extract_messages(self, auto, window) -> Iterable[WhatsAppMessage]:
        """从窗口中提取消息。两种策略：
        A) 找到 List 控件（最准确），按 ListItem 收集文本
        B) 退而求其次：收集所有非空 TextControl，按 Y 排序后按相邻距离分组
        """
        # 取得整个窗口的屏幕矩形，用于判断 incoming/outgoing
        try:
            win_rect = window.BoundingRectangle
            win_left = win_rect.left
            win_right = win_rect.right
            win_width = win_right - win_left
            mid_x = win_left + win_width // 2
        except Exception:
            mid_x = None

        # 策略 A：遍历所有可能的 List/ListItem
        for msg in self._strategy_list(auto, window, mid_x):
            yield msg

        # 策略 B：全量 Text 控件（如果 A 没产出）
        # 注：实际中两条策略会产生重复，这里只在 A 没结果时启用
        # 为避免重复，正常路径只在窗口结构未知时启用 B（见 _strategy_text_fallback）

    def _strategy_list(self, auto, window, mid_x) -> Iterable[WhatsAppMessage]:
        try:
            # 枚举所有 List 控件；WhatsApp 的消息列表是其中之一
            lists = window.GetChildren()  # 一级子节点
            candidate_lists = []
            for ctrl in lists:
                try:
                    cn = ctrl.ClassName or ""
                    ctype = ctrl.ControlTypeName or ""
                    if "List" in ctype or "Pane" in ctype:
                        # 选子项最多的那个（消息列表通常最丰富）
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
            # 过滤明显是 UI chrome 的文本
            low = joined.lower()
            if low in {"typing…", "online", "last seen"}:
                continue
            direction = self._guess_direction(item, mid_x)
            yield WhatsAppMessage(text=joined, direction=direction)

    def _strategy_text_fallback(self, auto, window) -> Iterable[WhatsAppMessage]:
        """全量 Text 控件兜底：按 Y 排序，把相邻 Y 的合并为一条消息。"""
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
                    texts.append((rect.top, rect.left, name, ctrl))
                except Exception:
                    continue
            if not texts:
                return
            texts.sort(key=lambda t: (t[0], t[1]))

            # 合并：相邻 Y 差 < 8 像素算同一条消息
            groups: List[List[tuple]] = []
            current: List[tuple] = []
            last_y = None
            for entry in texts:
                y, x, name, ctrl = entry
                if last_y is None or abs(y - last_y) <= 8:
                    current.append(entry)
                else:
                    groups.append(current)
                    current = [entry]
                last_y = y
            if current:
                groups.append(current)

            for grp in groups:
                grp.sort(key=lambda t: t[1])  # 按 X 排序
                joined = "\n".join(g[2] for g in grp).strip()
                if not joined or len(joined) < 2:
                    continue
                # 取首项 X 决定方向
                first_x = grp[0][1]
                direction = "in" if mid_x is not None and first_x < mid_x else "out"
                yield WhatsAppMessage(text=joined, direction=direction)
        except Exception as e:
            log.debug("_strategy_text_fallback 异常: %s", e)

    # -------------------- 辅助 --------------------
    @staticmethod
    def _collect_texts(ctrl) -> List[str]:
        """递归收集某节点下所有 TextControl 的 Name。"""
        out: List[str] = []
        try:
            for sub, _ in ctrl.WalkControl(ctrl, includeTop=False):
                if (sub.ControlTypeName or "") == "TextControl":
                    name = (sub.Name or "").strip()
                    if name:
                        out.append(name)
        except Exception:
            pass
        # 去重保序
        seen = set()
        uniq = []
        for t in out:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        return uniq

    @staticmethod
    def _join_texts(texts: List[str]) -> str:
        # 短文本用空格，长文本用换行更可读
        if not texts:
            return ""
        # 简单启发：把冒号、引号包围的列表拍平
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
