#!/usr/bin/env python3
"""Derive a runnable engine config from a repo yaml and the actual card count.

The yaml in ``scripts/runs/<model>/`` is written for the full node the model was
tuned on -- ``bailing_v3/sft.yaml`` asks for ``sp=2 ep=8``, i.e. 16 cards. CI pods
have 2, 4, 8 or 16, so the plan has to be narrowed at run time. Editing the yaml
would mean the CI never tests what the repo actually ships, so instead this script
reads it, overrides only the parallel plan, and writes a copy to /tmp.

Two invariants the narrowing must respect:

* ``sp`` divides the world size, otherwise the sequence split leaves a ragged tail.
* ``ep`` divides the world size and cannot exceed ``world / sp``, since expert
  parallelism lives inside one data-parallel replica.

Halving is used rather than rounding because both values are powers of two in every
config in the repo, so halving terminates on an exact divisor instead of drifting
to 1.

Trimming layers also has to disable MTP, and that belongs here rather than in the
model: the MTP layer is appended to ``model.layers`` at index
``num_hidden_layers`` (``modeling_bailing_moe_v3.py:1324``), so after trimming it
would claim a layer index whose weights were never downloaded. The loader is
``strict=False``, so it would silently keep a randomly initialised head.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


def narrow(sp: int, ep: int, world: int) -> tuple[int, int]:
    """Shrink sp/ep until both divide ``world`` and ep fits inside one replica."""
    sp = max(1, int(sp))
    ep = max(1, int(ep))

    while sp > 1 and world % sp != 0:
        sp //= 2
    max_ep = max(1, world // sp)
    if ep > max_ep:
        ep = max_ep
    while ep > 1 and world % ep != 0:
        ep //= 2
    return sp, ep


def derive(
    *,
    source: Path | None,
    sp_in: str,
    ep_in: str,
    world: int,
    layers: int,
    out: Path,
) -> tuple[int, int, int, int]:
    """Write the derived config and return (old_sp, old_ep, sp, ep)."""
    doc: dict = {}
    if source is not None and source.is_file():
        doc = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    engine = doc.setdefault("engine_config", {}) or {}
    plan = engine.setdefault("parallel_plan", {}) or {}

    old_sp = int(plan.get("sp_size", 1) or 1)
    old_ep = int(plan.get("ep_size", 1) or 1)

    sp = old_sp if sp_in == "auto" else int(sp_in)
    ep = old_ep if ep_in == "auto" else int(ep_in)
    sp, ep = narrow(sp, ep, world)

    plan["sp_size"], plan["ep_size"] = sp, ep

    # See the module docstring: after trimming, the MTP layer's weights are gone
    # and the loader would keep a random head instead of failing.
    if layers > 0:
        mtp = engine.setdefault("mtp", {}) or {}
        mtp["enabled"] = False
        engine["mtp"] = mtp

    doc["engine_config"] = engine
    out.write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return old_sp, old_ep, sp, ep


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--yaml", default="", help="source engine config; may be empty")
    parser.add_argument("--cards", type=int, required=True)
    parser.add_argument("--sp", default="auto")
    parser.add_argument("--ep", default="auto")
    parser.add_argument("--layers", type=int, default=0)
    parser.add_argument("--out", required=True)
    parser.add_argument("--output", default="/dev/stdout")
    args = parser.parse_args(argv)

    source = Path(args.yaml) if args.yaml else None
    out = Path(args.out)
    old_sp, old_ep, sp, ep = derive(
        source=source,
        sp_in=str(args.sp),
        ep_in=str(args.ep),
        world=args.cards,
        layers=args.layers,
        out=out,
    )

    print(
        f"parallel: sp={old_sp} ep={old_ep} -> sp={sp} ep={ep} "
        f"(cards={args.cards}, source={args.yaml or '<none>'})",
        file=sys.stderr,
    )
    with open(args.output, "a", encoding="utf-8") as handle:
        handle.write(f"SP={sp}\nEP={ep}\nTT_CONFIG={out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
