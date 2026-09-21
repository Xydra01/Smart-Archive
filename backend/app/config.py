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
    # Persisted source groups, kept beside the index manifest (separate file).
    groups_file: Path = ARCHIVE_ROOT / "data" / "groups.json"

    # --- Ollama / models ---
    ollama_host: str = "http://localhost:11434"
    # Default LLM: llama3.2:3b — a fast, direct-answering instruct model (~2GB)
    # that runs comfortably on 8GB RAM and doesn't burn tokens on hidden
    # reasoning. Any local Ollama chat model works; swap via ARCHIVE_LLM_MODEL.
    # A few realistic alternatives by hardware:
    #   llama3.2:3b                                   (default; fastest, ~2GB)
    #   qwen3.5:4b                                    (reasoning model, ~3.4GB)
    #   llama3.1:8b / qwen3.5:8b                       (better answers, ~5-6GB)
    #   MobiusDevelopment/Bonsai-27B-Q1_0-gguf:latest (1-bit 27B; wants >8GB to be fast)
    # See the README "Choosing a model" section for a fuller list.
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
    max_selection: int = 10_000  # cap on sources in a single scoped query

    # --- Vision ingest (opt-in; slow, so default off) ---
    # When on, a local vision-language model extracts searchable text from
    # charts, diagrams, figures, and image-only tables during indexing. This is
    # front-loaded work: slow at ingest, free at query time, and skipped for
    # unchanged files by the manifest.
    vision_enabled: bool = False
    vision_model: str = "qwen2.5vl:3b"
    vision_timeout_s: float = 120.0  # per-visual wall-clock timeout
    vision_max_images_per_page: int = 6  # image cap per page
    vision_max_images_per_file: int = 400  # image cap per file
    vision_min_image_pixels: int = 4096  # skip tiny/decorative images (~64x64)
    vision_render_dpi: int = 150  # DPI when rendering pages for figures

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
