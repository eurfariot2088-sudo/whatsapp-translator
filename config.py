"""
config.py
统一管理配置：翻译后端、目标语言、API Key、轮询频率、气泡颜色、截图热键等。
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

# 常用语言列表（用于下拉框）
LANGUAGES = [
    ("auto", "自动检测"),
    ("zh-CN", "中文（简体）"),
    ("zh-TW", "中文（繁体）"),
    ("en", "英语"),
    ("ja", "日语"),
    ("ko", "韩语"),
    ("fr", "法语"),
    ("de", "德语"),
    ("es", "西班牙语"),
    ("ru", "俄语"),
    ("pt", "葡萄牙语"),
    ("it", "意大利语"),
    ("th", "泰语"),
    ("vi", "越南语"),
    ("ar", "阿拉伯语"),
    ("hi", "印地语"),
    ("id", "印尼语"),
    ("tr", "土耳其语"),
    ("nl", "荷兰语"),
    ("pl", "波兰语"),
]


@dataclass
class TranslatorConfig:
    """翻译后端相关配置。"""

    # backend: "google" 或 "doubao"
    backend: str = "google"
    # 目标语言（自动消息翻译用）
    target_lang: str = "zh-CN"
    # 源语言，auto 表示自动检测
    source_lang: str = "auto"
    # 豆包（火山引擎 ARK）相关
    doubao_api_key: str = ""
    doubao_model: str = "doubao-seed-1-6-250615"
    doubao_endpoint: str = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
    doubao_timeout: int = 15
    # Google 翻译超时（秒）
    google_timeout: int = 10


@dataclass
class ReaderConfig:
    """WhatsApp 读取相关配置。"""

    process_name: str = "WhatsApp.exe"
    window_keyword: str = "WhatsApp"
    # 轮询频率（秒）—— 降低到 0.8 秒以支持实时翻译
    poll_interval: float = 0.8
    # 每次最多保留的历史消息数（去重用）
    max_history: int = 800
    # 是否只翻译收到的消息
    only_incoming: bool = False
    # 消息最短长度
    min_length: int = 1
    # 首次扫描时是否翻译已有历史消息（False=只翻译新消息，True=翻译全部）
    translate_history_on_start: bool = False


@dataclass
class GuiConfig:
    """GUI 行为与外观配置。"""

    start_minimized: bool = False
    close_to_tray: bool = True
    # 全局热键
    hotkey_show: str = "ctrl+alt+t"
    hotkey_screenshot: str = "ctrl+alt+s"
    # 主题
    theme: str = "light"
    # 气泡颜色（HEX）
    bubble_in_color: str = "#FFFFFF"       # 收到的消息气泡背景（白色）
    bubble_in_text: str = "#333333"         # 收到的消息文字颜色
    bubble_out_color: str = "#95EC69"       # 发出的消息气泡背景（微信绿）
    bubble_out_text: str = "#333333"        # 发出的消息文字颜色
    bubble_trans_color: str = "#F0F0F0"     # 译文区域背景
    bubble_trans_text: str = "#666666"      # 译文文字颜色
    # 聊天区域背景色
    chat_bg_color: str = "#EDEDED"          # 微信式浅灰背景
    # 底部回复区翻译的目标语言（独立于自动翻译目标语言）
    reply_target_lang: str = "en"
    # 字体
    font_family: str = "Microsoft YaHei"
    font_size: int = 11


@dataclass
class AppConfig:
    """根配置。"""

    translator: TranslatorConfig = field(default_factory=TranslatorConfig)
    reader: ReaderConfig = field(default_factory=ReaderConfig)
    gui: GuiConfig = field(default_factory=GuiConfig)

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
            return cls()


_settings: AppConfig | None = None


def get_settings() -> AppConfig:
    global _settings
    if _settings is None:
        _settings = AppConfig.load()
    return _settings


def save_settings() -> None:
    if _settings is not None:
        _settings.save()
