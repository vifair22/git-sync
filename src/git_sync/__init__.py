"""git-sync: mirror GitLab repos to GitHub and generate profile READMEs."""
from __future__ import annotations

from pathlib import Path

try:
    from ._version import VERSION
except ImportError:
    _semver = (Path(__file__).resolve().parents[2] / "release_version").read_text().strip()
    VERSION = f"{_semver}_dev"
