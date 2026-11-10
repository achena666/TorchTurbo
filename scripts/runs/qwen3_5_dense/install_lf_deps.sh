#!/bin/bash
set -e

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/../deps/lf_deps.sh"

pip install torchvision==0.25.0

# 清理 HF transformers_modules 缓存：SFS 上有并发写入时 rm 会碰
# "Directory not empty"（CI #17 npu-e2e 的真实挂点），清理失败不该否决安装。
rm -rf ~/.cache/huggingface/modules/transformers_modules/ || true
