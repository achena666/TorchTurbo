# CI 辅助脚本

原来塞在 workflow heredoc 里的逻辑都搬到了这里。每个文件都能在本机直接跑——
这正是目的：之前的写法只能靠 push 上去、等一个 NPU pod 才能验证。

**按职责划分，一个文件回答一个问题**：

| 文件 | 回答的问题 | 子命令 |
|---|---|---|
| `affected.py` | 这次改动该跑什么？ | `--explain` / `--audit` / `--self-test` / `--changed-from git` |
| `gate.py` | 这些结果算不算通过？ | `timing` / `judge --mode pr\|full` |
| `check_static.py` | 语法 / workflow / 映射表对不对？ | `--only compile\|workflows\|map` |
| `pytest.sh` | 这个泳道的测试怎么跑？ | 环境变量驱动（`MODE` / `SHARD` / …） |
| `train.sh` | 一次训练冒烟分几步？ | `prepare` / `probe` / `install` / `run` / `report` |
| `model.py` | 模型目录能用吗？没有就弄一份 | `probe` / `find` / `mark` / `fetch` / `trim` |
| `env.py` | 这台 pod 现在是什么状态？ | `verify` / `capture` / `check` / `cleanup` |
| `image.py` | 基础镜像该不该建 / 怎么建 / 发什么引用 | `env` / `tag` / `build` / `verify` / `publish` / `resolve` / `summary` / `cleanup` |
| `result.py` | 事实 → 人看的摘要 | `init` / `set` / `phase` / `report` / `notice` / `junit` / `migrate-map` |
| `parallel_plan.py` | 卡数 → sp/ep | — |
| `train_supervisor.sh` | 训练进程有没有卡死 / 会不会留下 | — |
| `diag/` | 异常与 NaN 从哪来 | `sitecustomize` 钩子 |

被下面这些 workflow 调用，`.github/workflows` 里的入口只负责声明输入：

```
ci-pr.yml           PR 门禁   select -> {cpu-unit, npu-unit, npu-model} -> gate
ci-full.yml         main 全量（当前只手动）
ci-image.yml        构建基础镜像（**只手动 dispatch**）
ci-tests.yml        三条测试泳道的唯一入口（workflow_call + dispatch）
                    kind=cpu-unit / npu-unit / npu-model
ci-tests-model.yml  npu-model 的实现体（workflow_call，无手动入口）
model_map.json      模型 -> 启动脚本 / 权重 / 端到端画像（被上面几个读）
```

yaml 里只留「输什么、调哪个脚本」，超过 10 行的 bash 一律下沉到这里。
除语法高亮之外的好处是 **shellcheck / actionlint 能看**，而且改一行能在本机验。

### 为什么把三个文件合成一个

合并前先问一句：**它们是不是在回答同一个问题？**

| 合并 | 原来的问题 |
|---|---|
| `model_cache.sh` + `hf_fetch.py` + `flash_trim.py` → `model.py` | 三个都在回答「模型能不能用」，拆开后 probe 与 fetch 对「什么算完整」各写一遍 |
| `verify_env.py` + `torch_guard.sh` + `npu_cleanup.sh` → `env.py` | 三个都在回答「pod 现在什么状态」（体检 / 防破坏 / 复原） |
| `run_pytest.sh` + `junit_merge.py` → `pytest.sh` | 后者只被前者调用，而且只用了它的单分片路径 |

反过来，下面这些**不合并**，因为它们回答的是不同的问题：

* `affected.py`（选什么）与 `gate.py`（算不算过）—— 选错和判错是两类故障，
  合一起会让门禁自己变成黑盒。
* `train.sh`（编排）与 `train_supervisor.sh`（进程监督）—— 后者带 trap 语义，
  混进编排里会让人不敢改它。
* `result.py`（事实 → 文字）—— 其他脚本都向它写事实，它不该反过来依赖任何人。

## 约定

* 人看的日志走 **stderr**，机器读的 `KEY=value` 走 `--output`。调用方把它追加到
  `$GITHUB_OUTPUT`，或者直接 `source`。
* 事实写进**文件**，不写进 step outputs。step outputs 在步骤失败时是空的，
  而失败的 run 恰恰是唯一会被人翻出来看的那种。
* 跳过必须带原因。`affected.py` 的 skip 原因与 gate 的 `skipped-reason`
  是同一套语义：长期「无声跳过」会腐蚀 gate 的可信度。

## 本机校验

```bash
python3 scripts/ci/check_static.py          # 编译 + workflow + 映射表
python3 scripts/ci/affected.py --self-test  # 选测算法没退化
python3 scripts/ci/affected.py --audit      # 覆盖体检

bash -n scripts/ci/*.sh

# 看某个改动会被怎么选
python3 scripts/ci/affected.py --changed-file src/torchturbo/engine/core.py --explain

# 装依赖前后 torch 有没有被换掉
python3 scripts/ci/env.py capture --file /tmp/t.json
python3 scripts/ci/env.py check   --file /tmp/t.json

# 分片计划，不需要网络
python3 scripts/ci/affected.py --force-all --shard 0/5 --output /tmp/s.json

# 8 卡下的并行策略
python3 scripts/ci/parallel_plan.py --yaml scripts/runs/bailing_v3/sft.yaml \
  --cards 8 --out /tmp/tt.yaml --output /tmp/plan.out && cat /tmp/plan.out

# 模型目录完不完整（不需要网络）
python3 scripts/ci/model.py probe --dir /root/.cache/models/<repo>

# junit 计数
python3 scripts/ci/result.py junit --file /tmp/r.json --junit /tmp/pytest.xml

# 门禁判定（手写 env 模拟各种结果）
R_SEL=success SEL_STAGE=ok R_CPU_UNIT=success R_NPU_UNIT=skipped R_NPU_MODEL=skipped \
  WANT_NPU=false WANT_E2E='[]' python3 scripts/ci/gate.py judge --mode pr
```
