#!/usr/bin/env bash
# Prove that installing something did not swap the NPU build of torch.
#
# Why this exists
# ---------------
# The L1 image ships a self-built torch (version contains `nomimalloc`) plus
# torch_npu. Both install steps run pip, and pip happily resolves `torch` from
# PyPI as a transitive dependency -- `--no-build-isolation` is only a mitigation:
# any package whose metadata asks for a newer torch will pull a fresh wheel from
# the index and shadow the one the image was built with.
#
# The failure is not loud. `import torch` still works, `torch.npu` is gone, and
# every tensor quietly moves to the CPU: pytest notices immediately, training
# does not -- it just takes 100x longer on a machine nobody is watching. That is
# why the check has to fail the job rather than warn.
#
# Usage
#   torch_guard.sh capture --file F     record what is importable now
#   torch_guard.sh verify  --file F     fail if it changed since the capture
#
# `verify` compares both the version string and `torch.__file__`. A rebuilt or
# re-resolved copy differs in at least one of them, and keeping both means the
# error message can say which. Exit code: 0 unchanged, 1 changed, 2 unusable.
set -uo pipefail

PYTHON=${PYTHON:-python}
PROBE=$(mktemp)
trap 'rm -f "$PROBE"' EXIT

# Writes KEY=value lines, always exiting 0: a missing torch is an observation to
# record, not a reason to abort the capture (the L2 image legitimately lacks it).
# Values are shell-quoted because the file is sourced: an unquoted version string
# like 2.10.0+nomimalloc is harmless, but "No module named 'torch'" is executed.
cat >"$PROBE" <<'PY'
import shlex
import sys

out = []


def emit(key, value):
    out.append(f"{key}={shlex.quote(str(value))}")


try:
    import torch
except Exception as exc:  # noqa: BLE001 - reported, not raised
    emit("TORCH_OK", "no")
    emit("TORCH_ERROR", f"{type(exc).__name__}: {exc}")
else:
    emit("TORCH_OK", "yes")
    emit("TORCH_VERSION", torch.__version__)
    emit("TORCH_FILE", torch.__file__)
    try:
        import torch_npu

        emit("TORCH_NPU_VERSION", getattr(torch_npu, "__version__", "unknown"))
        emit("TORCH_NPU_FILE", torch_npu.__file__)
    except Exception as exc:  # noqa: BLE001
        emit("TORCH_NPU_ERROR", f"{type(exc).__name__}: {exc}")

sys.stdout.write("\n".join(out) + "\n")
PY

capture() {
    local file=$1
    "$PYTHON" "$PROBE" >"$file" 2>/dev/null || : >"$file"
}

describe() {
    local file=$1
    # shellcheck disable=SC1090
    ( . "$file" 2>/dev/null
      if [ "${TORCH_OK:-no}" != yes ]; then
          printf 'torch unavailable (%s)' "${TORCH_ERROR:-not importable}"
      else
          printf 'torch %s @ %s; torch_npu %s' \
              "${TORCH_VERSION:-?}" "${TORCH_FILE:-?}" "${TORCH_NPU_VERSION:-MISSING}"
      fi )
}

cmd=${1:-}
shift || true

file=""
while [ $# -gt 0 ]; do
    case "$1" in
        --file) file=$2; shift 2 ;;
        *) echo "[torch_guard] unknown option $1" >&2; exit 2 ;;
    esac
done
[ -n "$file" ] || { echo "[torch_guard] --file is required" >&2; exit 2; }

case "$cmd" in
    capture)
        capture "$file"
        echo "[torch_guard] captured: $(describe "$file")" >&2
        ;;
    verify)
        [ -f "$file" ] || { echo "[torch_guard] no capture at $file" >&2; exit 2; }
        # shellcheck disable=SC1090
        . "$file"
        before_ok=${TORCH_OK:-no}
        before=${TORCH_VERSION:-}
        before_file=${TORCH_FILE:-}
        before_npu=${TORCH_NPU_VERSION:-}

        after=$(mktemp)
        capture "$after"
        # Variables from the first source would survive an empty re-capture and
        # make a vanished torch look unchanged, so clear them explicitly.
        unset TORCH_OK TORCH_VERSION TORCH_FILE TORCH_NPU_VERSION \
              TORCH_ERROR TORCH_NPU_ERROR
        # shellcheck disable=SC1090
        . "$after"
        after_ok=${TORCH_OK:-no}
        after_v=${TORCH_VERSION:-}
        after_file=${TORCH_FILE:-}
        after_npu=${TORCH_NPU_VERSION:-}
        rm -f "$after"

        if [ "$before_ok" != yes ] && [ "$after_ok" != yes ]; then
            echo "[torch_guard] torch was absent before and after; nothing to compare" >&2
            exit 0
        fi
        if [ "$before_ok" = yes ] && [ "$after_ok" != yes ]; then
            echo "::error::torch is no longer importable after install: ${TORCH_ERROR:-unknown}"
            exit 1
        fi
        if [ "$before" != "$after_v" ] || [ "$before_file" != "$after_file" ]; then
            echo "::error::torch was replaced: ${before} (${before_file}) -> ${after_v} (${after_file})"
            echo "::error::the NPU build is gone; pin the offender in the install script or add an explicit torch constraint"
            exit 1
        fi
        if [ -n "$before_npu" ] && [ -z "$after_npu" ]; then
            echo "::error::torch_npu disappeared after install (was ${before_npu})"
            exit 1
        fi
        echo "[torch_guard] ok: torch ${after_v}; torch_npu ${after_npu:-n/a}" >&2
        ;;
    -h|--help|help)
        sed -n '18,26p' "$0"
        exit 0
        ;;
    *)
        echo "[torch_guard] usage: $0 {capture|verify} --file F" >&2
        exit 2
        ;;
esac
