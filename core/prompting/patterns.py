"""Axis catalogs: the design space a variation matrix explores.

These are deliberately plain data structures rather than generated strings. The
value of a 30-pattern sweep comes from *what* it explores, so the catalog has to
be reviewable and editable in a diff by whoever is art-directing the run.

Every entry has the shape::

    {"label": "centered-wordmark", "patch": {...}, "tags": ["wordmark"]}

``label`` is a stable slug: batch item labels and exported filenames derive from
it, so renaming one changes how past runs line up against new ones.
``patch`` is merged into a generation request (see ``core.batches``) or read by
``core.prompting.composer`` as an axis value.
"""

from __future__ import annotations

from typing import Any


def _structure(
    label: str,
    fragment: str,
    *,
    negative: str = "",
    tags: tuple[str, ...] = (),
    aspect: str = "square",
) -> dict[str, Any]:
    patch: dict[str, Any] = {"prompt_fragment": fragment}
    if negative:
        patch["negative_fragment"] = negative
    patch["attributes"] = {"composition": label.replace("-", " ")}
    return {
        "label": label,
        "patch": patch,
        "tags": list(tags) + [f"aspect:{aspect}"],
    }


# --------------------------------------------------------------------- logos
# 30 construction patterns covering the practical logo design space: pure type,
# lockups, monograms, contained marks, pictorial marks, and textured marks.
LOGO_STRUCTURES: tuple[dict[str, Any], ...] = (
    _structure(
        "centered-wordmark",
        "centered wordmark logotype, single line of custom lettering, generous letter spacing",
        tags=("wordmark", "type-only"),
        aspect="wide",
    ),
    _structure(
        "stacked-lockup",
        "stacked lockup, symbol above the wordmark, tight vertical alignment, shared optical center",
        tags=("lockup",),
    ),
    _structure(
        "horizontal-lockup",
        "horizontal lockup, symbol to the left of the wordmark, baseline aligned, balanced weight",
        tags=("lockup",),
        aspect="wide",
    ),
    _structure(
        "monogram-circle",
        "monogram inside a perfect circle, two overlapping initials, even stroke weight",
        tags=("monogram", "contained"),
    ),
    _structure(
        "monogram-shield",
        "monogram inside a shield outline, heraldic proportions, strong flat silhouette",
        tags=("monogram", "contained", "emblem"),
    ),
    _structure(
        "lettermark-ligature",
        "lettermark built from a single custom ligature, two letters fused into one shape",
        tags=("monogram", "type-only"),
    ),
    _structure(
        "negative-space-mark",
        "negative space mark, hidden second shape revealed by the counterform, two-tone silhouette",
        tags=("clever", "pictorial"),
    ),
    _structure(
        "geometric-abstract",
        "abstract geometric mark, constructed from circles and triangles on a strict grid",
        tags=("abstract", "geometric"),
    ),
    _structure(
        "minimal-line-mark",
        "minimal single-stroke line mark, uniform thin stroke, one continuous path",
        tags=("abstract", "minimal"),
    ),
    _structure(
        "mascot-bust",
        "friendly mascot bust mark, simplified head and shoulders, flat shapes, no gradients",
        negative="photorealistic face, complex shading",
        tags=("mascot", "pictorial"),
    ),
    _structure(
        "animal-silhouette",
        "animal silhouette mark, single solid shape, recognizable profile, no interior detail",
        tags=("pictorial", "silhouette"),
    ),
    _structure(
        "emblem-ribbon",
        "emblem with a ribbon banner across the lower third, contained composition, vintage balance",
        tags=("emblem", "contained"),
    ),
    _structure(
        "badge-border-text",
        "circular badge with text running along the inner border, central icon, concentric rings",
        tags=("emblem", "badge", "contained"),
    ),
    _structure(
        "hexagon-containment",
        "mark contained in a hexagon, flat fill, precise 30 degree angles, technical feel",
        tags=("contained", "geometric"),
    ),
    _structure(
        "overlapping-transparency",
        "two overlapping shapes with multiply transparency at the intersection, three flat tones",
        tags=("abstract", "layered"),
    ),
    _structure(
        "gradient-orb",
        "smooth gradient orb mark, soft two-color blend, subtle inner light, modern app icon feel",
        negative="harsh banding, noise",
        tags=("abstract", "gradient"),
    ),
    _structure(
        "isometric-cube",
        "isometric cube construction mark, three visible faces in three tones, impossible-object hint",
        tags=("geometric", "dimensional"),
    ),
    _structure(
        "dynamic-swoosh",
        "dynamic swoosh mark, single tapering curve suggesting forward motion, asymmetric balance",
        tags=("abstract", "motion"),
    ),
    _structure(
        "organic-leaf",
        "organic leaf mark, soft asymmetric curves, hand-balanced silhouette, natural growth feel",
        tags=("pictorial", "organic"),
    ),
    _structure(
        "waveform-mark",
        "waveform mark, evenly spaced vertical bars of varying height, audio equalizer rhythm",
        tags=("abstract", "geometric"),
    ),
    _structure(
        "location-pin",
        "location pin mark, teardrop containment with an icon inside, flat two-tone fill",
        tags=("pictorial", "contained"),
    ),
    _structure(
        "keyhole-mark",
        "keyhole mark, security metaphor from a single subtractive cut, solid outer shape",
        tags=("pictorial", "clever"),
    ),
    _structure(
        "layered-chevron",
        "layered chevron mark, three nested angles with increasing tone, upward direction",
        tags=("abstract", "geometric"),
    ),
    _structure(
        "grid-dot-matrix",
        "dot matrix mark, disciplined grid of circles forming a larger shape, modular rhythm",
        tags=("abstract", "modular"),
    ),
    _structure(
        "brush-stroke",
        "single brush stroke mark, expressive ink edge, visible bristle texture, high contrast",
        negative="clean vector edges",
        tags=("hand-made", "textured"),
    ),
    _structure(
        "typographic-ampersand",
        "oversized typographic ampersand as the mark, elegant serif contrast, single glyph focus",
        tags=("type-only", "editorial"),
    ),
    _structure(
        "split-letter",
        "split letter mark, one initial divided by a diagonal gap into two tones",
        tags=("monogram", "geometric"),
    ),
    _structure(
        "arch-dome",
        "arch mark, semicircular dome over a flat base, architectural symmetry, museum calm",
        tags=("geometric", "architectural"),
    ),
    _structure(
        "stamp-seal",
        "stamp seal mark, slightly distressed circular border, letterpress imperfection",
        negative="crisp digital edges",
        tags=("emblem", "textured"),
    ),
    _structure(
        "pixel-modular",
        "pixel modular mark, built from equal squares on an 8x8 grid, deliberate aliasing",
        negative="antialiased curves",
        tags=("modular", "retro"),
    ),
)


# ---------------------------------------------------------------- thumbnails
# 30 layouts for video thumbnails. These are about legibility at small size and
# where attention lands first, which is why most of them name the focal split.
THUMBNAIL_STRUCTURES: tuple[dict[str, Any], ...] = (
    _structure(
        "left-face-right-text",
        "subject face on the left third, large bold headline occupying the right two thirds",
        tags=("face", "text-heavy"),
        aspect="wide",
    ),
    _structure(
        "centered-subject-framed",
        "single subject centered, thick contrasting frame border, shallow background",
        tags=("centered",),
        aspect="wide",
    ),
    _structure(
        "big-number",
        "one enormous numeral dominating the frame, small supporting caption beneath",
        tags=("text-heavy", "numeric"),
        aspect="wide",
    ),
    _structure(
        "before-after-split",
        "vertical split, before state on the left and after state on the right, hard dividing line",
        tags=("comparison",),
        aspect="wide",
    ),
    _structure(
        "arrow-focus",
        "bright arrow pointing at the focal element, everything else desaturated",
        tags=("annotation",),
        aspect="wide",
    ),
    _structure(
        "question-overlay",
        "short question in large type across the upper half, reacting subject in the lower half",
        tags=("text-heavy", "face"),
        aspect="wide",
    ),
    _structure(
        "three-panel-comparison",
        "three equal vertical panels comparing three options, numbered labels",
        tags=("comparison",),
        aspect="wide",
    ),
    _structure(
        "top-band-title",
        "solid color band across the top holding the title, image filling the remainder",
        tags=("text-heavy",),
        aspect="wide",
    ),
    _structure(
        "diagonal-split",
        "diagonal split composition, two contrasting scenes meeting at a sharp angle",
        tags=("comparison", "dynamic"),
        aspect="wide",
    ),
    _structure(
        "circle-crop-subject",
        "subject in a circular crop on one side, flat color field and text on the other",
        tags=("face", "geometric"),
        aspect="wide",
    ),
    _structure(
        "product-hero-plain",
        "single product centered on a plain seamless background, soft studio shadow",
        tags=("product",),
        aspect="wide",
    ),
    _structure(
        "screenshot-callout",
        "application screenshot with a highlighted region and a callout box",
        tags=("annotation", "screen"),
        aspect="wide",
    ),
    _structure(
        "versus-split",
        "two subjects facing each other across a central VS divider, symmetrical tension",
        tags=("comparison",),
        aspect="wide",
    ),
    _structure(
        "timeline-strip",
        "horizontal timeline strip of four small stages with connecting line and labels",
        tags=("informational",),
        aspect="wide",
    ),
    _structure(
        "checklist-overlay",
        "three checklist items with large check marks overlaid on a soft background image",
        tags=("informational", "text-heavy"),
        aspect="wide",
    ),
    _structure(
        "reaction-face-corner",
        "main scene filling the frame with a small reacting face inset in a corner",
        tags=("face",),
        aspect="wide",
    ),
    _structure(
        "bold-outline-subject",
        "subject cut out with a thick bright outline against a flat contrasting field",
        tags=("cutout",),
        aspect="wide",
    ),
    _structure(
        "blurred-background-subject",
        "sharp subject in the foreground, strongly blurred context behind, clear depth separation",
        tags=("depth",),
        aspect="wide",
    ),
    _structure(
        "hand-drawn-annotation",
        "hand drawn circles and arrows annotating a photograph, marker texture",
        tags=("annotation", "hand-made"),
        aspect="wide",
    ),
    _structure(
        "progress-meter",
        "large progress meter or gauge as the focal element, value label centered",
        tags=("informational", "numeric"),
        aspect="wide",
    ),
    _structure(
        "ranked-podium",
        "three ranked items on a podium arrangement, first place raised and centered",
        tags=("comparison", "numeric"),
        aspect="wide",
    ),
    _structure(
        "map-locator",
        "stylized map with a highlighted location marker, muted terrain, single accent color",
        tags=("informational",),
        aspect="wide",
    ),
    _structure(
        "chat-bubble-overlay",
        "two chat bubbles overlaid on a scene, short exchange, high contrast bubble fills",
        tags=("annotation", "text-heavy"),
        aspect="wide",
    ),
    _structure(
        "price-tag",
        "prominent price tag element over the product, crossed-out old value beside it",
        tags=("product", "numeric"),
        aspect="wide",
    ),
    _structure(
        "countdown",
        "countdown motif, large remaining count with subtle radial urgency background",
        tags=("numeric", "urgency"),
        aspect="wide",
    ),
    _structure(
        "tool-flatlay",
        "overhead flatlay of related tools arranged on a neutral surface, even lighting",
        tags=("product", "overhead"),
        aspect="wide",
    ),
    _structure(
        "code-on-screen",
        "editor window with legible syntax highlighted code, one line emphasized",
        tags=("screen", "technical"),
        aspect="wide",
    ),
    _structure(
        "chart-and-subject",
        "rising chart on one side and the presenter on the other, clear numeric axis",
        tags=("informational", "face"),
        aspect="wide",
    ),
    _structure(
        "silhouette-glow",
        "backlit subject silhouette against a glowing rim light, dark surrounding field",
        tags=("silhouette", "dramatic"),
        aspect="wide",
    ),
    _structure(
        "full-bleed-text",
        "full bleed typography filling the entire frame, no imagery, extreme weight contrast",
        tags=("text-heavy", "type-only"),
        aspect="wide",
    ),
)


# ------------------------------------------------------------------- tone
TONE_AND_MANNER: tuple[dict[str, Any], ...] = (
    {
        "label": "minimal",
        "patch": {
            "prompt_fragment": "minimal design, generous white space, restrained palette, precise alignment",
            "negative_fragment": "ornament, clutter, texture noise, busy detail",
            "palette": ["#0F172A", "#64748B", "#E2E8F0", "#FFFFFF"],
            "attributes": {"typography": "geometric sans, wide tracking"},
        },
        "tags": ["calm", "modern"],
    },
    {
        "label": "premium",
        "patch": {
            "prompt_fragment": "premium finish, deep contrast, metallic accent, refined proportions",
            "negative_fragment": "cheap plastic look, neon, cluttered layout",
            "palette": ["#0B0B0D", "#1C1C21", "#C8A96A", "#F5F1E8"],
            "attributes": {"typography": "high contrast serif, tight tracking"},
        },
        "tags": ["luxury", "dark"],
    },
    {
        "label": "playful",
        "patch": {
            "prompt_fragment": "playful mood, rounded shapes, bouncy asymmetry, bright saturated colors",
            "negative_fragment": "somber tone, sharp corners, corporate stiffness",
            "palette": ["#FF6B4A", "#FFC93C", "#3ABEF0", "#2D2A32"],
            "attributes": {"typography": "rounded sans, heavy weight"},
        },
        "tags": ["bright", "friendly"],
    },
    {
        "label": "technical",
        "patch": {
            "prompt_fragment": "technical diagram feel, precise grid, monospace labels, measured spacing",
            "negative_fragment": "hand drawn wobble, decorative flourish",
            "palette": ["#0D1117", "#238636", "#58A6FF", "#C9D1D9"],
            "attributes": {"typography": "monospace, uniform weight"},
        },
        "tags": ["engineering", "dark"],
    },
    {
        "label": "retro",
        "patch": {
            "prompt_fragment": "retro seventies print feel, limited ink palette, slight registration offset, halftone grain",
            "negative_fragment": "clean digital gradient, modern minimalism",
            "palette": ["#E8A33D", "#C1502E", "#2F4B4E", "#F1E4CE"],
            "attributes": {"typography": "condensed slab serif"},
        },
        "tags": ["vintage", "warm"],
    },
    {
        "label": "organic",
        "patch": {
            "prompt_fragment": "organic natural feel, earthy muted tones, soft irregular edges, paper texture",
            "negative_fragment": "synthetic neon, hard geometry, glossy plastic",
            "palette": ["#5C6B4C", "#A8B79A", "#D9CBB2", "#3B3227"],
            "attributes": {"typography": "humanist sans, soft terminals"},
        },
        "tags": ["natural", "calm"],
    },
    {
        "label": "editorial",
        "patch": {
            "prompt_fragment": "editorial magazine layout, strong typographic hierarchy, generous margins, single accent",
            "negative_fragment": "amateur spacing, competing focal points",
            "palette": ["#111111", "#8C1D18", "#EDE9E2", "#FFFFFF"],
            "attributes": {"typography": "didone headline, sans deck"},
        },
        "tags": ["print", "sophisticated"],
    },
    {
        "label": "neon",
        "patch": {
            "prompt_fragment": "neon night aesthetic, glowing edges, deep shadow, chromatic accent light",
            "negative_fragment": "flat daylight, pastel wash, low contrast",
            "palette": ["#08040F", "#FF2E88", "#00E5FF", "#7B2CFF"],
            "attributes": {"typography": "wide sans, glowing stroke"},
        },
        "tags": ["dark", "vivid"],
    },
    {
        "label": "hand-drawn",
        "patch": {
            "prompt_fragment": "hand drawn character, visible pencil and ink texture, slightly uneven line weight",
            "negative_fragment": "vector precision, mechanical repetition",
            "palette": ["#2B2B2B", "#E4D8C3", "#B5563F", "#6E8B74"],
            "attributes": {"typography": "hand lettered, irregular baseline"},
        },
        "tags": ["warm", "crafted"],
    },
    {
        "label": "corporate",
        "patch": {
            "prompt_fragment": "corporate clarity, confident blue palette, symmetric balance, dependable structure",
            "negative_fragment": "experimental layout, distressed texture, playful chaos",
            "palette": ["#0A2540", "#1668B3", "#7FA8CC", "#F2F6FA"],
            "attributes": {"typography": "neutral grotesque, medium weight"},
        },
        "tags": ["trustworthy", "light"],
    },
)


CAMERA_ANGLE: tuple[dict[str, Any], ...] = (
    {"label": "eye-level", "patch": {"prompt_fragment": "eye level shot, neutral perspective"}, "tags": ["neutral"]},
    {"label": "low-angle", "patch": {"prompt_fragment": "low angle shot looking up, imposing scale"}, "tags": ["dramatic"]},
    {"label": "high-angle", "patch": {"prompt_fragment": "high angle shot looking down, vulnerable framing"}, "tags": ["dramatic"]},
    {"label": "close-up", "patch": {"prompt_fragment": "close up, shallow depth of field, facial detail"}, "tags": ["intimate"]},
    {"label": "wide-establishing", "patch": {"prompt_fragment": "wide establishing shot, subject small in the environment"}, "tags": ["context"]},
    {"label": "over-the-shoulder", "patch": {"prompt_fragment": "over the shoulder framing, foreground occlusion"}, "tags": ["narrative"]},
    {"label": "dutch-tilt", "patch": {"prompt_fragment": "dutch tilt, canted horizon, unease"}, "tags": ["dramatic"]},
    {"label": "overhead-flat", "patch": {"prompt_fragment": "directly overhead flat lay, orthographic feel"}, "tags": ["graphic"]},
)


COLOR_SCHEME: tuple[dict[str, Any], ...] = (
    {"label": "monochrome", "patch": {"prompt_fragment": "monochrome palette, single hue with tonal range"}, "tags": ["restrained"]},
    {"label": "complementary", "patch": {"prompt_fragment": "complementary color scheme, two opposing hues"}, "tags": ["contrast"]},
    {"label": "analogous", "patch": {"prompt_fragment": "analogous color scheme, neighbouring hues, gentle transition"}, "tags": ["harmonious"]},
    {"label": "triadic", "patch": {"prompt_fragment": "triadic color scheme, three evenly spaced hues"}, "tags": ["vivid"]},
    {"label": "warm-dominant", "patch": {"prompt_fragment": "warm dominant palette, amber and terracotta"}, "tags": ["warm"]},
    {"label": "cool-dominant", "patch": {"prompt_fragment": "cool dominant palette, slate and teal"}, "tags": ["cool"]},
    {"label": "high-key", "patch": {"prompt_fragment": "high key lighting, bright even exposure, minimal shadow"}, "tags": ["light"]},
    {"label": "low-key", "patch": {"prompt_fragment": "low key lighting, deep shadow, small bright area"}, "tags": ["dark"]},
)


_CATALOGS: dict[str, tuple[dict[str, Any], ...]] = {
    "logo_structure": LOGO_STRUCTURES,
    "thumbnail_structure": THUMBNAIL_STRUCTURES,
    "tone_and_manner": TONE_AND_MANNER,
    "camera_angle": CAMERA_ANGLE,
    "color_scheme": COLOR_SCHEME,
}


def list_axis_catalogs() -> list[str]:
    """Return the available axis catalog names."""

    return sorted(_CATALOGS)


def get_axis_catalog(name: str) -> list[dict[str, Any]]:
    """Return a deep copy of a catalog so callers cannot mutate module data."""

    try:
        catalog = _CATALOGS[name]
    except KeyError as exc:
        raise LookupError(
            f"Unknown axis catalog {name!r}; "
            f"expected one of {', '.join(list_axis_catalogs())}"
        ) from exc

    return [
        {
            "label": entry["label"],
            "patch": _deep_copy_patch(entry["patch"]),
            "tags": list(entry.get("tags", [])),
        }
        for entry in catalog
    ]


def _deep_copy_patch(patch: dict[str, Any]) -> dict[str, Any]:
    copied: dict[str, Any] = {}
    for key, value in patch.items():
        if isinstance(value, dict):
            copied[key] = dict(value)
        elif isinstance(value, list):
            copied[key] = list(value)
        else:
            copied[key] = value
    return copied


__all__ = [
    "CAMERA_ANGLE",
    "COLOR_SCHEME",
    "LOGO_STRUCTURES",
    "THUMBNAIL_STRUCTURES",
    "TONE_AND_MANNER",
    "get_axis_catalog",
    "list_axis_catalogs",
]
