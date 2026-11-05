#!/usr/bin/env python3
"""Download only the shards that hold the first N layers of a model.

Trimming layers needs three separate edits, and missing any one of them fails in a
different place, so they are kept together here:

1. **Which shards to fetch.** ``model.layers.<i>.*`` for ``i >= layers`` is
   skipped, and a shard is fetched only if some kept tensor lives in it. The
   loader itself walks ``model.named_parameters()`` and looks each name up in
   ``weight_map`` (``checkpoint/weight_loader.py:317``), so it never asks for a
   shard that is not there.

2. **The index must shrink too.** Building the weight converter constructs a
   ``CheckpointInfo``, which opens *every* file listed in ``weight_map`` to read
   its safetensors header (``models/weight_converter.py:29-40``) -- regardless of
   how many layers the model has. Leaving 24 entries in place with 3 files on disk
   walks alphabetically to shard 8 and raises FileNotFoundError before training
   starts. This is the failure that cost a whole run.

3. **``config.json`` must shrink.** Layer construction is driven by
   ``range(num_hidden_layers + num_nextn_predict_layers)``
   (``modeling_bailing_moe_v3.py:1324``), and MTP occupies
   ``layers[num_hidden_layers]`` -- index 42 in the full checkpoint. After
   trimming, index 6 is not the MTP layer any more, so MTP has to be disabled
   rather than renumbered. ``num_nextn_predict_layers = 0`` here, and the caller
   sets ``mtp.enabled: false`` in the engine config to match.

Note that the download size is not the model size: shards are fetched whole, and a
shard can hold tensors from layers that were dropped. Three shards of Ling-3.0-flash
weigh 31.77 GB while the trimmed model has 13.4 B parameters.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import hf_fetch

# Everything the loader needs before it can decide which shards to read: the
# config, the index, and the tokenizer. ``*.py`` covers trust_remote_code models.
SMALL_FILES = [
    "config.json",
    "model.safetensors.index.json",
    "generation_config.json",
    "special_tokens_map.json",
    "tokenizer*.json",
    "*.py",
]

INDEX = "model.safetensors.index.json"


def _layer_index(tensor_name: str) -> int | None:
    """``model.layers.7.mlp...`` -> 7; anything else -> None.

    Only direct children of ``model.layers`` count. The MTP layer is also a child
    of that list in the checkpoint, so it is trimmed along with the rest and
    re-mapped to nothing -- which is why MTP is turned off rather than moved.
    """
    parts = tensor_name.split(".")
    if len(parts) >= 3 and parts[0] == "model" and parts[1] == "layers":
        if parts[2].isdigit():
            return int(parts[2])
    return None


def plan_shards(weight_map: dict[str, str], layers: int) -> set[str]:
    """Shards that hold at least one tensor of layer ``< layers`` or no layer."""
    need: set[str] = set()
    for name, shard in weight_map.items():
        index = _layer_index(name)
        if index is not None and index >= layers:
            continue
        need.add(shard)
    return need


def trim(*, repo: str, directory: str, layers: int, attempts: int) -> None:
    dst = Path(directory)
    dst.mkdir(parents=True, exist_ok=True)

    hf_fetch.download(
        repo_id=repo,
        local_dir=dst,
        allow_patterns=SMALL_FILES,
        attempts=attempts,
    )

    index_path = dst / INDEX
    if not index_path.is_file():
        raise SystemExit(
            f"{index_path} is missing; this model does not ship a safetensors index "
            "and cannot be trimmed by layer"
        )
    index_doc = json.loads(index_path.read_text(encoding="utf-8"))
    weight_map: dict[str, str] = index_doc["weight_map"]
    all_shards = set(weight_map.values())

    need = plan_shards(weight_map, layers)
    print(
        f"{layers} layers -> need {len(need)}/{len(all_shards)} shards", flush=True
    )
    for shard in sorted(need):
        print(f"   {shard}", flush=True)
    hf_fetch.download(
        repo_id=repo,
        local_dir=dst,
        allow_patterns=sorted(need),
        attempts=attempts,
    )

    kept = {name: shard for name, shard in weight_map.items() if shard in need}
    index_doc["weight_map"] = kept
    index_doc.setdefault("metadata", {})["tt_trimmed_layers"] = layers
    index_path.write_text(json.dumps(index_doc, indent=2) + "\n", encoding="utf-8")
    print(
        f"index: kept {len(kept)}/{len(weight_map)} entries ({len(need)} shards)",
        flush=True,
    )

    cfg_path = dst / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["num_hidden_layers"] = layers
    cfg["num_nextn_predict_layers"] = 0
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(
        f"config: num_hidden_layers={layers} num_nextn_predict_layers=0",
        flush=True,
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--dir", required=True)
    parser.add_argument("--layers", type=int, required=True)
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args(argv)
    if args.layers < 1:
        parser.error("--layers must be >= 1; use hf_fetch.py to download in full")
    trim(
        repo=args.repo,
        directory=args.dir,
        layers=args.layers,
        attempts=args.attempts,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
