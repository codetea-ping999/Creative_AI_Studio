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
    "narration": "{subject}は{mood}の気配を感じながら、次の一歩を選んだ。",
    "image_prompt": "{subject}, {mood}, cinematic composition, detailed lighting",
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
            return value[:48]
    return _DEFAULT_SUBJECT


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
    if template is not None:
        return template.format(subject=subject, mood=mood, index=index)

    # Unknown field: emit something traceable rather than an empty string, which
    # the quality evaluator would (correctly) flag as an incomplete payload.
    digest = hashlib.sha1(f"{rng_key}:{field_name}:{index}".encode("utf-8")).hexdigest()
    return f"{field_name.replace('_', ' ')} {index} [{digest[:6]}]"


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
