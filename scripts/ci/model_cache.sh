#!/bin/bash
# Model acquisition for CI.
#
# Three separate things happen to a model in these workflows and all three are easy
# to get subtly wrong, so they live here rather than in a heredoc that only ever
# runs inside CI:
#
#   probe  decide whether a cache directory is usable
#   find   look for one in every place the platform has ever put it
#   mark   record what a directory was built from, so a changed recipe re-fetches
#
# Network fetching lives in hf_fetch.py / flash_trim.py, because retry behaviour
# and index rewriting are easier to read (and to test) in Python.
#
# Output convention: human-readable logs go to stderr, machine-readable
# `KEY=value` lines go to --output. A caller can therefore append the facts to
# $GITHUB_OUTPUT -- or source the file -- while still tracing what happened in the
# job log.

set -uo pipefail

PROG=$(basename "$0")
OUT=/dev/stdout
MIN_BYTES=3000000000
MARKER_NAME=.tt-cache-marker

log() { printf '[model_cache] %s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }

usage() {
    cat >&2 <<USAGE
usage: $PROG <subcommand> [options]

  probe  --dir DIR [--min-bytes N]
  find   --repo REPO [--hint PATH] [--home DIR] [--min-bytes N] [--marker TEXT]
  mark   --dir DIR --text TEXT

common: --output FILE   (default: stdout)
USAGE
    exit 2
}

# ---------------------------------------------------------------------------
# probe: is this directory a complete model?
#
# Directory inode size says nothing (22..499 bytes in practice), and a hub-style
# `snapshots/<sha>/` is nothing but symlinks, so the check is "config.json plus
# real safetensors bytes" and `du -L` is mandatory -- without it du counts the
# links, not the files.
# ---------------------------------------------------------------------------
probe_dir() {
    local dir=$1
    local min_bytes=$2

    [ -f "$dir/config.json" ] || { log "probe $dir: no config.json"; return 1; }

    local bytes shards
    bytes=$(timeout 180 du -sbL "$dir" 2>/dev/null | awk '{print $1}')
    shards=$(timeout 120 find "$dir" -maxdepth 2 -name '*.safetensors' 2>/dev/null | wc -l)

    if ! [ "${bytes:-0}" -ge "$min_bytes" ] 2>/dev/null; then
        log "probe $dir: ${bytes:-0} bytes < ${min_bytes} (incomplete)"
        return 1
    fi
    if ! [ "${shards:-0}" -ge 1 ] 2>/dev/null; then
        log "probe $dir: no *.safetensors under it"
        return 1
    fi

    # The index is the only authority on completeness: it names every shard the
    # loader may ask for. A shard listed but absent means the previous download was
    # killed part way, and trusting the directory would fail much later, inside
    # CheckpointInfo, with a FileNotFoundError that looks unrelated.
    local index="$dir/model.safetensors.index.json"
    if [ -f "$index" ]; then
        if ! python3 - "$index" "$dir" <<'PY'
import json
import os
import sys

index, root = sys.argv[1], sys.argv[2]
try:
    doc = json.load(open(index, encoding="utf-8"))
except Exception as exc:  # truncated or half-written index
    print(f"[model_cache]   index unreadable: {exc}")
    sys.exit(1)
need = sorted(set(doc.get("weight_map", {}).values()))
gone = [s for s in need if not os.path.isfile(os.path.join(root, s))]
if gone:
    print(f"[model_cache]   index lists {len(gone)} shard(s) not on disk:")
    for shard in gone[:5]:
        print(f"[model_cache]     {shard}")
    sys.exit(1)
PY
        then
            log "probe $dir: index promises files that are missing"
            return 1
        fi
    fi

    printf '%s %s\n' "$bytes" "$shards"
}

# ---------------------------------------------------------------------------
# find: every location this platform has ever used for a model cache.
#
# Ordered from most explicit to most implicit; the first complete hit wins.
# ---------------------------------------------------------------------------
cmd_find() {
    local repo="" hint="" home=${HF_HOME:-/root/.cache/huggingface}
    local min_bytes=$MIN_BYTES marker=""

    while [ $# -gt 0 ]; do
        case "$1" in
            --repo) repo=$2; shift 2 ;;
            --hint) hint=$2; shift 2 ;;
            --home) home=$2; shift 2 ;;
            --min-bytes) min_bytes=$2; shift 2 ;;
            --marker) marker=$2; shift 2 ;;
            *) die "find: unknown option $1" ;;
        esac
    done
    [ -n "$repo" ] || die "find: --repo is required"

    # modelscope escapes '.' as '___'; both spellings exist in the wild.
    local escaped
    escaped=$(printf '%s' "$repo" | sed 's/\./___/g')

    local candidates=()
    [ -n "$hint" ] && candidates+=("$hint")
    candidates+=(
        "/root/.cache/models/$repo"
        "/root/.cache/models/$escaped"
        "/root/.cache/modelscope/hub/models/$repo"
        "/root/.cache/modelscope/hub/models/$escaped"
        "/root/.cache/modelscope/hub/$repo"
    )
    local snap
    for snap in "$home"/hub/models--${repo//\//--}/snapshots/* ; do
        [ -d "$snap" ] && candidates+=("$snap")
    done

    local cand result found=no
    for cand in "${candidates[@]}"; do
        [ -d "$cand" ] || continue
        # A marker mismatch means the directory was built to a different recipe
        # (other layer count, other repo). Reusing it would load the wrong shape of
        # model, so treat it as a miss even though probe() would pass.
        if [ -n "$marker" ] && [ -f "$cand/$MARKER_NAME" ]; then
            if [ "$(cat "$cand/$MARKER_NAME" 2>/dev/null)" != "$marker" ]; then
                log "stale $cand (marker: $(cat "$cand/$MARKER_NAME" 2>/dev/null))"
                continue
            fi
        elif [ -n "$marker" ]; then
            log "stale $cand (no marker; expected '$marker')"
            continue
        fi
        if result=$(probe_dir "$cand" "$min_bytes"); then
            log "HIT  $cand  bytes=${result% *} shards=${result#* }"
            printf 'MODEL=%s\nBYTES=%s\nSHARDS=%s\nHIT=yes\n' \
                "$cand" "${result% *}" "${result#* }" >>"$OUT"
            found=yes
            break
        fi
        log "miss $cand"
    done

    if [ "$found" != yes ]; then
        log "no usable cache for $repo"
        printf 'HIT=no\n' >>"$OUT"
    fi
}

cmd_mark() {
    local dir="" text=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --dir) dir=$2; shift 2 ;;
            --text) text=$2; shift 2 ;;
            *) die "mark: unknown option $1" ;;
        esac
    done
    [ -n "$dir" ] && [ -n "$text" ] || die "mark: --dir and --text are required"
    printf '%s\n' "$text" >"$dir/$MARKER_NAME"
    log "marked $dir as '$text'"
}

main() {
    [ $# -gt 0 ] || usage
    local cmd=$1
    shift

    local args=()
    while [ $# -gt 0 ]; do
        if [ "$1" = "--output" ]; then
            OUT=$2
            shift 2
        else
            args+=("$1")
            shift
        fi
    done

    case "$cmd" in
        probe)
            local dir="" min_bytes=$MIN_BYTES
            local i=0
            while [ $i -lt ${#args[@]} ]; do
                case "${args[$i]}" in
                    --dir) dir=${args[$((i + 1))]}; i=$((i + 2)) ;;
                    --min-bytes) min_bytes=${args[$((i + 1))]}; i=$((i + 2)) ;;
                    *) die "probe: unknown option ${args[$i]}" ;;
                esac
            done
            [ -n "$dir" ] || die "probe: --dir is required"
            probe_dir "$dir" "$min_bytes"
            ;;
        find)  cmd_find ${args[@]+"${args[@]}"} ;;
        mark)  cmd_mark ${args[@]+"${args[@]}"} ;;
        -h|--help|help) usage ;;
        *) die "unknown subcommand '$cmd'" ;;
    esac
}

main "$@"
