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
        "installed_models": installed,
    }


def _ollama_reachable() -> bool:
    try:
        _client().list()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Embeddings
# --------------------------------------------------------------------------
def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts with the configured embedding model."""
    client = _client()
    vectors: list[list[float]] = []
    for text in texts:
        resp = client.embeddings(model=settings.embed_model, prompt=text)
        vectors.append(resp["embedding"])
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
