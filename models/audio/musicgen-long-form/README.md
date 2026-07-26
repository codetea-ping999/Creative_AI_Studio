# MusicGen long-form local checkpoint

This optional directory uses the AudioCraft local-export format:

- `state_dict.bin`
- `compression_state_dict.bin`
- `t5-base/` (a complete local `t5-base` Transformers export)

The files are intentionally not tracked. AudioCraft, both checkpoint files,
and the local T5 encoder/tokenizer must be available before
`musicgen-long-form` is advertised as ready by `GET /models`. The loader forces
Hugging Face/Transformers offline mode, points the text conditioner to this
local T5 export, and uses AudioCraft's PyTorch attention backend when xformers
is unavailable.
