#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 静态检查：不跑模型、不起 NPU，几十秒内把「结构性错误」挡住。

五项：

1. 语法编译      —— src / scripts / tests 全部 py_compile
2. workflow 解析 —— .github/workflows/*.yml 能被 YAML 解析，且有 on / jobs
3. 映射表漂移    —— 代码里 @register_model 的名字必须都在 model_map.tsv 里
4. 决策层契约    —— workflow 读的 `needs.select.outputs.X` 必须真的被声明。
   这一条踩过：`affected.py` 算了 `targets` 却没人读，`select` 输出的是
   全部 144 个文件，选测静默失效了好几天。现在只要有一边拼错就红。
5. 选择器防篡改  —— `scripts/ci/**` 改动必须走全量（`ci-config-changed` 阀门）。
   否则一个 PR 可以把自己的选择器改成「永远选 0 个测试」并且全绿。

第 3 项原本写在训练 workflow 的 ② 里，跑到那里才发现要白等一轮装依赖；
放到 L0 才能在 20 s 内报出来。训练侧仍保留同样的检查（那里是最终防线）。
"""

from __future__ import annotations

import glob
import json
import py_compile
import re
import subprocess
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


def check_contract() -> list[str]:
    """workflow 读的 select 输出必须真的被声明；决策层必须能算出来。

    这一类错「能跑、能绿、只是少跑了东西」，比直接报错难发现得多：
    拼错一个 output 名，GitHub 给空字符串，`if: ... == 'true'` 永假 →
    job 静默不跑，gate 又把它当成「不需要」。
    """
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        print("contract: 跳过（没装 PyYAML）")
        return []

    bad: list[str] = []
    needed = ("l0_run", "l1_run", "l2a_run", "l2b_run", "l1_files")

    # 1. 决策层真的会写这些 key 吗 —— 直接跑一次，别猜源码长什么样。
    #    这是唯一能同时挡住「忘了写」和「写错名字」的办法。
    out = Path("/tmp/tt_lane_probe.txt")
    out.unlink(missing_ok=True)
    rc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ci" / "affected.py"),
         "--changed-file", "pyproject.toml", "--github-output", str(out)],
        capture_output=True, text=True,
    ).returncode
    if rc != 0:
        bad.append(f"affected.py 跑不通（rc={rc}），决策层不可用")
    else:
        written = {
            line.split("=", 1)[0]
            for line in out.read_text(encoding="utf-8").splitlines() if "=" in line
        }
        for key in needed:
            if key not in written:
                bad.append(f"affected.py 没有产出 {key}（工作流会拿到空字符串 → 静默不跑）")

    # 2. 每个 workflow：读到的 needs.select.outputs.X 必须已声明
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        doc = yaml.safe_load(text) or {}
        select = (doc.get("jobs") or {}).get("select") or {}
        declared = set((select.get("outputs") or {}).keys())

        used = set(re.findall(r"needs\.select\.outputs\.([A-Za-z0-9_]+)", text))
        for name in sorted(used - declared):
            bad.append(f"{rel}: 读了 needs.select.outputs.{name}，但 select 没声明它")

        # merge-base 需要历史；fetch-depth: 1 会让 diff 失败 → 静默退回全量。
        if "--changed-from git" in text and "fetch-depth: 0" not in text:
            bad.append(f"{rel}: 用了 --changed-from git 但 checkout 没设 fetch-depth: 0")

    print(f"contract: {len(needed)} 个 lane 输出 / {len(bad)} 个问题")
    return bad


def check_selector_guard() -> list[str]:
    """选择器改了自己必须触发全量，否则 PR 能靠改选择器「过」。

    `ci-config-changed` 是唯一拦住这个的口子：它把 `.github/**` 与
    `scripts/ci/**` 的改动一律当全量。
    """
    src = (ROOT / "scripts" / "ci" / "affected.py").read_text(encoding="utf-8")
    bad: list[str] = []
    if "ci-config-changed" not in src:
        bad.append("affected.py 丢了 ci-config-changed 阀门：改选择器不再触发全量")
    for prefix in (".github/", "scripts/ci/"):
        if prefix not in src:
            bad.append(f"affected.py 的 ci-config-changed 没覆盖 {prefix}")
    print(f"selector: {len(bad)} 个问题")
    return bad


CHECKS = {
    "compile": check_compile,
    "workflows": check_workflows,
    "map": check_model_map,
    "contract": check_contract,
    "selector": check_selector_guard,
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
