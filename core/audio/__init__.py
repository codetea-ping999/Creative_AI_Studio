"""Audio post-processing shared by the music and narration generators."""

from .postprocess import (
    MUSIC_PRESET,
    SPEECH_PRESET,
    apply_fades,
    duck,
    normalize_peak,
    normalize_rms,
    process_audio,
    trim_silence,
)

__all__ = [
    "MUSIC_PRESET",
    "SPEECH_PRESET",
    "apply_fades",
    "duck",
    "normalize_peak",
    "normalize_rms",
    "process_audio",
    "trim_silence",
]
