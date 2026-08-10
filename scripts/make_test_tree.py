#!/usr/bin/env python3
"""Generate a nested dummy folder tree for exercising the backup stage.

Deterministic: the same --seed always produces byte-identical content, so a
tree can be regenerated and compared across runs.

    python scripts/make_test_tree.py /tmp/footage_src
    python scripts/make_test_tree.py /tmp/footage_src --big-mb 120

The layout deliberately includes the awkward cases the backup engine has to
handle: dotfiles, sidecar files, an empty folder, spaces in names, a file with
no extension, a zero-byte file, and a symlink (which the engine must skip).
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

CHUNK = 1024 * 1024  # write big files a megabyte at a time

# (relative path, size in bytes) — small files spelled out explicitly.
SMALL_FILES: list[tuple[str, int]] = [
    ("README.txt", 512),
    (".DS_Store", 6148),
    ("A001/.hidden_sidecar", 128),
    ("A001/clip_001.mov", 2 * 1024 * 1024),
    ("A001/clip_001.mov.xmp", 1024),
    ("A001/clip_002.mov", 3 * 1024 * 1024),
    ("A001/proxies/clip_001_proxy.mp4", 512 * 1024),
    ("A001/proxies/clip_002_proxy.mp4", 640 * 1024),
    ("A002/clip_010.mov", 1536 * 1024),
    ("A002/notes without extension", 300),
    ("A002/audio/scene 4 take 2.wav", 900 * 1024),
    ("A002/audio/.recorder_state", 64),
    ("A002/deep/deeper/deepest/marker.json", 220),
    ("empty_file.bin", 0),
]

EMPTY_DIRS = ["A003_empty_card", "A002/deep/empty_leaf"]

# (link path, target relative to the tree root) — the engine must not follow these.
SYMLINKS = [
    ("A001/link_to_clip_001.mov", "A001/clip_001.mov"),
    ("A002/link_to_A001", "A001"),
]


def write_file(path: Path, size: int, rng: random.Random) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    remaining = size
    with open(path, "wb") as handle:
        while remaining > 0:
            n = min(CHUNK, remaining)
            handle.write(rng.randbytes(n))
            remaining -= n


def build(root: Path, big_mb: int, seed: int, with_symlinks: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    # Absolute, so symlink targets resolve no matter what cwd this ran from.
    root = root.resolve()
    rng = random.Random(seed)

    for rel, size in SMALL_FILES:
        write_file(root / rel, size, rng)

    # One large file so the streaming/chunked path is genuinely exercised.
    write_file(root / "A001" / "clip_bigfile.mov", big_mb * 1024 * 1024, rng)

    for rel in EMPTY_DIRS:
        (root / rel).mkdir(parents=True, exist_ok=True)

    if with_symlinks:
        for link_rel, target_rel in SYMLINKS:
            link = root / link_rel
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.is_symlink() or link.exists():
                continue
            try:
                os.symlink(root / target_rel, link)
            except OSError as err:
                print(f"  (skipped symlink {link_rel}: {err})", file=sys.stderr)

    return root


def describe(root: Path) -> tuple[int, int, int]:
    """Return (regular file count, total bytes, symlink count)."""
    files = 0
    total = 0
    links = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in list(dirnames):
            if (Path(dirpath) / name).is_symlink():
                links += 1
                dirnames.remove(name)
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                links += 1
                continue
            files += 1
            total += path.stat().st_size
    return files, total, links


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="directory to create the tree in")
    parser.add_argument("--big-mb", type=int, default=50,
                        help="size of the single large file, in MB (default: 50)")
    parser.add_argument("--seed", type=int, default=1337,
                        help="RNG seed; same seed = same bytes (default: 1337)")
    parser.add_argument("--no-symlinks", action="store_true",
                        help="skip creating the symlinks")
    args = parser.parse_args()

    root = build(args.root.expanduser(), args.big_mb, args.seed,
                 with_symlinks=not args.no_symlinks)
    files, total, links = describe(root)
    print(f"Created {root}")
    print(f"  {files} files, {total / (1024*1024):.1f} MB, {links} symlinks (to be skipped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
