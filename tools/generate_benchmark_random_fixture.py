#!/usr/bin/env python3
"""Generate the committed zip-random benchmark fixture from a local zip file."""

from __future__ import annotations

import argparse
import random
import re
import zipfile
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ZIP = ROOT / "tests" / "pd_code.zip"
DEFAULT_OUTPUT = ROOT / "tests" / "benchmark_random_pd_codes.txt"


Entry = Tuple[str, int, str]


def crossing_count(text: str) -> int:
    numbers = re.findall(r"-?\d+", text)
    if len(numbers) % 4:
        raise ValueError("PD code does not contain a multiple of four integers")
    return len(numbers) // 4


def compact_pd_text(text: str) -> str:
    return " ".join(text.strip().split())


def read_eligible_entries(zip_path: Path, max_crossings: int) -> List[Entry]:
    with zipfile.ZipFile(zip_path) as archive:
        entries: List[Entry] = []
        for name in sorted(item for item in archive.namelist() if not item.endswith("/")):
            text = archive.read(name).decode("utf-8", errors="replace").strip()
            crossings = crossing_count(text)
            if crossings <= max_crossings:
                entries.append((name, crossings, compact_pd_text(text)))
    return entries


def select_entries(entries: Sequence[Entry], sample_size: int, seed: int, prefix_size: int) -> List[Entry]:
    if sample_size > len(entries):
        raise ValueError(f"requested {sample_size} samples from only {len(entries)} eligible entries")
    if prefix_size < 0 or prefix_size > sample_size:
        raise ValueError("--prefix-size must be between 0 and --sample-size")
    prefix = random.Random(seed).sample(list(entries), prefix_size) if prefix_size else []
    prefix_names = {entry[0] for entry in prefix}
    remaining = [entry for entry in entries if entry[0] not in prefix_names]
    suffix = random.Random(seed).sample(remaining, sample_size - prefix_size)
    return [*prefix, *suffix]


def render_fixture(
    selected: Iterable[Entry],
    *,
    seed: int,
    sample_size: int,
    prefix_size: int,
    max_crossings: int,
) -> str:
    lines = [
        "# Deterministic random sample from tests/pd_code.zip.",
        (
            f"# Selection: seed={seed}, sample_size={sample_size}, "
            f"prefix_size={prefix_size}, source files with <= {max_crossings} crossings."
        ),
        "# The source zip is local-only and is not committed.",
    ]
    for index, (name, crossings, text) in enumerate(selected, 1):
        lines.append(f"# source={name}; crossings={crossings}")
        lines.append(f"zip_random_{index:02d}: {text}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path, default=DEFAULT_ZIP, help="local pd_code.zip path")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="fixture file to write")
    parser.add_argument("--seed", type=int, default=20260708)
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--prefix-size", type=int, default=20)
    parser.add_argument("--max-crossings", type=int, default=150)
    args = parser.parse_args()

    entries = read_eligible_entries(args.zip, args.max_crossings)
    selected = select_entries(entries, args.sample_size, args.seed, args.prefix_size)
    args.output.write_text(
        render_fixture(
            selected,
            seed=args.seed,
            sample_size=args.sample_size,
            prefix_size=args.prefix_size,
            max_crossings=args.max_crossings,
        ),
        encoding="utf-8",
    )
    print(f"wrote {len(selected)} cases from {len(entries)} eligible entries to {args.output}")
    for index, (name, crossings, _) in enumerate(selected, 1):
        print(f"zip_random_{index:02d}: {crossings:3d} {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
