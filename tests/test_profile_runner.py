"""Tests for the profile runner (aggregate + render + publish, with hash gate)."""
from __future__ import annotations

import base64
from pathlib import Path

from git_sync.clients.gitlab import GitLabEvent, GitLabProject, GitLabUser
from git_sync.profile.runner import ProfileRunner
from git_sync.state import State


def _gl(gid, path, *, visibility="public", size=1000):
    return GitLabProject(
        id=gid, path_with_namespace=path, name=path.rsplit("/", 1)[-1],
        description="", visibility=visibility, default_branch="main",
        last_activity_at="2026-04-20T12:00:00Z", archived=False, size_bytes=size,
        namespace_kind="group",
    )


class FakeGitLab:
    def __init__(self, *, projects=(), languages=None, events=(), known_projects=None):
        self.projects = list(projects)
        self.languages = dict(languages or {})
        self.events = list(events)
        # known_projects maps path -> GitLabProject (what get_project returns).
        self.known_projects: dict[str, GitLabProject] = dict(known_projects or {})
        self.files: dict[tuple[int, str], dict[str, str]] = {}
        self.puts: list[dict] = []
        self.created_projects: list[dict] = []
        self._next_id = 1000

    def list_projects(self, **_):
        yield from self.projects

    def get_languages(self, project_id):
        return dict(self.languages.get(project_id, {}))

    def list_user_events(self, user_id, limit=50):
        return list(self.events[:limit])

    def me(self):
        return GitLabUser(id=1, username="alice")

    def get_project(self, path_or_id):
        key = str(path_or_id)
        if key in self.known_projects:
            return self.known_projects[key]
        from git_sync.clients.http import HTTPError
        raise HTTPError(404, "GET", key, "not found")

    def create_project(self, *, name, visibility="public", default_branch="main"):
        self._next_id += 1
        project = GitLabProject(
            id=self._next_id,
            path_with_namespace=f"alice/{name}",
            name=name, description="",
            visibility=visibility, default_branch=default_branch,
            last_activity_at="2026-04-20T12:00:00Z",
            archived=False, size_bytes=0,
            namespace_kind="user",
        )
        self.known_projects[f"alice/{name}"] = project
        self.created_projects.append({
            "name": name, "visibility": visibility,
            "default_branch": default_branch,
        })
        return project

    def get_file(self, project_id, file_path, *, ref):
        return self.files.get((project_id, file_path))

    def put_file(self, project_id, file_path, content, *,
                 branch, commit_message, author_name, author_email,
                 last_commit_id=None):
        self.puts.append({
            "project_id": project_id, "file_path": file_path,
            "content": content, "branch": branch,
            "commit_message": commit_message,
            "author_name": author_name, "author_email": author_email,
            "last_commit_id": last_commit_id,
        })
        self.files[(project_id, file_path)] = {
            "content": content, "blob_id": "b", "last_commit_id": "c-after",
        }


class FakeGitHub:
    def __init__(self, *, files=None, repos=None):
        self.files: dict[tuple[str, str, str], dict[str, str]] = dict(files or {})
        # repos maps (owner, name) -> GitHubRepo
        self.repos: dict[tuple[str, str], "GitHubRepo"] = dict(repos or {})
        self.puts: list[dict] = []
        self.created_repos: list[dict] = []

    def get_repo(self, owner, repo):
        return self.repos.get((owner, repo))

    def create_repo(self, name, *, private, description=""):
        from git_sync.clients.github import GitHubRepo
        repo = GitHubRepo(
            name=name, full_name=f"alice/{name}", private=private,
            description=description, default_branch="main", archived=False,
        )
        self.repos[("alice", name)] = repo
        self.created_repos.append(
            {"name": name, "private": private, "description": description},
        )
        return repo

    def get_file(self, owner, repo, path, *, ref=None):
        return self.files.get((owner, repo, path))

    def put_file(self, owner, repo, path, content_base64, *,
                 commit_message, branch, author_name, author_email, sha=None):
        self.puts.append({
            "owner": owner, "repo": repo, "path": path,
            "content_base64": content_base64,
            "commit_message": commit_message, "branch": branch,
            "author_name": author_name, "author_email": author_email,
            "sha": sha,
        })
        self.files[(owner, repo, path)] = {"content": content_base64, "sha": "after"}


def _profile_gl_project(gid=42, path="alice/alice", default_branch="main"):
    return GitLabProject(
        id=gid, path_with_namespace=path, name=path.rsplit("/", 1)[-1],
        description="", visibility="public", default_branch=default_branch,
        last_activity_at="2026-04-20T12:00:00Z", archived=False, size_bytes=0,
        namespace_kind="user",
    )


def _default_gh_repo():
    from git_sync.clients.github import GitHubRepo
    return GitHubRepo(
        name="alice", full_name="alice/alice", private=False,
        description="", default_branch="main", archived=False,
    )


def _runner(tmp_path: Path, *, gl=None, gh=None, state=None, disclaimer=""):
    about = tmp_path / "about.md"
    about.write_text("I like C.")
    gl = gl or FakeGitLab(
        projects=[_gl(10, "alice/foo")],
        languages={10: {"C": 100.0}},
        known_projects={"alice/alice": _profile_gl_project()},
    )
    gh = gh or FakeGitHub(repos={("alice", "alice"): _default_gh_repo()})
    return ProfileRunner(
        gitlab_client=gl,
        gitlab_url="https://git.example.com",
        gitlab_profile_path="alice/alice",
        github_client=gh,
        github_owner="alice",
        github_profile_repo="alice",
        author_name="Alice",
        author_email="a@e",
        about_path=about,
        github_disclaimer=disclaimer,
        state=state or State(),
        top_n_languages=5,
        recent_activity_count=5,
        recent_repos_count=5,
    )


def test_first_run_publishes_both(tmp_path):
    r = _runner(tmp_path, disclaimer="mirror")

    result = r.run()

    assert result.published_gitlab is True
    assert result.published_github is True
    assert len(r.gitlab.puts) == 1
    assert r.gitlab.puts[0]["last_commit_id"] is None  # create, no CAS
    assert r.gitlab.puts[0]["author_name"] == "Alice"
    assert r.gitlab.puts[0]["content"].startswith("I like C.")

    assert len(r.github.puts) == 1
    decoded = base64.b64decode(r.github.puts[0]["content_base64"]).decode()
    assert decoded.startswith("> mirror")
    assert "I like C." in decoded
    assert r.github.puts[0]["sha"] is None


def test_second_identical_run_is_noop(tmp_path):
    r = _runner(tmp_path)
    r.run()
    r.gitlab.puts.clear()
    r.github.puts.clear()

    result = r.run()

    assert result.skipped_gitlab_unchanged is True
    assert result.skipped_github_unchanged is True
    assert r.gitlab.puts == []
    assert r.github.puts == []


def test_dry_run_makes_no_writes(tmp_path):
    r = _runner(tmp_path)
    result = r.run(dry_run=True)

    assert r.gitlab.puts == []
    assert r.github.puts == []
    assert result.published_gitlab is False
    assert result.published_github is False
    assert r.state.profile.last_gitlab_hash is None


def test_gitlab_update_passes_last_commit_id(tmp_path):
    gl = FakeGitLab(
        projects=[_gl(10, "alice/foo")],
        languages={10: {"C": 100.0}},
        known_projects={"alice/alice": _profile_gl_project()},
    )
    gl.files[(42, "README.md")] = {
        "content": "old", "blob_id": "b", "last_commit_id": "c-before",
    }
    r = _runner(tmp_path, gl=gl)

    r.run()

    assert r.gitlab.puts[0]["last_commit_id"] == "c-before"


def test_github_update_passes_sha(tmp_path):
    gh = FakeGitHub(
        files={("alice", "alice", "README.md"): {"content": "x", "sha": "prior"}},
        repos={("alice", "alice"): _default_gh_repo()},
    )
    r = _runner(tmp_path, gh=gh)

    r.run()

    assert r.github.puts[0]["sha"] == "prior"


def test_auto_creates_gitlab_profile_repo_if_missing(tmp_path):
    gl = FakeGitLab(
        projects=[_gl(10, "alice/foo")],
        languages={10: {"C": 100.0}},
        known_projects={},  # profile repo missing on GitLab
    )
    r = _runner(tmp_path, gl=gl)

    r.run()

    assert len(r.gitlab.created_projects) == 1
    assert r.gitlab.created_projects[0]["name"] == "alice"
    assert r.gitlab.created_projects[0]["visibility"] == "public"
    assert r.gitlab.created_projects[0]["default_branch"] == "main"
    assert len(r.gitlab.puts) == 1
    assert r.gitlab.puts[0]["branch"] == "main"
    assert r.gitlab.puts[0]["last_commit_id"] is None


def test_auto_creates_github_profile_repo_if_missing(tmp_path):
    gh = FakeGitHub()  # no repos
    r = _runner(tmp_path, gh=gh)

    r.run()

    assert len(r.github.created_repos) == 1
    assert r.github.created_repos[0]["name"] == "alice"
    assert r.github.created_repos[0]["private"] is False
    assert len(r.github.puts) == 1
    assert r.github.puts[0]["sha"] is None


def test_auto_create_is_idempotent_across_two_runs(tmp_path):
    gl = FakeGitLab(
        projects=[_gl(10, "alice/foo")],
        languages={10: {"C": 100.0}},
        known_projects={},
    )
    gh = FakeGitHub()
    r = _runner(tmp_path, gl=gl, gh=gh)

    r.run()
    created_gl = len(gl.created_projects)
    created_gh = len(gh.created_repos)

    r.run()

    assert len(gl.created_projects) == created_gl  # no additional create
    assert len(gh.created_repos) == created_gh


def test_github_disclaimer_only_on_github_output(tmp_path):
    r = _runner(tmp_path, disclaimer="see gitlab for sources")
    r.run()

    gitlab_body = r.gitlab.puts[0]["content"]
    github_body = base64.b64decode(
        r.github.puts[0]["content_base64"]
    ).decode()
    assert "see gitlab for sources" in github_body
    assert "see gitlab for sources" not in gitlab_body


def test_gitlab_failure_does_not_block_github(tmp_path):
    class BoomGL(FakeGitLab):
        def put_file(self, *a, **kw):
            raise RuntimeError("gitlab down")

    gl = BoomGL(
        projects=[_gl(10, "alice/foo")],
        languages={10: {"C": 100.0}},
        known_projects={"alice/alice": _profile_gl_project()},
    )
    r = _runner(tmp_path, gl=gl)

    result = r.run()

    assert result.failed_gitlab is True
    assert result.published_gitlab is False
    assert result.published_github is True
    assert r.state.profile.last_gitlab_hash is None
    assert r.state.profile.last_github_hash is not None


def test_missing_about_file_renders_without_bio(tmp_path):
    # about path points somewhere that doesn't exist
    r = _runner(tmp_path)
    r.about_path = tmp_path / "nonexistent.md"

    r.run()

    assert "I like C." not in r.gitlab.puts[0]["content"]


def test_last_publish_utc_set_on_publish(tmp_path):
    r = _runner(tmp_path)
    r.run()
    assert r.state.profile.last_publish_utc is not None
