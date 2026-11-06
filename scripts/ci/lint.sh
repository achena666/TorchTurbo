#!/usr/bin/env bash
# Lint 工具的下载与调用。放这里是因为每条都在处理「在哪个架构上跑」和
# 「钉哪个版本」，而这两件事写进 yaml 就没法本地跑、也过不了 shellcheck。
#
# 子命令：
#   hadolint [Dockerfile]   二进制而不是镜像：hadolint/hadolint 只有 amd64，
#                           而 CI_CPU_RUNNER 默认是 arm64 托管机。
#   stage                   select 的收尾：把「倒在哪一步」写成 step output。

set -uo pipefail

HADOLINT_VERSION="${HADOLINT_VERSION:-v2.15.1}"
GITHUB_OUTPUT="${GITHUB_OUTPUT:-/dev/null}"

cmd_hadolint() {
  local dockerfile="${1:-docker/npu/Dockerfile_aarch64}"
  local arch
  case "$(uname -m)" in
    aarch64|arm64) arch=arm64 ;;
    x86_64|amd64)  arch=x86_64 ;;
    *) echo "::error::unsupported arch $(uname -m)"; return 1 ;;
  esac

  curl -sSfL -o /tmp/hadolint \
    "https://github.com/hadolint/hadolint/releases/download/${HADOLINT_VERSION}/hadolint-linux-${arch}" \
    || { echo "::error::下载 hadolint 失败（${arch}）"; return 1; }
  chmod +x /tmp/hadolint

  # --failure-threshold error：只拦解析类问题，不拦「建议钉版本」这类风格告警。
  /tmp/hadolint --failure-threshold error "$dockerfile"
}

# select 的最后一步。必须 always()：
# 失败之后 job status 才是 failure，这一步正是用来分辨「静态挂了」和「计划挂了」。
cmd_stage() {
  local stage
  case "${JOB_STATUS:-}" in
    cancelled) stage=cancelled ;;
    *) if [ "${STATIC_OUTCOME:-}" != "success" ]; then
         stage=static
       elif [ "${PICK_OUTCOME:-}" != "success" ]; then
         stage=select
       else
         stage=ok
       fi ;;
  esac
  echo "stage=$stage" >> "$GITHUB_OUTPUT"
  echo "stage: $stage"
}

case "${1:-}" in
  hadolint) shift; cmd_hadolint "$@" ;;
  stage)    cmd_stage ;;
  *)
    echo "用法：$0 {hadolint [Dockerfile]|stage}" >&2
    exit 2
    ;;
esac
