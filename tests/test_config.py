"""Tests for config loading."""
from __future__ import annotations

import pytest

from git_sync import config


_BASE_TOML = """
[gitlab]
url = "https://git.example.com"

[github]
owner = "alice"

[author]
name = "Alice"
email = "alice@example.com"

[profile]
gitlab_path = "alice/alice"
github_repo = "alice"

[paths]
state = "/tmp/state.json"
cache = "/tmp/cache"
about = "/tmp/about.md"
"""


def _write(tmp_path, body=_BASE_TOML):
    p = tmp_path / "config.toml"
    p.write_text(body)
    return p


def test_load_happy_path(tmp_path, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xxx")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-yyy")
    monkeypatch.delenv("GITLAB_URL", raising=False)
    monkeypatch.delenv("GITHUB_OWNER", raising=False)

    cfg = config.load(_write(tmp_path))

    assert cfg.gitlab.url == "https://git.example.com"
    assert cfg.gitlab.token == "glpat-xxx"
    assert cfg.github.owner == "alice"
    assert cfg.github.token == "ghp-yyy"
    assert cfg.author.name == "Alice"
    assert cfg.author.email == "alice@example.com"
    assert cfg.profile.gitlab_path == "alice/alice"
    assert cfg.profile.github_repo == "alice"
    assert cfg.profile.top_n_languages == 8
    assert cfg.profile.recent_activity_count == 20
    assert cfg.profile.recent_repos_count == 5
    assert cfg.schedule.mirror_interval_hours == 24
    assert cfg.schedule.profile_interval_hours == 24
    assert cfg.logging.level == "INFO"
    assert cfg.mirror.enabled is True
    assert cfg.profile.enabled is True


def test_missing_gitlab_token_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-yyy")
    with pytest.raises(config.ConfigError, match="GITLAB_TOKEN"):
        config.load(_write(tmp_path))


def test_missing_github_token_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xxx")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(config.ConfigError, match="GITHUB_TOKEN"):
        config.load(_write(tmp_path))


def test_env_overrides_toml_url(tmp_path, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xxx")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-yyy")
    monkeypatch.setenv("GITLAB_URL", "https://env.example.com")
    cfg = config.load(_write(tmp_path))
    assert cfg.gitlab.url == "https://env.example.com"


def test_env_overrides_toml_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xxx")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-yyy")
    monkeypatch.setenv("GITHUB_OWNER", "bob")
    cfg = config.load(_write(tmp_path))
    assert cfg.github.owner == "bob"


def test_missing_file_raises(tmp_path):
    with pytest.raises(config.ConfigError, match="not found"):
        config.load(tmp_path / "nope.toml")


def test_invalid_toml_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xxx")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-yyy")
    p = tmp_path / "config.toml"
    p.write_text("this is = not [valid toml")
    with pytest.raises(config.ConfigError, match="Invalid TOML"):
        config.load(p)


def test_missing_paths_section_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xxx")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-yyy")
    body = """
[gitlab]
url = "https://git.example.com"

[github]
owner = "alice"

[author]
name = "Alice"
email = "alice@example.com"

[profile]
gitlab_path = "alice/alice"
github_repo = "alice"
"""
    p = tmp_path / "config.toml"
    p.write_text(body)
    with pytest.raises(config.ConfigError, match="paths"):
        config.load(p)


def test_missing_author_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xxx")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-yyy")
    body = """
[gitlab]
url = "https://git.example.com"

[github]
owner = "alice"

[profile]
gitlab_path = "alice/alice"
github_repo = "alice"

[paths]
state = "/tmp/state.json"
cache = "/tmp/cache"
about = "/tmp/about.md"
"""
    p = tmp_path / "config.toml"
    p.write_text(body)
    with pytest.raises(config.ConfigError, match="author"):
        config.load(p)


def test_strip_blobs_negative_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xxx")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-yyy")
    body = """
[gitlab]
url = "https://git.example.com"

[github]
owner = "alice"

[author]
name = "Alice"
email = "alice@example.com"

[mirror]
strip_blobs_larger_than_mb = 0

[profile]
gitlab_path = "alice/alice"
github_repo = "alice"

[paths]
state = "/tmp/state.json"
cache = "/tmp/cache"
about = "/tmp/about.md"
"""
    p = tmp_path / "config.toml"
    p.write_text(body)
    with pytest.raises(config.ConfigError, match="strip_blobs_larger_than_mb"):
        config.load(p)


def test_strip_blobs_unset_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xxx")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-yyy")
    cfg = config.load(_write(tmp_path))
    assert cfg.mirror.strip_blobs_larger_than_mb is None


def test_strip_blobs_set(tmp_path, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xxx")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-yyy")
    body = """
[gitlab]
url = "https://git.example.com"

[github]
owner = "alice"

[author]
name = "Alice"
email = "alice@example.com"

[mirror]
strip_blobs_larger_than_mb = 95

[profile]
gitlab_path = "alice/alice"
github_repo = "alice"

[paths]
state = "/tmp/state.json"
cache = "/tmp/cache"
about = "/tmp/about.md"
"""
    p = tmp_path / "config.toml"
    p.write_text(body)
    cfg = config.load(p)
    assert cfg.mirror.strip_blobs_larger_than_mb == 95


def test_missing_profile_repo_paths_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xxx")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-yyy")
    body = """
[gitlab]
url = "https://git.example.com"

[github]
owner = "alice"

[author]
name = "Alice"
email = "alice@example.com"

[paths]
state = "/tmp/state.json"
cache = "/tmp/cache"
about = "/tmp/about.md"
"""
    p = tmp_path / "config.toml"
    p.write_text(body)
    with pytest.raises(config.ConfigError, match="gitlab_path"):
        config.load(p)


def test_overrides_applied(tmp_path, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xxx")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-yyy")
    body = """
[gitlab]
url = "https://git.example.com"

[github]
owner = "alice"

[author]
name = "Alice"
email = "alice@example.com"

[profile]
gitlab_path = "alice/alice"
github_repo = "alice"
top_n_languages = 3
recent_activity_count = 5
recent_repos_count = 2
github_disclaimer = "mirror disclaimer"

[paths]
state = "/tmp/state.json"
cache = "/tmp/cache"
about = "/tmp/about.md"

[schedule]
mirror_interval_hours = 6
profile_interval_hours = 12

[logging]
level = "debug"
"""
    p = tmp_path / "config.toml"
    p.write_text(body)
    cfg = config.load(p)
    assert cfg.profile.top_n_languages == 3
    assert cfg.profile.recent_activity_count == 5
    assert cfg.profile.recent_repos_count == 2
    assert cfg.profile.github_disclaimer == "mirror disclaimer"
    assert cfg.schedule.mirror_interval_hours == 6
    assert cfg.schedule.profile_interval_hours == 12
    assert cfg.logging.level == "DEBUG"
