"""Language-aware text measurement shared by story merging and subtitle building.

The studio is Japanese-first (``StoryDocument.language`` defaults to ``ja``), so
neither word counting nor line wrapping may assume that words are separated by
spaces. Both helpers therefore branch on how much of the text is CJK.
"""

from __future__ import annotations

import textwrap
import unicodedata

# Codepoint ranges whose scripts are written without spaces between words. Each
# character in these ranges carries roughly one word of information, which is why
# they are counted and wrapped per character instead of per whitespace token.
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xAC00, 0xD7A3),  # Hangul syllables
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0xFF66, 0xFF9D),  # Halfwidth Katakana
    (0x20000, 0x2A6DF),  # CJK Unified Ideographs Extension B
)

# Text is treated as CJK once this share of its non-space characters is CJK. A
# Japanese sentence quoting an English product name stays CJK; an English
# sentence with one kanji gloss stays Latin.
CJK_TEXT_THRESHOLD = 0.3

# Subtitle line budgets. Latin follows the common 42-character broadcast limit;
# CJK glyphs are wider and denser, so a Japanese line is capped at 20 characters.
SUBTITLE_MAX_CHARS_LATIN = 42
SUBTITLE_MAX_CHARS_CJK = 20

# Sentence-final punctuation: always a good place to end a subtitle line.
_SENTENCE_BREAKS = "。．！？!?…"
# Secondary breaks: used only when a line would otherwise overflow.
_SOFT_BREAKS = "、，,；;：:）」』】〉»"


def is_cjk_char(character: str) -> bool:
    """Return True for a space-less-script character that counts as one word."""

    if unicodedata.category(character).startswith("P"):
        # Ideographic punctuation (。、・「」) lives inside these blocks but is not
        # a word, so it must never inflate a word count.
        return False
    codepoint = ord(character)
    return any(low <= codepoint <= high for low, high in _CJK_RANGES)


def is_cjk_context_char(character: str) -> bool:
    """Return True for a character that sits inside space-less script text.

    Unlike :func:`is_cjk_char` this includes ideographic punctuation (``。、「」``)
    and fullwidth forms. Those are not words, so they must not be counted, but a
    line break next to them still must not become a space: ``"…た。" + "彼女は…"``
    is one continuous Japanese sentence.
    """

    codepoint = ord(character)
    if 0x3000 <= codepoint <= 0x303F:  # CJK symbols and punctuation
        return True
    if 0xFF01 <= codepoint <= 0xFF65:  # Fullwidth forms and halfwidth katakana marks
        return True
    return any(low <= codepoint <= high for low, high in _CJK_RANGES)


def cjk_ratio(text: str) -> float:
    """Return the share of non-whitespace characters that are CJK."""

    visible = [character for character in text if not character.isspace()]
    if not visible:
        return 0.0
    return sum(1 for character in visible if is_cjk_char(character)) / len(visible)


def is_cjk_text(text: str) -> bool:
    """Return True when the text should be measured with CJK rules."""

    return cjk_ratio(text) >= CJK_TEXT_THRESHOLD


def count_words(text: str) -> int:
    """Count words with one rule that behaves sensibly in Japanese and English.

    The rule:

    - every CJK character (kana, ideograph, Hangul) counts as one word, because
      those scripts are written without spaces and per-character counting is what
      Japanese writing tools report;
    - the rest of the text is split on whitespace and each token containing at
      least one alphanumeric character counts as one word, so markdown syntax
      such as ``##`` or ``---`` is not counted;
    - punctuation never counts, in either script.

    So ``"# 第一章\n吾輩は猫である。"`` counts 10 (``第一章`` is 3 plus
    ``吾輩は猫である`` is 7; ``#`` and ``。`` are skipped) and ``"hello world"``
    counts 2.
    """

    cjk_total = 0
    remainder: list[str] = []
    for character in text:
        if is_cjk_char(character):
            cjk_total += 1
            # A CJK character also terminates any adjacent Latin token so that
            # "AI時代" is counted as one Latin word plus two CJK characters.
            remainder.append(" ")
        else:
            remainder.append(character)

    latin_total = sum(
        1
        for token in "".join(remainder).split()
        if any(character.isalnum() for character in token)
    )
    return cjk_total + latin_total


def collapse_whitespace(text: str) -> str:
    """Collapse whitespace runs, dropping spaces that separate two CJK characters.

    Narration arrives with hard line breaks from the LLM. Re-joining Japanese with
    a space would insert a character that does not exist in the language, so the
    separator is dropped whenever both sides of the break are CJK.
    """

    tokens = text.split()
    if not tokens:
        return ""

    joined = tokens[0]
    for token in tokens[1:]:
        if is_cjk_context_char(joined[-1]) and is_cjk_context_char(token[0]):
            joined += token
        else:
            joined += " " + token
    return joined


def split_subtitle_lines(text: str, *, max_chars: int | None = None) -> list[str]:
    """Split narration into subtitle-sized lines.

    Latin text is wrapped at whitespace to ``SUBTITLE_MAX_CHARS_LATIN`` (42)
    characters. CJK text is wrapped to ``SUBTITLE_MAX_CHARS_CJK`` (20) characters,
    breaking after sentence punctuation (``。！？``) when possible, otherwise after
    a soft break (``、``), and only as a last resort mid-phrase at the limit.
    """

    normalized = collapse_whitespace(text)
    if not normalized:
        return []

    if is_cjk_text(normalized):
        limit = max_chars or SUBTITLE_MAX_CHARS_CJK
        return _split_cjk_lines(normalized, limit)

    limit = max_chars or SUBTITLE_MAX_CHARS_LATIN
    return textwrap.wrap(normalized, width=limit, break_long_words=True) or []


def _split_cjk_lines(text: str, limit: int) -> list[str]:
    lines: list[str] = []
    current = ""
    soft_break_at = 0

    for character in text:
        current += character
        if character in _SENTENCE_BREAKS:
            lines.append(current)
            current = ""
            soft_break_at = 0
            continue
        if character in _SOFT_BREAKS:
            soft_break_at = len(current)
        if len(current) >= limit:
            # Prefer a soft break, but only when it does not leave a stub line;
            # half the budget is the smallest line worth showing on screen.
            if soft_break_at >= max(1, limit // 2):
                lines.append(current[:soft_break_at])
                current = current[soft_break_at:]
            else:
                lines.append(current)
                current = ""
            soft_break_at = 0

    if current:
        lines.append(current)
    return [line for line in (entry.strip() for entry in lines) if line]


__all__ = [
    "CJK_TEXT_THRESHOLD",
    "SUBTITLE_MAX_CHARS_CJK",
    "SUBTITLE_MAX_CHARS_LATIN",
    "cjk_ratio",
    "collapse_whitespace",
    "count_words",
    "is_cjk_char",
    "is_cjk_context_char",
    "is_cjk_text",
    "split_subtitle_lines",
]
