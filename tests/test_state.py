"""Tests for state persistence."""
from __future__ import annotations

from git_sync import state


def test_load_missing_returns_empty(tmp_path):
    s = state.load(tmp_path / "state.json")
    assert s.repos == {}
    assert s.profile.last_publish_utc is None
    assert s.profile.language_cache == {}


def test_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    original = state.State()
    original.repos["42"] = state.RepoState(
        gitlab_id=42,
        gitlab_path="vifair22/foo",
        github_name="foo",
        last_known_visibility="public",
        last_sync_utc="2026-04-20T12:00:00+00:00",
    )
    original.profile.last_gitlab_hash = "abc123"
    original.profile.last_github_hash = "def456"
    original.profile.last_publish_utc = "2026-04-20T12:00:00+00:00"
    original.profile.language_cache_utc = "2026-04-20T12:00:00+00:00"
    original.profile.language_cache = {"C": 100_000, "Python": 50_000}

    state.save(p, original)
    reloaded = state.load(p)

    assert "42" in reloaded.repos
    assert reloaded.repos["42"].gitlab_path == "vifair22/foo"
    assert reloaded.repos["42"].github_name == "foo"
    assert reloaded.repos["42"].last_known_visibility == "public"
    assert reloaded.repos["42"].last_sync_utc == "2026-04-20T12:00:00+00:00"
    assert reloaded.profile.last_gitlab_hash == "abc123"
    assert reloaded.profile.last_github_hash == "def456"
    assert reloaded.profile.language_cache == {"C": 100_000, "Python": 50_000}


def test_save_is_atomic_no_tmp_on_success(tmp_path):
    p = tmp_path / "state.json"
    state.save(p, state.State())
    siblings = sorted(x.name for x in p.parent.iterdir())
    assert siblings == [p.name]


def test_save_creates_parent_dir(tmp_path):
    p = tmp_path / "nested" / "deeper" / "state.json"
    state.save(p, state.State())
    assert p.exists()
