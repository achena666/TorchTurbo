"""测试目录的共享配置。

只做一件事：把「需要 NPU」的测试标出来，并且在没装 torch_npu 的机器上
**根本不去收集它们**。

为什么不能只靠 `-m "not npu"`：marker 过滤发生在收集之后，而收集阶段就会
import 模块；这些文件顶部写着 `import torch_npu`，在没有 NPU 的 CPU 机器上
会直接 ImportError，导致整个分片红掉——那不是测试失败，是环境不存在。

判据是「文件里出现 torch_npu」而不是路径：测试按目录摆得很杂（tests/ops/npu、
tests/models/qwen4_exp、tests/ops/providers/npu ...），按目录列表一定会漏，
而按导入判永远不会漏。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_CACHE: dict[Path, bool] = {}


def _has_torch_npu() -> bool:
    # find_spec 不真正 import，比导入一次快得多，也不会触发 NPU 初始化。
    return importlib.util.find_spec("torch_npu") is not None


def _is_npu_test(path: Path) -> bool:
    if path.suffix != ".py" or not path.name.startswith("test_"):
        return False
    hit = _CACHE.get(path)
    if hit is None:
        try:
            hit = "torch_npu" in path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            hit = False
        _CACHE[path] = hit
    return hit


def pytest_ignore_collect(collection_path: Path = None, **kwargs):  # type: ignore[no-untyped-def]
    """没有 torch_npu 时跳过整个文件，连 import 都不发生。"""
    if collection_path is None or _has_torch_npu():
        return None
    return True if _is_npu_test(Path(collection_path)) else None


def pytest_collection_modifyitems(config, items):  # type: ignore[no-untyped-def]
    """给 NPU 用例打 marker，让 `-m npu` / `-m "not npu"` 能分流。"""
    for item in items:
        path = Path(str(getattr(item, "path", item.fspath)))
        if _is_npu_test(path):
            item.add_marker(pytest.mark.npu)
