"""Capture/OCR helpers for explicit user-approved screen analysis."""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from context_gate import run_gate


_OCR_LOCK = threading.Lock()
_OCR_INSTANCE: Any | None = None

_DEFAULT_SENSITIVE_APPS = {
    "kakao",
    "kakaotalk",
    "line",
    "bank",
    "증권",
    "은행",
    "password",
    "1password",
    "bitwarden",
    "keepass",
    "mail",
    "outlook",
    "gmail",
}


@dataclass
class CaptureResult:
    image: Image.Image | None
    app_name: str
    window_title: str
    blocked: bool = False
    reason: str = ""


def _sensitive_keywords() -> set[str]:
    extra = {
        item.strip().lower()
        for item in os.getenv("RECODER_SENSITIVE_APPS", "").split(",")
        if item.strip()
    }
    return _DEFAULT_SENSITIVE_APPS | extra


def _is_sensitive(app_name: str, window_title: str) -> bool:
    haystack = f"{app_name} {window_title}".lower()
    return any(keyword and keyword in haystack for keyword in _sensitive_keywords())


def _active_window_info() -> tuple[str, str, tuple[int, int, int, int] | None]:
    if sys.platform == "win32":
        try:
            import psutil
            import win32gui
            import win32process

            hwnd = win32gui.GetForegroundWindow()
            title = win32gui.GetWindowText(hwnd) or ""
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            app_name = psutil.Process(pid).name()
            rect = win32gui.GetWindowRect(hwnd)
            return app_name, title, rect
        except Exception:
            return "", "", None
    return "", "", None


def capture_foreground_window(max_width: int = 1800) -> CaptureResult:
    """Capture only the active window when possible, falling back to the primary monitor."""
    app_name, title, rect = _active_window_info()
    if _is_sensitive(app_name, title):
        return CaptureResult(None, app_name, title, True, "민감 앱으로 판단되어 화면 캡처를 차단했습니다.")

    try:
        import mss

        with mss.mss() as sct:
            if rect:
                left, top, right, bottom = rect
                width = max(1, right - left)
                height = max(1, bottom - top)
                monitor = {"left": left, "top": top, "width": width, "height": height}
            else:
                monitor = sct.monitors[1]

            shot = sct.grab(monitor)
            image = Image.frombytes("RGB", shot.size, shot.rgb)

        if image.width > max_width:
            ratio = max_width / image.width
            image = image.resize((max_width, max(1, int(image.height * ratio))))

        return CaptureResult(image, app_name, title)
    except Exception as e:
        return CaptureResult(None, app_name, title, True, f"화면 캡처 실패: {e}")


def _get_ocr():
    global _OCR_INSTANCE
    if _OCR_INSTANCE is not None:
        return _OCR_INSTANCE
    with _OCR_LOCK:
        if _OCR_INSTANCE is not None:
            return _OCR_INSTANCE
        from rapidocr_onnxruntime import RapidOCR

        _OCR_INSTANCE = RapidOCR()
        return _OCR_INSTANCE


def extract_text_with_ocr(image: Image.Image) -> str:
    """Run RapidOCR singleton on a PIL image and return joined text lines."""
    import numpy as np

    ocr = _get_ocr()
    result, _ = ocr(np.array(image))
    if not result:
        return ""
    lines: list[str] = []
    for item in result:
        try:
            text = item[1]
        except Exception:
            text = ""
        if text:
            lines.append(str(text))
    return "\n".join(lines)


def capture_and_ocr() -> dict[str, Any]:
    cap = capture_foreground_window()
    if cap.blocked or cap.image is None:
        return {
            "status": "blocked",
            "blocked": True,
            "reason": cap.reason,
            "app_name": cap.app_name,
            "window_title": cap.window_title,
            "text": "",
            "quality_score": 0.0,
        }

    text = extract_text_with_ocr(cap.image)
    gate = run_gate(text)
    return {
        "status": "ok",
        "blocked": False,
        "reason": gate.failure_reason,
        "app_name": cap.app_name,
        "window_title": cap.window_title,
        "text": gate.text,
        "raw_text": text,
        "quality_score": gate.quality_score,
        "passed": gate.passed,
    }


def save_debug_capture(image: Image.Image, root: Path) -> Path:
    """Optional local-only debug helper. Not used by default."""
    target = root / ".recoder" / "debug_capture.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target)
    return target
