"""Tests for the GitLab client."""
from __future__ import annotations

from git_sync.clients import gitlab


def _client(stub_server):
    return gitlab.GitLabClient(stub_server.base_url, token="glpat-xxx")


def test_me_parses_user(stub_server):
    stub_server.enqueue(
        "GET", "/api/v4/user", body={"id": 42, "username": "vifair22"},
    )
    user = _client(stub_server).me()
    assert user == gitlab.GitLabUser(id=42, username="vifair22")
    assert stub_server.hits[0]["headers"]["private-token"] == "glpat-xxx"


def test_list_projects_parses_fields_and_paginates(stub_server):
    page1 = [
        {
            "id": 10,
            "path_with_namespace": "vifair22/foo",
            "name": "foo",
            "description": "a repo",
            "visibility": "public",
            "default_branch": "master",
            "last_activity_at": "2026-04-19T12:00:00Z",
            "archived": False,
            "statistics": {"repository_size": 12345},
            "namespace": {"kind": "group"},
        },
    ]
    page2 = [
        {
            "id": 11,
            "path_with_namespace": "vifair22/bar",
            "name": "bar",
            "description": None,
            "visibility": "private",
            "default_branch": None,
            "last_activity_at": "2026-04-18T12:00:00Z",
            "archived": True,
            "namespace": {"kind": "user"},
        },
    ]
    stub_server.enqueue(
        "GET", "/api/v4/projects", body=page1, headers={"X-Next-Page": "2"},
    )
    stub_server.enqueue(
        "GET", "/api/v4/projects", body=page2, headers={"X-Next-Page": ""},
    )

    projects = list(_client(stub_server).list_projects())

    assert [p.id for p in projects] == [10, 11]

    foo = projects[0]
    assert foo.path_with_namespace == "vifair22/foo"
    assert foo.visibility == "public"
    assert foo.default_branch == "master"
    assert foo.size_bytes == 12345
    assert foo.archived is False
    assert foo.namespace_kind == "group"

    bar = projects[1]
    assert bar.description == ""
    assert bar.default_branch is None
    assert bar.size_bytes == 0
    assert bar.archived is True
    assert bar.namespace_kind == "user"

    query = stub_server.hits[0]["path"]
    assert "membership=true" in query
    assert "min_access_level=40" in query
    assert "statistics=true" in query


def test_list_projects_without_statistics(stub_server):
    stub_server.enqueue("GET", "/api/v4/projects", body=[])
    list(_client(stub_server).list_projects(include_statistics=False))
    assert "statistics=false" in stub_server.hits[0]["path"]


def test_get_languages_returns_percentages(stub_server):
    stub_server.enqueue(
        "GET",
        "/api/v4/projects/10/languages",
        body={"C": 78.5, "Python": 21.5},
    )
    langs = _client(stub_server).get_languages(10)
    assert langs == {"C": 78.5, "Python": 21.5}


def test_get_languages_empty(stub_server):
    stub_server.enqueue("GET", "/api/v4/projects/10/languages", body={})
    assert _client(stub_server).get_languages(10) == {}


def test_get_project_returns_project(stub_server):
    stub_server.enqueue(
        "GET", "/api/v4/projects/vifair22%2Fvifair22",
        body={
            "id": 99, "path_with_namespace": "vifair22/vifair22", "name": "vifair22",
            "description": "", "visibility": "public", "default_branch": "main",
            "last_activity_at": "2026-04-20T12:00:00Z", "archived": False,
        },
    )
    p = _client(stub_server).get_project("vifair22/vifair22")
    assert p.id == 99
    assert p.default_branch == "main"


def test_create_project_posts_and_parses(stub_server):
    stub_server.enqueue(
        "POST", "/api/v4/projects",
        status=201,
        body={
            "id": 200, "path_with_namespace": "alice/alice", "name": "alice",
            "description": "", "visibility": "public", "default_branch": "main",
            "last_activity_at": "2026-04-20T12:00:00Z", "archived": False,
        },
    )
    p = _client(stub_server).create_project(name="alice")
    assert p.id == 200
    import json as _j
    sent = _j.loads(stub_server.hits[0]["body"])
    assert sent["name"] == "alice"
    assert sent["path"] == "alice"
    assert sent["visibility"] == "public"
    assert sent["default_branch"] == "main"


def test_get_file_returns_blob_info(stub_server):
    stub_server.enqueue(
        "GET",
        "/api/v4/projects/99/repository/files/README.md",
        body={
            "content": "aGk=", "blob_id": "bl-1", "last_commit_id": "c-1",
        },
    )
    f = _client(stub_server).get_file(99, "README.md", ref="master")
    assert f == {"content": "aGk=", "blob_id": "bl-1", "last_commit_id": "c-1"}
    assert "ref=master" in stub_server.hits[0]["path"]


def test_get_file_returns_none_on_404(stub_server):
    stub_server.enqueue(
        "GET",
        "/api/v4/projects/99/repository/files/README.md",
        status=404, body={"message": "404 File Not Found"},
    )
    assert _client(stub_server).get_file(99, "README.md", ref="master") is None


def test_put_file_creates_via_post_when_no_last_commit(stub_server):
    stub_server.enqueue(
        "POST", "/api/v4/projects/99/repository/files/README.md", body={},
    )
    _client(stub_server).put_file(
        99, "README.md", "hello",
        branch="master", commit_message="create",
        author_name="a", author_email="a@b",
    )
    hit = stub_server.hits[0]
    assert hit["method"] == "POST"
    import json as _j
    sent = _j.loads(hit["body"])
    assert sent["branch"] == "master"
    assert sent["content"] == "hello"
    assert sent["author_name"] == "a"
    assert "last_commit_id" not in sent


def test_put_file_updates_via_put_when_last_commit_provided(stub_server):
    stub_server.enqueue(
        "PUT", "/api/v4/projects/99/repository/files/README.md", body={},
    )
    _client(stub_server).put_file(
        99, "README.md", "hello",
        branch="master", commit_message="update",
        author_name="a", author_email="a@b",
        last_commit_id="c-1",
    )
    import json as _j
    sent = _j.loads(stub_server.hits[0]["body"])
    assert stub_server.hits[0]["method"] == "PUT"
    assert sent["last_commit_id"] == "c-1"


def test_list_user_events_respects_limit(stub_server):
    raw_events = [
        {
            "id": i,
            "action_name": "pushed to",
            "created_at": f"2026-04-{i:02d}T12:00:00Z",
            "target_type": None,
            "target_title": None,
            "project_id": 10,
        }
        for i in range(1, 11)
    ]
    stub_server.enqueue(
        "GET",
        "/api/v4/users/42/events",
        body=raw_events[:5],
        headers={"X-Next-Page": "2"},
    )
    stub_server.enqueue(
        "GET",
        "/api/v4/users/42/events",
        body=raw_events[5:],
        headers={"X-Next-Page": ""},
    )

    events = _client(stub_server).list_user_events(user_id=42, limit=7)

    assert len(events) == 7
    assert events[0].id == 1
    assert events[0].action_name == "pushed to"
    assert events[0].project_id == 10
