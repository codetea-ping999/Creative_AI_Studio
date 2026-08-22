"""Audio post-processing shared by the music and narration generators."""

from .postprocess import (
    MUSIC_PRESET,
    SPEECH_PRESET,
    apply_fades,
    duck,
    duck_envelope,
    normalize_peak,
    normalize_rms,
    process_audio,
    skipped_processing_report,
    trim_silence,
)

__all__ = [
    "MUSIC_PRESET",
    "SPEECH_PRESET",
    "apply_fades",
    "duck",
    "duck_envelope",
    "normalize_peak",
    "normalize_rms",
    "process_audio",
    "skipped_processing_report",
    "trim_silence",
]
