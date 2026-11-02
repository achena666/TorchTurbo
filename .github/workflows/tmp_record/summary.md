# 一、构建镜像CI

资源列表

| 资源类型 | runner名 | 是否可用 | 作用 |
|---|---|---|---|
| CPU |  ubuntu-22.04-arm | 是 |  构建镜像(pull基础镜像，push turbo镜像) |
| CPU |  linux-aarch64-cpu-x(4\24) | 否 | 构建镜像(pull基础镜像，push turbo镜像) |
| NPU |  linux-aarch64-a3-800i-x(2\4\6\8) | 是 | 测试用例 NPU计算 |


该部分使用CPU资源，分为三步：

1. pull cann基础镜像
2. build cann镜像为turbo镜像
3. push 镜像，以供后续PR CI使用

**pull image**

| 资源 | 阶段 | 网速 | 时间 |
|---|---|---|---|
|linux-aarch64-cpu-4 | FROM quay.io/ascend/cann:9.0.0-a3-openeuler24.03-py3.11|  <90Mb/s | 2min 12s |
| linux-aarch64-cpu-4| swr.cn-north-12.myhuaweicloud.com/ascend/torchturbo_npu:base-latest | <4Mb/s  | 14min |


**build image**

| 资源 | 阶段 | 耗时 | 占比 |
|---|---|---|---|
|linux-aarch64-cpu-4 | `#10` 编译 fla-npu（Ascend 算子） | **84.7 min** | 40.5% |
| linux-aarch64-cpu-4| `#12` 源码编译 PyTorch 2.10.0 | **68.1 min** | 32.5% |
| linux-aarch64-cpu-4| `#17` 导出并推送镜像到 SWR | **50.1 min** | 23.9% |
| linux-aarch64-cpu-4| 其余全部（拉基础镜像 + pip + 收尾） | 6.9 min | 3.1% |

**push image**

| 资源| 子阶段 | 耗时 |
|---|---|---|
| linux-aarch64-cpu-4| `exporting layers`（本地写盘） | 47.6 s |
| linux-aarch64-cpu-4 | **`pushing layers`（上传 SWR）** | **2951.6 s ≈ 49.2 min** |
| linux-aarch64-cpu-4 | push manifest（两个 tag） | 2.9 s + 0.5 s |


# 二、PR CI

该部分使用NPU pod，分为两步：

1. pull turbo镜像

2. 执行测试用例

**pull turbo镜像**

| 资源 | 阶段 | 网速 | 时间 |
|---|---|---|---|
|linux-aarch64-a3-800i-8 | FROM quay.io/ascend/cann:9.0.0-a3-openeuler24.03-py3.11|  | 42min |
| linux-aarch64-a3-800i-8| FROM 	swr.cn-south-1.myhuaweicloud.com/ascendhub/cann:9.0.0-a3-openeuler24.03-py3.11| | 3min 12s  |

---

# 三、base 镜像构建

产物：`swr.cn-north-12.myhuaweicloud.com/ascend/torchturbo_npu:base-latest`（6.06 GiB）

## 3.1 构建包与引入包

| # | 构建包 | 引入包 | 风险 | 风险说明 |
|---|---|---|---|---|
| 1 | `FROM cann:9.0.0-a3` | CANN 9.0.0 toolkit、openEuler 24.03、Python 3.11 | 低 | tag 未锁 digest |
| 2 | `dnf install` ×15 | cmake、gcc、g++、make、git、curl、nfs-utils、numactl-devel、openblas-devel、patch、tmux、unzip | 低 | 无版本约束；与 #9 的 pip cmake 两套并存 |
| 4 | `triton==3.5.0` | — | 低 | 钉死 |
| 5 | `triton-ascend==3.2.2` | numpy 1.26.4、scipy 1.13.1、pandas 3.0.6、**pytest 8.3.2**、pytest-xdist、pybind11、attrs、decorator、psutil 等 | **中** | 传递依赖全浮动；覆盖 `triton/` 致元数据失真；是 numpy 1.26.4 的唯一钉子 |
| 6 | `torch_npu==2.10.0.post2` | **torch==2.10.0（PyPI 版）** | **低** |  |
| 7 | fla-npu 源码编译（**84.7 min**） | pyyaml 6.0.3、numpy、sympy、scipy、protobuf、expecttest、packaging | **低** | 已确认commit |
| 8 | `flash-linear-attention@954438d1` | **transformers 5.22.0**、einops 0.8.2、huggingface-hub、tokenizers 0.23.3、safetensors 0.8.0、regex、tqdm、rich、typer 等 20+ 包 | **高** | `transformers>=4.45.0` 无上界；依赖在 Dockerfile 中不可见；若要求 numpy>=2 会顶掉 1.26.4 |
| 9 | PyTorch v2.10.0 源码编译（**68.1 min**）<br>必需：aarch64 下 CMakeLists 用裸 `set(USE_MIMALLOC ON)` 覆盖 `-DUSE_MIMALLOC=OFF`，只能改源码；且 `torch_npu` 硬绑 `torch==2.10.0` | setuptools、cmake 4.4.4、ninja 1.13.2、requests、six、filelock、fsspec、jinja2、networkx | **低** | tag 固定 → `sed` 目标行确定，无上游漂移；残留风险仅「换 torch 版本时未同步保证 `USE_MIMALLOC=OFF`」 |
| 10 | `py-spy==0.4.2` | — | 低 | 钉死 |

## 3.2 PR CI 使用期间

| 步骤 | 包 | 风险 | 风险说明 |
|---|---|---|---|
| 拉镜像 | `base-latest` | **中** | 移动 tag，kubelet `IfNotPresent` 只比 tag 字符串 → **重建后不会自动更新**，需改 digest 或清缓存 |
| 装测试依赖 | `pytest>=7.0` | 低 | 镜像已有 8.3.2（来自 triton-ascend），实际不下载 |
| 装测试依赖 | `omegaconf>=2.3.0` | **中** | 镜像缺（唯一缺失的 pyproject 依赖）；**手工清单与 pyproject 会漂移** |
| 装测试依赖 | `safetensors>=0.4.3` | 低 | 镜像已有 0.8.0（来自 fla） |
| 装测试依赖 | `numpy<2` | **中** | 与镜像 1.26.4 一致；但 transformers 若要求 >=2 会冲突 |
| 装测试依赖 | `transformers>=4.51` | **高** | 镜像已有 5.22.0（来自 fla）；该约束**无上界**，当前解析到最新版，测试依赖其私有 API |
| 装本项目 | `pip install -e . --no-deps` | 低 | `--no-deps` 保 torch；**但也意味着 pyproject 的依赖全部不生效** |
| 装本项目 | `--no-build-isolation` | 低 | 缺此参数会下载整份 torch（`build-system.requires` 里有 torch） |
| 运行测试 | torch 版本守卫 | 低 | `BEFORE/AFTER` 比对；实测 `2.10.0+cpu.nomimalloc >= 2.10.0` 成立，守卫不会触发 |

## 3.3 最需关注

| 项 | 问题 | 动作 |
|---|---|---|
| transformers 隐式引入 | 由 fla 的 `>=4.45.0` 决定，Dockerfile 里看不见 | 改显式 `pip install transformers==<ver> einops==<ver>` + `pip install --no-deps /tmp/flash-linear-attention` |
| numpy 版本 | 镜像 1.26.4 仅由 triton-ascend 传递钉住，Dockerfile / pyproject 均未声明 `numpy<2`；fla 或 transformers 任一要求 `>=2` 就会顶掉，NPU 算子栈在 kernel 加载期才报错 | Dockerfile 显式加 `pip install "numpy<2"`；PR CI 侧保持 `numpy<2` 最后安装 |
