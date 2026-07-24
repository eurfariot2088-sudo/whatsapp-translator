"""
translator.py
翻译引擎抽象层。
- Google 后端：基于 deep-translator（GoogleTranslate 免费通道，无需 Key）
- 豆包 后端：基于火山引擎 ARK Chat Completions（OpenAI 兼容协议），需 API Key

支持：
- 同步翻译 translate()
- 并发翻译 translate_batch()（用线程池加速多条消息）
- 本地缓存，重复消息秒回
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List, Optional

import requests

from config import TranslatorConfig

log = logging.getLogger(__name__)


@dataclass
class TranslationResult:
    text: str
    src: str
    backend: str
    elapsed_ms: int


class TranslateError(Exception):
    pass


# ---------------------------------------------------------------------------
# 工具：判断是否需要翻译
# ---------------------------------------------------------------------------
_CJK_RANGES = (
    (0x3040, 0x30FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
)


def _has_cjk(s: str) -> bool:
    return any(any(a <= ord(c) <= b for a, b in _CJK_RANGES) for c in s)


def needs_translation(text: str, target_lang: str) -> bool:
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
        from deep_translator import GoogleTranslator  # type: ignore
        self._GoogleTranslator = GoogleTranslator

    def translate(self, text: str, target: str = "", source: str = "") -> TranslationResult:
        tgt = target or self.cfg.target_lang
        src = source or self.cfg.source_lang or "auto"
        t0 = time.time()
        try:
            tr = self._GoogleTranslator(source=src, target=tgt)
            out = tr.translate(text)
        except Exception as e:
            raise TranslateError(f"Google 翻译失败: {e}") from e
        return TranslationResult(
            text=out or "",
            src=src,
            backend=self.name,
            elapsed_ms=int((time.time() - t0) * 1000),
        )


# ---------------------------------------------------------------------------
# 豆包翻译后端
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

    def translate(self, text: str, target: str = "", source: str = "") -> TranslationResult:
        tgt = target or self.cfg.target_lang
        t0 = time.time()
        user_prompt = f"请将下面的文本翻译为 {tgt}：\n\n{text}"
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
# 顶层 Translator（带缓存 + 并发批量翻译）
# ---------------------------------------------------------------------------
class Translator:
    def __init__(self, cfg: TranslatorConfig):
        self.cfg = cfg
        self._backend = self._build_backend(cfg)
        self._cache: dict[str, str] = {}
        self._cache_lock = __import__("threading").Lock()
        self._cache_limit = 1024
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="translator")

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
        with self._cache_lock:
            self._cache.clear()

    def _cache_get(self, key: str) -> Optional[str]:
        with self._cache_lock:
            return self._cache.get(key)

    def _cache_put(self, key: str, val: str):
        with self._cache_lock:
            if len(self._cache) >= self._cache_limit:
                for _ in range(64):
                    if self._cache:
                        self._cache.pop(next(iter(self._cache)), None)
            self._cache[key] = val

    def translate(self, text: str, target: str = "", source: str = "") -> Optional[TranslationResult]:
        """同步翻译单条文本。返回 None 表示无需翻译。"""
        tgt = target or self.cfg.target_lang
        if not needs_translation(text, tgt):
            return None
        cache_key = f"{tgt}:{text}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return TranslationResult(text=cached, src="cache",
                                     backend=self._backend.name, elapsed_ms=0)
        last_err: Optional[Exception] = None
        for attempt in range(2):
            try:
                result = self._backend.translate(text, target=tgt, source=source)
                self._cache_put(cache_key, result.text)
                return result
            except TranslateError as e:
                last_err = e
                log.warning("翻译失败（第 %d 次）: %s", attempt + 1, e)
                time.sleep(0.3)
        raise last_err or TranslateError("未知翻译错误")

    def translate_async(self, text: str, target: str = "", source: str = "",
                        callback=None) -> None:
        """异步翻译单条文本，完成后调用 callback(result)。"""
        def _worker():
            try:
                result = self.translate(text, target=target, source=source)
                if callback:
                    callback(result)
            except TranslateError as e:
                if callback:
                    callback(e)
        self._executor.submit(_worker)

    def translate_batch(self, texts: List[str], target: str = "") -> List[Optional[TranslationResult]]:
        """并发批量翻译，保持顺序。"""
        tgt = target or self.cfg.target_lang
        results: List[Optional[TranslationResult]] = [None] * len(texts)
        futures = {}
        for i, text in enumerate(texts):
            if not needs_translation(text, tgt):
                results[i] = None
                continue
            cache_key = f"{tgt}:{text}"
            cached = self._cache_get(cache_key)
            if cached is not None:
                results[i] = TranslationResult(text=cached, src="cache",
                                               backend=self._backend.name, elapsed_ms=0)
                continue
            fut = self._executor.submit(self._backend.translate, text, tgt)
            futures[fut] = i
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                result = fut.result()
                cache_key = f"{tgt}:{texts[i]}"
                self._cache_put(cache_key, result.text)
                results[i] = result
            except TranslateError as e:
                log.warning("批量翻译第 %d 条失败: %s", i, e)
                results[i] = None
        return results

    def shutdown(self):
        self._executor.shutdown(wait=False)
