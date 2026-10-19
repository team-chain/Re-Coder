"""Opt-in local cross-encoder inference; no downloads or model-supplied code."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


class LocalReranker:
    """Load an explicitly installed, single-score safetensors cross-encoder."""

    def __init__(self, path: str):
        root = Path(path).expanduser().resolve()
        if not root.is_dir() or not list(root.glob("*.safetensors")):
            raise ValueError("Reranker must be an existing local safetensors model directory")
        from sentence_transformers import CrossEncoder

        device = os.getenv("RECODER_REPAIR_RERANKER_DEVICE", "cpu")
        self.model = CrossEncoder(
            str(root),
            device=device,
            max_length=512,
            local_files_only=True,
            trust_remote_code=False,
            model_kwargs={"use_safetensors": True},
        )
        if self.model.num_labels != 1:
            raise ValueError("Reranker must produce exactly one relevance score per pair")
        self.identity = (
            "cross-encoder-v1:"
            + hashlib.sha256(
                json.dumps(
                    {
                        "files": [
                            (str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
                            for p in sorted(root.rglob("*"))
                            if p.is_file() and ".cache" not in p.relative_to(root).parts
                        ],
                        "device": device,
                        "max_length": 512,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
        )

    def __call__(self, query: str, passages: list[str]) -> list[float]:
        """Score a bounded set of already-retrieved candidates."""
        if len(passages) > 20:
            raise ValueError("Reranker candidate limit is 20")
        if not passages:
            return []
        scores = self.model.predict(
            [(query[:4000], text[:2800]) for text in passages],
            batch_size=4,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [float(score) for score in scores]
