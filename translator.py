"""
translator.py
翻译引擎 —— 基于 Google 翻译（deep-translator），质量稳定支持 100+ 语言。

支持的目标语言（常见 11 种）：
  英语 en, 葡萄牙语 pt, 西班牙语 es, 俄语 ru, 阿拉伯语 ar,
  韩语 ko, 日语 ja, 马来语 ms, 法语 fr, 土耳其语 tr, 泰语 th, 越南语 vi

功能：
- Google 翻译后端（高质量）
- 本地 LRU 缓存
- 并发批量翻译（线程池）
- 自动识别源语言
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class TranslationResult:
    text: str
    src_lang: str
    backend: str
    elapsed_ms: int


class TranslateError(Exception):
    pass


# Google 翻译支持的语言代码
# key = 语言代码, value = 中文名
GOOGLE_LANGUAGES: Dict[str, str] = {
    "auto": "自动检测",
    "en": "英语",
    "pt": "葡萄牙语",
    "es": "西班牙语",
    "ru": "俄语",
    "ar": "阿拉伯语",
    "ko": "韩语",
    "ja": "日语",
    "ms": "马来语",
    "fr": "法语",
    "tr": "土耳其语",
    "th": "泰语",
    "vi": "越南语",
    "zh-CN": "中文（简体）",
    "zh-TW": "中文（繁体）",
    "de": "德语",
    "it": "意大利语",
    "nl": "荷兰语",
    "pl": "波兰语",
    "id": "印尼语",
    "hi": "印地语",
}


def get_language_list() -> List[tuple]:
    """返回 (code, name) 列表，用于下拉框。"""
    return list(GOOGLE_LANGUAGES.items())


# ============================================================================
# Google 翻译后端
# ============================================================================
class GoogleBackend:
    name = "google"

    def __init__(self):
        from deep_translator import GoogleTranslator  # type: ignore
        self._GT = GoogleTranslator
        self._instances: Dict[str, object] = {}
        self._lock = threading.Lock()

    def _get_translator(self, source: str, target: str):
        key = f"{source}:{target}"
        with self._lock:
            if key not in self._instances:
                self._instances[key] = self._GT(source=source, target=target)
                # 限制缓存大小
                if len(self._instances) > 50:
                    # 清空一半
                    keys = list(self._instances.keys())
                    for k in keys[:25]:
                        del self._instances[k]
            return self._instances[key]

    def translate(self, text: str, target: str = "zh-CN", source: str = "auto") -> TranslationResult:
        t0 = time.time()
        try:
            tr = self._get_translator(source, target)
            out = tr.translate(text)
        except Exception as e:
            # 尝试重建 translator 实例（可能是连接超时）
            try:
                key = f"{source}:{target}"
                with self._lock:
                    if key in self._instances:
                        del self._instances[key]
                tr = self._get_translator(source, target)
                out = tr.translate(text)
            except Exception as e2:
                raise TranslateError(f"Google 翻译失败: {e2}") from e2

        elapsed = int((time.time() - t0) * 1000)
        return TranslationResult(
            text=out or "",
            src_lang=source,
            backend=self.name,
            elapsed_ms=elapsed,
        )


# ============================================================================
# 顶层 Translator（带缓存 + 并发）
# ============================================================================
class Translator:
    def __init__(self, target_lang: str = "zh-CN", source_lang: str = "auto"):
        self.target_lang = target_lang
        self.source_lang = source_lang
        self._backend = GoogleBackend()
        self._cache: Dict[str, str] = {}
        self._cache_lock = threading.Lock()
        self._cache_limit = 2048
        self._executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="translator")

    def set_target(self, lang: str):
        self.target_lang = lang

    def set_source(self, lang: str):
        self.source_lang = lang

    def _cache_get(self, key: str) -> Optional[str]:
        with self._cache_lock:
            return self._cache.get(key)

    def _cache_put(self, key: str, val: str):
        with self._cache_lock:
            if len(self._cache) >= self._cache_limit:
                # 淘汰 1/4
                keys = list(self._cache.keys())
                for k in keys[:self._cache_limit // 4]:
                    self._cache.pop(k, None)
            self._cache[key] = val

    def translate(self, text: str, target: str = "", source: str = "") -> Optional[TranslationResult]:
        """同步翻译。返回 None 表示无需翻译（已是目标语言）。"""
        tgt = target or self.target_lang
        src = source or self.source_lang

        text = (text or "").strip()
        if not text:
            return None

        # 快速判断：如果目标是中文且文本全是中文，跳过
        if tgt.startswith("zh") and self._is_chinese(text):
            return None

        cache_key = f"{src}:{tgt}:{text}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return TranslationResult(text=cached, src_lang=src,
                                     backend="cache", elapsed_ms=0)

        last_err = None
        for attempt in range(2):
            try:
                result = self._backend.translate(text, target=tgt, source=src)
                if result and result.text:
                    self._cache_put(cache_key, result.text)
                return result
            except TranslateError as e:
                last_err = e
                log.warning("翻译失败（第 %d 次）: %s", attempt + 1, e)
                time.sleep(0.5)

        raise last_err or TranslateError("未知翻译错误")

    def translate_async(self, text: str, target: str = "", source: str = "",
                        callback=None) -> None:
        """异步翻译，完成后调用 callback(result)。"""
        def _worker():
            try:
                result = self.translate(text, target=target, source=source)
                if callback:
                    callback(result)
            except Exception as e:
                if callback:
                    callback(e)
        self._executor.submit(_worker)

    def translate_batch(self, texts: List[str], target: str = "") -> List[Optional[TranslationResult]]:
        """并发批量翻译，保持顺序。"""
        tgt = target or self.target_lang
        results: List[Optional[TranslationResult]] = [None] * len(texts)
        futures = {}

        for i, text in enumerate(texts):
            text = (text or "").strip()
            if not text:
                results[i] = None
                continue

            if tgt.startswith("zh") and self._is_chinese(text):
                results[i] = None
                continue

            cache_key = f"{self.source_lang}:{tgt}:{text}"
            cached = self._cache_get(cache_key)
            if cached is not None:
                results[i] = TranslationResult(text=cached, src_lang=self.source_lang,
                                               backend="cache", elapsed_ms=0)
                continue

            fut = self._executor.submit(
                self._backend.translate, text, tgt, self.source_lang)
            futures[fut] = i

        for fut in as_completed(futures):
            i = futures[fut]
            try:
                result = fut.result()
                if result and result.text:
                    cache_key = f"{self.source_lang}:{tgt}:{texts[i]}"
                    self._cache_put(cache_key, result.text)
                results[i] = result
            except Exception as e:
                log.warning("批量翻译第 %d 条失败: %s", i, e)
                results[i] = None

        return results

    @staticmethod
    def _is_chinese(text: str) -> bool:
        """粗略判断文本是否以中文为主。"""
        if not text:
            return False
        cjk_count = 0
        total = 0
        for c in text:
            if '\u4e00' <= c <= '\u9fff':
                cjk_count += 1
            if not c.isspace():
                total += 1
        if total == 0:
            return False
        return cjk_count / total > 0.3

    def shutdown(self):
        self._executor.shutdown(wait=False)
