# CI diagnostics package

Put this directory on `PYTHONPATH` and every Python process in the job — torchrun
workers included — installs the hooks in `sitecustomize.py` at interpreter start.
No training code changes, no `--debug` flag to remember.

```yaml
- name: ⑤ Train
  env:
    PYTHONPATH: ${{ github.workspace }}/scripts/ci/diag
```

Do **not** write `:/tmp/diag:$PYTHONPATH` with a leading colon: an empty segment means
"current directory", which puts the repo root on `sys.path` and lets a same-named
directory shadow a real package.

## What it captures

| Probe | Tag | Why it exists |
|---|---|---|
| `excepthook` + `faulthandler` | `[ttcatch]` / file | torchrun truncates a forwarded child's stderr on exit, dropping the exception type and message. |
| `Trainer._save_checkpoint` no-op | `[ttcatch]` | Optimizer state plus two weight exports overflow the 50 GB overlay *after* training succeeded. |
| `chunk_loss` in/out | `[ttdiag]` | `assert_finite` runs before the first `log()`, so a bad step leaves no numbers. |
| `assert_finite` | `[ttdiag]` | Distinguishes NaN from ±Inf, and all-ranks from a subset. |
| Decoder layer / attention / MoE forward | `[ttdiag]` | Localises where the backbone first produced non-finite values. |

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `TT_TRACEBACK_FILE` | `/tmp/tt_traceback.log` | Where `excepthook` appends. Companion `.fault` holds faulthandler output. |
| `TT_DIAG_CALLS` | `2` | How many `chunk_loss` calls report. Each `.item()` synchronises the device, so keep it small. `0` disables. |
| `TT_LAYER_PASSES` | `1` | How many full forward passes report. Activation recompute replays the stack; one pass is enough. |
| `TT_SKIP_CHECKPOINT` | `0` | `1` replaces `Trainer._save_checkpoint` with a no-op so only the final `save_model()` writes weights. |

## Reading the output

```
[ttdiag] chunk_loss#1 rank=6 chunk_size=1024 chunks=4
[ttdiag]   rank=6 hidden[0] BAD nonfinite=675840 rows=264/2048 span=[1784,2048) absmax=nan
[ttdiag]   rank=6 labels[0] shape=(1, 2048) valid=1438 masked=610
[ttdiag]   normalizer[0]=5604
[ttdiag] chunk_loss#1 -> total=nan raw=['nan']
[ttdiag] fwd#1 rank=6 L3 mlp BAD nonfinite=... rows=... span=[...]
[ttdiag] assert loss rank=6 value=nan nonfinite=1 absmax=nan shape=() dtype=torch.float32
```

| Observation | Conclusion |
|---|---|
| `hidden[0]` clean, `total=nan` | Backbone fine — the loss head or its normalizer is wrong. |
| `hidden[0]` already bad | A model kernel produced NaN. |
| `span` contiguous and its length matches a sample | One sample went bad as a unit. |
| `span` scattered | Buffer reuse or independent overflow. |
| Only some ranks bad | Memory reuse, or a genuinely per-sample trigger. |

`nonfinite` and `absmax` are read together: `absmax=inf` is overflow, a tiny
`absmax` with all-bad counts points at underflow rather than a real number.
