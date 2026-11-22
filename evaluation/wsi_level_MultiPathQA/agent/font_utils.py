# -*- coding: utf-8 -*-
"""Cross-platform scalable fonts for grid-number overlays.

``agent_loop.py`` uses this loader only.  Callers keep their own size formulas.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from PIL import ImageFont

LOGGER = logging.getLogger(__name__)

BITMAP_FONT_SOURCE = "pillow-default-bitmap"
SCALABLE_DEFAULT_SOURCE = "pillow-default-scalable"

# Raw strings / forward slashes so Windows ``\a`` is not an escape.
GRID_LABEL_FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\msyhbd.ttc",
    "/mnt/c/Windows/Fonts/arialbd.ttf",
    "/mnt/c/Windows/Fonts/segoeuib.ttf",
    "/mnt/c/Windows/Fonts/msyhbd.ttc",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)


def load_grid_label_font(size: int) -> tuple[Any, str]:
    """Return ``(font, source)`` for a requested pixel size.

    ``source`` is the font path, ``pillow-default-scalable``, or
    ``pillow-default-bitmap``.
    """
    if type(size) is not int or size < 1:
        raise ValueError("size must be an integer >= 1")

    for path in GRID_LABEL_FONT_CANDIDATES:
        if not os.path.isfile(path):
            continue
        font = _try_truetype(path, size)
        if font is not None:
            return font, path

    try:
        return ImageFont.load_default(size=size), SCALABLE_DEFAULT_SOURCE
    except TypeError:
        LOGGER.warning("无可用 TrueType，网格编号将使用不可缩放默认字体")
        return ImageFont.load_default(), BITMAP_FONT_SOURCE


def _try_truetype(path: str, size: int):
    try:
        return ImageFont.truetype(path, size)
    except Exception as exc:
        if path.lower().endswith((".ttc", ".otc")):
            try:
                return ImageFont.truetype(path, size, index=0)
            except Exception as ttc_exc:
                exc = ttc_exc
        LOGGER.warning(
            "字体文件存在但加载失败: %s (%s: %s)",
            path,
            type(exc).__name__,
            exc,
        )
        return None


__all__ = [
    "BITMAP_FONT_SOURCE",
    "GRID_LABEL_FONT_CANDIDATES",
    "SCALABLE_DEFAULT_SOURCE",
    "load_grid_label_font",
]
