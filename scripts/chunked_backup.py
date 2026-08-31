#!/usr/bin/env python3
"""Read every ESP32 flash byte twice in restartable chunks and require equality."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path


FLASH_BYTES = 16 * 1024 * 1024
CHUNK_BYTES = 1 * 1024 * 1024
DEFAULT_BAUD = 115200
MAX_PAIR_ATTEMPTS = 5


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def read_chunk(esptool: Path, port: str, baud: int, offset: int, size: int, destination: Path) -> None:
    temporary = destination.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    command = [
        str(esptool),
        "--chip",
        "esp32",
        "--port",
        port,
        "--baud",
        str(baud),
        "--before",
        "no-reset",
        "--after",
        "no-reset",
        "read-flash",
        hex(offset),
        hex(size),
        str(temporary),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=180)
    if completed.returncode != 0:
        detail = (completed.stdout + completed.stderr).strip()[-1200:]
        raise RuntimeError(detail or f"esptool exited {completed.returncode}")
    if not temporary.exists() or temporary.stat().st_size != size:
        raise RuntimeError(f"read produced {temporary.stat().st_size if temporary.exists() else 0} bytes, expected {size}")
    os.replace(temporary, destination)


def verified_pair(
    esptool: Path,
    port: str,
    baud: int,
    offset: int,
    size: int,
    first: Path,
    second: Path,
) -> str:
    if first.exists() and second.exists() and first.stat().st_size == size and second.stat().st_size == size:
        first_hash = sha256(first)
        if first_hash == sha256(second):
            return first_hash
    for attempt in range(1, MAX_PAIR_ATTEMPTS + 1):
        first.unlink(missing_ok=True)
        second.unlink(missing_ok=True)
        try:
            read_chunk(esptool, port, baud, offset, size, first)
            read_chunk(esptool, port, baud, offset, size, second)
            first_hash = sha256(first)
            if first_hash != sha256(second):
                raise RuntimeError("the two independent chunk reads differ")
            return first_hash
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            first.unlink(missing_ok=True)
            second.unlink(missing_ok=True)
            print(f"chunk {offset:#08x} attempt {attempt} failed: {error}", flush=True)
            if attempt == MAX_PAIR_ATTEMPTS:
                raise
            time.sleep(1)
    raise AssertionError("unreachable")


def assemble(chunks: list[Path], destination: Path) -> None:
    temporary = destination.with_suffix(".tmp")
    with temporary.open("wb") as output:
        for chunk in chunks:
            with chunk.open("rb") as source:
                while block := source.read(1024 * 1024):
                    output.write(block)
        output.flush()
        os.fsync(output.fileno())
    if temporary.stat().st_size != FLASH_BYTES:
        raise RuntimeError(f"assembled image has {temporary.stat().st_size} bytes")
    os.replace(temporary, destination)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--output", type=Path, default=Path("xsure-backup"))
    parser.add_argument("--esptool", type=Path, default=Path(".venv/bin/esptool"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    chunks_dir = args.output / "verified-chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    first_chunks: list[Path] = []
    second_chunks: list[Path] = []
    chunk_hashes: list[dict[str, object]] = []
    count = FLASH_BYTES // CHUNK_BYTES
    for index, offset in enumerate(range(0, FLASH_BYTES, CHUNK_BYTES), start=1):
        size = min(CHUNK_BYTES, FLASH_BYTES - offset)
        first = chunks_dir / f"{offset:08x}-read-1.bin"
        second = chunks_dir / f"{offset:08x}-read-2.bin"
        digest = verified_pair(args.esptool, args.port, args.baud, offset, size, first, second)
        first_chunks.append(first)
        second_chunks.append(second)
        chunk_hashes.append({"offset": offset, "size": size, "sha256": digest})
        print(f"verified chunk {index}/{count}: {offset:#08x}-{offset + size:#08x}", flush=True)

    first_image = args.output / "xsure-original-1.bin"
    second_image = args.output / "xsure-original-2.bin"
    assemble(first_chunks, first_image)
    assemble(second_chunks, second_image)
    first_hash = sha256(first_image)
    second_hash = sha256(second_image)
    if first_hash != second_hash:
        raise RuntimeError("assembled full-flash images differ")
    manifest = {
        "schemaVersion": 1,
        "method": "two independent reads of every 1 MiB chunk",
        "port": args.port,
        "baud": args.baud,
        "flashBytes": FLASH_BYTES,
        "chunkBytes": CHUNK_BYTES,
        "sha256": first_hash,
        "chunks": chunk_hashes,
    }
    (args.output / "backup-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"BACKUPS MATCH: {first_hash}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
