#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 静态检查：不跑模型、不起 NPU，几十秒内把「结构性错误」挡住。

三项：

1. 语法编译      —— src / scripts / tests 全部 py_compile
2. workflow 解析 —— .github/workflows/*.yml 能被 YAML 解析，且有 on / jobs
3. 映射表漂移    —— 代码里 @register_model 的名字必须都在 model_map.tsv 里

第 3 项原本写在训练 workflow 的 ② 里，跑到那里才发现要白等一轮装依赖；
放到 L0 才能在 20 s 内报出来。训练侧仍保留同样的检查（那里是最终防线）。
"""

from __future__ import annotations

import glob
import json
import py_compile
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAP = ROOT / ".github" / "workflows" / "model_map.tsv"
PKG = "torchturbo"


def check_compile() -> list[str]:
    bad: list[str] = []
    for base in ("src", "scripts", "tests"):
        for path in sorted((ROOT / base).rglob("*.py")):
            try:
                py_compile.compile(str(path), doraise=True, cfile="/tmp/tt_pyc")
            except py_compile.PyCompileError as exc:
                bad.append(f"{path.relative_to(ROOT)}: {exc.msg.strip().splitlines()[-1]}")
    print(f"compile: {len(bad)} 个文件编译失败")
    return bad


def check_workflows() -> list[str]:
    try:
        import yaml  # noqa: PLC0415 - 可选依赖，缺了就降级
    except ImportError:
        print("::warning::没装 PyYAML，workflow 只做结构检查")
        yaml = None

    bad: list[str] = []
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        rel = path.relative_to(ROOT)
        if yaml is not None:
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            except Exception as exc:                       # noqa: BLE001
                bad.append(f"{rel}: YAML 解析失败: {type(exc).__name__}: {exc}")
                continue
            if not isinstance(doc, dict):
                bad.append(f"{rel}: 顶层不是 mapping")
                continue
            # YAML 1.1 会把裸 on: 解析成 True，两种都接受。
            if not any(k in doc for k in ("on", True)):
                bad.append(f"{rel}: 缺 on:")
            if "jobs" not in doc:
                bad.append(f"{rel}: 缺 jobs")
        else:
            text = path.read_text(encoding="utf-8")
            if "jobs:" not in text:
                bad.append(f"{rel}: 缺 jobs")
    print(f"workflows: {len(list((ROOT / '.github' / 'workflows').glob('*.yml')))} 个文件，{len(bad)} 个有问题")
    return bad


def check_model_map() -> list[str]:
    registered: set[str] = set()
    for path in sorted((ROOT / "src" / PKG / "models").glob("*/model.py")):
        src = path.read_text(encoding="utf-8")
        registered.update(re.findall(r'@register_model\(\s*["\']([^"\']+)["\']', src))

    mapped: set[str] = set()
    if MAP.exists():
        for line in MAP.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                mapped.add(line.split()[0])

    missing = sorted(registered - mapped)
    unknown = sorted(mapped - registered)
    print(f"model_map: registered={len(registered)} mapped={len(mapped)}")
    if unknown:
        # 只警告：先删代码后删表是正常顺序。
        print("::warning::映射表里有未注册的名字: " + ", ".join(unknown))
    return [f"model_map 未收录: {', '.join(missing)}"] if missing else []


CHECKS = {
    "compile": check_compile,
    "workflows": check_workflows,
    "map": check_model_map,
}


def main(argv: list[str]) -> int:
    # 训练侧只需要 map 那一项（它不跑全仓编译），其余调用方不带参数跑全部。
    only = ""
    if "--only" in argv:
        only = argv[argv.index("--only") + 1]

    picked = [CHECKS[only]] if only in CHECKS else list(CHECKS.values())
    problems = [p for check in picked for p in check()]
    if not problems:
        print("L0 OK")
        return 0
    for p in problems:
        print(f"::error::{p}")
    print(json.dumps({"problems": len(problems)}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
