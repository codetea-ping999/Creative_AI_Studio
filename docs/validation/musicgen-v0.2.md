# MusicGen v0.2 real-model validation

Issue: [#26](https://github.com/codetea-ping999/Creative_AI_Studio/issues/26)

Date: 2026-07-26

## Result

The local `musicgen-small` and `musicgen-medium` weights completed the required
2, 8, and 30 second generation matrix. The generated WAV files were playable,
the same-seed run was bit-identical, a different seed changed the output, and
the complete Generate → Stage → Gallery → Feedback → Composer → Reuse → Export
flow succeeded.

Weights, WAV files, the validation database, and raw JSON reports remain ignored
local artifacts. They are not part of the repository or pull request.

## Environment

| Item | Value |
| --- | --- |
| Host | MacBook Pro (Apple M1 Max, 10 cores, 64 GB) |
| OS | macOS 26.5.2, arm64 |
| Python | 3.14.4 |
| PyTorch | 2.10.0 |
| Transformers | 4.57.6 |
| Device | CPU |
| dtype | `float32` |
| CUDA available | no |
| MPS available | yes, but not used for this matrix |
| Model cache | one runtime |

All reproducibility comparisons used the same weight files, host, device, dtype,
prompt, package versions, and generation parameters.

## Generation matrix

The timings below are direct `scripts/smoke_musicgen.py` measurements. Peak RSS
is the maximum resident set size of the validation process.

| Model | Requested | WAV duration | Elapsed | Peak RSS | Quality proxy | SHA-256 prefix |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `musicgen-small` | 2 s | 1.94 s | 15.354 s | 2,829.5 MB | 66.4 | `8832d53efab3` |
| `musicgen-small` | 8 s | 7.94 s | 61.433 s | 3,862.4 MB | 73.9 | `55c62429791d` |
| `musicgen-small` | 30 s | 29.94 s | 248.456 s | 7,593.1 MB | 81.8 | `caf5f6a6689d` |
| `musicgen-medium` | 2 s | 1.94 s | 53.528 s | 8,408.5 MB | 53.8 | `8838d7f34d45` |
| `musicgen-medium` | 8 s | 7.94 s | 202.665 s | 9,696.7 MB | 67.3 | `301df71cb2d5` |
| `musicgen-medium` | 30 s | 29.94 s | 856.039 s | 13,039.2 MB | 78.8 | `01f6a83e6dea` |

The quality value is the repository's local playback-oriented heuristic. It
does not claim musicality or prompt-alignment scoring.

All six cases were also generated through the live API and registered in the
Gallery:

| Model | Requested | Job | Gallery asset |
| --- | ---: | --- | --- |
| `musicgen-small` | 2 s | `job_0fb89aeab4a444509edfbff52b2bcfe7` | `asset_ca4b779ba29de751b11dfd25` |
| `musicgen-small` | 8 s | `job_8db9b2d982754c0f8779620d729b42f4` | `asset_e02972fde8e77beee5c992f3` |
| `musicgen-small` | 30 s | `job_6b69748240904ae19fe74e8f7a7cf23d` | `asset_ad234fc38ab0d161a85229c6` |
| `musicgen-medium` | 2 s | `job_58d329715b4547daa4682d9f3b9e48d6` | `asset_ef686811770eeb3a3b3b3423` |
| `musicgen-medium` | 8 s | `job_e01dafbb690d4bb6a8840e0062c7dfe4` | `asset_b6fe69e9d13b85095b644b0c` |
| `musicgen-medium` | 30 s | `job_97ca5c7a9066439b9201dcb4efea8ef2` | `asset_e7be2382dba7bec8a3419232` |

The API-generated SHA-256 values matched the direct smoke matrix for every
case, providing an additional end-to-end reproducibility check.

## Reproducibility

Using `musicgen-small`, a two-second request, and seed `260726`:

| Run | Elapsed | Peak RSS | SHA-256 prefix |
| --- | ---: | ---: | --- |
| First | 15.354 s | 2,829.5 MB | `8832d53efab3` |
| Same seed | 14.445 s | 2,839.4 MB | `8832d53efab3` |
| Seed `260727` | 15.313 s | 2,840.6 MB | `80acd0032c26` |

The same-seed WAV bytes were identical. Changing only the seed changed the WAV
bytes.

The test exposed two runtime compatibility problems that are covered by
regression tests:

1. Transformers 4.57 rejects a `generator` keyword in MusicGen `generate()`.
   Generation now scopes and restores the device RNG state instead.
2. Passing `top_p=0.0` enabled an unintended degenerate sampling path and made
   different seeds produce identical output. Disabled sampling controls are now
   omitted from the Transformers call.

## Parameter effects

Each row changed only the named value from the same `musicgen-small`, two-second,
seed `260726` baseline. All nine WAV hashes changed.

| Parameter | Variant | SHA-256 prefix |
| --- | --- | --- |
| mood | `energetic` | `64f19efe0cc4` |
| bpm | `140` | `2f3fe2a4c782` |
| genre | `jazz` | `4e01d0cb9647` |
| instruments | `saxophone, upright bass` | `4328eb453814` |
| structure | `verse-chorus` | `65dcca98c94d` |
| guidance scale | `5.0` | `e732e988bb3e` |
| temperature | `0.7` | `f31109dc8db3` |
| top K | `100` | `2904a7e87033` |
| top P | `0.8` | `60a7bd31060` |

This establishes that every supported control reaches and changes the real-model
generation path. A changed hash is not, by itself, a claim about the perceptual
strength or desirability of the change.

## Golden Path

The following operations completed against the live local API and web app:

- Generate created a successful `musicgen-small` job and Gallery asset.
- Stage loaded the actual WAV metadata and played the clip through the native
  audio control.
- Feedback was saved with quality 4, semantic 5, creative 4, and reuse/export
  signals.
- Composer restored the original prompt, seed, model, duration, BPM, mood,
  genre, instruments, structure, and sampling controls.
- Variation inherited seed `260726` and recorded parent/source lineage.
- Rerun issued the random seed `6965605403456465284`.
- An explicit-seed rerun used `260726` and reproduced the original WAV hash.
- Export wrote both the WAV and its metadata sidecar.

Primary evidence:

| Item | Identifier |
| --- | --- |
| Initial job | `job_0fb89aeab4a444509edfbff52b2bcfe7` |
| Initial asset | `asset_ca4b779ba29de751b11dfd25` |
| Variation job | `job_0438ea1dd9104fca98778206122a9726` |
| Random rerun job | `job_74b8652eca824f92a9e968fcf3ea9812` |
| Explicit-seed rerun job | `job_359b6678f56145bc9a40e36ea53b8790` |
| Export | `outputs/issue26/exports/audio/issue26/musicgen-golden-path.wav` |

Two UI defects found during the Golden Path are fixed and covered:

- arbitrary stored mood/genre/structure values remain visible as restored select
  options instead of appearing to fall back to the first preset;
- nested local `OUTPUT_DIR` paths are normalized to the API static mount, so
  Stage requests the playable `/outputs/audio/<file>.wav` URL.

## Smoke target

With weights installed:

```text
make musicgen-smoke
[OK] musicgen-small 2s seed=260726 repeat=1: 14.91s, 2815.6 MiB RSS, …
```

The real smoke run completed in 14.91 seconds with approximately 2,815.6 MiB
peak RSS. Repeating the command after temporarily removing only the local
`musicgen-small/config.json` link printed the missing-file reason, skipped, and
returned exit code 0. The link was restored immediately afterward.

For the full local matrix and parameter sweep:

```bash
python scripts/smoke_musicgen.py \
  --model musicgen-small --model musicgen-medium \
  --duration 2 --duration 8 --duration 30 \
  --report artifacts/musicgen-duration-matrix.json

python scripts/smoke_musicgen.py \
  --model musicgen-small --duration 2 \
  --repeat 2 --compare-seed 260727 \
  --report artifacts/musicgen-reproducibility.json

python scripts/smoke_musicgen.py \
  --model musicgen-small --duration 2 --parameter-sweep \
  --report artifacts/musicgen-parameter-sweep.json
```

The report path is intentionally under the ignored `artifacts/` directory.

## Failure record

No model load, inference, WAV write, Gallery registration, playback, reuse, or
export failures remained after the compatibility fixes above. CPU inference was
slow for `musicgen-medium` (approximately 14.3 minutes for 30 seconds), which is
the main operational constraint observed.
