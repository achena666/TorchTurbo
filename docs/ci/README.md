# CI 体系

五个 workflow，一个必过检查（`gate`）。改动的路径决定跑哪几条泳道，其余一律跳过——
但**跳过必须带原因**，否则门禁判红。

```
pull_request ──► ci-gate-pr.yml
                   ├─ select   算改动 → 选测试 / 判 NPU / 判画像
                   ├─ L0       编译 + workflow 解析 + 映射表漂移 (+actionlint +hadolint)
                   ├─ L1       CPU pytest（affected 选中的，或全量 ×N 片）
                   ├─ L2a      NPU pytest（只要 torch_npu 的那批）
                   ├─ L2b      E2E：一个族内小模型跑一步
                   └─ gate     唯一 required check

手动 ─────────────► ci-full.yml    全量（不选测；调试期唯一入口）
                    ci-image.yml   构建基础镜像
                    ci-pytest.yml  跑指定 pytest 路径
                    ci-train.yml   训练一个模型
```

## 三条泳道是**串行门控**，不是并行

```
select ─► L0 ─► L1(CPU) ─► L2a(NPU 特性) ─► L2b(NPU 端到端)
```

用 `needs` 串起来，语义是**前面没过就不开后面的卡**：

| 前一级 | 后一级 | 被拦住时省下 |
|---|---|---|
| L1 CPU | L2a / L2b | 2 卡 ~2 min + 8 卡 ~10 min |
| L2a 特性 | L2b | 8 卡 ~10 min |

两点必须注意：

1. **`needs` 只保证"失败时不跑"**，不保证排队执行 —— L1 在 GH 托管机、
   L2a/L2b 在自建池，本来就不抢资源。串起来纯粹是省钱，不改善墙钟
   （CPU 那 ~90 s 会变成 NPU 的等待时间）。
2. **L2b 不能用 `needs: [select, l1, l2a]` 的隐式跳过**：L2a 在"本次不需要
   NPU 特性"时是 `skipped`，而 `needs` 里任何 skipped 都会把 L2b 一起带下去。
   但 E2E 与特性是两条独立判断（改 `engine/` 会起 E2E 却不起特性），所以
   L2b 用 `if: always() && ... needs.l2a.result != 'failure'` 自己写条件。

`gate.py` 对这个模式有专门的措辞：上游红了导致的下游 `skipped` 记成
"上游未通过，按串行链跳过"，而不是 `skipped-unknown`（后者意味着"没人知道
为什么没跑"，那才是真问题）。

**当前（调试期）所有 workflow 都只手动触发**：`push` / `schedule` 已全部注释掉，
写在哪里、怎么恢复都留在原处。恢复后应该是：

```
PR ──────────────► ci-gate-pr.yml（select -> L0 -> L1 -> L2a -> L2b -> gate）
每晚 ─────────────► ci-full.yml（全量，不选测）
每晚 ─────────────► ci-image.yml（基础镜像，内容没变就跳过）
手动 ─────────────► ci-pytest.yml / ci-train.yml
```


后两个同时是 `workflow_call`（给门禁调用）与 `workflow_dispatch`（给自己排查用）。
合成一个文件而不是维护两个入口，是因为**两边的默认值刚好相反**：

| | 调用方要的 | 手动要的 |
|---|---|---|
| 训练超时 | 45 min（截死就跑不完了） | 300 min（全量权重 255 GB） |
| 模型获取超时 | 跳过并判绿 | 判红（不能把网络问题当通过） |
| pytest 目标 | 选中的分片 | 一个目录 / node id |

拆成两个文件就得把人肉维护一份重复的输入声明，而且两份会慢慢漂。
合成一个后，dispatch 的默认值只影响手动跑，调用方永远显式传值。

## 三条泳道怎么被选中

| 改动 | L0 | L1 CPU | L2a NPU | L2b E2E |
|---|:--:|:--:|---|:--:|
| `src/torchturbo/ops/**` | ✅ | ✅ | **✅** | 最便宜画像 |
| `src/torchturbo/models/<族>/**` | ✅ | ✅ | — | 该族画像 |
| `src/torchturbo/{engine,models/modules,parallel}/**` | ✅ | ✅ | — | 最便宜画像 |
| `src/torchturbo/adapters/**` | ✅ | ✅ | — | lf/swift 对应画像 |
| `src/torchturbo/runtime/**` | ✅ | ✅ | — | — |
| `docker/npu/**` | ✅ | ✅ | **✅** | 最便宜画像 |
| `pyproject.toml` | ✅ | **全量** | **✅** | 最便宜画像 |
| `.github/**`、`scripts/ci/**` | ✅ | **全量** | — | — |
| `scripts/runs/<dir>/**` | ✅ | `tests/scripts` | — | 该 `run_dir` 的模型 |
| `tests/**` | ✅ | affected | — | — |
| `docs/**`、`*.md` | ✅ | — | — | — |

两张表对应两个脚本：路径 → 用例是 `scripts/ci/affected.py`，路径 → 画像在
`affected.py` 的 `pick_e2e()` 里，画像本身写在 `model_map.tsv` 第 6 列。

改 CI 自身为什么跑全量 CPU：这些脚本就是「怎么跑测试」本身，改错了会**静默少跑**，
只有全量能看出来。但它们不烧 NPU。

## 镜像：谁生产、谁消费

| | 做法 | 为什么 |
|---|---|---|
| 触发 | **只手动 dispatch**（`schedule` 已注释掉） | 一次 4~5 h / 几十 GB；镜像变了是最难排查的问题，先不给它自动改的机会 |
| 要不要建 | 内容指纹 tag 已存在 → 跳过 | 没改动时整轮 2~3 分钟，而不是 5 小时 |
| tag | `base-<年周>-c<docker/npu 内容哈希 8 位>` | 同名同内容，不靠外部状态就能判断「建过没」 |
| 年周 | `2026w45` | 每周强制重建一次，才能发现 cann / pip 上游漂移 |
| PR 用哪个镜像 | **固定链接 `…:base-latest`**（暂不接 artifact） | 「PR 悄悄换了一个镜像」是最难排查的一类问题 |

构建成功后会把 `{ref, tag, digest}` 写成 `base-image-ref` artifact（保留 90 天）。
**生产开着、消费暂关**：`ci-image.yml` 照旧发布，但 PR / 全量的 `select`
不读它，只用上面的固定链接。要接上时把 `--repo "$GITHUB_REPOSITORY"` 加回
`image.py resolve` 那一行即可（解析顺序：人工覆盖 → artifact → 固定链接）。
**整条链路不需要任何长期凭据** —— `gh variable set` 对 `GITHUB_TOKEN` 是 403，
所以才走 artifact。

需要立刻重建（比如怀疑基础镜像坏了）：dispatch `ci-image.yml` 并勾 **force_rebuild**。
想临时切镜象排查：设仓库变量 `BASE_IMAGE_TAG`，它优先于固定链接。

## 五个 fail-open 阀门

任一命中即全量（`mode=all`）：

1. 选择器异常 / 非 0 退出
2. 改动文件为空（`merge-base` 失败、浅克隆没历史……）
3. `ci:full` 标签（或 `workflow_dispatch` 的 `force_all`）
4. 手动全量（`ci-full.yml` 只走全量）；`push` / `schedule` 当前都不触发 CI
5. 改动 **> 300 文件**

第 5 条的方向是审过的：**大 PR 该跑更多测试，不是更少**。改 1000 个文件的 PR
往往是大重构，正是全量最能抓问题的时候；全量分片后也就 8~10 min。

## 标签

| 标签 | 效果 |
|---|---|
| `ci:full` | 三条泳道全量，E2E 跑全部画像 |
| `ci:e2e` | 路径没命中时也强制跑 L2（起 NPU + 最便宜画像） |
| `ci:e2e:strict` | E2E 因模型超时跳过时**判红**；默认是「跳过但绿」 |
| `ci:skip-npu` | 强制关掉 L2（hotfix 用） |

## fork PR 的 L2 不自动跑

fork PR 拿不到 secrets，而 L1 之外的两条泳道都需要 SWR 凭据。更关键的是：
**把 secrets 递到 fork 的代码上等于 RCE**，`pull_request_target` 就是这么出事的，
本仓库明确不用。

所以 fork PR 的 L2 被跳过，并在 Summary 里写 `⏭ 跳过 | fork PR（secrets 不递）`。
需要跑的话，由 maintainer 把 commit 推进本仓分支：

```bash
git fetch origin pull/123/head:ci/pr-123
git push origin ci/pr-123
gh workflow run ci-gate-pr.yml -F ref=ci/pr-123
```

或者直接手动跑 `ci-pytest.yml` / `ci-train.yml`。

## 时间预算

不追求极限，只要求**不失控**。

| 档位 | 阈值 | 违反时 |
|---|---:|---|
| 目标墙钟 | ≤ 25 min | `::warning::` |
| 告警线 | > 35 min | 告警 |
| 硬上限 | 45 min（`timeout-minutes`） | job 被杀，gate 红 |

`queue`（排队）与 `wall`（执行）分开报：只看总和无法区分「该扩容」和「该优化步骤」。

超时只负责**拦住卡死**，不负责卡住正常但较慢的运行——所以 step 级超时给到实测值的
3~4 倍，而不是贴着实测值。

超时之后一定有收尾（`if: always()` → `scripts/ci/env.py cleanup`）：被杀掉的训练会
留下占卡的子进程、没释放的显存和半个 checkpoint，下个 job 会以完全无关的方式失败。

## NPU 池串行：不要用 job 级 `concurrency`

第一版在 `ci-pytest.yml` / `ci-train.yml` 里按 runner 标签分过组
（`ci-pytest-<label>`，`cancel-in-progress: false`），想的是"让手动跑和 PR 排同一个队"。
实测**这个做法会吃掉分片**：

> 同一个 concurrency group 里，GitHub 只保留**一个** pending 的 job；
> 新排进来的会把前一个 pending 的 cancel 掉（`cancel-in-progress: false` 只保证
> 不打断 *正在跑* 的那个）。

`ci-full` 第一次 run 就是这么挂的：5 个 L1 分片在 12:19:20 同时入队，
2/3/4 在 1 秒后全部变成 `cancelled`，只剩 0 和 1 真的跑了。

所以现在两个可复用泳道里**没有任何 `concurrency`**。串行本来就由 runner 提供：
self-hosted runner 忙着的时候，下一个 job 天然停在 `queued`。要给某个池限并发，
应该去 ARC / runner group 那一层做，而不是在被调用的 workflow 里。

workflow 级 `concurrency` 仍然保留在三个入口文件里（`ci-pr-*` / `ci-full-*` /
`image-ci-base`），那里语义是"同一条 ref 的新 run 顶掉旧的"，是对的。

## `timeout-minutes` 必须包 `fromJSON`

`workflow_dispatch` 里声明成 `type: number` 的输入，实际传进 `inputs` 的是**字符串**
（`"30"`，不是 `30`）。直接写

```yaml
timeout-minutes: ${{ inputs.timeout_minutes || 60 }}
```

在 dispatch 触发时求值结果是字符串 `"30"`，而 `timeout-minutes` 只接受 number。
GitHub 的反应不是报错，而是**把这个 job 从 run 里整个丢掉**：run 页面只看得到
另一个被 skip 的 job，结论是 failure，没有任何提示。很容易被误判成"runner 没起来"。

写法必须是：

```yaml
timeout-minutes: ${{ fromJSON(inputs.timeout_minutes || 60) }}
```

`fromJSON` 对 number 和字符串两种输入都成立（number 输入在 `workflow_call` 下
仍然走这条路，实测同样正常），所以两条触发链可以用同一份表达式。

这个 bug 让 `ci-pytest.yml` / `ci-train.yml` 的**手动入口**完全不可用 —— 提交
`f2acd5d` 之后的三次 dispatch 全是这个原因。定位过程：二分探针（`applied` 过的
`zz-probe*.yml` 已删除），`p`/`q` 两个不带 `timeout-minutes` 的 job 正常创建，
而带表达式的 `r` 直接消失。

## 需要配的东西

| 名称 | 类型 | 必需 | 用途 |
|---|---|:--:|---|
| `SWR_USERNAME` / `SWR_PASSWORD` | secret | ✅ | 拉私有基础镜像；image CI 推送 |
| `BASE_IMAGE_TAG` | variable | 可选 | **人工覆盖**镜像引用，平时不用设（不设就用固定链接） |
| `CI_CPU_RUNNER` | variable | 可选 | 默认 `ubuntu-22.04-arm` |
| `CI_NPU_RUNNER` | variable | 可选 | 默认 `linux-aarch64-a3-800i-2` |
| `CI_E2E_RUNNER` | variable | 可选 | 默认 `linux-aarch64-a3-800i-8` |

## 本地怎么验

```bash
python3 scripts/ci/check_static.py            # L0 三项
python3 scripts/ci/affected.py --self-test    # 选测算法没退化
python3 scripts/ci/affected.py --audit        # 覆盖体检
bash scripts/ci/pytest.sh                     # 真在本机跑一遍（MODE=cpu）
python3 scripts/ci/gate.py judge --mode pr    # 用手写的 env 验判定
python3 scripts/ci/image.py tag --dry-run     # 看当前内容的镜像 tag

# 某个改动会被怎么选
python3 scripts/ci/affected.py --changed-file src/torchturbo/engine/core.py --explain

# 装完依赖 torch 有没有被 pip 换掉
python3 scripts/ci/env.py capture --file /tmp/t.json
python3 scripts/ci/env.py check   --file /tmp/t.json
```

## 一个刻意的取舍

`ci-pytest.yml` 的**裸机**泳道不用 `pip install -e ".[dev]"`，而是先装
「可用版本」的 torch，再 `--no-deps` 装本包。

原因：`pyproject` 钉的是 `torch>=2.10.0`（NPU 镜像里那套），GH 托管机只能从 PyPI 拿
arm64 CPU wheel。钉死了装不上，放开了又和镜像不一致——所以裸机泳道只承诺
「纯 python 逻辑能跑」，精度相关的验证交给容器泳道。

## 实测记录

`tmp_record/` 里是当初定这些数字用的原始日志（镜像拉取 7.4 MiB/s、训练 163 s 等）。
它们原来放在 `.github/workflows/` 下，改个 md 就会触发整轮 CI，所以搬到了这里。
