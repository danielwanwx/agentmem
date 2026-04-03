"""Embedding provider abstraction.

Supports multiple embedding backends:
  - ollama: Local Ollama (qwen3-embedding:8b, 4096-dim) — default
  - openai: OpenAI text-embedding-3-small (1536-dim) — requires OPENAI_API_KEY
  - voyage: Voyage voyage-3-lite (1024-dim) — requires VOYAGE_API_KEY
  - none: Disables vector search entirely (BM25-only)

Usage:
    provider = get_provider()  # reads config
    vec = provider.embed("some text")
    if vec is None:  # provider unavailable or disabled
        # fall back to BM25-only search
"""

from __future__ import annotations

import json
import logging
import os
import struct
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / ".am-memory" / "config.json"

# Rate limiting for cloud providers
_last_call_time: float = 0.0
_MIN_INTERVAL_SECONDS = 1.0  # max ~60 requests/minute


class EmbeddingProvider(ABC):
    """Abstract base class for embedding providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name (e.g. 'ollama', 'openai')."""

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Embedding vector dimensionality."""

    @abstractmethod
    def embed(self, text: str) -> list[float] | None:
        """Embed a single text string. Returns vector or None on failure."""

    def embed_batch(self, texts: list[str]) -> list[list[float]] | None:
        """Embed multiple texts. Default: sequential calls."""
        results = []
        for t in texts:
            vec = self.embed(t)
            if vec is None:
                return None
            results.append(vec)
        return results


class OllamaProvider(EmbeddingProvider):
    """Local Ollama embeddings (qwen3-embedding:8b, 4096-dim)."""

    OLLAMA_URL = "http://localhost:11434/api/embed"
    MODEL = "qwen3-embedding:8b"

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def dimensions(self) -> int:
        return 4096

    def embed(self, text: str) -> list[float] | None:
        try:
            import httpx
            resp = httpx.post(
                self.OLLAMA_URL,
                json={"model": self.MODEL, "input": [text]},
                timeout=30.0,
            )
            resp.raise_for_status()
            embeddings = resp.json()["embeddings"]
            return embeddings[0] if embeddings else None
        except Exception:
            return None

    def embed_batch(self, texts: list[str]) -> list[list[float]] | None:
        try:
            import httpx
            resp = httpx.post(
                self.OLLAMA_URL,
                json={"model": self.MODEL, "input": texts},
                timeout=60.0,
            )
            resp.raise_for_status()
            return resp.json()["embeddings"]
        except Exception:
            return None


class OpenAIProvider(EmbeddingProvider):
    """OpenAI text-embedding-3-small (1536-dim)."""

    MODEL = "text-embedding-3-small"

    @property
    def name(self) -> str:
        return "openai"

    @property
    def dimensions(self) -> int:
        return 1536

    def embed(self, text: str) -> list[float] | None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            logger.warning("OPENAI_API_KEY not set")
            return None
        _rate_limit()
        try:
            import httpx
            resp = httpx.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": self.MODEL, "input": text},
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]
        except Exception:
            logger.debug("OpenAI embed failed", exc_info=True)
            return None

    def embed_batch(self, texts: list[str]) -> list[list[float]] | None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return None
        _rate_limit()
        try:
            import httpx
            resp = httpx.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": self.MODEL, "input": texts},
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()["data"]
            # Sort by index to ensure correct ordering
            data.sort(key=lambda x: x["index"])
            return [d["embedding"] for d in data]
        except Exception:
            logger.debug("OpenAI embed_batch failed", exc_info=True)
            return None


class VoyageProvider(EmbeddingProvider):
    """Voyage voyage-3-lite (1024-dim)."""

    MODEL = "voyage-3-lite"

    @property
    def name(self) -> str:
        return "voyage"

    @property
    def dimensions(self) -> int:
        return 1024

    def embed(self, text: str) -> list[float] | None:
        api_key = os.environ.get("VOYAGE_API_KEY")
        if not api_key:
            logger.warning("VOYAGE_API_KEY not set")
            return None
        _rate_limit()
        try:
            import httpx
            resp = httpx.post(
                "https://api.voyageai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": self.MODEL, "input": [text]},
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]
        except Exception:
            logger.debug("Voyage embed failed", exc_info=True)
            return None


class NoneProvider(EmbeddingProvider):
    """Disabled embedding — BM25-only search."""

    @property
    def name(self) -> str:
        return "none"

    @property
    def dimensions(self) -> int:
        return 0

    def embed(self, text: str) -> list[float] | None:
        return None


# --- Provider registry ---

_PROVIDERS: dict[str, type[EmbeddingProvider]] = {
    "ollama": OllamaProvider,
    "openai": OpenAIProvider,
    "voyage": VoyageProvider,
    "none": NoneProvider,
}

_cached_provider: EmbeddingProvider | None = None


def get_provider() -> EmbeddingProvider:
    """Get the configured embedding provider (cached)."""
    global _cached_provider
    if _cached_provider is not None:
        return _cached_provider
    config = _load_config()
    name = config.get("embedding_provider", "ollama")
    cls = _PROVIDERS.get(name, OllamaProvider)
    _cached_provider = cls()
    return _cached_provider


def set_provider(name: str) -> None:
    """Set the embedding provider in config. Clears the cache."""
    global _cached_provider
    if name not in _PROVIDERS:
        raise ValueError(f"Unknown provider: {name}. Available: {', '.join(_PROVIDERS)}")
    config = _load_config()
    old_provider = config.get("embedding_provider", "ollama")
    config["embedding_provider"] = name
    _save_config(config)
    _cached_provider = None
    if old_provider != name:
        old_dims = _PROVIDERS[old_provider]().dimensions
        new_dims = _PROVIDERS[name]().dimensions
        if old_dims != new_dims and old_dims > 0 and new_dims > 0:
            logger.warning(
                "Embedding dimension changed from %d to %d. "
                "Run 'am reembed' to re-embed existing documents, "
                "or 'am vectors clear' to drop existing vectors.",
                old_dims, new_dims,
            )


def _load_config() -> dict:
    try:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text())
    except Exception:
        pass
    return {}


def _save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


def _rate_limit() -> None:
    """Simple rate limiter for cloud API calls."""
    global _last_call_time
    now = time.time()
    elapsed = now - _last_call_time
    if elapsed < _MIN_INTERVAL_SECONDS:
        time.sleep(_MIN_INTERVAL_SECONDS - elapsed)
    _last_call_time = time.time()
