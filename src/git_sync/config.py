"""Configuration loader for git-sync.

Non-secret settings come from a TOML file (path in ``GIT_SYNC_CONFIG`` or the
``--config`` CLI flag). Secrets come from the environment: ``GITLAB_TOKEN`` and
``GITHUB_TOKEN`` are required. ``GITLAB_URL`` and ``GITHUB_OWNER`` may be set in
either place; the environment wins.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class GitLabConfig:
    url: str
    token: str


@dataclass(frozen=True)
class GitHubConfig:
    token: str
    owner: str


@dataclass(frozen=True)
class MirrorConfig:
    enabled: bool
    strip_blobs_larger_than_mb: int | None  # None = no rewriting
    exclude_groups: tuple[str, ...]  # first path-segment prefixes to skip
    mirror_private_repos: bool  # mirror private GitLab projects to private GitHub repos
    only_group_owned: bool  # skip projects in the user's personal namespace


@dataclass(frozen=True)
class HighlightEntry:
    path: str       # "namespace/repo" on GitLab
    stack: str      # free-form stack/tools description, shown in (parens)
    summary: str    # one-paragraph description


@dataclass(frozen=True)
class ProfileConfig:
    enabled: bool
    top_n_languages: int
    recent_activity_count: int
    recent_repos_count: int
    github_disclaimer: str
    gitlab_path: str  # "owner/repo" on GitLab for the profile README
    github_repo: str  # repo name on GitHub (owner is github.owner)
    highlights: tuple[HighlightEntry, ...]  # empty = fall back to recent_repos


@dataclass(frozen=True)
class AuthorConfig:
    name: str
    email: str


@dataclass(frozen=True)
class PathsConfig:
    state: Path
    cache: Path
    about: Path


@dataclass(frozen=True)
class ScheduleConfig:
    mirror_interval_hours: int
    profile_interval_hours: int


@dataclass(frozen=True)
class LoggingConfig:
    level: str


@dataclass(frozen=True)
class Config:
    gitlab: GitLabConfig
    github: GitHubConfig
    author: AuthorConfig
    mirror: MirrorConfig
    profile: ProfileConfig
    paths: PathsConfig
    schedule: ScheduleConfig
    logging: LoggingConfig


DEFAULT_CONFIG_PATH = Path("/etc/git-sync/config.toml")


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Required environment variable {name} is unset or empty")
    return value


def load(path: Path | None = None) -> Config:
    cfg_path = path or Path(os.environ.get("GIT_SYNC_CONFIG") or DEFAULT_CONFIG_PATH)
    if not cfg_path.is_file():
        raise ConfigError(f"Config file not found: {cfg_path}")

    with cfg_path.open("rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"Invalid TOML in {cfg_path}: {e}") from e

    try:
        return _build(data)
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError(f"Invalid config ({cfg_path}): {e}") from e


def _build(data: dict[str, Any]) -> Config:
    gitlab_url = (
        os.environ.get("GITLAB_URL", "").strip()
        or str(data.get("gitlab", {}).get("url", "")).strip()
    )
    if not gitlab_url:
        raise ConfigError("gitlab.url must be set via config or GITLAB_URL env var")

    github_owner = (
        os.environ.get("GITHUB_OWNER", "").strip()
        or str(data.get("github", {}).get("owner", "")).strip()
    )
    if not github_owner:
        raise ConfigError("github.owner must be set via config or GITHUB_OWNER env var")

    gitlab = GitLabConfig(url=gitlab_url, token=_require_env("GITLAB_TOKEN"))
    github = GitHubConfig(token=_require_env("GITHUB_TOKEN"), owner=github_owner)

    mirror_data = data.get("mirror", {})
    strip_raw = mirror_data.get("strip_blobs_larger_than_mb")
    strip_mb = int(strip_raw) if strip_raw is not None else None
    if strip_mb is not None and strip_mb <= 0:
        raise ConfigError("mirror.strip_blobs_larger_than_mb must be positive")
    exclude_raw = mirror_data.get("exclude_groups") or []
    if not isinstance(exclude_raw, list):
        raise ConfigError("mirror.exclude_groups must be a list of strings")
    exclude_groups = tuple(str(g).strip() for g in exclude_raw if str(g).strip())
    mirror = MirrorConfig(
        enabled=bool(mirror_data.get("enabled", True)),
        strip_blobs_larger_than_mb=strip_mb,
        exclude_groups=exclude_groups,
        mirror_private_repos=bool(mirror_data.get("mirror_private_repos", False)),
        only_group_owned=bool(mirror_data.get("only_group_owned", False)),
    )

    profile_data = data.get("profile", {})
    gitlab_path = str(profile_data.get("gitlab_path", "")).strip()
    github_repo = str(profile_data.get("github_repo", "")).strip()
    if not gitlab_path:
        raise ConfigError("profile.gitlab_path must be set (e.g., 'alice/alice')")
    if not github_repo:
        raise ConfigError("profile.github_repo must be set (e.g., 'alice')")
    highlights_raw = profile_data.get("highlights") or []
    if not isinstance(highlights_raw, list):
        raise ConfigError("profile.highlights must be an array of tables")
    highlights: list[HighlightEntry] = []
    for idx, h in enumerate(highlights_raw):
        if not isinstance(h, dict):
            raise ConfigError(f"profile.highlights[{idx}] must be a table")
        try:
            highlights.append(
                HighlightEntry(
                    path=str(h["path"]).strip(),
                    stack=str(h["stack"]).strip(),
                    summary=str(h["summary"]).strip(),
                ),
            )
        except KeyError as e:
            raise ConfigError(
                f"profile.highlights[{idx}] missing required key: {e.args[0]}"
            ) from None
    profile = ProfileConfig(
        enabled=bool(profile_data.get("enabled", True)),
        top_n_languages=int(profile_data.get("top_n_languages", 8)),
        recent_activity_count=int(profile_data.get("recent_activity_count", 20)),
        recent_repos_count=int(profile_data.get("recent_repos_count", 5)),
        github_disclaimer=str(profile_data.get("github_disclaimer", "")),
        gitlab_path=gitlab_path,
        github_repo=github_repo,
        highlights=tuple(highlights),
    )

    author_data = data.get("author") or {}
    author_name = str(author_data.get("name", "")).strip()
    author_email = str(author_data.get("email", "")).strip()
    if not author_name or not author_email:
        raise ConfigError("[author] section requires both name and email")
    author = AuthorConfig(name=author_name, email=author_email)

    paths_data = data.get("paths")
    if not paths_data:
        raise ConfigError("[paths] section is required")
    paths = PathsConfig(
        state=Path(paths_data["state"]).expanduser(),
        cache=Path(paths_data["cache"]).expanduser(),
        about=Path(paths_data["about"]).expanduser(),
    )

    schedule_data = data.get("schedule", {})
    schedule = ScheduleConfig(
        mirror_interval_hours=int(schedule_data.get("mirror_interval_hours", 24)),
        profile_interval_hours=int(schedule_data.get("profile_interval_hours", 24)),
    )

    logging_data = data.get("logging", {})
    logging_config = LoggingConfig(
        level=str(logging_data.get("level", "INFO")).upper(),
    )

    return Config(
        gitlab=gitlab,
        github=github,
        author=author,
        mirror=mirror,
        profile=profile,
        paths=paths,
        schedule=schedule,
        logging=logging_config,
    )
