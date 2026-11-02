#!/usr/bin/env python3
"""Verify the NPU runtime stack inside the L1 base image.

Runs inside the job container (see npu-smoke.yml step 2). Checks that the
preinstalled software matches what Dockerfile_aarch64 is expected to produce,
that the NPUs are reachable, and that every card can actually compute.

Exit code is non-zero when something fatal is wrong (missing NPU, broken
import, wrong matmul result). Purely informational findings are printed as
GitHub workflow warnings and do not fail the job.
"""

from __future__ import annotations

import platform
import sys

# Packages worth printing even though they are not imported here.
INFORMATIONAL_MODULES = ("fla_npu", "fla")


def _warn(message: str) -> None:
    print(f"::warning::{message}")


def report_versions() -> None:
    import torch
    import torch_npu
    import triton

    print("python:", sys.version.split()[0], "| machine:", platform.machine())
    print("torch :", torch.__version__, "| torch_npu:", torch_npu.__version__)
    # pip metadata reports the vanilla triton version (3.5.0) because triton-ascend
    # overwrites files inside triton/ without changing the dist metadata. The runtime
    # version is the only reliable signal, so printing it is intentional.
    print("triton:", triton.__version__, "(runtime; pip metadata differs by design)")

    for name in INFORMATIONAL_MODULES:
        try:
            print(f"{name}: {__import__(name).__version__}")
        except Exception as exc:  # noqa: BLE001 - informational only
            print(f"{name}: MISSING ({type(exc).__name__}: {exc})")


def check_numpy() -> None:
    """triton-ascend needs numpy 1.x, nothing in the repo pins it.

    numpy 1.26.4 only reaches the image transitively through triton-ascend, so a
    future rebuild could silently resolve to 2.x and break the NPU operator stack
    at kernel load time. Report it here rather than constraining it in the install
    step, because a pip constraint would install numpy into an image that lacks it
    and hide the problem instead of surfacing it.
    """
    try:
        import numpy
    except ImportError as exc:
        print(f"numpy: MISSING ({exc})")
        _warn("numpy not importable, triton-ascend kernels will fail to load")
        return

    print("numpy :", numpy.__version__)
    if int(numpy.__version__.split(".")[0]) >= 2:
        _warn(f"numpy {numpy.__version__} >= 2, triton-ascend operator stack may break")


def check_nomimalloc_tag() -> None:
    """Distinguish the self-built wheel from the PyPI one.

    "nomimalloc" is a hardcoded PYTORCH_BUILD_VERSION string, it proves the wheel
    came from image-ci, it cannot prove mimalloc is actually compiled out.
    """
    import torch

    if "nomimalloc" not in torch.__version__:
        _warn("no nomimalloc tag, image may not have been built by image-ci")


def check_npu_devices() -> int:
    import torch
    import torch_npu  # noqa: F401 - registers the npu backend

    available = torch.npu.is_available()
    count = torch.npu.device_count()
    print("npu   :", available, "| devices:", count)

    assert available, "torch.npu.is_available() == False, no NPU assigned to this job"
    assert count > 0, "torch.npu.device_count() == 0, no NPU assigned to this job"
    return count


def check_matmul(count: int) -> None:
    import torch

    for index in range(count):
        dev = f"npu:{index}"
        a = torch.randn(1024, 1024, device=dev, dtype=torch.float16)
        b = torch.randn(1024, 1024, device=dev, dtype=torch.float16)
        err = ((a @ b).float().cpu() - a.float().cpu() @ b.float().cpu()).abs().max().item()
        print(f"{dev} matmul ok, max_abs_err={err:.4f}")
        assert err < 1.0, f"{dev} unexpected matmul error err={err}"


def main() -> int:
    report_versions()
    check_numpy()
    check_nomimalloc_tag()
    count = check_npu_devices()
    check_matmul(count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
