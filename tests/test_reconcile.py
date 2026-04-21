"""Tests for the reconcile planner (pure function)."""
from __future__ import annotations

from git_sync.clients.github import GitHubRepo
from git_sync.clients.gitlab import GitLabProject
from git_sync.mirror import reconcile
from git_sync.state import RepoState, State


def _gl(
    gid, path, visibility="public", default_branch="main", archived=False,
    namespace_kind="group",
):
    return GitLabProject(
        id=gid,
        path_with_namespace=path,
        name=path.rsplit("/", 1)[-1],
        description="",
        visibility=visibility,
        default_branch=default_branch,
        last_activity_at="2026-04-20T12:00:00Z",
        archived=archived,
        size_bytes=0,
        namespace_kind=namespace_kind,
    )


def _gh(name, *, private=False, default_branch="main", archived=False):
    return GitHubRepo(
        name=name,
        full_name=f"alice/{name}",
        private=private,
        description="",
        default_branch=default_branch,
        archived=archived,
    )


def test_derive_github_name_uses_last_segment():
    assert reconcile.derive_github_name(_gl(1, "vifair22/foo")) == "foo"
    assert reconcile.derive_github_name(_gl(1, "vifair22/sub/bar")) == "bar"


def test_public_gitlab_no_github_creates_and_mirrors():
    p = _gl(1, "vifair22/foo", "public")
    plan = reconcile.plan([p], {}, State())
    assert len(plan.actions) == 1
    a = plan.actions[0]
    assert a.github_name == "foo"
    assert a.desired_private is False
    assert a.mirror_data is True
    assert a.project is p
    assert "create" in a.reason


def test_public_gitlab_public_github_mirrors_only():
    p = _gl(1, "vifair22/foo", "public")
    plan = reconcile.plan([p], {"foo": _gh("foo", private=False)}, State())
    assert plan.actions[0].desired_private is False
    assert plan.actions[0].mirror_data is True
    assert plan.actions[0].reason == "mirror"


def test_public_gitlab_private_github_flips_and_mirrors():
    p = _gl(1, "vifair22/foo", "public")
    plan = reconcile.plan([p], {"foo": _gh("foo", private=True)}, State())
    a = plan.actions[0]
    assert a.desired_private is False
    assert a.mirror_data is True
    assert "flip" in a.reason


def test_private_gitlab_no_github_is_skipped():
    p = _gl(1, "vifair22/secret", "private")
    plan = reconcile.plan([p], {}, State())
    assert plan.actions == []
    assert len(plan.skipped) == 1


def test_private_gitlab_public_github_flips_to_private():
    p = _gl(1, "vifair22/foo", "private")
    plan = reconcile.plan([p], {"foo": _gh("foo", private=False)}, State())
    a = plan.actions[0]
    assert a.desired_private is True
    assert a.mirror_data is False
    assert "hide" in a.reason


def test_internal_gitlab_treated_as_private():
    p = _gl(1, "vifair22/foo", "internal")
    plan = reconcile.plan([p], {"foo": _gh("foo", private=False)}, State())
    assert plan.actions[0].desired_private is True
    assert plan.actions[0].mirror_data is False


def test_private_gitlab_private_github_is_skipped():
    p = _gl(1, "vifair22/foo", "private")
    plan = reconcile.plan([p], {"foo": _gh("foo", private=True)}, State())
    assert plan.actions == []


def test_orphan_in_state_hides_public_github():
    state = State()
    state.repos["42"] = RepoState(
        gitlab_id=42,
        gitlab_path="vifair22/gone",
        github_name="gone",
        last_known_visibility="public",
    )
    plan = reconcile.plan([], {"gone": _gh("gone", private=False)}, state)
    a = plan.actions[0]
    assert a.project is None
    assert a.desired_private is True
    assert a.mirror_data is False
    assert "gone" in a.reason


def test_orphan_in_state_but_github_absent_is_skipped():
    state = State()
    state.repos["42"] = RepoState(
        gitlab_id=42, gitlab_path="vifair22/gone", github_name="gone",
        last_known_visibility="public",
    )
    plan = reconcile.plan([], {}, state)
    assert plan.actions == []
    assert any("already gone" in s for s in plan.skipped)


def test_orphan_already_private_is_skipped():
    state = State()
    state.repos["42"] = RepoState(
        gitlab_id=42, gitlab_path="vifair22/gone", github_name="gone",
        last_known_visibility="private",
    )
    plan = reconcile.plan([], {"gone": _gh("gone", private=True)}, state)
    assert plan.actions == []
    assert any("already hidden" in s for s in plan.skipped)


def test_live_project_in_state_is_not_treated_as_orphan():
    state = State()
    state.repos["1"] = RepoState(
        gitlab_id=1, gitlab_path="vifair22/foo", github_name="foo",
        last_known_visibility="public",
    )
    p = _gl(1, "vifair22/foo", "public")
    plan = reconcile.plan([p], {"foo": _gh("foo", private=False)}, state)
    assert len(plan.actions) == 1
    assert plan.actions[0].project is p


def test_mirror_private_creates_private_github():
    p = _gl(1, "vifair22/secret", "private")
    plan = reconcile.plan([p], {}, State(), mirror_private=True)
    assert len(plan.actions) == 1
    a = plan.actions[0]
    assert a.desired_private is True
    assert a.mirror_data is True
    assert "create" in a.reason


def test_mirror_private_flips_public_github_to_private_and_mirrors():
    p = _gl(1, "vifair22/secret", "private")
    plan = reconcile.plan(
        [p], {"secret": _gh("secret", private=False)}, State(),
        mirror_private=True,
    )
    a = plan.actions[0]
    assert a.desired_private is True
    assert a.mirror_data is True
    assert "flip" in a.reason


def test_mirror_private_mirrors_existing_private_github():
    p = _gl(1, "vifair22/secret", "private")
    plan = reconcile.plan(
        [p], {"secret": _gh("secret", private=True)}, State(),
        mirror_private=True,
    )
    a = plan.actions[0]
    assert a.desired_private is True
    assert a.mirror_data is True
    assert a.reason == "mirror (private)"


def test_mirror_private_leaves_public_path_unchanged():
    p = _gl(1, "vifair22/foo", "public")
    plan = reconcile.plan([p], {}, State(), mirror_private=True)
    assert plan.actions[0].desired_private is False


def test_collision_skips_both_and_records():
    p1 = _gl(1, "groupA/shared", "public")
    p2 = _gl(2, "groupB/shared", "public")
    plan = reconcile.plan([p1, p2], {}, State())
    assert plan.actions == []
    assert "shared" in plan.collisions
    assert set(plan.collisions["shared"]) == {"groupA/shared", "groupB/shared"}
    assert len(plan.skipped) == 2
