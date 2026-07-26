# Template Writer

Weight-free text runtime used as the default for `media_type: text`.

This directory holds no model weights. It exists so the `template-writer`
manifest has a real `local_path`, the same way `models/video/procedural/` backs
the storyboard video runtime. The runtime itself lives in
`core/models/text_runtimes.py` (`build_template_runtime`).

## What it does

Given a story task's brief and JSON schema, it synthesizes a schema-valid
document deterministically: the same prompt and seed always produce the same
output. It does not understand language — it fills the requested structure with
phrasing derived from the brief.

## Why it exists

- The story → storyboard → assembly path stays runnable and testable before any
  language model has been downloaded, and in CI where downloads are unavailable.
- A user with no GGUF placed still gets a usable skeleton to edit rather than an
  error.

## Moving to a real model

Place a GGUF file under `models/text/<model>/`, then enable a
`llama_cpp_text_loader` manifest (see
`models/manifests/text/qwen-writer-local.json`). Nothing in the generator
changes: both runtimes expose the same `generate` contract.
