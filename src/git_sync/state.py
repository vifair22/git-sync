"""Persistent state for git-sync.

State is a single JSON document written atomically via ``os.replace`` so a
crashed run never leaves a half-written file. Keyed by GitLab project ID
(survives project renames on the GitLab side).
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RepoState:
    gitlab_id: int
    gitlab_path: str
    github_name: str
    last_known_visibility: str  # "public" or "private"
    last_sync_utc: str | None = None
    last_error: str | None = None
    last_sync_source_digest: str | None = None


@dataclass
class ProfileState:
    last_gitlab_hash: str | None = None
    last_github_hash: str | None = None
    last_publish_utc: str | None = None
    language_cache_utc: str | None = None
    language_cache: dict[str, int] = field(default_factory=dict)


@dataclass
class State:
    repos: dict[str, RepoState] = field(default_factory=dict)
    profile: ProfileState = field(default_factory=ProfileState)


def load(path: Path) -> State:
    if not path.exists():
        return State()
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return _from_dict(data)


def save(path: Path, state: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _to_dict(state)
    _atomic_write_json(path, data)


def _atomic_write_json(path: Path, data: Any) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _to_dict(state: State) -> dict[str, Any]:
    return {
        "repos": {k: asdict(v) for k, v in state.repos.items()},
        "profile": asdict(state.profile),
    }


def _from_dict(data: dict[str, Any]) -> State:
    repos = {k: RepoState(**v) for k, v in data.get("repos", {}).items()}
    profile_data = data.get("profile") or {}
    profile = ProfileState(**profile_data) if profile_data else ProfileState()
    return State(repos=repos, profile=profile)
