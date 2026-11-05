"""In-process diagnostics for CI training runs.

Installed by putting this directory on ``PYTHONPATH``; Python imports
``sitecustomize`` at interpreter start, so every worker process gets the same hooks
without the workflow touching a single line of the training code.

Why in-process instead of reading the job log:

* **torchrun truncates.** It forwards a child's stderr with a ``[rankN]:`` prefix and
  drops the tail when the child exits, which is exactly where the exception type and
  message live. An ``excepthook`` writing to a file survives any amount of truncation.
* **The first failing step leaves no evidence.** ``assert_finite`` runs before the
  first ``log()`` call, so a non-finite loss produces one line and no numbers at all.
  Probes report the value before the raise.

Everything here is opt-out by environment variable so the same file can be used by
any model workflow. See ``README.md`` for the knobs.

Design rule: a probe must never change the outcome of the run it observes. Every
report is wrapped in its own ``try``, and installation failures fall back to the
unpatched behaviour instead of raising.
"""

from __future__ import annotations

import faulthandler
import importlib.abc
import functools
import os
import sys
import traceback

TRACEBACK_PATH = os.environ.get("TT_TRACEBACK_FILE", "/tmp/tt_traceback.log")
DIAG_CALLS = int(os.environ.get("TT_DIAG_CALLS", "2"))
LAYER_PASSES = int(os.environ.get("TT_LAYER_PASSES", "1"))
SKIP_CHECKPOINT = os.environ.get("TT_SKIP_CHECKPOINT", "0") not in ("0", "", "false")


def _rank() -> str:
    return os.environ.get("RANK", "?")


# ---------------------------------------------------------------------------
# Exception capture
# ---------------------------------------------------------------------------
def _hook(exc_type, exc, tb):
    try:
        with open(TRACEBACK_PATH, "a", encoding="utf-8") as handle:
            handle.write(f"\n===== excepthook pid={os.getpid()} =====\n")
            traceback.print_exception(exc_type, exc, tb, file=handle)
    except Exception:
        pass
    if sys.__excepthook__ is not None:
        sys.__excepthook__(exc_type, exc, tb)


sys.excepthook = _hook
try:
    faulthandler.enable(open(TRACEBACK_PATH + ".fault", "a"))
except Exception:
    pass


# ---------------------------------------------------------------------------
# Tensor inspection helpers
# ---------------------------------------------------------------------------
def _to_local(tensor):
    """Unwrap a DTensor the way ``distributed.assert_finite`` does."""
    try:
        from torch.distributed.tensor import DTensor

        if isinstance(tensor, DTensor):
            return tensor.to_local()
    except Exception:
        pass
    return tensor


def _scalar_str(value) -> str:
    """``nan`` / ``inf`` / six significant digits; non-scalars report only a size."""
    import torch

    try:
        if torch.is_tensor(value):
            value = _to_local(value)
            if value.numel() != 1:
                return f"tensor(numel={value.numel()})"
            number = float(value.detach().float().item())
        else:
            number = float(value)
    except Exception as exc:
        return f"?({type(exc).__name__})"
    if number != number:
        return "nan"
    if number == float("inf"):
        return "inf"
    if number == float("-inf"):
        return "-inf"
    return f"{number:.6g}"


def _health(tensor):
    """(non-finite count, absmax, shape, dtype). Does not modify the tensor."""
    import torch

    local = _to_local(tensor).detach()
    bad = int((~torch.isfinite(local)).sum().item())
    try:
        absmax = float(local.float().abs().max().item())
    except Exception:
        absmax = float("nan")
    return bad, absmax, tuple(local.shape), local.dtype


def _row_summary(tensor) -> str:
    """One line of health plus the span of contiguous bad token rows.

    A row (every index but the last dimension) counts as bad if any element in it is
    non-finite. A contiguous span means one sample went bad as a unit; scattered rows
    mean a buffer was written over or values overflowed independently.
    """
    import torch

    local = _to_local(tensor).detach()
    bad = ~torch.isfinite(local)
    n_bad = int(bad.sum().item())
    try:
        absmax = f"{float(local.float().abs().max().item()):.6g}"
    except Exception:
        absmax = "?"
    if n_bad == 0:
        return f"ok absmax={absmax}"
    if local.dim() < 2:
        return f"BAD nonfinite={n_bad} absmax={absmax}"
    rows = bad.reshape(-1, local.shape[-1]).any(-1)
    nz = rows.nonzero().reshape(-1)
    lo = int(nz[0].item())
    hi = int(nz[-1].item()) + 1
    n_rows = int(rows.sum().item())
    return (
        f"BAD nonfinite={n_bad} rows={n_rows}/{rows.numel()}"
        f" span=[{lo},{hi}) absmax={absmax}"
    )


# ---------------------------------------------------------------------------
# Installer: skip intermediate checkpoints
# ---------------------------------------------------------------------------
def _install_skip_ckpt(module) -> None:
    """Keep exactly one copy of the weights: the final ``trainer.save_model()``.

    ``_save_checkpoint`` writes the optimizer and scheduler state whenever
    ``save_only_model`` is false (``trainer.py:3068``). For a 13.4 B fp32 AdamW that
    is tens of gigabytes, and ``/tmp`` and ``/`` are the same 50 GB overlay, so the
    run dies *after* training has already succeeded. Patching here rather than in
    ``__init__`` avoids the ``load_best_model_at_end`` / FSDP compatibility checks
    that run during construction.
    """
    if not SKIP_CHECKPOINT:
        return
    Trainer = getattr(module, "Trainer", None)
    if Trainer is None:
        from transformers import Trainer  # pragma: no cover

    if getattr(Trainer, "_tt_skip_ckpt", False):
        return

    def _save_checkpoint(self, model, trial=None):
        step = getattr(self.state, "global_step", "?")
        print(
            f"[ttcatch] rank={_rank()} checkpoint at step {step} skipped; "
            "the final save_model exports the single HF copy",
            flush=True,
        )

    Trainer._save_checkpoint = _save_checkpoint
    Trainer._tt_skip_ckpt = True
    print("[ttcatch] checkpoint patch on: final HF export only", flush=True)


# ---------------------------------------------------------------------------
# Installer: chunk_loss / assert_finite
# ---------------------------------------------------------------------------
def _report_loss_inputs(call_no, hidden_states, labels, normalizers, loss_scales,
                        chunk_size, num_chunks) -> None:
    print(
        f"[ttdiag] chunk_loss#{call_no} rank={_rank()}"
        f" chunk_size={chunk_size} chunks={num_chunks}",
        flush=True,
    )
    for i, hidden in enumerate(hidden_states):
        try:
            print(f"[ttdiag]   rank={_rank()} hidden[{i}] {_row_summary(hidden)}",
                  flush=True)
        except Exception as exc:
            print(f"[ttdiag]   hidden[{i}] 报告失败: {exc}", flush=True)
        try:
            label = labels[i]
            valid = int((label.detach() != -100).sum().item())
            print(
                f"[ttdiag]   labels[{i}] shape={tuple(label.shape)}"
                f" valid={valid} masked={label.numel() - valid}",
                flush=True,
            )
        except Exception as exc:
            print(f"[ttdiag]   labels[{i}] 报告失败: {exc}", flush=True)
    for i, norm in enumerate(normalizers):
        print(f"[ttdiag]   normalizer[{i}]={_scalar_str(norm)}", flush=True)
    try:
        print(f"[ttdiag]   loss_scales={tuple(float(s) for s in loss_scales)}",
              flush=True)
    except Exception as exc:
        print(f"[ttdiag]   loss_scales 报告失败: {exc}", flush=True)


def _install_loss_diag(module) -> None:
    original = getattr(module, "chunk_loss", None)
    if original is None or getattr(original, "_tt_diag", False):
        return

    seen = {"n": 0}

    @functools.wraps(original)
    def wrapper(hidden_states, labels, **kwargs):
        seen["n"] += 1
        call_no = seen["n"]
        if call_no <= DIAG_CALLS:
            try:
                chunk_size = int(kwargs.get("chunk_size", 1024))
                num_chunks = -(
                    -hidden_states[0].numel()
                    // (hidden_states[0].shape[-1] * chunk_size)
                )
                _report_loss_inputs(
                    call_no,
                    hidden_states,
                    labels,
                    kwargs.get("normalizers") or (),
                    kwargs.get("loss_scales") or (),
                    chunk_size,
                    num_chunks,
                )
            except Exception as exc:
                print(f"[ttdiag] 入参报告失败: {exc}", flush=True)

        result = original(hidden_states, labels, **kwargs)

        if call_no <= DIAG_CALLS:
            try:
                total, raw = result
                raw_list = [_scalar_str(v) for v in raw.reshape(-1)]
                print(
                    f"[ttdiag] chunk_loss#{call_no} -> total={_scalar_str(total)}"
                    f" raw={raw_list}",
                    flush=True,
                )
            except Exception as exc:
                print(f"[ttdiag] 结果报告失败: {exc}", flush=True)
        return result

    wrapper._tt_diag = True
    module.chunk_loss = wrapper
    print("[ttdiag] chunk_loss probe on", flush=True)


def _install_assert_diag(module) -> None:
    original = getattr(module, "assert_finite", None)
    if original is None or getattr(original, "_tt_diag", False):
        return

    @functools.wraps(original)
    def wrapper(value, name):
        try:
            bad, absmax, shape, dtype = _health(value)
            print(
                f"[ttdiag] assert {name} rank={_rank()} value={_scalar_str(value)}"
                f" nonfinite={bad} absmax={absmax:.6g}"
                f" shape={shape} dtype={dtype}",
                flush=True,
            )
        except Exception as exc:
            print(f"[ttdiag] assert 报告失败: {exc}", flush=True)
        return original(value, name)

    wrapper._tt_diag = True
    module.assert_finite = wrapper
    print("[ttdiag] assert_finite probe on", flush=True)


# ---------------------------------------------------------------------------
# Installer: per-layer NaN localisation
# ---------------------------------------------------------------------------
_PROBE = {"pass": 0, "off": False}


def _wrap_class_forward(cls, label, *, mark_pass=False) -> bool:
    if cls is None:
        return False
    original = getattr(cls, "forward", None)
    if original is None or getattr(original, "_tt_layer_probe", False):
        return False

    @functools.wraps(original)
    def forward(self, *args, **kwargs):
        if _PROBE["off"]:
            return original(self, *args, **kwargs)
        layer_idx = getattr(self, "layer_idx", "?")
        if mark_pass and layer_idx == 0:
            _PROBE["pass"] += 1
            if _PROBE["pass"] > LAYER_PASSES:
                # Activation recompute replays the whole stack; reporting it again
                # would double the log and tell us nothing new.
                _PROBE["off"] = True
                return original(self, *args, **kwargs)
        out = original(self, *args, **kwargs)
        try:
            tensor = out[0] if isinstance(out, (tuple, list)) else out
            print(
                f"[ttdiag] fwd#{_PROBE['pass']} rank={_rank()} L{layer_idx} {label}"
                f" {_row_summary(tensor)}",
                flush=True,
            )
        except Exception as exc:
            print(f"[ttdiag] L{layer_idx} {label} 报告失败: {exc}", flush=True)
        return out

    forward._tt_layer_probe = True
    cls.forward = forward
    return True


def _install_layer_probe(module) -> None:
    installed = _wrap_class_forward(
        getattr(module, "BailingMoeV3DecoderLayer", None), "layer", mark_pass=True
    )
    for name in (
        "BailingMoeV3KimiDeltaAttention",
        "BailingMoeV3MultiLatentAttention",
    ):
        installed |= _wrap_class_forward(getattr(module, name, None), "attn")
    if installed:
        print("[ttdiag] layer probe on", flush=True)


def _install_moe_probe(module) -> None:
    """MoE is probed through the generic layer.

    The parallel plan replaces ``model.layers.*.mlp`` wholesale with ``MoELayer``, so
    ``BailingMoeV3SparseMoeBlock.forward`` never runs at train time and patching it
    would produce no output at all.
    """
    if _wrap_class_forward(getattr(module, "MoELayer", None), "mlp"):
        print("[ttdiag] moe probe on", flush=True)


# ---------------------------------------------------------------------------
# Loader hook
# ---------------------------------------------------------------------------
# Module name -> installer. Patches land on the module *object* after its code has
# executed, because the consumers do `from x import name` and rebinding `x.name`
# afterwards would not reach an already-bound reference.
_INSTALLERS = {
    "transformers.trainer": _install_skip_ckpt,
    "torchturbo.engine.backends.fsdp2.backend": _install_loss_diag,
    "torchturbo.adapters.llamafactory_v0.trainer": _install_assert_diag,
    "torchturbo.models.modules.moe.moe_layer": _install_moe_probe,
    "torchturbo.models.bailing_hybrid.modeling_bailing_moe_v3": _install_layer_probe,
}


class _After:
    def __init__(self, loader, installer):
        self._loader = loader
        self._installer = installer

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        self._loader.exec_module(module)
        try:
            self._installer(module)
        except Exception as exc:
            print(
                f"[ttcatch] patch failed ({module.__name__}): {exc}", flush=True
            )


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        installer = _INSTALLERS.get(fullname)
        if installer is None:
            return None
        for finder in sys.meta_path:
            if finder is self:
                continue
            try:
                spec = finder.find_spec(fullname, path, target)
            except Exception:
                continue
            if spec is not None and spec.loader is not None:
                spec.loader = _After(spec.loader, installer)
                return spec
        return None


sys.meta_path.insert(0, _Finder())
