#!/usr/bin/env python3
"""Preprocess model weights and derive the engine configuration."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

MIN_BYTES = 3_000_000_000
MARKER = ".tt-cache-marker"
INDEX = "model.safetensors.index.json"
SMALL_FILES = [
    "config.json",
    INDEX,
    "generation_config.json",
    "special_tokens_map.json",
    "tokenizer*.json",
    "*.py",
]

# Stage map (run_train.sh per-case stages):
# 1. resolve: map model_type -> launcher, install script, and model repo.
# 3a. preprocess/acquire: find, validate, fetch, or trim a model cache directory.
# 3b. preprocess/derive: write a card-count-specific engine configuration.


def log(message: str) -> None:
    print(f"[model] {message}", file=sys.stderr, flush=True)


def emit(destination: str, **values: object) -> None:
    text = "".join(f"{key}={value}\n" for key, value in values.items())
    if destination:
        with open(destination, "a", encoding="utf-8") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)
        sys.stdout.flush()


# Stage 3a: validate a cache directory and expose its size and shard count.
def probe(directory: str, min_bytes: int = MIN_BYTES) -> tuple[int, int] | None:
    """Stage 3a: validate config, logical size, shard count, and index entries."""
    root = Path(directory)
    if not (root / "config.json").is_file():
        log(f"probe {root}: no config.json")
        return None
    du = subprocess.run(["du", "-sbL", str(root)], capture_output=True, text=True)
    try:
        size = int(du.stdout.split()[0]) if du.returncode == 0 else 0
    except (IndexError, ValueError):
        size = 0
    if size < min_bytes:
        log(f"probe {root}: {size} bytes < {min_bytes}")
        return None
    shards = len(list(root.glob("*.safetensors"))) + len(list(root.glob("*/*.safetensors")))
    if not shards:
        log(f"probe {root}: no *.safetensors")
        return None
    index = root / INDEX
    if index.is_file():
        try:
            names = set(json.loads(index.read_text(encoding="utf-8"))["weight_map"].values())
        except (OSError, ValueError, KeyError) as exc:
            log(f"probe {root}: index unreadable: {exc}")
            return None
        missing = sorted(name for name in names if not (root / name).is_file())
        if missing:
            log(f"probe {root}: missing {len(missing)} indexed shards: {missing[:5]}")
            return None
    return size, shards


def cache_target(repo: str, cache_root: str, layers: int) -> Path:
    name = repo if layers <= 0 else f"{repo.rsplit('/', 1)[-1]}-L{layers}"
    return Path(cache_root) / name


def cached(
    repo: str,
    home: str,
    cache_root: str,
    min_bytes: int,
    marker: str,
    target: Path,
) -> tuple[Path, int, int] | None:
    """Stage 3a: search known cache roots and require a matching build marker."""
    escaped = repo.replace(".", "___")
    candidates = [
        target,
        Path(cache_root) / repo,
        Path(cache_root) / escaped,
        Path(f"/root/.cache/modelscope/hub/models/{repo}"),
        Path(f"/root/.cache/modelscope/hub/models/{escaped}"),
        Path(f"/root/.cache/modelscope/hub/{repo}"),
    ]
    candidates += sorted(Path(home).glob(f"hub/models--{repo.replace('/', '--')}/snapshots/*"))
    for candidate in dict.fromkeys(candidates):
        if not candidate.is_dir():
            continue
        stamp = candidate / MARKER
        if not stamp.is_file() or stamp.read_text(encoding="utf-8").strip() != marker:
            log(f"stale {candidate} (marker mismatch)")
            continue
        result = probe(str(candidate), min_bytes)
        if result is None:
            log(f"miss {candidate}")
            continue
        log(f"HIT {candidate} bytes={result[0]} shards={result[1]}")
        return candidate, *result
    return None


def download(repo: str, directory: str, patterns: list[str] | None, attempts: int) -> None:
    """Stage 3a: download files with retries for transient mirror failures."""
    from huggingface_hub import snapshot_download

    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        log(f"--- download attempt {attempt}/{attempts} ---")
        try:
            snapshot_download(repo_id=repo, local_dir=directory, allow_patterns=patterns)
            return
        except Exception as exc:  # noqa: BLE001 - retry every download failure
            last = exc
            log(f"attempt {attempt} failed: {type(exc).__name__}: {exc}")
            if attempt < attempts:
                wait = attempt * 30
                log(f"retrying in {wait}s")
                time.sleep(wait)
    raise SystemExit(f"download {repo} failed after {attempts} attempts") from last


def layer_index(name: str) -> int:
    parts = name.split(".")
    if len(parts) > 2 and parts[:2] == ["model", "layers"] and parts[2].isdigit():
        return int(parts[2])
    return -1


def trim(repo: str, directory: Path, layers: int, attempts: int) -> None:
    """Stage 3a: keep only shards needed by the first ``layers`` layers."""
    directory.mkdir(parents=True, exist_ok=True)
    download(repo, str(directory), SMALL_FILES, attempts)
    index = directory / INDEX
    if not index.is_file():
        raise SystemExit(f"{index} is missing; cannot trim without a safetensors index")
    doc = json.loads(index.read_text(encoding="utf-8"))
    weight_map: dict[str, str] = doc["weight_map"]
    need = {shard for name, shard in weight_map.items() if layer_index(name) < layers}
    log(f"{layers} layers -> need {len(need)}/{len(set(weight_map.values()))} shards")
    download(repo, str(directory), sorted(need), attempts)
    doc["weight_map"] = {name: shard for name, shard in weight_map.items() if shard in need}
    doc.setdefault("metadata", {})["tt_trimmed_layers"] = layers
    index.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    config["num_hidden_layers"] = layers
    config["num_nextn_predict_layers"] = 0
    (directory / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def acquire(args: argparse.Namespace) -> dict[str, object]:
    """Stage 3a: return a complete model directory, fetching or trimming on miss."""
    if args.hint:
        result = probe(args.hint, args.min_bytes)
        if result is None:
            raise SystemExit(f"explicit model is incomplete: {args.hint}")
        return {"MODEL": args.hint, "BYTES": result[0], "SHARDS": result[1], "HIT": "yes"}

    target = cache_target(args.repo, args.cache_root, args.layers)
    marker = f"repo={args.repo} layers={args.layers}"
    found = cached(args.repo, args.home, args.cache_root, args.min_bytes, marker, target)
    if found is not None:
        return {"MODEL": found[0], "BYTES": found[1], "SHARDS": found[2], "HIT": "yes"}
    if args.layers > 0:
        log(f"trim {args.repo} -> {target}")
        trim(args.repo, target, args.layers, args.attempts)
    else:
        log(f"fetch {args.repo} -> {target}")
        target.mkdir(parents=True, exist_ok=True)
        download(args.repo, str(target), None, args.attempts)
    result = probe(str(target), args.min_bytes)
    if result is None:
        raise SystemExit(f"prepared model is incomplete: {target}")
    (target / MARKER).write_text(marker + "\n", encoding="utf-8")
    return {"MODEL": target, "BYTES": result[0], "SHARDS": result[1], "HIT": "no"}


# Stage 3b: resolve the launcher config and derive card-count-specific sp/ep.
def source_config(train_script: str) -> Path | None:
    """Stage 3b: resolve the TT_CONFIG yaml referenced by a launcher."""
    script = Path(train_script)
    text = script.read_text(encoding="utf-8")
    match = re.search(r'^TT_CONFIG=\$\{TT_CONFIG:-(.*)\}$', text, re.M)
    if match is None:
        return None
    raw = match.group(1).strip('"')
    raw = raw.replace('$(dirname "$0")', str(script.parent)).replace("${SCRIPT_DIR}", str(script.parent))
    source = Path(raw)
    return source if source.is_file() else None


def derive(
    source: Path | None,
    sp_in: str,
    ep_in: str,
    cards: int,
    layers: int,
    out: Path,
) -> tuple[int, int, int, int]:
    """Stage 3b: write the derived engine config and return old/new sp/ep."""
    import yaml

    doc: dict = {}
    if source is not None:
        doc = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    engine = doc.setdefault("engine_config", {}) or {}
    plan = engine.setdefault("parallel_plan", {}) or {}
    old_sp = int(plan.get("sp_size", 1) or 1)
    old_ep = int(plan.get("ep_size", 1) or 1)
    sp = old_sp if sp_in == "auto" else int(sp_in)
    ep = old_ep if ep_in == "auto" else int(ep_in)
    while sp > 1 and cards % sp:
        sp //= 2
    ep = min(ep, max(1, cards // sp))
    while ep > 1 and cards % ep:
        ep //= 2
    plan["sp_size"], plan["ep_size"] = sp, ep
    if layers > 0:
        mtp = engine.setdefault("mtp", {}) or {}
        mtp["enabled"] = False
        engine["mtp"] = mtp
    doc["engine_config"] = engine
    out.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return old_sp, old_ep, sp, ep


# Stage 1: map model_type to its run directory, launcher, install script, and repo.
def cmd_resolve(args: argparse.Namespace) -> int:
    """Stage 1: resolve model scripts from model_map.json."""
    try:
        models = json.loads(Path(args.map).read_text(encoding="utf-8"))["models"]
        row = models[args.model_type]
    except (OSError, ValueError, KeyError) as exc:
        raise SystemExit(f"cannot resolve {args.model_type} from {args.map}: {exc}") from exc
    if args.framework not in {"lf", "swift"} or not isinstance(row, dict):
        raise SystemExit(f"invalid framework or map row for {args.model_type}")
    run_dir = str(row.get("run_dir", "-"))
    base = Path("scripts/runs") / run_dir
    if run_dir == "-" or not base.is_dir():
        raise SystemExit(f"missing run_dir for {args.model_type}: {base}")
    launchers = str(row.get(args.framework, "-")).split(",")
    train = next((base / name for name in launchers if name != "-" and (base / name).is_file()), None)
    if train is None:
        raise SystemExit(f"no {args.framework} launcher under {base}")
    # Per-framework install script. The lf name is also the legacy default, so
    # only lf may fall back: a swift run must never install lf's dependencies.
    candidates = [base / f"install_{args.framework}_deps.sh"]
    if args.framework == "lf":
        candidates.append(base / "install_deps.sh")
    install = next((path for path in candidates if path.is_file()), None)
    if install is None:
        raise SystemExit(f"no install script under {base}")
    emit("", RUN_DIR=run_dir, INSTALL_SCRIPT=install, TRAIN_SCRIPT=train, HF_REPO=row.get("hf_repo", "-"))
    return 0


def cmd_preprocess(args: argparse.Namespace) -> int:
    """Stage 3 preprocess: acquire the model (3a), then write config (3b)."""
    model = acquire(args)
    source = source_config(args.train_script)
    if source is None:
        log(f"no config yaml for {args.train_script}; writing a minimal config")
    old_sp, old_ep, sp, ep = derive(source, args.sp, args.ep, args.cards, args.layers, Path(args.out))
    emit(args.output, **model, SP=sp, EP=ep, TT_CONFIG=args.out)
    log(f"parallel: sp={old_sp} ep={old_ep} -> sp={sp} ep={ep}; model={model['MODEL']}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    result = probe(args.directory, args.min_bytes)
    if result is None:
        return 1
    print(*result)
    return 0


def cmd_cases(args: argparse.Namespace) -> int:
    """Training orchestration: expand selected E2E cases into TSV lines."""
    try:
        items = json.loads(args.json or "[]")
    except ValueError as exc:
        raise SystemExit(f"invalid E2E_CASES JSON: {exc}") from exc
    if not isinstance(items, list) or any(not isinstance(item, dict) or "model_type" not in item for item in items):
        raise SystemExit("E2E_CASES must be a list of objects with model_type")
    for item in items:
        print(f"{item['model_type']}\t{item.get('layers', 0)}")
    return 0


# CLI: expose resolve and preprocess stages to run_train.sh and local debugging.
def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    resolve = sub.add_parser("resolve")
    resolve.add_argument("--map", required=True)
    resolve.add_argument("--model-type", required=True)
    resolve.add_argument("--framework", required=True)
    resolve.set_defaults(func=cmd_resolve)

    probe_cmd = sub.add_parser("probe")
    probe_cmd.add_argument("--dir", dest="directory", required=True)
    probe_cmd.add_argument("--min-bytes", type=int, default=MIN_BYTES)
    probe_cmd.set_defaults(func=cmd_probe)

    preprocess = sub.add_parser("preprocess")
    preprocess.add_argument("--repo", required=True)
    preprocess.add_argument("--hint", default="")
    preprocess.add_argument("--layers", type=int, default=0)
    preprocess.add_argument("--cards", type=int, required=True)
    preprocess.add_argument("--sp", default="auto")
    preprocess.add_argument("--ep", default="auto")
    preprocess.add_argument("--train-script", required=True)
    preprocess.add_argument("--out", required=True)
    preprocess.add_argument("--output", default="")
    preprocess.add_argument("--home", default=os.environ.get("HF_HOME", "/root/.cache/huggingface"))
    preprocess.add_argument("--cache-root", default="/root/.cache/models")
    preprocess.add_argument("--min-bytes", type=int, default=MIN_BYTES)
    preprocess.add_argument("--attempts", type=int, default=3)
    preprocess.set_defaults(func=cmd_preprocess)

    cases = sub.add_parser("cases")
    cases.add_argument("json", nargs="?", default="[]")
    cases.set_defaults(func=cmd_cases)
    return root


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
