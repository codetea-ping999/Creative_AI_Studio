# MusicGen Melody validation

Issue #27 adds Gallery-backed melody conditioning through the local
Transformers `MusicgenMelodyForConditionalGeneration` and
`MusicgenMelodyProcessor` runtime.

## Reference contract

- Input must be a registered Gallery WAV asset.
- PCM mono and stereo WAV files are accepted.
- References must be between 1 and 30 seconds, inclusive.
- Audio is mixed to mono and resampled to the model sampling rate before the
  processor produces `input_features`.
- The reference path is resolved from the asset registry. Client-supplied paths
  are not opened.

The 30-second upper bound remains aligned with the short-form audio workflow.
An offline real-model smoke used 1, 4, and 30-second references with a 2-second
requested output. All three succeeded and produced 1.94-second WAV files. The
prepared reference sample counts were 32,000, 128,000, and 960,000 at 32 kHz,
respectively. This confirms that reference duration does not override requested
output duration and that the inclusive 30-second boundary is accepted.

## Verification

Automated coverage includes:

- non-WAV, empty, shorter than 1 second, longer than 30 seconds, and three-channel
  rejection;
- mono conversion and 8 kHz to 32 kHz resampling;
- job-free API rejection for a non-melody model or invalid reference;
- `input_features`, conditioning metadata, and Gallery parent lineage;
- existing variation and rerun regression tests.

The real-model smoke ran with `HF_HUB_OFFLINE=1` and
`TRANSFORMERS_OFFLINE=1`. It registered the generated WAV in the Gallery with
the source asset as its parent.

Listening validation remains a human review step and should be recorded on
Issue #27 using a generated non-silent reference/output pair.
