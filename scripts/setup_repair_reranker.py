"""Explicitly install a pinned Apache-2.0 reranker and opt a repair config into it."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

MODEL = "BAAI/bge-reranker-v2-m3"
REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config.exists() else {}
    if not isinstance(config, dict):
        raise ValueError("Repair config must be a JSON object")
    from huggingface_hub import snapshot_download

    target = args.assets.expanduser().resolve() / "bge-reranker-v2-m3"
    snapshot_download(
        MODEL,
        revision=REVISION,
        local_dir=target,
        token=False,
        allow_patterns=["*.json", "*.safetensors", "*.model", "README.md"],
    )
    (target / "recoder-reranker.json").write_text(
        json.dumps(
            {
                "model": MODEL,
                "revision": REVISION,
                "license": "Apache-2.0",
                "source": "https://huggingface.co/BAAI/bge-reranker-v2-m3",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    config["reranker_model"] = str(target)
    args.config.parent.mkdir(parents=True, exist_ok=True)
    args.config.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print("Reranker configured. Restart Core to load:", args.config.resolve())  # noqa: T201


if __name__ == "__main__":
    main()
