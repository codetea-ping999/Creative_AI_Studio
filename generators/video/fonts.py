"""Font selection for text drawn onto procedural storyboard frames (#449).

The procedural storyboard runtime draws its text card with Pillow's built-in
default font, which only covers Latin. Japanese (or any other CJK) prompt text
then renders as "tofu" boxes. This module keeps the default font for text it
can render and, only when a line contains characters the default font lacks,
swaps in a CJK-capable *system* font found at a well-known path.

No font files are bundled (licensing) and no dependency is added. Resolution
order is deterministic:

1. ``STORYBOARD_FONT_PATH`` (env override) when it names an existing file.
2. The first existing path in ``CJK_FONT_CANDIDATES`` (macOS, Linux, Windows).
3. Nothing -- the caller keeps the default font, i.e. exactly the pre-#449
   behavior (tofu, but never a crash).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from functools import lru_cache
import os

from PIL import ImageFont

STORYBOARD_FONT_ENV = "STORYBOARD_FONT_PATH"

# Checked in order; the first existing file wins. Kept to well-known system
# locations so the pick is stable for a given machine.
CJK_FONT_CANDIDATES: tuple[str, ...] = (
    # macOS
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    # Linux (Debian/Ubuntu fonts-noto-cjk, Fedora google-noto-sans-cjk, Arch noto-fonts-cjk)
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/google-noto-sans-cjk-fonts/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    # Windows
    "C:\\Windows\\Fonts\\msgothic.ttc",
    "C:\\Windows\\Fonts\\YuGothM.ttc",
    "C:\\Windows\\Fonts\\meiryo.ttc",
)

# A code point in Supplementary Private Use Area-B: no real font maps it, so it
# renders as the font's ".notdef" (tofu) glyph and serves as the reference.
_NOTDEF_PROBE = "\U0010fffd"

_Font = ImageFont.FreeTypeFont | ImageFont.ImageFont


def resolve_cjk_font_path(
    *,
    env: Mapping[str, str] | None = None,
    candidates: Iterable[str] = CJK_FONT_CANDIDATES,
    is_file: Callable[[str], bool] = os.path.isfile,
) -> str | None:
    """Return the CJK-capable font path to use, or ``None`` when none exists."""

    environ = os.environ if env is None else env
    override = (environ.get(STORYBOARD_FONT_ENV) or "").strip()
    if override and is_file(override):
        return override
    for candidate in candidates:
        if is_file(candidate):
            return candidate
    return None


@lru_cache(maxsize=None)
def _default_cjk_font_path() -> str | None:
    return resolve_cjk_font_path()


@lru_cache(maxsize=16)
def _load_truetype(path: str, size: int) -> ImageFont.FreeTypeFont | None:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        # Unreadable/corrupt file: degrade to the primary font, never fail a render.
        return None


def _glyph_signature(font: _Font, char: str) -> tuple[tuple[int, int], bytes] | None:
    try:
        mask = font.getmask(char)
    except (UnicodeEncodeError, OSError, ValueError):
        return None
    return mask.size, bytes(mask)


def font_covers_text(font: _Font, text: str) -> bool:
    """Whether ``font`` has a real glyph for every character of ``text``.

    A missing glyph renders as the font's ``.notdef`` box, so a character whose
    mask is byte-identical to that of a known-unmapped code point is treated as
    uncovered. ASCII is assumed covered (every font in play supports it).
    """

    notdef = _glyph_signature(font, _NOTDEF_PROBE)
    for char in set(text):
        if char.isascii() or char.isspace():
            continue
        signature = _glyph_signature(font, char)
        if signature is None or signature == notdef:
            return False
    return True


def font_for_text(
    text: str,
    primary: _Font,
    size: int,
    *,
    fallback_path: str | None = None,
) -> _Font:
    """Pick the font to draw ``text`` with.

    Returns ``primary`` whenever it covers ``text`` (so Latin-only frames are
    unchanged) or no CJK-capable font can be found/loaded.
    """

    if font_covers_text(primary, text):
        return primary
    path = fallback_path if fallback_path is not None else _default_cjk_font_path()
    if not path:
        return primary
    fallback = _load_truetype(path, size)
    return fallback if fallback is not None else primary
