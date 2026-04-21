"""Tests for profile/stats.py aggregation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from git_sync.clients.gitlab import GitLabEvent, GitLabProject, GitLabUser
from git_sync.profile import stats
from git_sync.state import State


def _gl(gid, path, *, visibility="public", size=1000, last="2026-04-20T12:00:00Z"):
    return GitLabProject(
        id=gid, path_with_namespace=path, name=path.rsplit("/", 1)[-1],
        description="", visibility=visibility, default_branch="main",
        last_activity_at=last, archived=False, size_bytes=size,
        namespace_kind="group",
    )


def _event(eid, *, project_id=None):
    return GitLabEvent(
        id=eid, action_name="pushed to", created_at="2026-04-20T12:00:00Z",
        target_type=None, target_title=None, project_id=project_id,
    )


class FakeGL:
    def __init__(self, projects=(), languages=None, events=(), user=None):
        self.projects = list(projects)
        self.languages = dict(languages or {})
        self.events = list(events)
        self.user = user or GitLabUser(id=1, username="alice")
        self.language_calls = 0

    def list_projects(self, **_):
        yield from self.projects

    def get_languages(self, project_id):
        self.language_calls += 1
        return dict(self.languages.get(project_id, {}))

    def list_user_events(self, user_id, limit=50):
        assert user_id == self.user.id
        return list(self.events[:limit])

    def me(self):
        return self.user


NOW = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)


def test_aggregate_size_weights_languages():
    projects = [
        _gl(1, "a/big", size=100_000),
        _gl(2, "a/small", size=1_000),
    ]
    langs = {
        1: {"C": 80.0, "Python": 20.0},
        2: {"Python": 100.0},
    }
    fake = FakeGL(projects, langs)

    data = stats.aggregate(fake, State(), top_n_languages=5, now=NOW)

    # Big contributes 80,000 C + 20,000 Python. Small contributes 1,000 Python.
    top_by_name = {s.name: s for s in data.top_languages}
    assert top_by_name["C"].bytes == 80_000
    assert top_by_name["Python"].bytes == 21_000
    # Percentage math: total = 101,000
    assert abs(top_by_name["C"].pct - 100 * 80_000 / 101_000) < 0.01


def test_top_n_truncates():
    projects = [_gl(i, f"a/p{i}", size=1000) for i in range(1, 5)]
    langs = {i: {f"L{i}": 100.0} for i in range(1, 5)}
    fake = FakeGL(projects, langs)

    data = stats.aggregate(fake, State(), top_n_languages=2, now=NOW)

    assert len(data.top_languages) == 2


def test_cache_hit_skips_language_calls():
    state = State()
    state.profile.language_cache = {"C": 123}
    state.profile.language_cache_utc = (NOW - timedelta(hours=1)).isoformat()

    fake = FakeGL([_gl(1, "a/p", size=1000)], {1: {"C": 100.0}})

    data = stats.aggregate(fake, state, now=NOW)

    assert fake.language_calls == 0
    assert data.top_languages[0].name == "C"
    assert data.top_languages[0].bytes == 123


def test_cache_expired_triggers_refresh():
    state = State()
    state.profile.language_cache = {"OldLang": 999}
    state.profile.language_cache_utc = (NOW - timedelta(hours=25)).isoformat()

    fake = FakeGL([_gl(1, "a/p", size=1000)], {1: {"C": 100.0}})

    data = stats.aggregate(fake, state, now=NOW)

    assert fake.language_calls == 1
    assert state.profile.language_cache == {"C": 1000}
    assert "OldLang" not in {s.name for s in data.top_languages}


def test_cache_populated_even_when_previously_empty():
    state = State()
    fake = FakeGL([_gl(1, "a/p", size=5000)], {1: {"Rust": 100.0}})

    stats.aggregate(fake, state, now=NOW)

    assert state.profile.language_cache == {"Rust": 5000}
    assert state.profile.language_cache_utc == NOW.isoformat()


def test_cache_invalid_timestamp_triggers_refresh():
    state = State()
    state.profile.language_cache = {"C": 42}
    state.profile.language_cache_utc = "not a date"
    fake = FakeGL([_gl(1, "a/p", size=100)], {1: {"C": 100.0}})

    stats.aggregate(fake, state, now=NOW)

    assert fake.language_calls == 1


def test_zero_size_project_contributes_nothing():
    fake = FakeGL(
        [_gl(1, "a/empty", size=0), _gl(2, "a/real", size=1000)],
        {1: {"C": 100.0}, 2: {"Python": 100.0}},
    )
    data = stats.aggregate(fake, State(), now=NOW)
    # get_languages may still be called for empty, but its contribution is 0.
    # Empty project is skipped entirely (size_bytes <= 0).
    assert fake.language_calls == 1
    names = [s.name for s in data.top_languages]
    assert "C" not in names
    assert "Python" in names


def test_language_fetch_error_is_isolated():
    class ErrGL(FakeGL):
        def get_languages(self, project_id):
            if project_id == 1:
                raise RuntimeError("boom")
            return super().get_languages(project_id)

    fake = ErrGL(
        [_gl(1, "a/broken", size=1000), _gl(2, "a/ok", size=1000)],
        {2: {"Rust": 100.0}},
    )
    data = stats.aggregate(fake, State(), now=NOW)

    assert {s.name for s in data.top_languages} == {"Rust"}


def test_recent_repos_count_zero_returns_all_public():
    projects = [
        _gl(1, "a/p1", last="2026-01-01T00:00:00Z"),
        _gl(2, "a/p2", last="2026-02-01T00:00:00Z"),
        _gl(3, "a/p3", visibility="private", last="2026-03-01T00:00:00Z"),
        _gl(4, "a/p4", last="2026-04-01T00:00:00Z"),
    ]
    fake = FakeGL(projects)
    data = stats.aggregate(fake, State(), recent_repos_count=0, now=NOW)

    # All 3 public repos, sorted by last_activity desc
    assert [r.name for r in data.recent_repos] == ["p4", "p2", "p1"]


def test_recent_repos_public_only_and_sorted():
    projects = [
        _gl(1, "a/old-public", last="2026-01-01T00:00:00Z"),
        _gl(2, "a/new-private", visibility="private", last="2026-04-20T12:00:00Z"),
        _gl(3, "a/new-public", last="2026-04-15T12:00:00Z"),
        _gl(4, "a/mid-public", last="2026-03-01T12:00:00Z"),
    ]
    fake = FakeGL(projects)
    data = stats.aggregate(fake, State(), recent_repos_count=5, now=NOW)

    names = [r.path_with_namespace for r in data.recent_repos]
    assert names == ["a/new-public", "a/mid-public", "a/old-public"]
    assert data.total_public_repos == 3
    assert data.total_all_repos == 4


def test_recent_activity_filtered_to_public_projects():
    projects = [
        _gl(1, "a/public", visibility="public"),
        _gl(2, "a/secret", visibility="private"),
    ]
    events = [
        _event(100, project_id=1),  # public -> keep
        _event(101, project_id=2),  # private -> drop
        _event(102, project_id=None),  # no project -> keep
        _event(103, project_id=999),  # unknown -> drop
    ]
    fake = FakeGL(projects, events=events)

    data = stats.aggregate(fake, State(), recent_activity_count=5, now=NOW)

    assert [e.id for e in data.recent_activity] == [100, 102]


def test_recent_activity_honours_count_after_filter():
    projects = [_gl(1, "a/public")]
    events = [_event(i, project_id=1) for i in range(1, 11)]
    fake = FakeGL(projects, events=events)

    data = stats.aggregate(fake, State(), recent_activity_count=3, now=NOW)

    assert len(data.recent_activity) == 3
    assert [e.id for e in data.recent_activity] == [1, 2, 3]


def test_empty_inputs_produce_empty_profile():
    data = stats.aggregate(FakeGL(), State(), now=NOW)

    assert data.top_languages == []
    assert data.recent_activity == []
    assert data.recent_repos == []
    assert data.total_all_repos == 0
    assert data.total_public_repos == 0
