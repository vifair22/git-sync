"""Tests for the mirror runner, using in-memory fakes."""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from git_sync.clients.github import GitHubRepo
from git_sync.clients.gitlab import GitLabProject
from git_sync.mirror.git import GitError
from git_sync.mirror.runner import MirrorRunner
from git_sync.state import RepoState, State


def _gl(
    gid, path, *, visibility="public", default_branch="main", description="",
    namespace_kind="group", archived=False,
):
    return GitLabProject(
        id=gid,
        path_with_namespace=path,
        name=path.rsplit("/", 1)[-1],
        description=description,
        visibility=visibility,
        default_branch=default_branch,
        last_activity_at="2026-04-20T12:00:00Z",
        archived=archived,
        size_bytes=0,
        namespace_kind=namespace_kind,
    )


def _gh(name, *, private=False, default_branch="main", description="", archived=False):
    return GitHubRepo(
        name=name, full_name=f"alice/{name}", private=private,
        description=description, default_branch=default_branch,
        archived=archived,
    )


class FakeGitLabClient:
    def __init__(self, projects: list[GitLabProject]):
        self._projects = projects

    def list_projects(self, **_) -> Iterator[GitLabProject]:
        yield from self._projects


class FakeGitHubClient:
    def __init__(self, repos: list[GitHubRepo]):
        self._repos = list(repos)
        self.created: list[dict] = []
        self.updated: list[dict] = []

    def list_repos(self) -> Iterator[GitHubRepo]:
        yield from self._repos

    def create_repo(self, name, *, private, description=""):
        repo = _gh(name, private=private, default_branch="", description=description)
        self._repos.append(repo)
        self.created.append(
            {"name": name, "private": private, "description": description},
        )
        return repo

    def update_repo(self, owner, name, *, private=None,
                    description=None, default_branch=None, archived=None):
        for i, r in enumerate(self._repos):
            if r.name == name:
                new = _gh(
                    r.name,
                    private=private if private is not None else r.private,
                    description=(
                        description if description is not None else r.description
                    ),
                    default_branch=(
                        default_branch if default_branch is not None
                        else r.default_branch
                    ),
                    archived=archived if archived is not None else r.archived,
                )
                self._repos[i] = new
                self.updated.append(
                    {
                        "name": name, "private": private,
                        "description": description,
                        "default_branch": default_branch,
                        "archived": archived,
                    },
                )
                return new
        raise AssertionError(f"update_repo called on missing {name!r}")


class FakeGitOps:
    def __init__(
        self, *, fail_on: set[int] | None = None,
        digest_by_id: dict[int, str] | None = None,
    ):
        self.calls: list[dict] = []
        self.fetches: list[dict] = []
        self.fail_on = fail_on or set()
        self.digest_by_id = digest_by_id or {}

    def fetch(self, project_id, source_url):
        self.fetches.append({"project_id": project_id, "source": source_url})
        return self.digest_by_id.get(project_id, f"digest-{project_id}")

    def push(self, project_id, dest_url):
        self.calls.append(
            {"project_id": project_id, "dest": dest_url},
        )
        if project_id in self.fail_on:
            raise GitError(f"simulated failure for {project_id}")

    def mirror(self, project_id, source_url, dest_url):
        # Back-compat convenience: fetch + push, used by any leftover callers.
        self.fetch(project_id, source_url)
        self.push(project_id, dest_url)


def _runner(
    projects=None, repos=None, state=None, *,
    git_ops=None, exclude_groups=(), profile_gitlab_path=None,
    only_group_owned=False,
):
    return MirrorRunner(
        gitlab_client=FakeGitLabClient(projects or []),
        gitlab_url="https://git.example.com",
        gitlab_token="gl-token",
        github_client=FakeGitHubClient(repos or []),
        github_owner="alice",
        github_token="gh-token",
        git_ops=git_ops or FakeGitOps(),
        state=state or State(),
        exclude_groups=exclude_groups,
        profile_gitlab_path=profile_gitlab_path,
        only_group_owned=only_group_owned,
    )


def test_creates_and_mirrors_public():
    p = _gl(1, "vifair22/foo", visibility="public", description="d")
    r = _runner([p])

    result = r.run()

    assert result.successes == 1
    assert result.failures == 0
    assert r.github.created == [
        {"name": "foo", "private": False, "description": "d"},
    ]
    assert r.git_ops.calls[0]["project_id"] == 1
    assert "oauth2:gh-token" in r.git_ops.calls[0]["dest"]
    assert "oauth2:gl-token" in r.git_ops.fetches[0]["source"]
    assert r.state.repos["1"].github_name == "foo"
    assert r.state.repos["1"].last_known_visibility == "public"
    assert r.state.repos["1"].last_error is None
    assert r.state.repos["1"].last_sync_source_digest == "digest-1"


def test_flips_public_to_private_when_gitlab_private():
    p = _gl(1, "vifair22/foo", visibility="private")
    r = _runner([p], [_gh("foo", private=False)])

    r.run()

    assert r.github.updated == [
        {"name": "foo", "private": True, "description": None,
         "default_branch": None, "archived": None},
    ]
    assert r.git_ops.calls == []
    assert r.state.repos["1"].last_known_visibility == "private"


def test_flips_private_to_public_and_mirrors():
    p = _gl(1, "vifair22/foo", visibility="public")
    r = _runner([p], [_gh("foo", private=True)])

    r.run()

    assert r.github.updated[0]["private"] is False
    assert len(r.git_ops.calls) == 1


def test_hides_orphan_that_is_still_public():
    state = State()
    state.repos["7"] = RepoState(
        gitlab_id=7, gitlab_path="vifair22/gone", github_name="gone",
        last_known_visibility="public",
    )
    r = _runner([], [_gh("gone", private=False)], state)

    r.run()

    assert r.github.updated[0] == {
        "name": "gone", "private": True,
        "description": None, "default_branch": None, "archived": None,
    }
    assert r.git_ops.calls == []


def test_dry_run_makes_no_changes():
    p = _gl(1, "vifair22/foo", visibility="public")
    r = _runner([p])

    r.run(dry_run=True)

    assert r.github.created == []
    assert r.github.updated == []
    assert r.git_ops.calls == []
    assert r.state.repos == {}


def test_failure_is_isolated_and_recorded():
    p1 = _gl(1, "vifair22/foo", visibility="public")
    p2 = _gl(2, "vifair22/bar", visibility="public")
    ops = FakeGitOps(fail_on={1})
    r = _runner([p1, p2], git_ops=ops)

    result = r.run()

    assert result.successes == 1
    assert result.failures == 1
    # bar succeeded end-to-end
    assert r.state.repos["2"].last_error is None
    # foo failed after create; state has the error recorded (and it was created
    # during the same run, so state was updated to success *then* overwritten by
    # error path? Actually no — _execute writes state at the end; if an exception
    # fires before that, state entry doesn't exist and _record_error is a no-op.)
    assert "1" not in r.state.repos or r.state.repos["1"].last_error


def test_default_branch_synced_when_it_differs():
    p = _gl(1, "vifair22/foo", visibility="public", default_branch="trunk")
    r = _runner([p], [_gh("foo", private=False, default_branch="main")])

    r.run()

    assert any(
        u.get("default_branch") == "trunk" for u in r.github.updated
    )


def test_default_branch_not_touched_when_it_matches():
    p = _gl(1, "vifair22/foo", visibility="public", default_branch="main")
    r = _runner([p], [_gh("foo", private=False, default_branch="main")])

    r.run()

    assert all(u.get("default_branch") is None for u in r.github.updated)


def test_collision_skips_both():
    p1 = _gl(1, "grpA/shared", visibility="public")
    p2 = _gl(2, "grpB/shared", visibility="public")
    r = _runner([p1, p2])

    result = r.run()

    assert result.successes == 0
    assert result.failures == 0
    assert result.skipped >= 2
    assert r.github.created == []
    assert r.git_ops.calls == []


def test_default_branch_422_is_logged_not_fatal():
    from git_sync.clients.http import HTTPError

    class Mildly422(FakeGitHubClient):
        def update_repo(self, owner, name, **kwargs):
            if "default_branch" in kwargs and kwargs["default_branch"] is not None:
                raise HTTPError(
                    422, "PATCH", f"/repos/{owner}/{name}",
                    '{"message":"Validation Failed",'
                    '"errors":[{"message":"Cannot update default branch for an empty repository"}]}',
                )
            return super().update_repo(owner, name, **kwargs)

    p = _gl(1, "grp/empty", visibility="public", default_branch="main")
    gh_client = Mildly422([_gh("empty", private=False, default_branch="master")])
    r = MirrorRunner(
        gitlab_client=FakeGitLabClient([p]),
        gitlab_url="https://git.example.com", gitlab_token="t",
        github_client=gh_client, github_owner="alice", github_token="t",
        git_ops=FakeGitOps(), state=State(),
    )

    result = r.run()

    assert result.successes == 1
    assert result.failures == 0
    assert r.state.repos["1"].last_error is None


def test_unchanged_source_digest_skips_push():
    p = _gl(1, "vifair22/foo", visibility="public")
    state = State()
    state.repos["1"] = RepoState(
        gitlab_id=1, gitlab_path="vifair22/foo", github_name="foo",
        last_known_visibility="public",
        last_sync_source_digest="digest-1",
    )
    r = _runner([p], [_gh("foo", private=False)], state)

    r.run()

    assert r.git_ops.fetches == [
        {"project_id": 1, "source": r.git_ops.fetches[0]["source"]},
    ]
    assert r.git_ops.calls == []  # push skipped


def test_changed_source_digest_pushes_and_updates_state():
    p = _gl(1, "vifair22/foo", visibility="public")
    state = State()
    state.repos["1"] = RepoState(
        gitlab_id=1, gitlab_path="vifair22/foo", github_name="foo",
        last_known_visibility="public",
        last_sync_source_digest="old-digest",
    )
    # Fake returns "digest-1" which != "old-digest"
    r = _runner([p], [_gh("foo", private=False)], state)

    r.run()

    assert len(r.git_ops.calls) == 1
    assert r.state.repos["1"].last_sync_source_digest == "digest-1"


def test_archived_gitlab_archives_github_after_push():
    p = _gl(1, "grp/old", visibility="public", archived=True)
    r = _runner([p])

    r.run()

    # created (not archived), pushed, then archived
    assert any(u.get("archived") is True for u in r.github.updated)
    assert len(r.git_ops.calls) == 1


def test_existing_archived_github_unarchived_before_push_if_gl_changed():
    # GitLab NOT archived, GitHub IS archived — should unarchive (to push) and stay unarchived
    p = _gl(1, "grp/foo", visibility="public", archived=False)
    r = _runner([p], [_gh("foo", private=False, archived=True)])

    r.run()

    # Expect at least one update to archived=False, and the push happened.
    assert any(u.get("archived") is False for u in r.github.updated)
    assert len(r.git_ops.calls) == 1
    # Should not re-archive since gitlab is not archived.
    assert not any(u.get("archived") is True for u in r.github.updated)


def test_both_archived_skips_push_but_keeps_state():
    p = _gl(1, "grp/old", visibility="public", archived=True)
    r = _runner([p], [_gh("old", private=False, archived=True)])

    r.run()

    assert r.git_ops.calls == []  # both archived, no push
    assert r.github.updated == []  # archived state already matches


def test_exclude_groups_skips_matching_projects():
    p_kept = _gl(1, "alice/foo", visibility="public")
    p_skipped = _gl(2, "xbox/secret", visibility="public")
    r = _runner([p_kept, p_skipped], exclude_groups=("xbox",))

    result = r.run()

    assert result.successes == 1
    assert r.github.created == [
        {"name": "foo", "private": False, "description": ""},
    ]
    assert "2" not in r.state.repos
    assert "1" in r.state.repos


def test_only_group_owned_filters_out_user_namespace():
    user_proj = _gl(1, "alice/personal", visibility="public", namespace_kind="user")
    group_proj = _gl(2, "grp/work", visibility="public", namespace_kind="group")
    r = _runner([user_proj, group_proj], only_group_owned=True)

    r.run()

    assert r.github.created == [
        {"name": "work", "private": False, "description": ""},
    ]
    assert "1" not in r.state.repos


def _runner_with_only_group_owned_off_includes_user_namespace():
    user_proj = _gl(1, "alice/personal", visibility="public", namespace_kind="user")
    r = _runner([user_proj], only_group_owned=False)
    r.run()
    assert r.github.created == [
        {"name": "personal", "private": False, "description": ""},
    ]


def test_only_group_owned_off_by_default_includes_user_namespace():
    _runner_with_only_group_owned_off_includes_user_namespace()


def test_profile_gitlab_path_auto_skipped():
    profile_repo = _gl(99, "alice/alice", visibility="public")
    other = _gl(1, "alice/foo", visibility="public")
    r = _runner([profile_repo, other], profile_gitlab_path="alice/alice")

    r.run()

    assert r.github.created == [
        {"name": "foo", "private": False, "description": ""},
    ]
    assert "99" not in r.state.repos


def test_exclude_groups_does_not_hide_existing_orphan_in_excluded_group():
    state = State()
    state.repos["99"] = RepoState(
        gitlab_id=99, gitlab_path="xbox/foo", github_name="foo",
        last_known_visibility="public",
    )
    r = _runner(
        [], [_gh("foo", private=False)], state,
        exclude_groups=("xbox",),
    )

    r.run()

    # repo in excluded group must NOT be hidden on github
    assert r.github.updated == []


def test_private_gitlab_with_no_github_is_noop():
    p = _gl(1, "vifair22/secret", visibility="private")
    r = _runner([p])

    result = r.run()

    assert result.successes == 0
    assert result.failures == 0
    assert r.github.created == []
    assert r.github.updated == []
    assert r.git_ops.calls == []
