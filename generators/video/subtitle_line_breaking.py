"""Minimum Japanese subtitle line-breaking rules (kinsoku shori) -- issue #240 / #60.

Burning subtitles into a delivered video (#241) needs wrapped lines handed to
it in an order that is already safe to display: no line should open with
punctuation that visually belongs to the previous line, and no line should
close with an opening bracket whose content belongs on the next one. This
module is that small, deterministic layer -- it does not render or measure
pixels (#241 does that); it only decides where line breaks fall.

Scope (what this module does)
------------------------------
- Wraps text to a character-count measure: CJK text is measured per
  character, Latin text at whitespace, matching the budgets
  ``core.story.text_utils`` already defines for subtitles
  (``SUBTITLE_MAX_CHARS_CJK`` / ``SUBTITLE_MAX_CHARS_LATIN``).
- Line-start prohibition (行頭禁則): closing brackets/quotes, ideographic
  comma/period, small kana, and a few punctuation marks never open a line --
  a wrap point that would leave one at a line's start instead pulls it back
  onto the previous line.
- Line-end prohibition (行末禁則): opening brackets/quotes never close a line
  -- a wrap point that would leave one at a line's end instead pushes it onto
  the next line.
- Explicit line breaks (``\\n``) the caller already authored are preserved as
  hard segment boundaries rather than silently rejoined; kinsoku adjustment
  still runs across them, so an authored break cannot introduce a violation.
- Long unbroken tokens (URLs, unspaced runs) are hard-broken at the measure
  instead of overflowing a line unboundedly.

Explicitly out of scope (minimum rule set, not a typesetting engine)
----------------------------------------------------------------------
This deliberately does not implement full Japanese kinsoku shori. In
particular:

- No hanging punctuation / oidashi (ぶら下げ・追い出し) beyond the single
  pulled/pushed character described above -- a professional engine may also
  shrink a character or re-flow an entire paragraph to avoid a break;
  this module never does either.
- No word-boundary awareness for Japanese (no morphological analyzer, no
  bunsetsu segmentation): CJK text wraps per character, same as the rest of
  this codebase.
- No adjustment of a line's *own* first/last character when it is not the
  product of a wrap point -- if the input text itself starts with a
  prohibited character (e.g. a caller hands in narration that literally opens
  with "。"), that character is preserved as authored, since there is no
  preceding line to move it onto.
- No pixel measurement -- this module counts characters, not rendered glyph
  widths (#241 measures pixels for the actual burned-in render).
"""

from __future__ import annotations

import textwrap
from typing import Sequence

from core.story.text_utils import (
    SUBTITLE_MAX_CHARS_CJK,
    SUBTITLE_MAX_CHARS_LATIN,
    collapse_whitespace,
    is_cjk_text,
)

# Line-start prohibited characters (行頭禁則文字): a wrap must never leave one
# of these as the first character of a line, because each visually belongs to
# the content immediately before it.
LINE_START_PROHIBITED: frozenset[str] = frozenset(
    "、。，．・"  # ideographic comma / period / middle dot
    "」』）】〉》〕〙〗"  # closing brackets and quotes
    "！？!?"  # exclamation / question, full- and halfwidth
    "ぁぃぅぇぉっゃゅょゎ"  # small hiragana
    "ァィゥェォッャュョヮ"  # small katakana
    "ーゝゞ"  # prolonged sound mark / iteration marks
    "…‥"  # ellipses
    "：；:;"  # colon / semicolon, full- and halfwidth
    ")]},."  # halfwidth closers, comma, period
)

# Line-end prohibited characters (行末禁則文字): a wrap must never leave one of
# these as the last character of a line, because it introduces content that
# belongs together with what follows.
LINE_END_PROHIBITED: frozenset[str] = frozenset(
    "「『（【〈《〔〘〖"  # opening brackets and quotes
    "([{"  # halfwidth openers
)

# The pull/push fix-up below relies on these two sets never overlapping: a
# character pulled onto the end of a line (because it may not start the next
# one) must never itself be something that may not end a line, or the two
# passes could fight each other. See ``apply_kinsoku_rules`` for how that
# invariant is used.
assert not (LINE_START_PROHIBITED & LINE_END_PROHIBITED), (
    "LINE_START_PROHIBITED and LINE_END_PROHIBITED must be disjoint for the "
    "single-pass kinsoku fix-up to terminate correctly."
)

_SENTENCE_BREAKS = "。．！？!?…"
_SOFT_BREAKS = "、，,；;：:）」』】〉»"


def is_line_start_prohibited(character: str) -> bool:
    """True when ``character`` must never open a subtitle line."""

    return character in LINE_START_PROHIBITED


def is_line_end_prohibited(character: str) -> bool:
    """True when ``character`` must never close a subtitle line."""

    return character in LINE_END_PROHIBITED


def break_subtitle_lines(text: str, *, max_chars: int | None = None) -> list[str]:
    """Wrap subtitle text deterministically, applying the minimum kinsoku rules.

    ``text`` may contain explicit ``\\n`` (or ``\\r\\n`` / ``\\r``) breaks;
    those are preserved as hard segment boundaries instead of being rejoined,
    then each segment is wrapped independently before the minimum kinsoku
    rules run across the *whole* resulting line list (so an authored break
    right before closing punctuation still gets corrected). The result never
    contains empty lines: an all-whitespace segment simply contributes none.

    Raises ``ValueError`` if ``max_chars`` is given but not a positive
    integer, since a zero or negative measure cannot wrap anything.
    """

    if max_chars is not None and max_chars <= 0:
        raise ValueError(f"max_chars must be a positive integer; got {max_chars!r}.")

    segments = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    lines: list[str] = []
    for segment in segments:
        cleaned = collapse_whitespace(segment)
        if not cleaned:
            continue
        if is_cjk_text(cleaned):
            lines.extend(_wrap_cjk(cleaned, max_chars or SUBTITLE_MAX_CHARS_CJK))
        else:
            lines.extend(_wrap_latin(cleaned, max_chars or SUBTITLE_MAX_CHARS_LATIN))

    return apply_kinsoku_rules(lines)


def apply_kinsoku_rules(lines: Sequence[str]) -> list[str]:
    """Fix up an already-wrapped line list so no boundary violates kinsoku.

    This never re-flows text: it only ever moves a single character across an
    *existing* line boundary, pulling a line-start-prohibited character back
    onto the previous line or pushing a line-end-prohibited character onto the
    next one. Because ``LINE_START_PROHIBITED`` and ``LINE_END_PROHIBITED``
    are disjoint, a single left-to-right pass over the boundaries is enough:
    a character just pulled onto the end of a line can never itself be a
    line-end violation, so the following push check cannot re-trigger the
    pull check that just ran, and each character moves at most once. Lines
    that end up empty (fully absorbed by a neighbour) are dropped.
    """

    result = [str(line) for line in lines]

    for index in range(len(result) - 1):
        # Pull a would-be line-starting prohibited character back onto the
        # previous line, even if that line then exceeds the target measure --
        # readability wins over exact width for this one character.
        while result[index + 1] and result[index + 1][0] in LINE_START_PROHIBITED:
            result[index] += result[index + 1][0]
            result[index + 1] = result[index + 1][1:]

        # Push a would-be line-ending opening bracket onto the next line.
        while result[index] and result[index][-1] in LINE_END_PROHIBITED:
            result[index + 1] = result[index][-1] + result[index + 1]
            result[index] = result[index][:-1]

    return [line for line in result if line]


def _wrap_cjk(text: str, limit: int) -> list[str]:
    """Greedy per-character wrap: sentence break > soft break > hard cut.

    This mirrors ``core.story.text_utils._split_cjk_lines`` -- the same
    budget, the same break preferences -- so a caller upgrading from
    ``split_subtitle_lines`` to ``break_subtitle_lines`` sees the same wrap
    points, just with kinsoku correction layered on top.
    """

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
            # Prefer a soft break, but only when it does not leave a stub
            # line; half the budget is the smallest line worth showing.
            if soft_break_at >= max(1, limit // 2):
                lines.append(current[:soft_break_at])
                current = current[soft_break_at:]
            else:
                lines.append(current)
                current = ""
            soft_break_at = 0

    if current:
        lines.append(current)
    return lines


def _wrap_latin(text: str, limit: int) -> list[str]:
    """Whitespace-token wrap with a hard fallback for unbroken tokens.

    ``break_long_words=True`` is what keeps a single URL-like token longer
    than ``limit`` from overflowing a line unboundedly: it is hard-split at
    the measure instead. Hyphen-splitting is disabled so a URL is not broken
    at a hyphen it happens to contain.
    """

    return textwrap.wrap(
        text,
        width=limit,
        break_long_words=True,
        break_on_hyphens=False,
    )


__all__ = [
    "LINE_END_PROHIBITED",
    "LINE_START_PROHIBITED",
    "apply_kinsoku_rules",
    "break_subtitle_lines",
    "is_line_end_prohibited",
    "is_line_start_prohibited",
]
