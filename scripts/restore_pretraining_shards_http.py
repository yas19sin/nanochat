#!/usr/bin/env python3
"""Restore large pretraining shards with resumable HTTP downloads.

This is intentionally more conservative than `hf download --local-dir`: it
downloads to `<filename>.part`, verifies the byte size reported by the Hub, and
only then replaces the final shard file.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

import requests
from huggingface_hub import get_token, hf_hub_url
from huggingface_hub.file_download import get_hf_file_metadata


DEFAULT_FILES = [
    "00000_eng_general_finepdfs_dclm_fwe_train.parquet",
    "00001_eng_general_finepdfs_dclm_fwe_train.parquet",
    "00002_eng_general_finepdfs_dclm_fwe_train.parquet",
    "00003_eng_general_finepdfs_dclm_fwe_train.parquet",
]


def format_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def find_hf_partial(local_dir: Path, filename: str, etag: str) -> Path | None:
    download_dir = local_dir / ".cache" / "huggingface" / "download"
    if not download_dir.exists():
        return None

    candidates = sorted(
        download_dir.glob(f"*.{etag}.incomplete"),
        key=lambda path: path.stat().st_size,
        reverse=True,
    )
    for candidate in candidates:
        if candidate.stat().st_size > 0:
            return candidate

    # Some versions write the final filename lock/metadata alongside hashed
    # chunks. Keep this as a narrow fallback for weird local-dir states.
    named = download_dir / f"{filename}.incomplete"
    if named.exists() and named.stat().st_size > 0:
        return named
    return None


def adopt_partial(local_dir: Path, filename: str, etag: str, part_path: Path) -> None:
    if part_path.exists() and part_path.stat().st_size > 0:
        return
    partial = find_hf_partial(local_dir, filename, etag)
    if partial is None:
        return
    print(f"[adopt] {filename}: using HF partial {format_bytes(partial.stat().st_size)}", flush=True)
    if part_path.exists():
        part_path.unlink()
    shutil.move(str(partial), str(part_path))


def remote_metadata(repo_id: str, repo_type: str, filename: str, token: str | None):
    url = hf_hub_url(repo_id=repo_id, filename=filename, repo_type=repo_type)
    metadata = get_hf_file_metadata(url, token=token)
    if metadata.size is None:
        raise RuntimeError(f"Hub did not report a size for {filename}")
    etag = (metadata.etag or "").strip('"')
    return url, int(metadata.size), etag


def download_one(
    *,
    repo_id: str,
    repo_type: str,
    local_dir: Path,
    filename: str,
    token: str | None,
    chunk_size: int,
    progress_seconds: float,
) -> None:
    url, expected_size, etag = remote_metadata(repo_id, repo_type, filename, token)
    final_path = local_dir / filename
    part_path = local_dir / f"{filename}.part"

    if final_path.exists() and final_path.stat().st_size == expected_size:
        print(f"[skip] {filename}: already complete ({format_bytes(expected_size)})", flush=True)
        return

    adopt_partial(local_dir, filename, etag, part_path)

    downloaded = part_path.stat().st_size if part_path.exists() else 0
    if downloaded > expected_size:
        print(f"[reset] {filename}: partial is larger than remote size", flush=True)
        part_path.unlink()
        downloaded = 0

    mode = "ab" if downloaded else "wb"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if downloaded:
        headers["Range"] = f"bytes={downloaded}-"

    print(
        f"[download] {filename}: {format_bytes(downloaded)} / {format_bytes(expected_size)}",
        flush=True,
    )

    with requests.get(url, headers=headers, stream=True, allow_redirects=True, timeout=(30, 120)) as response:
        if downloaded and response.status_code != 206:
            print(f"[reset] {filename}: server did not resume (HTTP {response.status_code})", flush=True)
            downloaded = 0
            mode = "wb"
            headers.pop("Range", None)
            response.close()
            with requests.get(url, headers=headers, stream=True, allow_redirects=True, timeout=(30, 120)) as retry:
                retry.raise_for_status()
                _write_response(retry, part_path, mode, expected_size, downloaded, chunk_size, progress_seconds)
        else:
            response.raise_for_status()
            _write_response(response, part_path, mode, expected_size, downloaded, chunk_size, progress_seconds)

    actual_size = part_path.stat().st_size
    if actual_size != expected_size:
        raise RuntimeError(
            f"{filename} incomplete: got {format_bytes(actual_size)}, expected {format_bytes(expected_size)}"
        )

    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.exists():
        final_path.unlink()
    part_path.replace(final_path)
    print(f"[done] {filename}: {format_bytes(expected_size)}", flush=True)


def _write_response(
    response: requests.Response,
    part_path: Path,
    mode: str,
    expected_size: int,
    starting_size: int,
    chunk_size: int,
    progress_seconds: float,
) -> None:
    downloaded = starting_size
    started = time.monotonic()
    last_progress = started
    with part_path.open(mode, buffering=0) as f:
        for chunk in response.iter_content(chunk_size=chunk_size):
            if not chunk:
                continue
            f.write(chunk)
            downloaded += len(chunk)
            now = time.monotonic()
            if now - last_progress >= progress_seconds:
                elapsed = max(now - started, 1e-6)
                rate = (downloaded - starting_size) / elapsed
                pct = 100 * downloaded / expected_size
                print(
                    f"  {pct:5.1f}% {format_bytes(downloaded)} / {format_bytes(expected_size)} "
                    f"at {format_bytes(int(rate))}/s",
                    flush=True,
                )
                f.flush()
                os.fsync(f.fileno())
                last_progress = now


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-id", default="Lyte/darija-nanochat-pretrain-mix")
    parser.add_argument("--repo-type", default="dataset")
    parser.add_argument("--local-dir", type=Path, required=True)
    parser.add_argument("--filename", action="append", default=[])
    parser.add_argument("--chunk-size-mib", type=int, default=8)
    parser.add_argument("--progress-seconds", type=float, default=30.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    token = get_token()
    files = args.filename or DEFAULT_FILES
    local_dir = args.local_dir.resolve()
    local_dir.mkdir(parents=True, exist_ok=True)
    chunk_size = args.chunk_size_mib * 1024 * 1024

    for filename in files:
        download_one(
            repo_id=args.repo_id,
            repo_type=args.repo_type,
            local_dir=local_dir,
            filename=filename,
            token=token,
            chunk_size=chunk_size,
            progress_seconds=args.progress_seconds,
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
