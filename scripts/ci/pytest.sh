#!/usr/bin/env bash
# pytest 泳道的全部逻辑：挑文件 → 分片 → marker 过滤 → 跑 → 出统计。
# CPU（裸机）与 NPU（container）两条泳道共用这一份 —— 它们唯一真正的差别是
# 「在哪台机器上跑」，跑什么、怎么切、怎么汇总必须完全一致，两份 yaml 迟早跑偏。
#
# 环境变量（由 workflow 传入）：
#   MODE        cpu | npu          cpu 加 -m "not npu"，npu 加 -m npu
#   TEST_FILES  JSON 数组；空 = 跑全部
#   TEST_PATH   单个 path / node id；非空则忽略 TEST_FILES 与 SHARD
#   SHARD       "0/3" 形式；空 = 不切
#   PYTEST_ARGS 额外参数（按空格拆是有意的）
#   JUNIT_OUT   junit 输出路径
#   RESULT_JSON result.py 的结果文件
set -uo pipefail

CI_SCRIPTS="${CI_SCRIPTS:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
MODE="${MODE:-cpu}"
TEST_FILES="${TEST_FILES:-}"
TEST_PATH="${TEST_PATH:-}"
SHARD="${SHARD:-}"
PYTEST_ARGS="${PYTEST_ARGS:--v --durations=20}"
JUNIT_OUT="${JUNIT_OUT:-/tmp/pytest.xml}"
RESULT_JSON="${RESULT_JSON:-/tmp/ci-result.json}"

python3 "$CI_SCRIPTS/result.py" init --file "$RESULT_JSON" \
  kind=pytest model_type="pytest-${MODE}" mode="$MODE" shard="${SHARD:--}"

# ---- 决定跑哪些文件 ---------------------------------------------------------
# 两件事必须在这里做完：分片（按文件轮转，片间用例数大致均衡）和
# 「文件列表为空就全量」（选择器没能给出结论时 fail-open）。
# 手动排查给的是 TEST_PATH（可能是 node id），它优先于一切。
if [ -n "$TEST_PATH" ]; then
  printf '%s\n' "$TEST_PATH" > /tmp/tt_pytest_files.txt
else
python3 - "$MODE" "$TEST_FILES" "$SHARD" >/tmp/tt_pytest_files.txt <<'PY'
import glob, json, sys

mode, raw, shard = sys.argv[1], sys.argv[2], sys.argv[3]
files = json.loads(raw) if raw.strip() else []
if not files:
    files = sorted(glob.glob("tests/**/test_*.py", recursive=True))
if shard:
    i, n = (int(x) for x in shard.split("/"))
    files = [f for k, f in enumerate(files) if k % n == i]
print("\n".join(files))
PY
fi

mapfile -t FILES < /tmp/tt_pytest_files.txt
if [ "${#FILES[@]}" -eq 0 ]; then
  echo "::error::没有可跑的测试文件（选择器和全量兜底都为空）"
  python3 "$CI_SCRIPTS/result.py" phase --file "$RESULT_JSON" \
    --name '④ pytest' --seconds 0 --rc 1
  exit 1
fi

echo "mode=${MODE} shard=${SHARD:--} files=${#FILES[@]}"
printf '  %s\n' "${FILES[@]}" | head -30

# ---- marker 过滤 ------------------------------------------------------------
# cpu：排除需要 torch_npu 的；npu：只要那些。
if [ "$MODE" = "npu" ]; then
  MARK=(-m npu)
else
  MARK=(-m "not npu")
fi

echo "--- collected ---"
# shellcheck disable=SC2086  # PYTEST_ARGS 故意不进数组，要按空格拆
python -m pytest "${FILES[@]}" "${MARK[@]}" ${PYTEST_ARGS} --collect-only -q 2>&1 \
  | tail -20 || true

echo "--- run ---"
START=$(date +%s)
set +e
# shellcheck disable=SC2086
python -m pytest "${FILES[@]}" "${MARK[@]}" ${PYTEST_ARGS} \
  --junitxml="$JUNIT_OUT" 2>&1 | tee /tmp/pytest.log
RC=${PIPESTATUS[0]}
ELAPSED=$(( $(date +%s) - START ))

python3 "$CI_SCRIPTS/result.py" phase --file "$RESULT_JSON" \
  --name "④ pytest (${MODE})" --seconds "$ELAPSED" --rc "$RC"
echo "::notice::pytest mode=${MODE} rc=${RC} in ${ELAPSED}s"

echo "--- junit ---"
python3 "$CI_SCRIPTS/result.py" junit --junit "$JUNIT_OUT" || true

echo "rc=${RC}" >> "${GITHUB_OUTPUT:-/dev/null}"
exit "$RC"
