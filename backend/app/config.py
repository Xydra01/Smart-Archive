"""Central configuration for the local AI search engine.

All tunables live here so the rest of the app never hard-codes paths or model
names. Values can be overridden with environment variables (prefix ARCHIVE_).
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo layout: this file is backend/app/config.py, so parents[2] == Archive/
ARCHIVE_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARCHIVE_", env_file=".env", extra="ignore"
    )

    # --- Filesystem ---
    archive_root: Path = ARCHIVE_ROOT
    raw_dir: Path = ARCHIVE_ROOT / "data" / "raw"
    chroma_dir: Path = ARCHIVE_ROOT / "data" / "chroma"
    keyword_dir: Path = ARCHIVE_ROOT / "data" / "keyword"

    # --- Ollama / models ---
    ollama_host: str = "http://localhost:11434"
    # Default LLM: llama3.2:3b — a fast, direct-answering instruct model (~2GB)
    # that runs comfortably on 8GB RAM and doesn't burn tokens on hidden
    # reasoning. Alternatives (set ARCHIVE_LLM_MODEL):
    #   qwen3.5:4b                                    (reasoning model, slower)
    #   MobiusDevelopment/Bonsai-27B-Q1_0-gguf:latest (1-bit 27B; needs >8GB to be fast)
    llm_model: str = "llama3.2:3b"
    embed_model: str = "nomic-embed-text"

    # --- Chunking (measured in tokens via tiktoken) ---
    chunk_tokens: int = 512
    chunk_overlap_tokens: int = 64

    # --- Retrieval ---
    collection_name: str = "archive"
    semantic_top_k: int = 20
    keyword_top_k: int = 20
    fusion_top_k: int = 8  # chunks handed to the LLM after fusion
    rrf_k: int = 60  # reciprocal-rank-fusion constant

    # --- Generation (kept modest for 8GB RAM) ---
    llm_num_ctx: int = 4096
    llm_temperature: float = 0.2
    llm_num_predict: int = 512  # cap answer length
    # qwen3.x are reasoning models; disable "thinking" so tokens go to the
    # answer, not an internal chain-of-thought. Harmless for non-thinking models.
    llm_think: bool = False

    def ensure_dirs(self) -> None:
        for d in (self.raw_dir, self.chroma_dir, self.keyword_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
