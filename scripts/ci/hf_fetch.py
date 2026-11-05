#!/usr/bin/env python3
"""Download a HuggingFace repo, or a subset of its files, with retries.

Why the retries live here and not in the workflow: ``huggingface_hub`` deliberately
does not retry most failures. ``http_stream_backoff`` is called with
``retry_on_exceptions=()`` and ``retry_on_status_codes=(408, 429)``, so any 5xx --
and hf-mirror serves 521 (origin unreachable) often enough to matter -- surfaces as
an exception and ends the download. One attempt per job is not a good trade when a
job costs 20 minutes of NPU time.

Two upstream behaviours worth knowing before adding more logic here:

* ``local_dir`` mode writes each file to a process-unique temp name and moves it
  into place (``_download_to_tmp_and_move``). That is NFS-safe by design, but it
  also means a killed attempt leaves no resumable state: a retry starts that file
  from byte zero. Retries are cheap to add, they do not make a slow mirror fast.
* ``is_xet_available()`` returns False when ``HF_HUB_DISABLE_XET`` is set, which is
  how the workflows keep the download on the plain-HTTP path. Left to itself, xet
  fetches from ``cas-server.xethub.hf.co``, a host ``HF_ENDPOINT`` does not proxy,
  and treats a short read as a permanent size mismatch.
"""

from __future__ import annotations

import argparse
import sys
import time


def download(
    *,
    repo_id: str,
    local_dir: str,
    allow_patterns: list[str] | None = None,
    attempts: int = 3,
    backoff: int = 30,
    max_workers: int = 4,
) -> str:
    """Call ``snapshot_download`` until it succeeds or ``attempts`` runs out.

    ``backoff`` is multiplied by the attempt number, so the default schedule is
    30 s / 60 s -- enough to ride out a mirror hiccup without stalling a job that
    is going to fail anyway.

    ``huggingface_hub`` is imported here rather than at module scope so the pure
    helpers in this package can be imported (and tested) without the network stack.
    """
    from huggingface_hub import snapshot_download

    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        print(f"--- download attempt {attempt}/{attempts} ---", flush=True)
        try:
            path = snapshot_download(
                repo_id=repo_id,
                local_dir=local_dir,
                allow_patterns=allow_patterns,
                max_workers=max_workers,
            )
        except Exception as exc:  # noqa: BLE001 - any failure is worth one retry
            last = exc
            print(
                f"attempt {attempt} failed: {type(exc).__name__}: {exc}",
                flush=True,
            )
            if attempt < attempts:
                wait = attempt * backoff
                print(f"retrying in {wait}s", flush=True)
                time.sleep(wait)
            continue
        print(f"downloaded {repo_id} -> {path}", flush=True)
        return path
    raise RuntimeError(
        f"download {repo_id} failed after {attempts} attempts"
    ) from last


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--dir", required=True)
    parser.add_argument(
        "--patterns",
        default="",
        help="comma-separated allow_patterns; empty downloads the whole repo",
    )
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--backoff", type=int, default=30)
    parser.add_argument("--max-workers", type=int, default=4)
    args = parser.parse_args(argv)

    patterns = [p.strip() for p in args.patterns.split(",") if p.strip()] or None
    download(
        repo_id=args.repo,
        local_dir=args.dir,
        allow_patterns=patterns,
        attempts=args.attempts,
        backoff=args.backoff,
        max_workers=args.max_workers,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
