#!/usr/bin/env python3
"""Run legacy scoring and the independent security oracle, even if one fails."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=Path("/app"))
    parser.add_argument("--out", type=Path, default=Path("/logs/verifier"))
    parser.add_argument("--complete-snapshot", action="store_true")
    args = parser.parse_args()
    if any((args.out / name).exists() for name in ("results.json", "reward.txt", "safety-oracle.json", "boundary.json")):
        parser.error("Results already exist; choose a new --out directory")
    codes = []
    for name in ("verify.py", "safety_oracle.py"):
        command = [
            sys.executable, str(Path(__file__).with_name(name)),
            "--app", str(args.app), "--out", str(args.out),
        ]
        if name == "safety_oracle.py" and args.complete_snapshot:
            command.append("--complete-snapshot")
        process = subprocess.run(command, check=False)
        codes.append(process.returncode)
    return 2 if any(code not in (0, 1) for code in codes) else int(any(codes))


if __name__ == "__main__":
    raise SystemExit(main())
