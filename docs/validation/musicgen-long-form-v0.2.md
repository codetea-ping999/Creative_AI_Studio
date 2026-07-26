# MusicGen long-form v0.2 validation

Issue: [#28](https://github.com/codetea-ping999/Creative_AI_Studio/issues/28)

Date: 2026-07-26

## Result

The optional `musicgen-long-form` path loads the local AudioCraft MusicGen Small
checkpoint and a local `t5-base` text conditioner without network access.
AudioCraft's PyTorch attention backend is used when xformers is unavailable.

The implementation keeps the existing Transformers MusicGen path at 2–30
seconds and exposes long-form duration (31–120 seconds) and extend stride
(5–29 seconds, default 18) only when the selected manifest has the `long-form`
tag.

## Environment

| Item | Value |
| --- | --- |
| Host | MacBook Pro (Apple M1 Max, 64 GB) |
| Python | 3.14.4 |
| PyTorch | 2.10.0 |
| AudioCraft | 1.3.0 |
| Transformers | 4.57.6 |
| Checkpoint | local `facebook/musicgen-small` AudioCraft export |
| Text conditioner | local `google-t5/t5-base` |
| Network access during generation | disabled |

## Real-model matrix

The ignored local WAVs and raw runtime logs are not committed. The final timing,
duration, segment count, and output paths are recorded here for both required
real-model cases.

| Requested duration | Stride | Device | Actual WAV | Segments | Elapsed | Result |
| --- | --- | --- | --- | --- | --- | --- |
| 45 seconds | 10 seconds | MPS | 45.0 seconds, mono, 32 kHz | 3 | 811.52 seconds | succeeded, `stub=false` |
| 120 seconds | omitted (default 18 seconds) | CPU | 120.0 seconds, mono, 32 kHz | 6 | 1534.21 seconds | succeeded, `stub=false` |

The 120-second run emitted progress at all six segment boundaries. No WAV was
present before the final segment completed; the completed file is
`outputs/issue28-real-validation/aud_b1101bdf0312403187779cd6e4350e4e.wav`.

## Lifecycle validation

Automated tests cover:

- 45-second generation with stride 10, segment progress, metadata, Gallery, and export;
- default stride 18 and bounds for duration/stride;
- rejection before job creation for invalid long-form requests;
- preservation of the 30-second limit for non-long-form MusicGen;
- segment-two failure with no WAV or Gallery asset;
- cancellation at a segment boundary with no partial WAV or Gallery asset;
- dependency, checkpoint, and local T5 readiness reasons in `/models`;
- UI visibility, model switching, payload serialization, restored drafts, tests, and build.

The real runtime was also loaded independently and produced a non-silent
two-second waveform before the long-form matrix was started.
