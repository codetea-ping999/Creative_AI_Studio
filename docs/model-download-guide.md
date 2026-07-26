# Model Download Guide

このドキュメントは、ローカルモデルの配置方法と manifest の書き方を整理するものです。

重要:

- `diffusers_image_loader` は実ランタイムをロードします。
- 生成に使うにはモデル一式が `local_path` に揃っている必要があります。
- `GET /models` 自体は manifest を読むだけで、モデル本体はロードしません。

## 現在の標準構成

```text
models/
├─ audio/
│  └─ musicgen-small/
├─ image/
│  ├─ sdxl/
│  └─ anime-sdxl/
├─ video/
│  ├─ procedural/
│  ├─ learned-runtime/
│  └─ cogvideox-2b/
├─ loras/
│  └─ mai.safetensors
└─ manifests/
   ├─ audio/
   │  └─ musicgen-small.json
   ├─ image/
   │  ├─ sdxl-local.json
   │  └─ anime-sdxl-local.json
   └─ video/
      └─ storyboard-local.json
```

既定 image manifest は [sdxl-local.json](/Users/toyoharukohyama/Documents/Creative_AI_Studio/models/manifests/image/sdxl-local.json) です。
既定 audio manifest は [musicgen-small.json](/Users/toyoharukohyama/Documents/Creative_AI_Studio/models/manifests/audio/musicgen-small.json) です。
アニメ向け checkpoint 用 manifest は [anime-sdxl-local.json](/Users/toyoharukohyama/Documents/Creative_AI_Studio/models/manifests/image/anime-sdxl-local.json) です。
既定 video manifest は [storyboard-local.json](/Users/toyoharukohyama/Documents/Creative_AI_Studio/models/manifests/video/storyboard-local.json) です。

## ダウンロード先

標準では SDXL のローカル配置先を `./models/image/sdxl` としています。
MusicGen Small は `./models/audio/musicgen-small` を想定しています。
アニメ向け checkpoint は `./models/image/anime-sdxl`、LoRA は `./models/loras/...` を想定しています。
Storyboard video runtime は `./models/video/procedural` を想定しています。
CogVideoX-2B weightは `./models/video/cogvideox-2b` を想定しています。

## ダウンロード方法

### Hugging Face CLI を使う例

事前に Hugging Face へログインします。

```bash
pip install "huggingface_hub[cli]"
huggingface-cli login
```

モデルを配置します。

```bash
huggingface-cli download stabilityai/stable-diffusion-xl-base-1.0 \
  --local-dir ./models/image/sdxl \
  --local-dir-use-symlinks False
```

### Git LFS を使う例

```bash
git lfs install
git clone https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0 ./models/image/sdxl
```

### MusicGen Small を配置する例

```bash
huggingface-cli download facebook/musicgen-small \
  --local-dir ./models/audio/musicgen-small \
  --local-dir-use-symlinks False
```

### Storyboard video runtime

動画は現時点では heavyweight checkpoint を必須にしていません。
`storyboard-video` は `./models/video/procedural` が存在すれば利用可能で、
ローカルで storyboard gif を生成します。

### CogVideoX-2B learned video runtime

CogVideoXはoptionalです。Diffusers形式のweightを次の場所へ配置します。

```bash
huggingface-cli download THUDM/CogVideoX-2b \
  --local-dir ./models/video/cogvideox-2b \
  --local-dir-use-symlinks False
```

`model_index.json`はリポジトリに含まれますが、それだけではavailableになりません。
`model_index.json`が列挙するcomponent (`scheduler`、`text_encoder`、`tokenizer`、
`transformer`、`vae`) の設定と各weightが揃うと`GET /models?media_type=video`が
`runtime_status=ready`、`is_available=true`を返します。
不足時は`availability_message`が不足ファイルを列挙します。生成確認は明示的に実行します。

```bash
make cogvideox-smoke
```

標準条件は720x480、49 frames、8 fps、20 steps、MP4です。MPSを優先し、
未対応operationが発生した場合だけ同じpipelineをCPUへ移して1回再試行します。
CogVideoX の推論中 step callback はまだ接続されていないため、running job の cancel は
生成処理の前後の境界で反映されます。Diffusers image job は step 単位の協調 cancel に
対応しています。

### Semantic judge model

semantic judge は生成モデルとは別の評価用 model です。
既定では image / video frame に CLIP、audio に CLAP を使います。
`QUALITY_SEMANTIC_LOCAL_ONLY=true` の運用では、事前に local path へ配置してから path override を設定してください。

```bash
huggingface-cli download openai/clip-vit-base-patch32 \
  --local-dir ./models/judges/clip-vit-base-patch32 \
  --local-dir-use-symlinks False

huggingface-cli download laion/clap-htsat-unfused \
  --local-dir ./models/judges/clap-htsat-unfused \
  --local-dir-use-symlinks False
```

`.env` の例:

```dotenv
QUALITY_ENABLE_SEMANTIC_JUDGE=true
QUALITY_SEMANTIC_LOCAL_ONLY=true
QUALITY_SEMANTIC_CACHE_DIR=./data/semantic-cache
QUALITY_SEMANTIC_IMAGE_MODEL_PATH=./models/judges/clip-vit-base-patch32
QUALITY_SEMANTIC_AUDIO_MODEL_PATH=./models/judges/clap-htsat-unfused
QUALITY_SEMANTIC_VIDEO_MODEL_PATH=./models/judges/clip-vit-base-patch32
QUALITY_SEMANTIC_VIDEO_BACKEND=image_frames
QUALITY_SEMANTIC_VIDEO_SAMPLE_FRAMES=3
```

video は現状 `image_frames` backend です。gif から数 frame を抽出し、image judge の平均として semantic alignment を算出します。

## manifest の考え方

manifest は「モデルの宣言情報」です。実体ファイルの場所と API 公開 ID を分けて管理します。

```json
{
  "id": "sdxl-local",
  "public_id": "sdxl",
  "display_name": "SDXL Local",
  "media_type": "image",
  "task_type": "text-to-image",
  "provider": "local",
  "runtime": "diffusers",
  "local_path": "./models/image/sdxl",
  "loader": "diffusers_image_loader",
  "dtype": "float16",
  "default_params": {
    "width": 1024,
    "height": 1024,
    "steps": 30,
    "guidance_scale": 7.5
  },
  "aliases": ["sdxl-local"],
  "tags": ["image", "base"],
  "is_default": true,
  "enabled": true
}
```

## 別モデルを追加する流れ

1. モデル本体を `models/image/<model-name>` へ配置する
2. `models/manifests/image/<model-name>.json` を追加する
3. `public_id` を API/UI で使う ID にする
4. `is_default` を切り替える
5. `curl http://127.0.0.1:8000/models` で認識を確認する

audio 用の確認:

```bash
curl "http://127.0.0.1:8000/models?media_type=audio"
```

video 用の確認:

```bash
curl "http://127.0.0.1:8000/models?media_type=video"
```

## アニメ checkpoint と LoRA

推奨フロー:

1. checkpoint を `models/image/anime-sdxl` に配置する
2. LoRA を `models/loras/<name>.safetensors` か `models/loras/<name>/` に置く
3. Web UI で `Model` を選ぶ
4. 必要な場合だけ `LoRA Path` と `LoRA Scale` を入れる
5. prompt は長文説明より、髪色・目の色・髪飾り・衣装のような特徴語に寄せる

API 例:

```json
{
  "prompt": "anime style, Sakurajima Mai, solo, long straight black hair, purple eyes, small bunny hair clip, beige cardigan, red necktie, school hallway",
  "negative_prompt": "bad anatomy, red eyes, blue hair, missing hair clip, text, watermark",
  "model_id": "anime-sdxl",
  "params": {
    "width": 1024,
    "height": 1024,
    "steps": 28,
    "guidance_scale": 6.5,
    "lora_path": "./models/loras/mai.safetensors",
    "lora_scale": 0.8
  }
}
```

## 確認ポイント

### manifest だけ確認

```bash
curl http://127.0.0.1:8000/models
```

### loader 側のパス解決を含めて確認

```bash
python3 -m unittest tests.test_model_system
```

## 現状の制約

- LoRA は現状 1 リクエストにつき 1 本を想定しています
- 未配置の checkpoint や MusicGen モデルは `/models` には出ますが `is_available: false` になります
- `model_index.json`や`config.json`だけを置いた状態も`is_available: false`です。weight本体が揃って初めて`ready`になります
- `storyboard-video` は procedural runtime なので追加ダウンロード不要です
- `learned-video` はadapterと`model_index.json`だけではavailableにならず、CogVideoX-2Bのcomponent設定とweight一式が必要です
- 判定ルールは `core/model_readiness.py` に集約され、`/models`・loader・`scripts/check_local_setup.py`・`make cogvideox-smoke` が同じ結果を返します
- モデルダウンロード管理 UI はまだありません
- semantic judge model は生成 model とは別管理で、初回 scoring 時に必要です

次の実装候補は [next-tasks.md](/Users/toyoharukohyama/Documents/Creative_AI_Studio/docs/next-tasks.md) にまとめています。
