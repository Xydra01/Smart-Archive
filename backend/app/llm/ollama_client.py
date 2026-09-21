"""Thin wrapper around the Ollama Python client for embeddings and generation.

Centralizes model names, host, and generation options so the rest of the app
talks to a stable interface. Also provides a readiness check so the API can
report clearly when a model hasn't been pulled yet.
"""

from __future__ import annotations

from typing import Iterator

import ollama

from ..config import settings


def _client() -> ollama.Client:
    return ollama.Client(host=settings.ollama_host)


def available_models() -> list[str]:
    """Return the list of locally installed Ollama model names."""
    try:
        resp = _client().list()
    except Exception:
        return []
    models = (
        resp.get("models", [])
        if isinstance(resp, dict)
        else getattr(resp, "models", [])
    )
    names: list[str] = []
    for m in models:
        name = m.get("model") if isinstance(m, dict) else getattr(m, "model", None)
        if name:
            names.append(name)
    return names


def readiness() -> dict:
    """Report whether the configured LLM and embedding models are installed."""
    installed = available_models()

    def _present(target: str) -> bool:
        # Ollama tags may or may not include ":latest"; match loosely.
        base = target.split(":")[0]
        return any(name == target or name.split(":")[0] == base for name in installed)

    return {
        "ollama_reachable": bool(installed) or _ollama_reachable(),
        "llm_model": settings.llm_model,
        "llm_ready": _present(settings.llm_model),
        "embed_model": settings.embed_model,
        "embed_ready": _present(settings.embed_model),
        "vision_enabled": settings.vision_enabled,
        "vision_model": settings.vision_model,
        "vision_ready": _present(settings.vision_model),
        "installed_models": installed,
    }


def _ollama_reachable() -> bool:
    try:
        _client().list()
        return True
    except Exception:
        return False


def model_installed(target: str) -> bool:
    """True if ``target`` (loosely, ignoring an optional :tag) is installed."""
    base = target.split(":")[0]
    return any(
        name == target or name.split(":")[0] == base for name in available_models()
    )


# --------------------------------------------------------------------------
# Vision
# --------------------------------------------------------------------------
def vision_available() -> bool:
    """True when vision ingest is enabled and the configured VLM is installed.

    Checked once at the start of an index job; if False, the job skips visual
    extraction but still indexes text and native tables.
    """
    return settings.vision_enabled and model_installed(settings.vision_model)


def _downscale_image(image_bytes: bytes, max_dim: int) -> bytes:
    """Shrink an image so its longest side is at most ``max_dim`` pixels.

    Full-resolution page scans (multiple megapixels) are slow to process and can
    make the VLM silently return nothing. Vision models read figures fine at
    ~1024px, so we downscale before the call. Returns the original bytes
    unchanged if it's already small enough or if anything goes wrong (best
    effort — never block extraction on a resize failure).
    """
    if max_dim <= 0:
        return image_bytes
    try:
        import io

        from PIL import Image

        im = Image.open(io.BytesIO(image_bytes))
        longest = max(im.width, im.height)
        if longest <= max_dim:
            return image_bytes
        scale = max_dim / float(longest)
        new_size = (max(1, int(im.width * scale)), max(1, int(im.height * scale)))
        im = im.convert("RGB").resize(new_size, Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:
        return image_bytes


def vision_extract(
    image_bytes: bytes, prompt: str, timeout_s: float | None = None
) -> str:
    """Extract text from a single image with the configured VLM.

    The Ollama call runs in a worker thread and is joined with a wall-clock
    timeout so one slow/huge image cannot stall an entire ingest. On timeout a
    TimeoutError is raised for the caller to treat as a per-visual failure.
    """
    import threading

    timeout_s = settings.vision_timeout_s if timeout_s is None else timeout_s
    image_bytes = _downscale_image(image_bytes, settings.vision_max_image_dim)
    result: dict[str, object] = {}

    def _run() -> None:
        try:
            resp = _client().generate(
                model=settings.vision_model,
                prompt=prompt,
                images=[image_bytes],
                # keep_alive holds the model in memory between images so a long
                # ingest doesn't pay repeated reloads. Smaller ctx + capped
                # output make each call faster; vision outputs are short.
                keep_alive=settings.vision_keep_alive,
                options={
                    "temperature": 0.1,
                    "num_ctx": settings.vision_num_ctx,
                    "num_predict": settings.vision_num_predict,
                },
            )
            result["text"] = resp.get("response", "")
        except Exception as e:  # surfaced to the caller below
            result["error"] = e

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        # The daemon thread is abandoned; it will not block process exit.
        raise TimeoutError(f"vision_extract exceeded {timeout_s}s")
    if "error" in result:
        raise result["error"]  # type: ignore[misc]
    return str(result.get("text", ""))


# --------------------------------------------------------------------------
# Embeddings
# --------------------------------------------------------------------------
def embed_texts(texts: list[str], batch_size: int = 64) -> list[list[float]]:
    """Embed texts with the configured embedding model, in batches.

    Batching dramatically speeds up indexing large documents: one Ollama call
    per ``batch_size`` chunks instead of one call per chunk.
    """
    if not texts:
        return []
    client = _client()
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        resp = client.embed(model=settings.embed_model, input=batch)
        vectors.extend(resp["embeddings"])
    return vectors


def embed_query(text: str) -> list[float]:
    return embed_texts([text])[0]


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------
def _gen_options() -> dict:
    return {
        "temperature": settings.llm_temperature,
        "num_ctx": settings.llm_num_ctx,
        "num_predict": settings.llm_num_predict,
    }


def _prep_prompt(prompt: str) -> str:
    """Optionally disable reasoning mode for qwen3.x-style thinking models.

    The ollama 0.4.x Python client has no ``think`` kwarg, so we use the
    in-prompt ``/no_think`` control token, which qwen3.x honors. Harmless for
    models that don't recognize it.
    """
    if not settings.llm_think:
        return f"{prompt}\n/no_think"
    return prompt


def generate(prompt: str, system: str | None = None) -> str:
    """Non-streaming generation, returns the full completion text."""
    resp = _client().generate(
        model=settings.llm_model,
        prompt=_prep_prompt(prompt),
        system=system or "",
        options=_gen_options(),
    )
    return resp.get("response", "")


def generate_stream(prompt: str, system: str | None = None) -> Iterator[str]:
    """Streaming generation, yields text deltas as they arrive.

    Falls back to the model's ``thinking`` stream when it produces no
    ``response`` tokens (some reasoning models route everything into thinking),
    so the UI always shows something useful.
    """
    stream = _client().generate(
        model=settings.llm_model,
        prompt=_prep_prompt(prompt),
        system=system or "",
        stream=True,
        options=_gen_options(),
    )
    emitted_response = False
    thinking_buffer: list[str] = []
    for part in stream:
        delta = part.get("response", "")
        if delta:
            emitted_response = True
            yield delta
        else:
            think = part.get("thinking") or ""
            if think:
                thinking_buffer.append(think)
    # If the model only produced a chain-of-thought, surface it rather than
    # returning an empty answer.
    if not emitted_response and thinking_buffer:
        yield "".join(thinking_buffer)
