"""Platform safe-area presets for the assembly pipeline (issue #239 / #60).

Burning subtitles into a delivered video (#241) needs to know, before it draws
anything, which region of the frame a platform's own UI chrome (status bars,
now-playing controls, share/like columns, caption toggles, ...) will not cover.
This module is the source-controlled data for that: three aspect-ratio presets
-- 9:16 (vertical shorts), 16:9 (landscape), and 1:1 (square) -- each carrying
a general "content safe" region and a narrower "subtitle safe" region nested
inside it.

Everything here is expressed in normalized (0..1) fractions of the frame's own
width/height rather than pixels. That is what lets one preset stay valid as
the output resolution changes while the aspect ratio does not: 1080x1920,
720x1280, and 2160x3840 are all "9:16" and all resolve through the same
``VERTICAL_9_16`` preset.

The values below are conservative general-purpose defaults (roughly modeled
on common short-form/landscape platform chrome), not a pixel-exact spec for
any single platform -- they are meant to be adjusted here, in one place, as
real subtitle rendering (#241) and preview overlays (#242) are built on top.

Non-goals (tracked separately under #60): Japanese line-breaking (#240),
actually drawing text within these regions (#241), and a preview overlay
(#242). This module only defines and validates the regions.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Float comparisons below guard against genuine overflow (an inset sum >= 1.0
# leaves no safe area at all), not against sub-pixel rounding noise, so a tiny
# epsilon keeps a deliberately exact preset (e.g. bottom shared between content
# and subtitle) from tripping on binary floating-point representation error.
_EPSILON = 1e-9

# How far a requested resolution's width/height ratio may drift from a
# preset's declared ratio and still resolve to that preset. This only needs to
# absorb even-dimension rounding (see
# ``generators.video.assembly.AssemblyGenerator._resolve_resolution``), not to
# blur genuinely different aspect ratios into one another.
_ASPECT_RATIO_TOLERANCE = 0.02

Orientation = Literal["vertical", "horizontal", "square"]


class SafeAreaRegion(BaseModel):
    """A normalized (0..1) bounding box within a frame.

    ``x``/``y`` are the top-left corner; ``width``/``height`` extend from
    there. All four are fractions of the frame's own width/height, so the same
    region is valid at any resolution sharing the frame's aspect ratio.
    """

    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_within_frame(self) -> "SafeAreaRegion":
        if self.x + self.width > 1.0 + _EPSILON:
            raise ValueError(
                "SafeAreaRegion extends past the right edge of the frame: "
                f"x={self.x} + width={self.width} > 1.0."
            )
        if self.y + self.height > 1.0 + _EPSILON:
            raise ValueError(
                "SafeAreaRegion extends past the bottom edge of the frame: "
                f"y={self.y} + height={self.height} > 1.0."
            )
        return self

    def contains(self, other: "SafeAreaRegion") -> bool:
        """True when ``other`` lies fully inside this region."""

        return (
            other.x >= self.x - _EPSILON
            and other.y >= self.y - _EPSILON
            and other.x + other.width <= self.x + self.width + _EPSILON
            and other.y + other.height <= self.y + self.height + _EPSILON
        )


class SafeAreaInsets(BaseModel):
    """Normalized inset from each edge of the frame, as [0, 1) fractions.

    Insets are fractions of the frame's own width/height (top/bottom relative
    to height, left/right relative to width), not pixels, so the same values
    stay valid when the output resolution changes but the aspect ratio does
    not.
    """

    model_config = ConfigDict(extra="forbid")

    top: float = Field(ge=0.0, lt=1.0)
    right: float = Field(ge=0.0, lt=1.0)
    bottom: float = Field(ge=0.0, lt=1.0)
    left: float = Field(ge=0.0, lt=1.0)

    @model_validator(mode="after")
    def _check_positive_area(self) -> "SafeAreaInsets":
        if self.top + self.bottom >= 1.0 - _EPSILON:
            raise ValueError(
                "SafeAreaInsets top+bottom must leave a positive safe height; "
                f"got top={self.top}, bottom={self.bottom}."
            )
        if self.left + self.right >= 1.0 - _EPSILON:
            raise ValueError(
                "SafeAreaInsets left+right must leave a positive safe width; "
                f"got left={self.left}, right={self.right}."
            )
        return self

    def region(self) -> SafeAreaRegion:
        """The bounding box remaining once these insets are trimmed from the frame."""

        return SafeAreaRegion(
            x=self.left,
            y=self.top,
            width=1.0 - self.left - self.right,
            height=1.0 - self.top - self.bottom,
        )


@dataclass(frozen=True)
class PixelRegion:
    """A safe-area region resolved to concrete, integer frame pixels."""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


@dataclass(frozen=True)
class ResolvedSafeArea:
    """The pixel regions a preset resolves to at one concrete resolution."""

    content: PixelRegion
    subtitle: PixelRegion


class SafeAreaPreset(BaseModel):
    """One aspect ratio's safe-area definition: a stable id plus two regions.

    ``content_safe`` is the general region major visual content should stay
    within. ``subtitle_safe`` is the narrower region recommended for burned-in
    subtitles specifically, and must lie fully inside ``content_safe``.
    """

    model_config = ConfigDict(extra="forbid")

    preset_id: str = Field(description="Stable, source-controlled identifier for this preset.")
    aspect_ratio: str = Field(description='Declared ratio as "W:H", e.g. "9:16".')
    orientation: Orientation
    content_safe: SafeAreaInsets
    subtitle_safe: SafeAreaInsets

    @model_validator(mode="after")
    def _check_subtitle_nested_in_content(self) -> "SafeAreaPreset":
        content_region = self.content_safe.region()
        subtitle_region = self.subtitle_safe.region()
        if not content_region.contains(subtitle_region):
            raise ValueError(
                f"Preset {self.preset_id!r}: subtitle_safe must lie fully inside "
                "content_safe, but it does not."
            )
        return self

    @property
    def ratio(self) -> float:
        """The aspect ratio as a single float (width / height)."""

        width_part, _, height_part = self.aspect_ratio.partition(":")
        return float(width_part) / float(height_part)

    def resolve(self, width: int, height: int) -> ResolvedSafeArea:
        """Resolve both regions against a concrete output resolution in pixels.

        Each edge is floored independently (not "round then clamp"), which is
        what guarantees a resolved region can never extend past the frame at
        any resolution: since a validated region always satisfies
        ``x + width <= 1.0``, ``floor((x + width) * frame_size) <= frame_size``
        for every non-negative ``frame_size``.
        """

        if width <= 0 or height <= 0:
            raise ValueError(
                "SafeAreaPreset.resolve requires a positive width and height in "
                f"pixels; got {width}x{height}."
            )
        return ResolvedSafeArea(
            content=_region_to_pixels(self.content_safe.region(), width, height),
            subtitle=_region_to_pixels(self.subtitle_safe.region(), width, height),
        )


def _region_to_pixels(region: SafeAreaRegion, width: int, height: int) -> PixelRegion:
    left = math.floor(region.x * width)
    top = math.floor(region.y * height)
    right = math.floor((region.x + region.width) * width)
    bottom = math.floor((region.y + region.height) * height)
    return PixelRegion(
        x=left,
        y=top,
        # A region's fractional width/height rounding to under one pixel at a
        # very small resolution is a degenerate-but-valid empty region, not an
        # error, so this clamps rather than yields a negative extent.
        width=max(0, right - left),
        height=max(0, bottom - top),
    )


# ---------------------------------------------------------------------------
# The three presets required by #239. Subtitle regions deliberately share
# left/right/bottom insets with their preset's content region and only widen
# the top inset: that keeps the subtitle band pinned to the bottom of the
# content-safe area (where captions conventionally sit) while making the
# nesting relationship easy to read directly off the two invariant literals.
# ---------------------------------------------------------------------------

VERTICAL_9_16 = SafeAreaPreset(
    preset_id="vertical_9x16",
    aspect_ratio="9:16",
    orientation="vertical",
    # Top: status bar / username overlay. Right: like/comment/share icon rail
    # common to vertical short-form apps. Bottom: caption strip / progress bar.
    content_safe=SafeAreaInsets(top=0.10, right=0.12, bottom=0.18, left=0.05),
    subtitle_safe=SafeAreaInsets(top=0.72, right=0.12, bottom=0.18, left=0.05),
)

HORIZONTAL_16_9 = SafeAreaPreset(
    preset_id="horizontal_16x9",
    aspect_ratio="16:9",
    orientation="horizontal",
    # Landscape players overlay far less chrome than vertical short-form apps;
    # the bottom margin mainly clears a playback control bar.
    content_safe=SafeAreaInsets(top=0.06, right=0.05, bottom=0.10, left=0.05),
    subtitle_safe=SafeAreaInsets(top=0.78, right=0.05, bottom=0.10, left=0.05),
)

SQUARE_1_1 = SafeAreaPreset(
    preset_id="square_1x1",
    aspect_ratio="1:1",
    orientation="square",
    content_safe=SafeAreaInsets(top=0.08, right=0.08, bottom=0.12, left=0.08),
    subtitle_safe=SafeAreaInsets(top=0.75, right=0.08, bottom=0.12, left=0.08),
)

SAFE_AREA_PRESETS: dict[str, SafeAreaPreset] = {
    preset.preset_id: preset for preset in (VERTICAL_9_16, HORIZONTAL_16_9, SQUARE_1_1)
}


def get_safe_area_preset(preset_id: str) -> SafeAreaPreset:
    """Look up a preset by its stable id, naming the id when it is unknown."""

    preset = SAFE_AREA_PRESETS.get(preset_id)
    if preset is None:
        raise ValueError(
            f"Unknown safe-area preset id {preset_id!r}; expected one of "
            + ", ".join(sorted(SAFE_AREA_PRESETS)) + "."
        )
    return preset


def resolve_preset_for_resolution(width: int, height: int) -> SafeAreaPreset:
    """Pick the safe-area preset whose aspect ratio best matches width/height.

    Matching by ratio (rather than requiring exact pixel dimensions) is what
    lets one preset keep applying as the output resolution changes within an
    aspect ratio: 1080x1920, 720x1280, and 2160x3840 all resolve to
    ``VERTICAL_9_16`` even though none share exact pixel dimensions.
    """

    if width <= 0 or height <= 0:
        raise ValueError(
            "resolve_preset_for_resolution requires a positive width and "
            f"height; got {width}x{height}."
        )

    target = width / height
    best_preset: SafeAreaPreset | None = None
    best_deviation = math.inf
    for preset in SAFE_AREA_PRESETS.values():
        deviation = abs(target / preset.ratio - 1.0)
        if deviation < best_deviation:
            best_deviation = deviation
            best_preset = preset

    if best_preset is None or best_deviation > _ASPECT_RATIO_TOLERANCE:
        raise ValueError(
            f"No safe-area preset matches resolution {width}x{height} (aspect "
            f"ratio {target:.4f}) within tolerance; supported presets: "
            + ", ".join(sorted(SAFE_AREA_PRESETS)) + "."
        )
    return best_preset


__all__ = [
    "HORIZONTAL_16_9",
    "PixelRegion",
    "ResolvedSafeArea",
    "SAFE_AREA_PRESETS",
    "SQUARE_1_1",
    "SafeAreaInsets",
    "SafeAreaPreset",
    "SafeAreaRegion",
    "VERTICAL_9_16",
    "get_safe_area_preset",
    "resolve_preset_for_resolution",
]
