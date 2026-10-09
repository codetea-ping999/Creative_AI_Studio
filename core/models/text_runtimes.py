"""Text generation runtimes normalized behind one calling convention.

Every text backend exposes the same ``generate`` callable so ``TextGenerator``
never branches on which backend is loaded:

    generate(prompt, *, system=None, max_tokens=..., temperature=..., top_p=...,
             seed=None, json_schema=None) -> str

Three backends are provided: a dependency-free deterministic scaffolder that
keeps the whole story pipeline runnable with no weights placed, local GGUF
inference through llama.cpp, and any OpenAI-compatible endpoint (Ollama, LM
Studio, vLLM) behind a loopback egress guard.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable
from urllib.parse import urlparse

BRIEF_HEADING = "### BRIEF"

# Hosts that are unambiguously this machine. Anything else is an egress and needs
# an explicit opt-in, because the project's default posture is local-only.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})

TextGenerateCallable = Callable[..., str]


# --------------------------------------------------------------------------
# Template runtime
# --------------------------------------------------------------------------

# Phrasing for property names the story tasks care about. Without these the
# scaffolder would emit "heading: value" placeholders, which are schema-valid but
# useless for checking that the pipeline actually produces something coherent.
_FIELD_PHRASES: dict[str, str] = {
    "heading": "{subject} — 場面{index}",
    "summary": "{subject}が{mood}の中で状況を進める場面{index}。",
    "image_negative": "blurry, low quality, distorted anatomy, watermark",
    "bgm_mood": "{mood}",
    "camera": "ken_burns_in",
    "text": "{subject}が{mood}に導かれて動き出す物語。",
    "hook": "{subject}の選択が結末を変える",
    "tone": "{mood}",
    "act": "第{index}幕",
    "purpose": "場面{index}の役割を果たす",
    "title": "{subject}",
    "prose_markdown": (
        "{subject}は静かに息を吸った。\n\n"
        "{mood}の空気が周囲を満たし、輪郭だけが浮かび上がる。\n\n"
        "そして{subject}は、次の一歩を選んだ。"
    ),
    "speaker": "{subject}",
    "direction": "落ち着いた声で",
    "label": "variation-{index}",
    "prompt": "{subject}, {mood}, variation {index}, detailed",
    "negative_prompt": "blurry, low quality, watermark",
    "name": "{subject}",
    "prompt_fragment": "{subject}, {mood}, consistent character design",
    "negative_fragment": "inconsistent features, extra limbs",
}

_DEFAULT_SUBJECT = "主人公"
_DEFAULT_MOOD = "静かな緊張"

# Long enough that an ordinary one- or two-sentence premise survives whole; a
# longer one is clipped at a phrase boundary by ``_clip_at_boundary``.
_SUBJECT_MAX_CHARS = 120
# Tried in order: a whole sentence reads better than a clause, which reads
# better than a word boundary.
_BOUNDARY_TIERS: tuple[tuple[str, ...], ...] = (
    ("。", "．", "！", "？", ". ", "! ", "? "),
    ("、", "，", "; ", ", "),
)

# Narration per story-arc role, parallel to ``_SCENE_ARC_CUES``, so the spoken
# track of a template story does not repeat one sentence for every scene.
_SCENE_ARC_NARRATION: tuple[str, ...] = (
    "{subject}。{mood}の中、物語が静かに幕を開ける。",
    "{anchor}。その瞬間、{mood}の空気が一変した。もう後戻りはできない。",
    "{anchor}。{mood}の気配が濃くなり、行く手を阻むものがひとつ、またひとつと迫ってくる。",
    "{anchor}。ついに決断の時が訪れる。{mood}の中、すべてはこの一瞬にかかっていた。",
    "{anchor}。やがて静けさが戻り、{mood}の余韻だけが残った。",
)

# Story-arc roles a scene can play, in order, each with the visual cues a
# storyboard frame for that role would carry (issue #450). ``image_prompt`` is
# the one scene field the studio sends verbatim to image/video generation, so a
# prompt built from subject and mood alone gave every scene the same picture.
# Lighting cues deliberately name no time of day or weather, which would
# contradict a premise such as "a stormy night".
_SCENE_ARC_CUES: tuple[str, ...] = (
    "establishing scene, calm before the story begins, soft ambient light",
    "inciting incident, the moment everything changes, dramatic side light",
    "rising tension, obstacles close in, low-key moody light",
    "climax, the decisive moment, intense high-contrast light",
    "resolution, quiet aftermath, gentle diffused light",
)

# A one-scene story has no later scenes to carry the turn, climax or outcome,
# so its single frame and narration stand for the whole arc instead of only
# the opening.
_SUBJECT_ANCHOR_MAX_CHARS = 24
_SINGLE_SCENE_CUE = "key moment of the whole story, the decisive action, dramatic light"
_SINGLE_SCENE_NARRATION = "{subject}。{mood}の中、物語は決定的な瞬間を迎える。"

# Shot types cycle independently of the arc role so that stories with more
# scenes than roles still get a distinct frame per scene. Seven entries exceed
# the widest run of scenes that can share one role at the 24-scene maximum.
_SCENE_SHOTS: tuple[str, ...] = (
    "wide establishing shot",
    "medium shot",
    "close-up",
    "low-angle shot",
    "over-the-shoulder shot",
    "high-angle shot",
    "extreme close-up",
)


def build_template_runtime(*, seed_salt: str = "") -> TextGenerateCallable:
    """Return a deterministic, dependency-free ``generate`` callable.

    The output is derived only from the prompt text, the requested schema, and the
    seed, so the same request always yields the same document. This is what makes
    the story flow testable and gives a user with no model placed a usable
    skeleton rather than an error.
    """

    def generate(
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.8,
        top_p: float = 0.95,
        seed: int | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        context = parse_brief(prompt)
        rng_key = f"{seed_salt}:{seed}:{prompt}"
        if json_schema is None:
            return _render_plain_text(context)
        payload = _synthesize_from_schema(
            json_schema, context, rng_key, root=json_schema
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    return generate


def parse_brief(prompt: str) -> dict[str, str]:
    """Extract the ``key: value`` brief block a task prompt embeds.

    The template runtime has no language model to infer intent from prose, so the
    task prompts state their inputs in a machine-readable block and this parser
    reads it back.
    """

    context: dict[str, str] = {}
    in_brief = False
    for line in prompt.splitlines():
        stripped = line.strip()
        if stripped.startswith(BRIEF_HEADING):
            in_brief = True
            continue
        if not in_brief:
            continue
        if stripped.startswith("###"):
            break
        if not stripped or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        normalized_key = key.strip().lstrip("-").strip().lower().replace(" ", "_")
        if normalized_key:
            context[normalized_key] = value.strip()
    return context


def _render_plain_text(context: dict[str, str]) -> str:
    subject = _subject(context)
    mood = _mood(context)
    return (
        f"# {subject}\n\n"
        f"{subject}は{mood}のなかで立ち止まった。\n\n"
        f"やがて息を整え、次の一歩を選んだ。\n"
    )


def _subject(context: dict[str, str]) -> str:
    for key in ("subject", "premise", "logline", "brief", "title", "scene"):
        value = context.get(key, "").strip()
        if value:
            return _clip_at_boundary(value, _SUBJECT_MAX_CHARS)
    return _DEFAULT_SUBJECT


def _clip_at_boundary(value: str, limit: int) -> str:
    """Shorten ``value`` to at most ``limit`` chars without cutting mid-phrase.

    A hard slice turned a long premise into a logline that stopped mid-word
    ("...meets a lost c"). Prefer the last sentence end, then the last clause
    boundary, then the last word boundary, and only hard-cut text with none of
    them (marked with "…").
    """

    if len(value) <= limit:
        return value
    window = value[:limit]
    floor = limit // 3
    for marks in _BOUNDARY_TIERS:
        boundary = max(window.rfind(mark) for mark in marks)
        if boundary >= floor:
            return window[:boundary].rstrip()
    space = window.rfind(" ")
    if space >= floor:
        return window[:space].rstrip(" ,;:")
    return window[: limit - 1] + "…"


def _subject_anchor(subject: str) -> str:
    """A short lead phrase of the subject for scenes after the opening.

    Later narrations need something story-specific even when no mood/tone was
    given, without repeating the whole (up to 120-char) subject every scene.
    """

    lead = subject
    for mark in ("。", "、", "，", "．", ". ", ", ", "; "):
        head = lead.split(mark, 1)[0].strip()
        if head:
            lead = head
    return _clip_at_boundary(lead, _SUBJECT_ANCHOR_MAX_CHARS)


def _mood(context: dict[str, str]) -> str:
    for key in ("mood", "tone", "genre", "bgm_mood", "variation_axis"):
        value = context.get(key, "").strip()
        if value:
            return value[:24]
    return _DEFAULT_MOOD


def _resolve_schema_ref(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """Follow a local ``$ref`` and flatten ``anyOf`` into a concrete subschema.

    Pydantic emits nested models as ``$ref`` into ``$defs`` and optional fields as
    ``anyOf: [T, null]``, so a synthesizer that ignores both would treat every
    nested object as a plain string.
    """

    resolved = schema
    for _ in range(8):  # bounded: a self-referential schema must not loop forever
        reference = resolved.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/"):
            target: Any = root
            for segment in reference.lstrip("#/").split("/"):
                if not isinstance(target, dict):
                    return {}
                target = target.get(segment, {})
            resolved = target if isinstance(target, dict) else {}
            continue

        variants = resolved.get("anyOf") or resolved.get("oneOf")
        if isinstance(variants, list):
            concrete = next(
                (
                    variant
                    for variant in variants
                    if isinstance(variant, dict) and variant.get("type") != "null"
                ),
                None,
            )
            if concrete is None:
                return {"type": "null"}
            resolved = concrete
            continue
        break
    return resolved


def _synthesize_from_schema(
    schema: dict[str, Any],
    context: dict[str, str],
    rng_key: str,
    *,
    root: dict[str, Any],
    field_name: str = "",
    index: int = 1,
) -> Any:
    schema = _resolve_schema_ref(schema, root)
    schema_type = schema.get("type")
    if schema_type is None and "properties" in schema:
        schema_type = "object"
    if schema_type is None and "additionalProperties" in schema:
        # A dict field such as ``attributes: dict[str, str]``.
        return _mapping_value(field_name, context)

    if schema_type == "object":
        properties: dict[str, Any] = schema.get("properties", {})
        if not properties:
            return _mapping_value(field_name, context)
        return {
            name: _synthesize_from_schema(
                subschema,
                context,
                rng_key,
                root=root,
                field_name=name,
                index=index,
            )
            for name, subschema in properties.items()
        }

    if schema_type == "array":
        item_schema = schema.get("items", {"type": "string"})
        count = _resolve_item_count(schema, context)
        return [
            _synthesize_from_schema(
                item_schema,
                context,
                rng_key,
                root=root,
                field_name=field_name,
                index=position + 1,
            )
            for position in range(count)
        ]

    if schema_type == "integer":
        return int(_numeric_default(schema, context, field_name, index))

    if schema_type == "number":
        return float(_numeric_default(schema, context, field_name, index))

    if schema_type == "boolean":
        return False

    if schema_type == "null":
        return None

    return _string_value(field_name, context, index, rng_key)


def _mapping_value(field_name: str, context: dict[str, str]) -> dict[str, str]:
    """Fill an open-ended mapping field such as a character's attributes."""

    if field_name == "attributes":
        return {
            "hair": "long black straight",
            "eyes": "dark brown",
            "outfit": "layered coat",
            "build": "slender",
        }
    return {"note": _subject(context)}


def _resolve_item_count(schema: dict[str, Any], context: dict[str, str]) -> int:
    for key in ("count", "scene_count", "beat_count"):
        raw_value = context.get(key)
        if raw_value:
            try:
                return max(1, min(24, int(float(raw_value))))
            except ValueError:
                continue
    minimum = schema.get("minItems")
    if isinstance(minimum, int) and minimum > 0:
        return min(24, minimum)
    return 3


def _numeric_default(
    schema: dict[str, Any],
    context: dict[str, str],
    field_name: str,
    index: int,
) -> float:
    if "default" in schema and isinstance(schema["default"], (int, float)):
        return float(schema["default"])
    if field_name == "duration_seconds":
        return 4.0
    if field_name == "order":
        return float(index - 1)
    if field_name == "word_count":
        return 0.0
    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)):
        return float(minimum)
    return float(index)


def _string_value(
    field_name: str,
    context: dict[str, str],
    index: int,
    rng_key: str,
) -> str:
    template = _FIELD_PHRASES.get(field_name)
    subject = _subject(context)
    mood = _mood(context)
    if field_name == "image_prompt":
        return _scene_image_prompt(subject, mood, index, _scene_total(context))
    if field_name == "narration":
        total = _scene_total(context)
        if total <= 1:
            return _SINGLE_SCENE_NARRATION.format(subject=subject, mood=mood)
        role = _scene_arc_role(index, total)
        return _SCENE_ARC_NARRATION[role].format(
            subject=subject, mood=mood, anchor=_subject_anchor(subject)
        )
    if template is not None:
        return template.format(subject=subject, mood=mood, index=index)

    # Unknown field: emit something traceable rather than an empty string, which
    # the quality evaluator would (correctly) flag as an incomplete payload.
    digest = hashlib.sha1(f"{rng_key}:{field_name}:{index}".encode("utf-8")).hexdigest()
    return f"{field_name.replace('_', ' ')} {index} [{digest[:6]}]"


def _scene_total(context: dict[str, str]) -> int:
    for key in ("scene_count", "count"):
        raw_value = context.get(key)
        if raw_value:
            try:
                return max(1, int(float(raw_value)))
            except ValueError:
                continue
    return len(_SCENE_ARC_CUES)


def _scene_arc_role(index: int, total: int) -> int:
    """Map a 1-based scene index onto an index into ``_SCENE_ARC_CUES``."""

    if total <= 1:
        return 0
    position = min(max(0, index - 1), total - 1)
    return round(position * (len(_SCENE_ARC_CUES) - 1) / (total - 1))


def _scene_image_prompt(subject: str, mood: str, index: int, total: int) -> str:
    """Compose a scene-specific visual prompt from the scene's place in the arc.

    The scene's position is mapped onto setup -> inciting -> rising -> climax ->
    resolution, and paired with a shot type, so each scene of one story gets a
    different frame while the subject still anchors every prompt. Pure function
    of its inputs, so the same story always yields the same prompts.

    The scene-specific shot and arc cue come first: CLIP-based image encoders
    (SDXL) only read the first 77 tokens, and a long Japanese subject alone
    can exceed that, which would cut off everything that tells scenes apart.
    """

    shot = _SCENE_SHOTS[max(0, index - 1) % len(_SCENE_SHOTS)]
    cue = _SINGLE_SCENE_CUE if total <= 1 else _SCENE_ARC_CUES[_scene_arc_role(index, total)]
    return (
        f"{shot}, {cue}, {subject}, {mood}, "
        "cinematic composition, detailed lighting"
    )


# --------------------------------------------------------------------------
# llama.cpp runtime
# --------------------------------------------------------------------------


def build_llama_cpp_runtime(
    model_path: Path,
    *,
    context_window: int,
    n_gpu_layers: int,
    chat_format: str | None = None,
) -> tuple[TextGenerateCallable, bool]:
    """Load a GGUF model through llama-cpp-python.

    Returns the generate callable and whether grammar-constrained JSON is
    available in the installed version.
    """

    try:
        from llama_cpp import Llama
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "Local GGUF text generation requires llama-cpp-python. "
            "Install it with: pip install llama-cpp-python\n"
            "On Apple silicon build with Metal: "
            "CMAKE_ARGS=\"-DGGML_METAL=on\" pip install --no-cache-dir llama-cpp-python"
        ) from exc

    llama_kwargs: dict[str, Any] = {
        "model_path": str(model_path),
        "n_ctx": context_window,
        "n_gpu_layers": n_gpu_layers,
        "verbose": False,
    }
    if chat_format:
        llama_kwargs["chat_format"] = chat_format
    model = Llama(**llama_kwargs)

    grammar_factory = _resolve_grammar_factory()

    def generate(
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.8,
        top_p: float = 0.95,
        seed: int | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        completion_kwargs: dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
        }
        if seed is not None:
            completion_kwargs["seed"] = seed
        if json_schema is not None:
            if grammar_factory is not None:
                completion_kwargs["grammar"] = grammar_factory(json_schema)
            else:
                # Without grammar support the model is merely asked for JSON; the
                # generator still validates and repairs, so this degrades in
                # quality rather than in correctness.
                completion_kwargs["response_format"] = {"type": "json_object"}

        response = model.create_chat_completion(**completion_kwargs)
        return str(response["choices"][0]["message"]["content"] or "")

    return generate, grammar_factory is not None


def _resolve_grammar_factory() -> Callable[[dict[str, Any]], Any] | None:
    try:
        from llama_cpp.llama_grammar import LlamaGrammar
    except (ModuleNotFoundError, ImportError):  # pragma: no cover - version guard
        return None

    from_json_schema = getattr(LlamaGrammar, "from_json_schema", None)
    if not callable(from_json_schema):  # pragma: no cover - version guard
        return None

    def factory(json_schema: dict[str, Any]) -> Any:
        return from_json_schema(json.dumps(json_schema))

    return factory


# --------------------------------------------------------------------------
# OpenAI-compatible endpoint runtime
# --------------------------------------------------------------------------


def resolve_text_endpoint(base_url: str) -> str:
    """Validate an endpoint against the local-only default and return it.

    Raises ``ValueError`` for a non-loopback host unless
    ``ALLOW_REMOTE_TEXT_ENDPOINTS=true`` is set, so a manifest cannot quietly
    start shipping prompts off the machine.
    """

    normalized = base_url.strip().rstrip("/")
    if not normalized:
        raise ValueError("Text endpoint base URL must not be empty.")

    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(
            f"Text endpoint must use http or https: {base_url!r}"
        )

    host = (parsed.hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        return normalized

    if os.getenv("ALLOW_REMOTE_TEXT_ENDPOINTS", "").strip().lower() == "true":
        return normalized

    raise ValueError(
        f"Refusing to use non-loopback text endpoint {host!r}. "
        "This studio defaults to local-only generation; set "
        "ALLOW_REMOTE_TEXT_ENDPOINTS=true to allow it explicitly."
    )


def build_openai_compatible_runtime(
    base_url: str,
    *,
    model_name: str,
    api_key_env: str | None = None,
    timeout_seconds: float = 300.0,
) -> TextGenerateCallable:
    """Call an OpenAI-compatible chat completions endpoint."""

    try:
        import httpx
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "Endpoint text generation requires httpx. Install it with: pip install httpx"
        ) from exc

    resolved_base_url = resolve_text_endpoint(base_url)
    # The key is read from the environment, never from the manifest, so a manifest
    # committed to the repository can never carry a secret.
    api_key = os.getenv(api_key_env, "") if api_key_env else ""

    def generate(
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.8,
        top_p: float = 0.95,
        seed: int | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
        }
        if seed is not None:
            payload["seed"] = seed
        if json_schema is not None:
            payload["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        response = httpx.post(
            f"{resolved_base_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        return str(body["choices"][0]["message"]["content"] or "")

    return generate


def extract_json_object(raw_text: str) -> dict[str, Any]:
    """Parse the first JSON object in a model response.

    Models wrap JSON in prose or code fences even when told not to, so the parser
    strips fences and then scans for a balanced object rather than requiring the
    whole response to be valid JSON.
    """

    text = raw_text.strip()
    if not text:
        raise ValueError("model returned an empty response")

    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = _scan_for_object(text)

    if not isinstance(parsed, dict):
        raise ValueError(
            f"expected a JSON object, got {type(parsed).__name__}"
        )
    return parsed


def _scan_for_object(text: str) -> Any:
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in model response")

    depth = 0
    in_string = False
    escaped = False
    for position in range(start, len(text)):
        character = text[position]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : position + 1])
    raise ValueError("JSON object in model response is not closed")


__all__ = [
    "BRIEF_HEADING",
    "build_llama_cpp_runtime",
    "build_openai_compatible_runtime",
    "build_template_runtime",
    "extract_json_object",
    "parse_brief",
    "resolve_text_endpoint",
]
