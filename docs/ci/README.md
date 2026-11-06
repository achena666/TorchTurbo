# CI 体系

五个 workflow，一个必过检查（`gate`）。改动的路径决定跑哪几条泳道，其余一律跳过——
但**跳过必须带原因**，否则门禁判红。

```
pull_request ──► ci-gate-pr.yml
                   ├─ select   静态检查（编译 / workflow / 映射表 / 契约 / 选择器防篡改
                   │           + actionlint + hadolint）
                   │           然后算改动 → 出计划（泳道开关 + 文件清单）
                   │           两者**同一个 job、同一个 runner**
                   ├─ cpu-unit-tests   CPU pytest（affected 选中的文件 ×N 片）
                   ├─ npu-unit-tests   NPU pytest（一旦开跑就泳道内全跑）
                   ├─ npu-model-tests  E2E：一个族内小模型跑一步
                   └─ gate     唯一 required check

手动 ─────────────► ci-full.yml    全量（不选测；调试期唯一入口）
                    ci-image.yml   构建基础镜像
                    ci-pytest.yml  三条测试泳道的唯一入口（kind 选择）
                                       内部转发到 npu-model-tests.yml
```

## 拓扑：静态检查 + 计划算好，然后三条泳道并行

```
                            ┌─► cpu-unit-tests
select（静态检查 → 算计划） ─┼─► npu-unit-tests         ─► gate
                            └─► npu-model-tests
```

三条泳道**同为 `needs: select`**，谁也不依赖谁，同时启动。`gate` 的 `needs`
里第一个就是 `select` —— 这是「提前结束」通路：select 没过（静态检查挂 / 选择器
算不出计划）时，三条泳道的 `if` 全为假、一张卡都不开，而 gate 因为
`if: always()` 仍会等到那三条 `skipped` 落地后立刻给结论。**整链 ~20 s 结束**，
不是 ~14 min。

### 静态检查与算计划为什么在**同一个 job** 里

它们是严格串行的两个步骤：静态没过就不该算计划。拆成两个 job 只会：

- 白占一个 runner 名额（`CI_CPU_RUNNER` 是稀缺资源）；
- 多一次 `checkout`，而且计划那边还必须是 `fetch-depth: 0` 的全量拉取；
- 多一段 `needs` + `if` 的样板，每个条件都是一处可能写错的地方。

合成一个 job 之后，「静态没过就不算计划」是 **step 的天然语义**：前一 step 失败，
后面的直接不执行。一行 `needs` 都不用写。

代价只有一处：job 红了只看 `needs.select.result` 分不清倒在哪一步。所以最后
一步用 `if: always()` 把 `stage`（`ok` / `static` / `select` / `cancelled`）写进
job 输出，`gate.py` 用它决定摘要该写「静态检查未通过」还是「计划未算出」。

同时，`actionlint` / `hadolint` 是**无条件跑**的（早先靠 `ci_changed` /
`docker_changed` 判断，那两个输出已经删除）：为省一次 2 s 的二进制下载去复制
一份 diff 逻辑，等于给选择器开第二个实现——那正是本仓最想避免的漂移来源。

`check_static.py` 的 `contract` 检查守这个拓扑，写错会当场报错：

1. 跑 `check_static.py` 的 job 必须**同时**跑 `affected.py`（别拆成两个 job）；
2. 同一个 job 里 `check_static.py` 必须**排在** `affected.py` 之前；
3. 算计划的入口只能有一处（`--changed-from git` 不许出现两次）；
4. `l1` / `l2a` / `l2b` 的 `needs` 必须**恰好**是 `[select]`（并行，不许串起来）；
5. `gate.needs` 必须包含 `select`（提前结束通路）。

### 为什么是并行而不是串行

串行（`cpu-unit → npu-unit → npu-model`）看起来更省卡，但它只省下
「CPU 红时本来就不该开的卡」，而本仓 cpu-unit 目前有 37 个历史红的基线 —— 串行的实际效果是让每个 PR 都白等
~90 s，却省不到什么。并行后：

| 指标 | 串行 | 并行 |
|---|---|---|
| 最坏墙钟（实测） | 16+90+119+605 ≈ **830 s** | 16+605 ≈ **621 s** |
| 总卡·秒 | 5078 | **5078（完全相同）** |
| 峰值并发 | ≤ 8 卡 | 10 卡（2+8） |
| cpu-unit 红时 | NPU 卡不开 | NPU 已开，**白烧 5078 卡·秒** |

也就是说并行**不增加总卡消耗**，只抬高峰值并发。代价是 CPU 泳道红时 NPU 会白跑——
这是已知取舍，接受它是因为墙钟收益（~3.5 min/PR）更值钱。

注意 `l2b` 自己仍带 `max-parallel: 1`：那是约束 E2E **内部**多个画像串行
（NPU 池显存共享），与泳道间并行无关。

### gate 的判定

并行之后不再有「上游红了所以下游 skipped」这种因果，所以 `gate.py` 里也**不再
有** `cpu_bad` / `npu_bad` 这类传递判断。跳过只剩两种原因：

| 情形 | 结论 |
|---|---|
| 自己的 `*_run=false` | ⏭ 绿（写明原因） |
| 自己的 `*_run=true` 却是 skipped | ❌ `skipped-unknown`（真问题） |
| select 没过（静态 / 计划） | ❌ 红，三条泳道记「计划未算出，按设计未启动」 |

**当前（调试期）所有 workflow 都只手动触发**：`push` / `schedule` 已全部注释掉，
写在哪里、怎么恢复都留在原处。恢复后应该是：

```
PR ──────────────► ci-gate-pr.yml（select[静态+计划] -> {三条测试泳道} -> gate）
每晚 ─────────────► ci-full.yml（全量，不选测）
每晚 ─────────────► ci-image.yml（基础镜像，内容没变就跳过）
手动 ─────────────► ci-pytest.yml（kind 选一条泳道）
```


`ci-pytest.yml` 同时是 `workflow_call`（给门禁调用）与 `workflow_dispatch`
（给自己排查用）：**手动入口全仓只有一个**。合成一个文件而不是维护两个入口，
是因为**两边的默认值刚好相反**：

| | 调用方要的 | 手动要的 |
|---|---|---|
| 训练超时 | 45 min（截死就跑不完了） | 300 min（全量权重 255 GB） |
| 模型获取超时 | 跳过并判绿 | 判红（不能把网络问题当通过） |
| pytest 目标 | 选中的分片 | 一个目录 / node id |

拆成两个文件就得人肉维护一份重复的输入声明，而且两份会慢慢漂。
合成一个后，dispatch 的默认值只影响手动跑，调用方永远显式传值。

### 为什么 `npu-model-tests.yml` 又是单独一个文件

因为它**不是同一个问题**：单位测试的差别只在「在哪跑、跑哪些」，而模型测试
要取权重、减层、按卡数收敛 sp/ep、看门狗、超时收尾 —— 实现体比两条 unit 泳道
加起来还长。塞进 `ci-pytest.yml` 只会让「哪条泳道」和「训练怎么跑」这两件事
互相牵制。

所以分界线是：**`ci-pytest.yml` 负责「选哪条泳道」，`npu-model-tests.yml`
负责「模型测试怎么跑」**。前者用一个 `kind` 输入 + 一个 `uses:` 转发，
后者的输入原样透传；后者**没有** `workflow_dispatch`，避免默认值再次分叉。

`kind` 同时吸收了原来的 `use_container`：`cpu-unit` 一定裸机、`npu-unit` 一定
容器。少一个布尔输入，就少一类「cpu-unit 却给了镜像」的无效组合。

## 决策层：一个计划，三个开关

分支判断**只允许存在一处**，在 `scripts/ci/affected.py` 里。它输出一份计划：

```
cpu_unit_run=true   npu_unit_run=true   npu_model_run=false
cpu_unit_files=["tests/ops/cpu/test_x.py", ...]     shard_list=["0/5","1/5",...]
```

yaml 里**只读布尔值，不做任何判断**。没有 `if: contains(github.event.pull_request.labels...)`，
没有 `if: github.event_name == 'push'`，也没有 `if: needs.x.outputs.y != '[]'`。

向下游传「跑不跑」，不传「为什么跑」：一旦 yaml 也要理解路径语义，就出现了
第二份判断逻辑，两份必然漂，而且漂了不会报错——只会少跑。

三条泳道用**同一张模板**，只换中间那一行：

```yaml
if: |
  always() && needs.select.result == 'success' &&
  needs.select.outputs.npu_unit_run == 'true'
```

`always()` 是为了**不让 `needs` 的隐式跳过替我们决策**：select 失败时三条泳道
本该全部 skipped，但 gate 还要跑起来给结论；而只要写了 `always()`，就必须自己
显式拦住「计划没算出来」这一种，所以第一行永远是
`needs.select.result == 'success'`。

`gate.py` 拿同样的开关做判定：该跑却 `skipped` 是 `skipped-unknown`（红），
不该跑而 `skipped` 是 ⏭（绿）。两边用**同一组变量名**，否则摘要会给出
和实际执行相反的解释。

### 决策层契约由 select 里的静态检查看守

`check_static.py` 的 `contract` 项会**真的跑一次** `affected.py`，断言那四个
输出真的被写出来，再逐个 workflow 校验 `needs.select.outputs.X` 都已声明。

这类 bug 的特点就是「能跑、能绿」：拼错一个输出名 → GitHub 给空字符串 →
`== 'true'` 永假 → job 静默不跑 → gate 把它当成「不需要」。实测中它让选测
失效了好几天才被发现。

`selector` 项守另一头：`scripts/ci/**` 的改动必须触发全量。否则一个 PR
可以把自己的选择器改成「永远选 0 个测试」并且全绿——那就是自己给自己发通行证。

## 三条泳道怎么被选中

表中 ✅ = 该泳道会跑；「—」= `*_run=false`，job 直接 skipped（带原因）。
npu-unit 一旦开跑就是**泳道内全跑**（17 个含 `torch_npu` 的文件），不再往下筛。

| 改动 | 静态 | cpu-unit | npu-unit | npu-model |
|---|:--:|:--:|---|:--:|
| `src/torchturbo/ops/**` | ✅ | 选中的 CPU 文件 | **✅ 全跑** | 最便宜画像 |
| `src/torchturbo/models/<族>/**` | ✅ | 选中的 CPU 文件 | — | 该族画像 |
| `src/torchturbo/{engine,models/modules,parallel}/**` | ✅ | 选中的 CPU 文件 | — | 最便宜画像 |
| `src/torchturbo/adapters/**` | ✅ | 选中的 CPU 文件 | — | lf/swift 对应画像 |
| `src/torchturbo/runtime/**` | ✅ | 选中的 CPU 文件 | — | — |
| `docker/npu/**` | ✅ | 选中的 CPU 文件 | **✅ 全跑** | 最便宜画像 |
| `pyproject.toml` | ✅ | **全量** | **✅ 全跑** | 最便宜画像 |
| `.github/**`、`scripts/ci/**` | ✅ | **全量** | — | — |
| `scripts/runs/<dir>/**` | ✅ | `tests/scripts` | — | 该 `run_dir` 的模型 |
| `tests/**` | ✅ | affected | — | — |
| `docs/**`、`*.md` | ✅ | — | — | — |

cpu-unit 只跑选中的文件，所以「一个改文档的 PR」连 CPU 泳道都不开——静态那 15 s
就是它的全部成本。

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

fork PR 拿不到 secrets，而两条 NPU 泳道都需要 SWR 凭据。更关键的是：
**把 secrets 递到 fork 的代码上等于 RCE**，`pull_request_target` 就是这么出事的，
本仓库明确不用。

所以 fork PR 的 L2 被跳过，并在 Summary 里写 `⏭ 跳过 | fork PR（secrets 不递）`。
需要跑的话，由 maintainer 把 commit 推进本仓分支：

```bash
git fetch origin pull/123/head:ci/pr-123
git push origin ci/pr-123
gh workflow run ci-gate-pr.yml -F ref=ci/pr-123
```

或者直接手动跑 `ci-pytest.yml`（用 `kind` 选泳道）。

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

第一版在 `ci-pytest.yml` 里按 runner 标签分过组
（`ci-pytest-<label>`，`cancel-in-progress: false`），想的是"让手动跑和 PR 排同一个队"。
实测**这个做法会吃掉分片**：

> 同一个 concurrency group 里，GitHub 只保留**一个** pending 的 job；
> 新排进来的会把前一个 pending 的 cancel 掉（`cancel-in-progress: false` 只保证
> 不打断 *正在跑* 的那个）。

`ci-full` 第一次 run 就是这么挂的：5 个 cpu-unit 分片在 12:19:20 同时入队，
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

这个 bug 让 `ci-pytest.yml` 的**手动入口**完全不可用 —— 提交
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
python3 scripts/ci/check_static.py            # 静态六项（含跑一次选择器验契约）
python3 scripts/ci/affected.py --self-test    # 选测算法没退化（24 条断言）
python3 scripts/ci/affected.py --audit        # 覆盖体检
bash scripts/ci/pytest.sh                     # 真在本机跑一遍（MODE=cpu）
python3 scripts/ci/gate.py judge --mode pr    # 用手写的 env 验判定
python3 scripts/ci/image.py tag --dry-run     # 看当前内容的镜像 tag

# 某个改动会被怎么选（含四个泳道开关）
python3 scripts/ci/affected.py --changed-file src/torchturbo/engine/core.py --explain

# 装完依赖 torch 有没有被 pip 换掉
python3 scripts/ci/env.py capture --file /tmp/t.json
python3 scripts/ci/env.py check   --file /tmp/t.json
```

验泳道判定最快的方法（不跑 CI，只看 gate 怎么说）：

```bash
run() { env R_SEL=$1 SEL_STAGE=$2 R_CPU_UNIT=$3 R_NPU_UNIT=$4 R_NPU_MODEL=$5 \
  CPU_UNIT_RUN=$6 NPU_UNIT_RUN=$7 NPU_MODEL_RUN=$8 STRICT=false SUMMARY=t \
  python3 scripts/ci/gate.py judge --mode pr; }

run success ok      skipped skipped skipped false false false  # 改文档：什么都不跑
run success ok      success skipped skipped true  false false  # 改模型：cpu-unit
run success ok      skipped success success false true  true   # 改 ops：两条 NPU
run success ok      success success success true  true  true   # 全绿
run failure static  skipped skipped skipped true  true  true   # 静态倒：其余按设计未启动
run failure select  skipped skipped skipped true  true  true   # 计划没算出
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
