#!/usr/bin/env python3
"""Pre-download (bake) the local embedding model into the image / a cache dir.

Self-hosted installs are often air-gapped, so we fetch the ONNX model at BUILD
time and ship it, rather than downloading on first use. Run this in the
Dockerfile after installing deps:

    BOW_EMBEDDINGS_CACHE_DIR=/opt/bow-models python scripts/download_embedding_model.py

At runtime set the same BOW_EMBEDDINGS_CACHE_DIR (or embeddings.cache_dir in
bow-config) so fastembed loads from disk with no network access.

Honors BOW_EMBEDDINGS_MODEL to bake a non-default model (e.g. the multilingual
intfloat/multilingual-e5-small). A no-op-friendly exit code 0 is returned when
embeddings are disabled, so the build step never blocks an image that opts out.
"""
import os
import sys


def main() -> int:
    enabled = os.environ.get("BOW_EMBEDDINGS_ENABLED", "true").strip().lower()
    if enabled in ("false", "0", "no"):
        print("embeddings disabled — skipping model bake")
        return 0

    model = os.environ.get("BOW_EMBEDDINGS_MODEL", "BAAI/bge-small-en-v1.5")
    cache_dir = os.environ.get("BOW_EMBEDDINGS_CACHE_DIR") or None

    try:
        from fastembed import TextEmbedding
    except ImportError:
        print("fastembed not installed — nothing to bake", file=sys.stderr)
        return 0

    print(f"baking embedding model '{model}' into {cache_dir or 'default cache'} ...")
    kwargs = {"cache_dir": cache_dir} if cache_dir else {}
    emb = TextEmbedding(model_name=model, **kwargs)
    # Force a real embed so tokenizer + ONNX weights are fully materialized.
    _ = list(emb.embed(["warmup"]))
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
