# Sakurajima Mai SDXL Test Prompt

Creative AI Studio のローカル SDXL 動作確認向けに整理した実行用プロンプトです。

## Basic Test

### Prompt

```text
anime style, Sakurajima Mai, long black hair, purple eyes, blunt bangs, hair bunny clip, school uniform, beige cardigan, red tie, grey skirt, black pantyhose, standing in a school hallway, soft window light, detailed face, clean line art, high detail
```

### Negative Prompt

```text
(worst quality, low quality:1.4), bad anatomy, deformed face, extra fingers, extra limbs, text, watermark, logo, monochrome, blurry, blue hair
```

### Recommended Params

```json
{
  "width": 1024,
  "height": 1024,
  "steps": 24,
  "guidance_scale": 7.0
}
```

## Fast Smoke Test

初回確認を速くしたい場合はこちらを使います。

### Prompt

```text
anime style, Sakurajima Mai, long black hair, purple eyes, hair bunny clip, school uniform, standing in a school hallway, soft light
```

### Negative Prompt

```text
low quality, bad anatomy, extra fingers, text, watermark, blue hair
```

### Recommended Params

```json
{
  "width": 512,
  "height": 512,
  "steps": 8,
  "guidance_scale": 6.0
}
```

## Suggested Variants

### Bunny Suit

```text
anime style, Sakurajima Mai, black bunny suit, bunny ears, white collar, black bow tie, fishnet pantyhose, high heels, embarrassed face, indoor studio lighting, detailed face
```

### Shichirigahama Sunset

```text
anime style, Sakurajima Mai, long black hair, purple eyes, long coat, walking on a sandy beach, sunset, golden hour, orange sky, waves hitting the shore, cinematic lighting, nostalgic mood
```

## Notes

- このアプリでは `model_id` に `sdxl` を指定します。
- Apple Silicon では MPS 実行時に `float32` を使うため、初回生成は少し重めです。
- まずは Fast Smoke Test で出力確認し、その後 Basic Test を回すのが安全です。
