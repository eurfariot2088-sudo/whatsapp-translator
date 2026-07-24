"""
config.py
统一管理配置：翻译后端、目标语言、API Key、轮询频率等。
配置文件保存在用户目录 %APPDATA%/WhatsAppTranslator/config.yaml，首次运行自动生成。
"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict

import yaml


def get_app_dir() -> Path:
    """获取跨平台应用数据目录。Windows 下为 %APPDATA%/WhatsAppTranslator。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    app_dir = Path(base) / "WhatsAppTranslator"
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


CONFIG_FILE = get_app_dir() / "config.yaml"


@dataclass
class TranslatorConfig:
    """翻译后端相关配置。"""

    # backend: "google" 或 "doubao"
    backend: str = "google"

    # 目标语言（Google / 豆包通用 BCP-47 风格）
    target_lang: str = "zh-CN"
    # 源语言，auto 表示自动检测
    source_lang: str = "auto"

    # 豆包（火山引擎 ARK）相关
    doubao_api_key: str = ""
    doubao_model: str = "doubao-seed-1-6-250615"  # 也可改成 doubao-lite-32k 等
    doubao_endpoint: str = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
    doubao_timeout: int = 15


@dataclass
class ReaderConfig:
    """WhatsApp 读取相关配置。"""

    # 桌面 WhatsApp 进程名
    process_name: str = "WhatsApp.exe"
    # 窗口标题包含的关键字（用于在多窗口中定位主窗口）
    window_keyword: str = "WhatsApp"
    # 轮询频率（秒）
    poll_interval: float = 1.5
    # 每次最多保留的历史消息数（去重用）
    max_history: int = 500
    # 是否只翻译「收到的」消息（True 会过滤掉自己发送的）
    only_incoming: bool = True
    # 消息最短长度，少于该长度的英文/数字噪声不翻译
    min_length: int = 2


@dataclass
class GuiConfig:
    """GUI 行为配置。"""

    # 启动时最小化到托盘
    start_minimized: bool = False
    # 关闭窗口时最小化到托盘（而不是退出）
    close_to_tray: bool = True
    # 全局热键：唤起主窗口
    hotkey_show: str = "ctrl+alt+t"
    # 主题：light / dark
    theme: str = "light"


@dataclass
class AppConfig:
    """根配置。"""

    translator: TranslatorConfig = field(default_factory=TranslatorConfig)
    reader: ReaderConfig = field(default_factory=ReaderConfig)
    gui: GuiConfig = field(default_factory=GuiConfig)

    # ---- 序列化 ----
    def to_dict(self) -> Dict[str, Any]:
        return {
            "translator": asdict(self.translator),
            "reader": asdict(self.reader),
            "gui": asdict(self.gui),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        t = data.get("translator", {}) or {}
        r = data.get("reader", {}) or {}
        g = data.get("gui", {}) or {}
        return cls(
            translator=TranslatorConfig(**{k: v for k, v in t.items() if k in TranslatorConfig.__annotations__}),
            reader=ReaderConfig(**{k: v for k, v in r.items() if k in ReaderConfig.__annotations__}),
            gui=GuiConfig(**{k: v for k, v in g.items() if k in GuiConfig.__annotations__}),
        )

    # ---- 文件 IO ----
    def save(self, path: Path = CONFIG_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.to_dict(), f, allow_unicode=True, sort_keys=False)

    @classmethod
    def load(cls, path: Path = CONFIG_FILE) -> "AppConfig":
        if not path.exists():
            cfg = cls()
            cfg.save(path)
            return cfg
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return cls.from_dict(data)
        except Exception:
            # 配置文件损坏时回退到默认
            return cls()


# 便捷单例
_settings: AppConfig | None = None


def get_settings() -> AppConfig:
    global _settings
    if _settings is None:
        _settings = AppConfig.load()
    return _settings


def save_settings() -> None:
    if _settings is not None:
        _settings.save()
