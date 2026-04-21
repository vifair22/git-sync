"""Tests for the GitHub client."""
from __future__ import annotations

import json

import pytest

from git_sync.clients import github


def _client(stub_server):
    return github.GitHubClient(token="ghp-xxx", base_url=stub_server.base_url)


def _repo_payload(name, *, private=False, default_branch="main", description="",
                  archived=False):
    return {
        "name": name,
        "full_name": f"alice/{name}",
        "private": private,
        "default_branch": default_branch,
        "description": description,
        "archived": archived,
    }


def test_list_repos_parses_fields(stub_server):
    stub_server.enqueue(
        "GET",
        "/user/repos",
        body=[_repo_payload("foo", private=False), _repo_payload("bar", private=True)],
    )
    repos = list(_client(stub_server).list_repos())

    assert [r.name for r in repos] == ["foo", "bar"]
    assert repos[0].private is False
    assert repos[0].full_name == "alice/foo"
    assert repos[1].private is True

    hit = stub_server.hits[0]
    assert hit["headers"]["authorization"] == "Bearer ghp-xxx"
    assert hit["headers"]["x-github-api-version"] == "2022-11-28"
    assert "affiliation=owner" in hit["path"]


def test_list_repos_follows_link_next(stub_server):
    next_url = f"{stub_server.base_url}/user/repos?page=2"
    stub_server.enqueue(
        "GET",
        "/user/repos",
        body=[_repo_payload("a")],
        headers={"Link": f'<{next_url}>; rel="next", <{next_url}&last>; rel="last"'},
    )
    stub_server.enqueue("GET", "/user/repos", body=[_repo_payload("b")])

    repos = list(_client(stub_server).list_repos())

    assert [r.name for r in repos] == ["a", "b"]
    assert len(stub_server.hits) == 2
    assert "page=2" in stub_server.hits[1]["path"]


def test_list_repos_handles_missing_optional_description(stub_server):
    payload = _repo_payload("foo")
    payload["description"] = None
    stub_server.enqueue("GET", "/user/repos", body=[payload])

    (repo,) = list(_client(stub_server).list_repos())
    assert repo.description == ""


def test_create_repo_sends_expected_body(stub_server):
    stub_server.enqueue(
        "POST",
        "/user/repos",
        status=201,
        body=_repo_payload("new", private=True, description="hi"),
    )

    repo = _client(stub_server).create_repo(
        "new", private=True, description="hi",
    )

    assert repo.name == "new"
    assert repo.private is True

    hit = stub_server.hits[0]
    assert hit["method"] == "POST"
    sent = json.loads(hit["body"])
    assert sent == {"name": "new", "private": True, "description": "hi"}


def test_update_repo_patches_only_supplied_fields(stub_server):
    stub_server.enqueue(
        "PATCH",
        "/repos/alice/foo",
        body=_repo_payload("foo", private=True),
    )

    _client(stub_server).update_repo("alice", "foo", private=True)

    hit = stub_server.hits[0]
    assert hit["method"] == "PATCH"
    sent = json.loads(hit["body"])
    assert sent == {"private": True}


def test_update_repo_all_fields(stub_server):
    stub_server.enqueue(
        "PATCH",
        "/repos/alice/foo",
        body=_repo_payload(
            "foo", private=False, default_branch="trunk", description="d",
        ),
    )

    _client(stub_server).update_repo(
        "alice", "foo",
        private=False,
        description="d",
        default_branch="trunk",
    )

    sent = json.loads(stub_server.hits[0]["body"])
    assert sent == {
        "private": False,
        "description": "d",
        "default_branch": "trunk",
    }


def test_get_repo_returns_repo(stub_server):
    stub_server.enqueue(
        "GET", "/repos/alice/alice",
        body=_repo_payload("alice", private=False),
    )
    r = _client(stub_server).get_repo("alice", "alice")
    assert r is not None
    assert r.name == "alice"
    assert r.private is False


def test_get_repo_returns_none_on_404(stub_server):
    stub_server.enqueue(
        "GET", "/repos/alice/alice",
        status=404, body={"message": "Not Found"},
    )
    assert _client(stub_server).get_repo("alice", "alice") is None


def test_get_file_returns_content_and_sha(stub_server):
    stub_server.enqueue(
        "GET",
        "/repos/alice/alice/contents/README.md",
        body={"content": "aGk=", "sha": "abc"},
    )
    f = _client(stub_server).get_file("alice", "alice", "README.md")
    assert f == {"content": "aGk=", "sha": "abc"}


def test_get_file_none_on_404(stub_server):
    stub_server.enqueue(
        "GET",
        "/repos/alice/alice/contents/README.md",
        status=404, body={"message": "Not Found"},
    )
    assert _client(stub_server).get_file("alice", "alice", "README.md") is None


def test_put_file_create_without_sha(stub_server):
    stub_server.enqueue(
        "PUT", "/repos/alice/alice/contents/README.md", body={},
    )
    _client(stub_server).put_file(
        "alice", "alice", "README.md",
        "aGk=",
        commit_message="create",
        branch="main",
        author_name="a", author_email="a@b",
    )
    sent = json.loads(stub_server.hits[0]["body"])
    assert "sha" not in sent
    assert sent["message"] == "create"
    assert sent["committer"] == {"name": "a", "email": "a@b"}
    assert sent["author"] == {"name": "a", "email": "a@b"}


def test_put_file_update_with_sha(stub_server):
    stub_server.enqueue(
        "PUT", "/repos/alice/alice/contents/README.md", body={},
    )
    _client(stub_server).put_file(
        "alice", "alice", "README.md",
        "aGk=",
        commit_message="update",
        branch="main",
        author_name="a", author_email="a@b",
        sha="existing-sha",
    )
    sent = json.loads(stub_server.hits[0]["body"])
    assert sent["sha"] == "existing-sha"


def test_update_repo_sets_archived(stub_server):
    stub_server.enqueue(
        "PATCH", "/repos/alice/foo",
        body=_repo_payload("foo", archived=True),
    )

    r = _client(stub_server).update_repo("alice", "foo", archived=True)

    assert r.archived is True
    sent = json.loads(stub_server.hits[0]["body"])
    assert sent == {"archived": True}


def test_update_repo_rejects_empty_patch(stub_server):
    client = _client(stub_server)
    with pytest.raises(ValueError, match="no fields"):
        client.update_repo("alice", "foo")
    assert stub_server.hits == []
