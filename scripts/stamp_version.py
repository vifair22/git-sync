#!/usr/bin/env python3
"""Stamp src/git_sync/_version.py with the full build version string.

Usage: python scripts/stamp_version.py [release|debug|asan|static]

Writes `VERSION = "<semver>_<YYYYMMDD.HHMM>.<type>"` derived from the
release_version file at the repo root and the current UTC time.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path


def main(argv: list[str]) -> int:
    build_type = argv[1] if len(argv) > 1 else "release"
    root = Path(__file__).resolve().parent.parent
    semver = (root / "release_version").read_text().strip()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d.%H%M")
    version = f"{semver}_{stamp}.{build_type}"
    out = root / "src" / "git_sync" / "_version.py"
    out.write_text(f'VERSION = "{version}"\n', encoding="utf-8")
    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
