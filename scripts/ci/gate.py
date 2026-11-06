#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门禁判定与计时 —— PR 与 nightly 共用一份。

两边的规则本来几乎一样，但抄成了两段 bash，于是「fork PR 的 L2 算不算合法跳过」
只在一个文件里修对过。收进这里之后两边不可能再分叉。

    gate.py timing   排队 / 执行 / 端到端，写进 Job Summary（不参与判定）
    gate.py judge    逐泳道判定，退出码即门禁结论

判定只用两条规则：

    required  跑了就必须成功。skipped 说明上游已经挂了 —— **包括 select 自己失败**。
              这一条是整套设计的核心：让「慢慢把 L2 关掉」必须显式改代码，
              而不是某个配置漂了之后无人察觉。
    optional  跳过可以，但必须是因为「本来就该跳过」。原因不对就是
              skipped-unknown，判红。

fork PR 单独处理：secrets 不递给 fork，L2 必然起不来。那是设计，不是故障，
所以认成合法跳过并写明理由（禁止 pull_request_target：secrets + fork 代码 = RCE）。

退出码：0 通过，1 未通过。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone

OK, SKIP, BAD = "✅ 通过", "⏭ 跳过", "❌"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


def _summary(markdown: str) -> None:
    path = _env("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown)
    else:
        print(markdown)


# ---------------------------------------------------------------- timing
def timing() -> int:
    """排队才是隐形杀手：执行 8 min 但排队 40 min，作者感受是 48 min。
    所以 queue 与 wall 分开报，且都不参与判定 —— 它们是扩容证据，不是失败。"""
    repo, run_id = _env("GITHUB_REPOSITORY"), _env("GITHUB_RUN_ID")
    if not (repo and run_id):
        print("::warning::缺 GITHUB_REPOSITORY / GITHUB_RUN_ID，跳过计时")
        return 0

    proc = subprocess.run(
        ["gh", "api", f"repos/{repo}/actions/runs/{run_id}",
         "--jq", r'"\(.created_at) \(.run_started_at)"'],
        capture_output=True, text=True,
    )
    stamps = proc.stdout.strip()
    if proc.returncode != 0 or not stamps:
        print("::warning::拿不到 run 时间戳（token 缺 actions:read？）")
        return 0

    def parse(text: str) -> datetime:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))

    created_s, started_s = stamps.split()
    created, started = parse(created_s), parse(started_s)
    now = datetime.now(timezone.utc)
    _summary(
        "\n| 计时 | 秒 |\n|---|---:|\n"
        f"| queue（排队） | {int((started - created).total_seconds())} |\n"
        f"| wall（执行） | {int((now - started).total_seconds())} |\n"
        f"| 端到端 | {int((now - created).total_seconds())} |\n"
    )
    return 0


# ---------------------------------------------------------------- judge
class Report:
    def __init__(self) -> None:
        self.rows: list[str] = []
        self.failed = False

    def required(self, name: str, result: str) -> None:
        if result == "success":
            self.rows.append(f"| {name} | {OK} |  |")
        elif result == "skipped":
            self.rows.append(f"| {name} | {BAD} 未执行（预期应执行） |  |")
            self.failed = True
        else:
            self.rows.append(f"| {name} | {BAD} {result} |  |")
            self.failed = True

    def optional(self, name: str, result: str, *, expect_skip: bool, why: str) -> None:
        if result == "success":
            self.rows.append(f"| {name} | {OK} |  |")
        elif result == "skipped" and expect_skip:
            self.rows.append(f"| {name} | {SKIP} | {why} |")
        elif result == "skipped":
            # 「不知道为什么没跑」比「跑失败了」更危险：它会安静地变成常态。
            self.rows.append(f"| {name} | {BAD} skipped-unknown |  |")
            self.failed = True
        else:
            self.rows.append(f"| {name} | {BAD} {result} |  |")
            self.failed = True

    def note(self, name: str, value: str) -> None:
        self.rows.append(f"| {name} | {value} |  |")

    def render(self, title: str, summary: str) -> None:
        _summary(
            f"## {title}\n\n| 泳道 | 结果 | 备注 |\n|---|---|---|\n"
            + "\n".join(self.rows)
            + f"\n\n选择：`{summary or 'unknown'}`\n"
        )


def judge_pr() -> int:
    rep = Report()
    results = {k: _env(f"R_{k}") for k in ("SEL", "L0", "L1", "L2A", "L2B")}
    # 决策层给的四个开关：「这个泳道本次该不该跑」。
    # 拿不到（旧调用方式 / 手敲测试）时按 true 处理，也就是宁可错判红。
    want = {k: _env(f"{k}_RUN", "true") for k in ("L0", "L1", "L2A", "L2B")}

    rep.required("L0 静态", results["L0"])

    # L1 在「本次没选中任何 CPU 测试」时本来就该 skipped，那不是故障。
    if want["L1"] == "false" and results["L1"] == "skipped":
        rep.rows.append(f"| L1 CPU | {SKIP} | 未选中 CPU 测试 |")
    else:
        rep.required("L1 CPU", results["L1"])

    # 三条泳道是串行门控（L0 -> L1 -> L2a -> L2b）。上游红了，下游一定 skipped，
    # 那是「按设计没跑」，不是「不知道为什么没跑」—— 否则摘要会在真正的原因
    # （L1 红）旁边再挂一个 skipped-unknown，把人引到错的地方去。
    # 只影响措辞，不影响结论：上游本身红了这一轮仍然是失败。
    cpu_bad = results["L0"] not in ("success", "skipped") or \
        results["L1"] not in ("success", "skipped")
    npu_bad = cpu_bad or results["L2A"] not in ("success", "skipped")

    if _env("IS_FORK") == "true":
        # fork PR 的 L2 不执行是设计，不是故障。
        for name, key in (("L2a NPU", "L2A"), ("L2b E2E", "L2B")):
            res = results[key]
            if res == "skipped":
                rep.rows.append(f"| {name} | {SKIP} | fork PR（secrets 不递） |")
            elif res == "success":
                rep.rows.append(f"| {name} | {OK} |  |")
            else:
                rep.rows.append(f"| {name} | {BAD} {res} |  |")
                rep.failed = True
    else:
        rep.optional(
            "L2a NPU", results["L2A"],
            expect_skip=(want["L2A"] != "true") or cpu_bad,
            why="上游未通过，按串行链跳过" if cpu_bad
            else "未选中需要真卡的测试文件",
        )
        rep.optional(
            "L2b E2E", results["L2B"],
            expect_skip=(want["L2B"] != "true") or npu_bad,
            why="上游未通过，按串行链跳过" if npu_bad else "无匹配画像",
        )

    # 跑起来了但自己宣告跳过（模型获取超时）：记 skipped-reason，
    # 默认判绿，只有 ci:e2e:strict 才判红。
    skip_reason = _env("SKIP_REASON")
    if skip_reason and results["L2B"] == "success":
        rep.note("L2b 跳过原因", f"⚠️ {skip_reason}")
        if _env("STRICT") == "true":
            rep.failed = True

    sel = results["SEL"]
    rep.note("select", OK if sel == "success" else f"{BAD} {sel}")
    if sel != "success":
        rep.failed = True

    rep.render("CI 结果", _env("SUMMARY"))
    print(f"::notice title=ci::{_env('SUMMARY') or 'select-failed'}")
    if rep.failed:
        print("::error::门禁未通过")
        return 1
    print("门禁通过")
    return 0


def judge_full() -> int:
    rep = Report()
    req = {k: _env(f"R_{k}") for k in ("SEL", "L0", "L1", "L2A")}
    want = {k: _env(f"{k}_RUN", "true") for k in ("L1", "L2A", "L2B")}

    rep.required("select", req["SEL"])
    rep.required("L0 静态", req["L0"])

    # 全量模式下三条泳道都应该跑（force-all）；但保留 lane 判断，
    # 万一将来全量也做了裁剪，这里不用再改。
    if want["L1"] == "false" and req["L1"] == "skipped":
        rep.rows.append(f"| L1 CPU | {SKIP} | 未选中 CPU 测试 |")
    else:
        rep.required("L1 CPU", req["L1"])

    # 串行门控后，上游红了下游一定是 skipped。这里不重复记一笔「未执行」，
    # 只把真正的红标出来，免得摘要里同一件事说两遍。
    upstream_failed = any(
        req[k] not in ("success", "skipped") for k in ("SEL", "L0", "L1")
    )
    if upstream_failed and req["L2A"] == "skipped":
        rep.rows.append(f"| L2a NPU | {SKIP} | 上游未通过，按串行链跳过 |")
    else:
        rep.required("L2a NPU", req["L2A"])

    # 全量里 E2E 也可能因为「今天没有画像要跑」而跳过，属正常。
    rep.optional(
        "L2b E2E", _env("R_L2B"), expect_skip=True,
        why="上游未通过，按串行链跳过" if upstream_failed else "无匹配画像",
    )

    rep.render("main 全量结果", _env("SUMMARY"))
    print(f"::notice title=ci-full::{_env('SUMMARY') or 'select-failed'}")
    if rep.failed:
        print("::error::全量未通过")
        return 1
    print("全量通过")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="门禁判定与计时")
    ap.add_argument("command", choices=["timing", "judge"])
    ap.add_argument("--mode", choices=["pr", "full"], default="pr")
    args = ap.parse_args(argv)

    if args.command == "timing":
        return timing()
    return judge_pr() if args.mode == "pr" else judge_full()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
