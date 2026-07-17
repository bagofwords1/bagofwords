"""Local, self-hosted text embeddings.

One small module, many consumers (file catalog search today; instruction
ranking later). Design constraints:

- SELF-HOSTED ONLY: no cloud embedding APIs. Inference runs in-process on CPU
  via fastembed (ONNX). Air-gapped installs bake the model into the image
  (see scripts/download_embedding_model.py) — nothing is fetched at runtime
  when the model directory is already populated.
- Graceful degradation: if fastembed isn't installed, the model can't load, or
  embeddings are disabled in config, `get_backend()` returns None and callers
  fall back to lexical-only behavior.
- Model choice is config-driven (`embeddings:` block in bow-config):
    * default `BAAI/bge-small-en-v1.5` — MIT, 33M params, 384 dims,
      <10ms/text on CPU: fast enough to embed lazily at query time.
    * `intfloat/multilingual-e5-small` — MIT, 384 dims: the multilingual pick.
    * larger models (e.g. Qwen3-Embedding-0.6B ONNX) work for installs that
      only batch-embed at index time and can afford the CPU latency.
- Every stored vector is tagged with `model_tag`; a mismatch means "treat as
  not embedded" (no silent cross-model cosine).
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"

# Keep per-text input bounded: file entries embed name+keywords+excerpt, and
# small models truncate around 512 tokens anyway.
MAX_CHARS_PER_TEXT = 4000


class EmbeddingBackend:
    """CPU ONNX embedding backend (fastembed). Thread-safe, lazy-loaded."""

    def __init__(self, model_name: str, cache_dir: str | None = None):
        self.model_name = model_name
        self.cache_dir = cache_dir
        self._model = None
        self._lock = threading.Lock()

    @property
    def model_tag(self) -> str:
        return f"fastembed:{self.model_name}"

    def _ensure_model(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from fastembed import TextEmbedding  # deferred import
                    kwargs = {}
                    if self.cache_dir:
                        kwargs["cache_dir"] = self.cache_dir
                    self._model = TextEmbedding(model_name=self.model_name, **kwargs)
        return self._model

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts synchronously. CPU-bound — call via
        `embed_texts_async` from async code."""
        if not texts:
            return []
        model = self._ensure_model()
        clipped = [(t or "")[:MAX_CHARS_PER_TEXT] for t in texts]
        return [vec.tolist() for vec in model.embed(clipped)]

    async def embed_texts_async(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self.embed_texts, texts)


_backend: EmbeddingBackend | None = None
_backend_resolved = False
_backend_lock = threading.Lock()


def _read_config():
    """Read the embeddings config block; env vars win for container installs.

    BOW_EMBEDDINGS_ENABLED=false disables outright.
    BOW_EMBEDDINGS_MODEL / BOW_EMBEDDINGS_CACHE_DIR override the model/dir.
    """
    enabled = os.environ.get("BOW_EMBEDDINGS_ENABLED", "").strip().lower()
    model = os.environ.get("BOW_EMBEDDINGS_MODEL", "").strip()
    cache_dir = os.environ.get("BOW_EMBEDDINGS_CACHE_DIR", "").strip()

    cfg_enabled, cfg_model, cfg_cache = None, None, None
    try:
        from app.settings.config import settings  # type: ignore
        block = getattr(settings.bow_config, "embeddings", None)
        if block is not None:
            cfg_enabled = getattr(block, "enabled", None)
            cfg_model = getattr(block, "model", None)
            cfg_cache = getattr(block, "cache_dir", None)
    except Exception:
        pass

    if enabled in ("false", "0", "no"):
        is_enabled = False
    elif enabled in ("true", "1", "yes"):
        is_enabled = True
    elif cfg_enabled is not None:
        is_enabled = bool(cfg_enabled)
    else:
        is_enabled = True  # enabled by default when the library is available

    return is_enabled, (model or cfg_model or DEFAULT_MODEL), (cache_dir or cfg_cache or None)


def get_backend() -> EmbeddingBackend | None:
    """Resolve the process-wide embedding backend (or None when unavailable).

    Cheap after the first call. Never raises: any failure (fastembed missing,
    disabled by config) resolves to None and callers stay lexical-only.
    The model itself loads lazily on first embed, so resolving the backend
    does not pay model-load cost.
    """
    global _backend, _backend_resolved
    if _backend_resolved:
        return _backend
    with _backend_lock:
        if _backend_resolved:
            return _backend
        try:
            enabled, model_name, cache_dir = _read_config()
            if not enabled:
                logger.info("embeddings disabled by config")
                _backend = None
            else:
                import fastembed  # noqa: F401 — availability check only
                _backend = EmbeddingBackend(model_name, cache_dir)
                logger.info(f"embeddings backend ready: {_backend.model_tag}")
        except ImportError:
            logger.info("fastembed not installed — embeddings unavailable (lexical-only)")
            _backend = None
        except Exception as e:
            logger.warning(f"embeddings backend unavailable: {e}")
            _backend = None
        _backend_resolved = True
        return _backend


def reset_backend_for_tests():
    """Test hook: clear the cached resolution (e.g. after monkeypatching)."""
    global _backend, _backend_resolved
    with _backend_lock:
        _backend = None
        _backend_resolved = False


def cosine(a: list[float], b: list[float]) -> float:
    """Plain cosine similarity; 0.0 on degenerate input."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = num_a = num_b = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        num_a += x * x
        num_b += y * y
    if num_a <= 0 or num_b <= 0:
        return 0.0
    return dot / ((num_a ** 0.5) * (num_b ** 0.5))
