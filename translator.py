"""
translator.py
翻译引擎抽象层。
- Google 后端：基于 deep-translator（GoogleTranslate 免费通道，无需 Key）
- 豆包 后端：基于火山引擎 ARK Chat Completions（OpenAI 兼容协议），需 API Key

对外暴露 Translator 类，统一接口 translate(text) -> str。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests

from config import TranslatorConfig

log = logging.getLogger(__name__)


@dataclass
class TranslationResult:
    text: str          # 翻译结果
    src: str           # 检测到的源语言
    backend: str       # 实际使用的后端
    elapsed_ms: int    # 耗时


class TranslateError(Exception):
    pass


# ---------------------------------------------------------------------------
# 工具：判断是否需要翻译
# ---------------------------------------------------------------------------
_CJK_RANGES = (
    (0x3040, 0x30FF),   # 平假/片假
    (0x3400, 0x4DBF),   # 扩展 A
    (0x4E00, 0x9FFF),   # CJK 基本
    (0xF900, 0xFAFF),   # CJK 兼容
)


def _has_cjk(s: str) -> bool:
    return any(any(a <= ord(c) <= b for a, b in _CJK_RANGES) for c in s)


def needs_translation(text: str, target_lang: str) -> bool:
    """粗略判断文本是否需要翻译。
    目标语言是中文家族时：含 CJK 直接跳过；纯外文则翻译。
    目标语言是英文时：含 ASCII 字母且不含 CJK 时跳过。
    """
    if not text or not text.strip():
        return False
    target_is_zh = target_lang.lower().startswith("zh")
    if target_is_zh and _has_cjk(text):
        return False
    if not target_is_zh and text.isascii():
        return False
    return True


# ---------------------------------------------------------------------------
# Google 翻译后端
# ---------------------------------------------------------------------------
class GoogleBackend:
    name = "google"

    def __init__(self, cfg: TranslatorConfig):
        self.cfg = cfg
        # 延迟导入，避免无网环境下启动报错
        from deep_translator import GoogleTranslator  # type: ignore
        self._GoogleTranslator = GoogleTranslator

    def translate(self, text: str) -> TranslationResult:
        t0 = time.time()
        try:
            # deep-translator 支持 source='auto'
            tr = self._GoogleTranslator(source=self.cfg.source_lang or "auto",
                                        target=self.cfg.target_lang)
            out = tr.translate(text)
        except Exception as e:  # 网络异常 / 限流 / 被风控
            raise TranslateError(f"Google 翻译失败: {e}") from e
        return TranslationResult(
            text=out or "",
            src=self.cfg.source_lang or "auto",
            backend=self.name,
            elapsed_ms=int((time.time() - t0) * 1000),
        )


# ---------------------------------------------------------------------------
# 豆包翻译后端（火山引擎 ARK）
# ---------------------------------------------------------------------------
DOUBAO_SYSTEM_PROMPT = (
    "你是一个专业翻译引擎。只输出译文本身，不要添加任何解释、注释、引号或前后缀。"
    "如果原文已经是目标语言，请原样返回。"
)


class DoubaoBackend:
    name = "doubao"

    def __init__(self, cfg: TranslatorConfig):
        self.cfg = cfg
        if not cfg.doubao_api_key:
            raise TranslateError("豆包后端需要在 config.yaml 中配置 doubao_api_key")

    def translate(self, text: str) -> TranslationResult:
        t0 = time.time()
        # 让模型直接把文本翻译为目标语言
        user_prompt = (
            f"请将下面的文本翻译为 {self.cfg.target_lang}：\n\n{text}"
        )
        payload = {
            "model": self.cfg.doubao_model,
            "messages": [
                {"role": "system", "content": DOUBAO_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {self.cfg.doubao_api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(
                self.cfg.doubao_endpoint,
                headers=headers,
                json=payload,
                timeout=self.cfg.doubao_timeout,
            )
        except requests.RequestException as e:
            raise TranslateError(f"豆包请求失败: {e}") from e

        if resp.status_code != 200:
            raise TranslateError(f"豆包 HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            data = resp.json()
            out = data["choices"][0]["message"]["content"].strip()
        except (KeyError, ValueError, IndexError) as e:
            raise TranslateError(f"豆包响应解析失败: {e}; body={resp.text[:200]}") from e

        return TranslationResult(
            text=out,
            src="auto",
            backend=self.name,
            elapsed_ms=int((time.time() - t0) * 1000),
        )


# ---------------------------------------------------------------------------
# 顶层 Translator（带缓存 + 失败重试）
# ---------------------------------------------------------------------------
class Translator:
    def __init__(self, cfg: TranslatorConfig):
        self.cfg = cfg
        self._backend = self._build_backend(cfg)
        self._cache: dict[str, str] = {}
        self._cache_limit = 512

    @staticmethod
    def _build_backend(cfg: TranslatorConfig):
        if cfg.backend == "google":
            return GoogleBackend(cfg)
        if cfg.backend == "doubao":
            return DoubaoBackend(cfg)
        raise TranslateError(f"未知翻译后端: {cfg.backend}")

    def switch_backend(self, new_cfg: TranslatorConfig):
        self.cfg = new_cfg
        self._backend = self._build_backend(new_cfg)
        self._cache.clear()

    def translate(self, text: str) -> Optional[TranslationResult]:
        if not needs_translation(text, self.cfg.target_lang):
            return None
        cached = self._cache.get(text)
        if cached is not None:
            return TranslationResult(text=cached, src="cache",
                                     backend=self._backend.name, elapsed_ms=0)
        last_err: Optional[Exception] = None
        for attempt in range(2):  # 最多重试一次
            try:
                result = self._backend.translate(text)
                # 写入缓存
                if len(self._cache) >= self._cache_limit:
                    # 简单 FIFO 清理
                    for _ in range(64):
                        self._cache.pop(next(iter(self._cache)), None)
                self._cache[text] = result.text
                return result
            except TranslateError as e:
                last_err = e
                log.warning("翻译失败（第 %d 次）: %s", attempt + 1, e)
                time.sleep(0.5)
        raise last_err or TranslateError("未知翻译错误")
