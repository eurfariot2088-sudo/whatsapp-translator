"""
ocr_engine.py
OCR 文字识别引擎封装。

支持的后端（按优先级自动选择）：
1. Windows 内置 OCR（Windows.Media.Ocr）- Win10/11 自带，无需安装
2. Tesseract-OCR - 如果用户已安装可用
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple
from PIL import Image

log = logging.getLogger(__name__)

_available_backend: Optional[str] = None  # "windows" | "tesseract" | None
_init_checked = False


def _detect_backend() -> Optional[str]:
    """检测可用的 OCR 后端。"""
    global _available_backend, _init_checked
    if _init_checked:
        return _available_backend
    _init_checked = True

    # 1. 尝试 Windows 内置 OCR
    try:
        import sys
        if sys.platform == "win32":
            from winsdk.windows.media.ocr import OcrEngine
            from winsdk.windows.globalization import Language
            # 尝试创建英文引擎，看是否可用
            lang = Language("en")
            if OcrEngine.is_language_supported(lang):
                _available_backend = "windows"
                log.info("OCR 后端: Windows 内置 OCR (Windows.Media.Ocr)")
                return _available_backend
    except Exception as e:
        log.debug("Windows 内置 OCR 不可用: %s", e)

    # 2. 尝试 Tesseract
    try:
        import pytesseract  # noqa
        # 尝试找到 tesseract 可执行文件
        import os
        paths = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]
        for p in os.environ.get("PATH", "").split(os.pathsep):
            paths.append(os.path.join(p, "tesseract.exe"))
        for p in paths:
            if os.path.isfile(p):
                _available_backend = "tesseract"
                log.info("OCR 后端: Tesseract-OCR (%s)", p)
                return _available_backend
    except Exception as e:
        log.debug("Tesseract 不可用: %s", e)

    log.warning("未找到可用的 OCR 后端")
    return None


def is_available() -> bool:
    """检查是否有可用的 OCR 后端。"""
    return _detect_backend() is not None


def get_backend_name() -> str:
    """返回当前使用的 OCR 后端名称。"""
    b = _detect_backend()
    if b == "windows":
        return "Windows 内置 OCR"
    elif b == "tesseract":
        return "Tesseract-OCR"
    return "无可用 OCR"


def ocr_image(image: Image.Image, languages: Optional[List[str]] = None) -> str:
    """
    识别图片中的文字。

    :param image: PIL Image
    :param languages: 语言代码列表，如 ["en", "zh-Hans"]，None 则自动尝试中英文
    :return: 识别到的文本
    """
    backend = _detect_backend()
    if backend is None:
        raise RuntimeError(
            "未找到可用的 OCR 引擎。\n\n"
            "请确认你的系统是 Windows 10/11（系统自带 OCR），\n"
            "或者安装 Tesseract-OCR：https://github.com/UB-Mannheim/tesseract/wiki"
        )

    if backend == "windows":
        return _ocr_windows(image, languages)
    elif backend == "tesseract":
        return _ocr_tesseract(image, languages)
    return ""


def ocr_image_with_data(image: Image.Image, languages: Optional[List[str]] = None) -> dict:
    """
    识别图片中的文字，返回详细数据（包含位置信息）。

    返回格式类似 pytesseract.image_to_data 的 DICT 格式：
    {'text': [...], 'top': [...], 'left': [...], 'height': [...], 'width': [...], 'conf': [...]}
    """
    backend = _detect_backend()
    if backend is None:
        raise RuntimeError("未找到可用的 OCR 引擎")

    if backend == "windows":
        return _ocr_windows_data(image, languages)
    elif backend == "tesseract":
        return _ocr_tesseract_data(image, languages)
    return {"text": [], "top": [], "left": [], "height": [], "width": [], "conf": []}


# ============================================================================
# Windows 内置 OCR 实现
# ============================================================================
def _ocr_windows(image: Image.Image, languages: Optional[List[str]]) -> str:
    """使用 Windows.Media.Ocr 识别文字。"""
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.globalization import Language
    from winsdk.windows.graphics.imaging import BitmapDecoder, SoftwareBitmap, BitmapPixelFormat
    import io
    import ctypes
    from winsdk.windows.storage.streams import InMemoryRandomAccessStream, DataWriter

    # 把 PIL Image 转成 PNG 字节流
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    # 转成 IRandomAccessStream
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png_bytes)
    writer.store_async().get()
    writer.flush_async().get()
    stream.seek(0)

    # 解码为 SoftwareBitmap
    decoder = BitmapDecoder.create_async(stream).get()
    bitmap = decoder.get_software_bitmap_async().get()

    # 转成灰度（Bgra8 格式 OCR 效果更好）
    bgra_bitmap = SoftwareBitmap.convert(bitmap, BitmapPixelFormat.BGRA8)

    # 选择语言
    lang_code = _pick_windows_lang(languages)
    lang = Language(lang_code)

    if not OcrEngine.is_language_supported(lang):
        # 尝试系统可用语言
        available = OcrEngine.get_available_recognizer_languages()
        if available.size > 0:
            lang = available[0]
        else:
            raise RuntimeError("系统未安装任何 OCR 语言包")

    engine = OcrEngine.try_create_from_language(lang)
    if engine is None:
        raise RuntimeError(f"无法创建 OCR 引擎 (语言: {lang_code})")

    result = engine.recognize_async(bgra_bitmap).get()
    lines = []
    for line in result.lines:
        lines.append(line.text)

    return "\n".join(lines).strip()


def _ocr_windows_data(image: Image.Image, languages: Optional[List[str]]) -> dict:
    """Windows OCR 返回带位置信息的详细数据。"""
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.globalization import Language
    from winsdk.windows.graphics.imaging import BitmapDecoder, SoftwareBitmap, BitmapPixelFormat
    import io
    from winsdk.windows.storage.streams import InMemoryRandomAccessStream, DataWriter

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png_bytes)
    writer.store_async().get()
    writer.flush_async().get()
    stream.seek(0)

    decoder = BitmapDecoder.create_async(stream).get()
    bitmap = decoder.get_software_bitmap_async().get()
    bgra_bitmap = SoftwareBitmap.convert(bitmap, BitmapPixelFormat.BGRA8)

    lang_code = _pick_windows_lang(languages)
    lang = Language(lang_code)
    if not OcrEngine.is_language_supported(lang):
        available = OcrEngine.get_available_recognizer_languages()
        if available.size > 0:
            lang = available[0]
        else:
            raise RuntimeError("系统未安装任何 OCR 语言包")

    engine = OcrEngine.try_create_from_language(lang)
    if engine is None:
        raise RuntimeError(f"无法创建 OCR 引擎 (语言: {lang_code})")

    result = engine.recognize_async(bgra_bitmap).get()

    # 组装成 pytesseract 类似的格式
    data = {"text": [], "top": [], "left": [], "height": [], "width": [], "conf": []}
    block_num = 0

    for line in result.lines:
        block_num += 1
        for word in line.words:
            rect = word.bounding_rect
            data["text"].append(word.text)
            data["top"].append(rect.y)
            data["left"].append(rect.x)
            data["height"].append(rect.height)
            data["width"].append(rect.width)
            data["conf"].append(95)  # Windows OCR 不返回置信度，给个默认值

    return data


def _pick_windows_lang(languages: Optional[List[str]]) -> str:
    """选择 Windows OCR 语言代码。"""
    if languages:
        # 映射常见代码到 Windows 代码
        mapping = {
            "en": "en",
            "eng": "en",
            "zh": "zh-Hans",
            "zh-CN": "zh-Hans",
            "zh-Hans": "zh-Hans",
            "zh_sim": "zh-Hans",
            "chi_sim": "zh-Hans",
        }
        for lang in languages:
            if lang in mapping:
                return mapping[lang]
            # 直接尝试原代码
            return lang
    return "en"


# ============================================================================
# Tesseract OCR 实现（备用）
# ============================================================================
def _find_tesseract() -> Optional[str]:
    import os
    paths = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]
    for p in os.environ.get("PATH", "").split(os.pathsep):
        paths.append(os.path.join(p, "tesseract.exe"))
    for p in paths:
        if os.path.isfile(p):
            return p
    return None


def _ocr_tesseract(image: Image.Image, languages: Optional[List[str]]) -> str:
    import pytesseract  # type: ignore
    path = _find_tesseract()
    if path:
        pytesseract.pytesseract.tesseract_cmd = path
    lang_str = _tess_lang(languages)
    try:
        text = pytesseract.image_to_string(image, lang=lang_str)
    except Exception as e:
        raise RuntimeError(f"Tesseract OCR 识别失败: {e}")
    return text.strip()


def _ocr_tesseract_data(image: Image.Image, languages: Optional[List[str]]) -> dict:
    import pytesseract  # type: ignore
    path = _find_tesseract()
    if path:
        pytesseract.pytesseract.tesseract_cmd = path
    lang_str = _tess_lang(languages)
    try:
        data = pytesseract.image_to_data(
            image, lang=lang_str, output_type=pytesseract.Output.DICT
        )
    except Exception as e:
        raise RuntimeError(f"Tesseract OCR 识别失败: {e}")
    return data


def _tess_lang(languages: Optional[List[str]]) -> str:
    if languages:
        # 映射到 tesseract 代码
        mapping = {
            "en": "eng",
            "eng": "eng",
            "zh": "chi_sim",
            "zh-CN": "chi_sim",
            "zh-Hans": "chi_sim",
            "zh_sim": "chi_sim",
            "chi_sim": "chi_sim",
        }
        mapped = []
        for lang in languages:
            mapped.append(mapping.get(lang, lang))
        return "+".join(mapped)
    return "eng+chi_sim"
