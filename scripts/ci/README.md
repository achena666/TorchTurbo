# CI helpers

Logic that used to live inside workflow YAML heredocs. Every file here runs locally,
which is the point: the previous arrangement could only be exercised by pushing and
waiting for an NPU pod.

| File | Role |
|---|---|
| `model_cache.sh` | `probe` a cache directory, `find` one anywhere the platform has put it, `mark` what it was built from |
| `hf_fetch.py` | `snapshot_download` with retries (the hub does not retry 5xx) |
| `flash_trim.py` | download only the shards holding the first N layers, then trim `index.json` and `config.json` |
| `parallel_plan.py` | narrow the repo's `sp_size`/`ep_size` to the actual card count |
| `torch_guard.sh` | `capture` / `verify` the NPU torch build across a pip install |
| `train_supervisor.sh` | run a launch script with a stall watchdog and process-group cleanup |
| `verify_env.py` | NPU stack probe: versions, `/dev/davinci*`, one matmul per card |
| `junit_summary.py` | parse a JUnit XML report (also `--json`, consumed by `result.py`) |
| `result.py` | accumulate facts in JSON, render the job summary from it |
| `diag/` | `sitecustomize` hooks: exception capture and NaN localisation |

Called from the two reusable workflows; the entries under `.github/workflows` are
thin and only declare inputs:

```
ci-npu-sft.yml      workflow_call   training     <- npu-smoke-lf.yml
ci-npu-pytest.yml   workflow_call   pytest       <- npu-smoke.yml
image-ci.yml        workflow_dispatch  build the L1 base image
```

## Conventions

* Human-readable logs go to **stderr**, machine-readable `KEY=value` lines go to
  `--output`. A caller appends the file to `$GITHUB_OUTPUT`, or sources it.
* Facts are written to a **file**, not to step outputs. Step outputs are empty when a
  step fails, and a failed run is the only one anyone reads.

## Local checks

```bash
python3 -m py_compile scripts/ci/*.py scripts/ci/diag/sitecustomize.py
bash -n scripts/ci/*.sh

# torch guard, without needing torch installed: point PYTHONPATH at a stub
bash scripts/ci/torch_guard.sh capture --file /tmp/before.env
bash scripts/ci/torch_guard.sh verify  --file /tmp/before.env

# layer trimming plan, no network
python3 -c "
import sys; sys.path.insert(0, 'scripts/ci')
from flash_trim import plan_shards
print(plan_shards({'model.layers.0.w': 'a.safetensors',
                   'model.layers.9.w': 'b.safetensors',
                   'lm_head.weight': 'a.safetensors'}, 1))
"

# parallel plan for 8 cards against the repo yaml
python3 scripts/ci/parallel_plan.py --yaml scripts/runs/bailing_v3/sft.yaml \
  --cards 8 --out /tmp/tt.yaml --output /tmp/plan.out && cat /tmp/plan.out
```
