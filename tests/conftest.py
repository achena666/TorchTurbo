"""测试目录的共享配置。

只做两件事：把「需要真 NPU 卡」的测试标出来，并且在没装 torch_npu 的
机器上**不去收集它们**。

为什么不能只靠 `-m "not npu"`：marker 过滤发生在收集之后，而收集阶段就会
import 模块；需要卡的这些文件会在 import / 建图阶段失败，导致整个分片红掉
—— 那不是测试失败，是环境不存在。

判据不写在这里：`scripts/ci/npu_rule.py` 是唯一一份，`affected.py`
（选泳道）、`pytest.sh`（glob 后过滤）也调它。原先三处各写一遍「文件里含
torch_npu」，那份规则两个方向都错 —— 注释里提了一句 torch_npu 的纯 CPU
测试被拖上卡，而要 `fla_npu` / `torch.npu` 的文件两条泳道都不跑。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_CACHE: dict[Path, bool] = {}


def _load_rule():
    """按路径加载 scripts/ci/npu_rule.py。

    不用 `import npu_rule`：pytest 的 sys.path 只有仓库根，scripts/ci 不是包。
    万一脚本不在（比如只拷了 tests/ 的独立环境），退回一个保守实现：
    宁可多跳过一个文件，也不要在没卡机器上炸掉整个分片。
    """
    rule_path = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "npu_rule.py"
    if rule_path.exists():
        spec = importlib.util.spec_from_file_location("ci_npu_rule", rule_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    class _Fallback:
        @staticmethod
        def needs_device(path: Path) -> bool:
            text = path.read_text(encoding="utf-8", errors="replace")
            return any(token in text for token in ("torch_npu", "torch.npu", "fla_npu"))

    return _Fallback


_RULE = _load_rule()


def _has_torch_npu() -> bool:
    # find_spec 不真正 import，比导入一次快得多，也不会触发 NPU 初始化。
    return importlib.util.find_spec("torch_npu") is not None


def _is_npu_test(path: Path) -> bool:
    if path.suffix != ".py" or not path.name.startswith("test_"):
        return False
    hit = _CACHE.get(path)
    if hit is None:
        hit = _RULE.needs_device(path)
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
